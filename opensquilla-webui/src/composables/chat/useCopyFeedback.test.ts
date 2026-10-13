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
    const first = feedback.onCopyClick()
    const second = feedback.onCopyClick()
    results[1]!(latest)
    await second
    expect(feedback.copyState.value).toBe(latest ? 'ok' : 'err')
    await vi.advanceTimersByTimeAsync(500)
    results[0]!(!latest)
    await first
    expect(feedback.copyState.value).toBe(latest ? 'ok' : 'err')
    await vi.advanceTimersByTimeAsync(700)
    expect(feedback.copyState.value).toBeNull()
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
})
