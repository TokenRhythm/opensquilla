// @vitest-environment happy-dom
import { effectScope, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApprovalCenterError, type ApprovalCenter } from '@/modules/approvalCenter'
import { useChatElevatedMode } from './useChatElevatedMode'

const scopes: ReturnType<typeof effectScope>[] = []
function harness(connection = 'connecting') {
  const sessionKey = ref('draft-a')
  const connectionState = ref(connection)
  const setElevatedMode = vi.fn<ApprovalCenter['setElevatedMode']>().mockResolvedValue()
  const scope = effectScope()
  scopes.push(scope)
  const api = scope.run(() => useChatElevatedMode({
    sessionKey, connectionState, approvalCenter: { setElevatedMode },
  }))!
  return { api, scope, sessionKey, connectionState, setElevatedMode }
}

beforeEach(() => localStorage.clear())
afterEach(() => {
  for (const scope of scopes.splice(0)) scope.stop()
  vi.restoreAllMocks()
})

describe('elevated preference startup synchronization', () => {
  it('loads local preferences immediately and sends once only after connected', async () => {
    localStorage.setItem('opensquilla.elevatedMode', 'on')
    const h = harness()
    h.api.loadElevatedMode()
    expect(h.api.elevatedMode.value).toBe('on')
    expect(h.setElevatedMode).not.toHaveBeenCalled()
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledExactlyOnceWith('draft-a', 'on', {
      signal: expect.any(AbortSignal),
    })
    await Promise.resolve()
    h.connectionState.value = 'disconnected'
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
  })

  it('sends only the current session and latest pending mode', () => {
    const h = harness()
    h.api.loadElevatedMode()
    h.sessionKey.value = 'draft-b'
    h.api.setElevatedMode('on', { sync: true })
    h.api.setElevatedMode('', { sync: true })
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledExactlyOnceWith('draft-b', 'off', {
      signal: expect.any(AbortSignal),
    })
  })

  it('does not synchronize an empty session or an unmounted pending preference', () => {
    const h = harness()
    h.api.loadElevatedMode()
    h.sessionKey.value = ''
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).not.toHaveBeenCalled()
    h.scope.stop()
    h.sessionKey.value = 'draft-b'
    expect(h.setElevatedMode).not.toHaveBeenCalled()
  })

  it('aborts an obsolete in-flight request and ignores its late forbidden result', async () => {
    const h = harness('connected')
    let rejectOld!: (error: Error) => void
    h.setElevatedMode.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectOld = reject }))
    h.api.setElevatedMode('on', { sync: true })
    const oldSignal = h.setElevatedMode.mock.calls[0]![2]!.signal!
    h.connectionState.value = 'disconnected'
    h.sessionKey.value = 'draft-b'
    expect(oldSignal.aborted).toBe(true)
    rejectOld(new ApprovalCenterError('forbidden', 'old session'))
    await Promise.resolve()
    expect(h.api.elevatedUnavailable.value).toBe(false)
    expect(h.api.elevatedMode.value).toBe('on')
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
  })

  it.each(['bypass', 'full'])('never transfers a dispatched %s write to another session', mode => {
    localStorage.setItem('opensquilla.elevatedMode', mode)
    localStorage.setItem('opensquilla.elevatedMode.version', '2')
    const h = harness('connected')
    h.setElevatedMode.mockImplementation(() => new Promise(() => {}))
    h.api.loadElevatedMode()
    const signal = h.setElevatedMode.mock.calls[0]![2]!.signal!
    expect(h.setElevatedMode.mock.calls[0]!.slice(0, 2)).toEqual(['draft-a', mode])

    h.sessionKey.value = 'existing-session-b'

    expect(signal.aborted).toBe(true)
    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
    expect(h.api.elevatedMode.value).toBe(mode)
  })

  it('never replays a dispatched write after an interrupted connection recovers', () => {
    const h = harness('connected')
    h.setElevatedMode.mockImplementation(() => new Promise(() => {}))
    h.api.setElevatedMode('bypass', { sync: true })
    const signal = h.setElevatedMode.mock.calls[0]![2]!.signal!

    h.connectionState.value = 'disconnected'
    expect(signal.aborted).toBe(true)
    h.connectionState.value = 'connected'

    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
    expect(h.api.elevatedMode.value).toBe('bypass')
  })

  it('retains forbidden-owner handling and does not automatically retry failures', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const h = harness('connected')
    h.setElevatedMode.mockRejectedValue(new ApprovalCenterError('forbidden', 'owner required'))
    await h.api.syncElevatedMode('on')
    expect(h.api.elevatedUnavailable.value).toBe(true)
    expect(h.api.elevatedMode.value).toBe('')
    h.connectionState.value = 'disconnected'
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
  })

  it('does not replay a failed request merely because transport reconnects', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const h = harness('connected')
    h.setElevatedMode.mockRejectedValue(new ApprovalCenterError('unavailable', 'request failed'))
    await h.api.syncElevatedMode('on')
    expect(h.api.elevatedUnavailable.value).toBe(false)
    h.connectionState.value = 'disconnected'
    h.connectionState.value = 'connected'
    expect(h.setElevatedMode).toHaveBeenCalledTimes(1)
  })

  it('aborts an in-flight write when the view is disposed', () => {
    const h = harness('connected')
    h.setElevatedMode.mockImplementation(() => new Promise(() => {}))
    h.api.loadElevatedMode()
    const signal = h.setElevatedMode.mock.calls[0]![2]!.signal!
    h.scope.stop()
    expect(signal.aborted).toBe(true)
  })

  it('preserves the existing legacy full-to-bypass preference migration', () => {
    localStorage.setItem('opensquilla.elevatedMode', 'full')
    const h = harness()
    h.api.loadElevatedMode()
    expect(h.api.elevatedMode.value).toBe('bypass')
    expect(localStorage.getItem('opensquilla.elevatedMode.version')).toBe('2')
    expect(h.setElevatedMode).not.toHaveBeenCalled()
  })
})
