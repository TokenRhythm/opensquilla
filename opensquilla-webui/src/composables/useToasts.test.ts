import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useToasts } from './useToasts'

const { toasts, pushToast, dismissToast, pauseToast, resumeToast, runToastAction } = useToasts()
function clear() { for (const toast of [...toasts.value]) dismissToast(toast.id) }
beforeEach(() => { clear(); vi.useFakeTimers() })
afterEach(() => { clear(); vi.useRealTimers() })

describe('toast lifetime and capacity', () => {
  it('waits for both pointer and focus to leave, then uses only the remaining time', () => {
    pushToast('recover', { duration: 5000 })
    const id = toasts.value[0]!.id
    vi.advanceTimersByTime(1700)
    pauseToast(id, 'pointer'); pauseToast(id, 'focus')
    vi.advanceTimersByTime(9000)
    resumeToast(id, 'pointer')
    vi.advanceTimersByTime(9000)
    expect(toasts.value).toHaveLength(1)
    resumeToast(id, 'focus')
    vi.advanceTimersByTime(3299)
    expect(toasts.value).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(toasts.value).toHaveLength(0)
  })

  it('updates a paused keyed notice in place without resetting its lifetime', () => {
    pushToast('first', { duration: 5000, dedupeKey: 'one' })
    const original = toasts.value[0]!
    vi.advanceTimersByTime(4000)
    pauseToast(original.id, 'focus')
    pushToast('updated', { duration: 30000, dedupeKey: 'one', tone: 'warn' })
    expect(toasts.value[0]).toBe(original)
    expect(original.message).toBe('updated')
    vi.advanceTimersByTime(5000)
    expect(toasts.value).toHaveLength(1)
    resumeToast(original.id, 'focus')
    vi.advanceTimersByTime(1000)
    expect(toasts.value).toHaveLength(0)
  })

  it('does not let ordinary success replace a recovery notice', () => {
    pushToast('failed', { tone: 'danger', action: { label: 'Retry', onClick: () => {} } })
    for (let i = 0; i < 12; i++) pushToast(`success ${i}`, { tone: 'ok' })
    expect(toasts.value.map(t => t.message)).toEqual(['failed', 'success 9', 'success 10', 'success 11'])
  })

  it('drops ordinary incoming notices if all four are important; equal priority replaces oldest', () => {
    for (let i = 0; i < 4; i++) pushToast(`failed ${i}`, { tone: 'danger' })
    pushToast('success', { tone: 'ok' })
    expect(toasts.value.map(t => t.message)).toEqual(['failed 0', 'failed 1', 'failed 2', 'failed 3'])
    pushToast('new failure', { tone: 'danger' })
    expect(toasts.value.map(t => t.message)).toEqual(['failed 1', 'failed 2', 'failed 3', 'new failure'])
  })

  it('never evicts a notice being read or acted on, with no hidden backlog', () => {
    for (let i = 0; i < 4; i++) pushToast(`read ${i}`)
    const ids = toasts.value.map(t => t.id)
    for (const id of ids) pauseToast(id, 'pointer')
    pushToast('failure', { tone: 'danger' })
    expect(toasts.value.map(t => t.id)).toEqual(ids)
    for (const id of ids) { resumeToast(id, 'pointer'); dismissToast(id) }
    vi.runAllTimers()
    expect(toasts.value).toHaveLength(0)
  })
})

describe('toast action ownership', () => {
  it('waits for asynchronous handoff, blocks double invocation, and pauses expiry', async () => {
    let resolve!: (value: boolean) => void
    const onClick = vi.fn(() => new Promise<boolean>(r => { resolve = r }))
    pushToast('failed', { duration: 1000, action: { label: 'Retry', onClick } })
    const id = toasts.value[0]!.id
    const pending = runToastAction(id, 'Failed')
    await runToastAction(id, 'Failed')
    vi.advanceTimersByTime(3000)
    expect(onClick).toHaveBeenCalledOnce()
    expect(toasts.value[0]?.busy).toBe(true)
    resolve(true); await pending
    expect(toasts.value).toHaveLength(0)
  })

  it('retains false results and shows invalidation in the original notice', async () => {
    pushToast('failed', { action: { label: 'Retry', onClick: () => false } })
    const toast = toasts.value[0]!
    await runToastAction(toast.id, 'Failed')
    expect(toasts.value[0]).toBe(toast)
    toast.action = { label: 'Retry', onClick: () => ({ handled: false, message: 'Draft changed', removeAction: true }) }
    await runToastAction(toast.id, 'Failed')
    expect(toast.message).toBe('Draft changed')
    expect(toast.action).toBeUndefined()
  })

  it('keeps the same recovery entry on a rejected promise', async () => {
    pushToast('failed', { action: { label: 'Retry', onClick: async () => { throw new Error('unavailable') } } })
    const id = toasts.value[0]!.id
    await runToastAction(id, 'Try again')
    expect(toasts.value).toHaveLength(1)
    expect(toasts.value[0]).toMatchObject({ id, message: 'Try again', tone: 'danger', busy: false })
    expect(toasts.value[0]?.action?.label).toBe('Retry')
  })

  it('does not swallow a replacement outcome while an older action is pending', async () => {
    let resolve!: () => void
    pushToast('failed', { dedupeKey: 'same', action: { label: 'Retry', onClick: () => new Promise<void>(r => { resolve = r }) } })
    const id = toasts.value[0]!.id
    const pending = runToastAction(id, 'Failed')
    pushToast('new failure', { tone: 'danger', dedupeKey: 'same' })
    resolve(); await pending
    expect(toasts.value).toHaveLength(1)
    expect(toasts.value[0]?.message).toBe('new failure')
  })

  it('only dismisses the original notice if the action emitted a new failure', async () => {
    pushToast('old failure', { action: { label: 'Retry', onClick: () => { pushToast('new failure', { tone: 'danger' }); return true } } })
    await runToastAction(toasts.value[0]!.id, 'Failed')
    expect(toasts.value.map(t => t.message)).toEqual(['new failure'])
  })

  it('does not resurrect a dismissed pending notice on late rejection', async () => {
    let reject!: (reason: Error) => void
    pushToast('failure', { action: { label: 'Retry', onClick: () => new Promise<void>((_, r) => { reject = r }) } })
    const id = toasts.value[0]!.id
    const pending = runToastAction(id, 'Failed')
    dismissToast(id); reject(new Error('late')); await pending
    expect(toasts.value).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})
