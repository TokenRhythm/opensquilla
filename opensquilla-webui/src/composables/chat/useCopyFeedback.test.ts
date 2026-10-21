// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, type App } from 'vue'
import { useCopyFeedback } from './useCopyFeedback'

const apps: App[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.useRealTimers()
})

function fixture() {
  const results: Array<(result: boolean) => void> = []
  const copy = vi.fn(() => new Promise<boolean>(resolve => results.push(resolve)))
  let feedback!: ReturnType<typeof useCopyFeedback>
  const app = createApp({
    setup() {
      feedback = useCopyFeedback(copy)
      return () => h('button', { onClick: feedback.onCopyClick }, feedback.copyLiveText.value)
    },
  })
  app.mount(document.body.appendChild(document.createElement('div')))
  apps.push(app)
  return { app, feedback, results }
}

describe('copy feedback ownership', () => {
  it.each([true, false])('keeps the newest %s result when an older copy finishes later', async latest => {
    vi.useFakeTimers()
    const { feedback, results } = fixture()
    const actionTitle = feedback.copyTitle.value
    // Vue's first happy-dom mount can schedule its 3s devtools-hook replay.
    // Count only timers added by copying, keeping that environment timer intact.
    const mountedTimers = vi.getTimerCount()
    const first = feedback.onCopyClick()
    const second = feedback.onCopyClick()
    results[1]!(latest)
    await second
    expect(feedback.copyState.value).toBe(latest ? 'ok' : 'err')
    expect(feedback.copyIconName.value).toBe(latest ? 'check' : 'x')
    if (latest) expect(feedback.copyTitle.value).toBe(actionTitle)
    expect(Boolean(feedback.copyLiveText.value)).toBe(!latest)
    expect(vi.getTimerCount()).toBe(mountedTimers + 1)
    await vi.advanceTimersByTimeAsync(500)
    results[0]!(!latest)
    await first
    expect(feedback.copyState.value).toBe(latest ? 'ok' : 'err')
    await vi.advanceTimersByTimeAsync(700)
    expect(feedback.copyState.value).toBeNull()
    expect(feedback.copyIconName.value).toBe('copy')
  })

  it('does not install feedback or a timer after unmount', async () => {
    vi.useFakeTimers()
    const { app, feedback, results } = fixture()
    const pending = feedback.onCopyClick()
    app.unmount()
    apps.splice(apps.indexOf(app), 1)
    results[0]!(true)
    await pending
    expect(feedback.copyState.value).toBeNull()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('clears the previous check and timer while a repeated copy is pending', async () => {
    vi.useFakeTimers()
    const { feedback, results } = fixture()
    const mountedTimers = vi.getTimerCount()
    const first = feedback.onCopyClick()
    results[0]!(true)
    await first
    expect(feedback.copyIconName.value).toBe('check')
    await vi.advanceTimersByTimeAsync(600)

    const second = feedback.onCopyClick()
    expect(feedback.copyState.value).toBeNull()
    expect(feedback.copyIconName.value).toBe('copy')
    expect(vi.getTimerCount()).toBe(mountedTimers)
    await vi.advanceTimersByTimeAsync(700)
    expect(feedback.copyState.value).toBeNull()

    results[1]!(true)
    await second
    expect(feedback.copyIconName.value).toBe('check')
    await vi.advanceTimersByTimeAsync(1199)
    expect(feedback.copyIconName.value).toBe('check')
    await vi.advanceTimersByTimeAsync(1)
    expect(feedback.copyIconName.value).toBe('copy')
  })

  it('invalidates a pending copy and clears feedback when its owner changes', async () => {
    vi.useFakeTimers()
    const { feedback, results } = fixture()
    const first = feedback.onCopyClick()
    results[0]!(true)
    await first
    expect(feedback.copyIconName.value).toBe('check')
    const second = feedback.onCopyClick()
    feedback.resetCopyFeedback()
    results[1]!(true)
    await second
    expect(feedback.copyState.value).toBeNull()
    expect(feedback.copyIconName.value).toBe('copy')
    expect(feedback.copyLiveText.value).toBe('')
  })
})
