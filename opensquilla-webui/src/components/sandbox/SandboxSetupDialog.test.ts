// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp } from 'vue'
import { createI18n } from 'vue-i18n'

import SandboxSetupDialog from './SandboxSetupDialog.vue'

let unmount: (() => void) | null = null

function mountDialog(pending = false, outcome = 'idle', onBackground = vi.fn()) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(SandboxSetupDialog, {
    open: true,
    pending,
    outcome,
    onBackground,
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        common: { cancel: 'Cancel' },
        settings: {
          sandbox: {
            actions: { retry: 'Retry' },
            setup: {
              title: 'Set up Safe mode',
              descriptionWithDuration: 'Administrator approval is required. First-time setup normally takes about 20–30 seconds. Keep OpenSquilla open.',
              continue: 'Start setup',
              configuring: 'Configuring…',
              runInBackground: 'Run in background',
              requestingApproval: 'Confirm the Windows prompt to continue.',
              configuringProtection: 'OpenSquilla is completing Safe mode setup.',
              takingLonger: 'Setup is still running.',
              elapsed: '{seconds}s elapsed',
              cancelled: 'Setup was cancelled.',
              failed: 'Safe mode could not be configured.',
              verificationFailed: 'Live safety verification did not pass.',
            },
          },
        },
      },
    },
  }))
  app.mount(host)
  unmount = () => app.unmount()
  return document.body
}

afterEach(() => {
  unmount?.()
  unmount = null
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('SandboxSetupDialog', () => {
  it('explains administrator approval and the measured duration before confirmation', () => {
    const body = mountDialog()

    expect(body.textContent).toContain('Administrator approval')
    expect(body.textContent).toContain('20–30 seconds')
    expect(body.querySelector('[data-testid="sandbox-setup-progress"]')).toBeNull()
  })

  it('shows honest elapsed feedback without a fake percentage', async () => {
    vi.useFakeTimers()
    const body = mountDialog(true)

    await vi.advanceTimersByTimeAsync(6_000)

    const progress = body.querySelector('[data-testid="sandbox-setup-progress"]')
    expect(progress?.textContent).toContain('6s elapsed')
    expect(body.textContent).not.toMatch(/\d+%/)
    expect(body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')?.disabled)
      .toBe(true)
  })

  it('replaces Cancel with an enabled background action while setup is pending', () => {
    const onBackground = vi.fn()
    const body = mountDialog(true, 'idle', onBackground)

    expect(body.textContent).not.toContain('Cancel')
    const background = body.querySelector<HTMLButtonElement>(
      '[data-testid="sandbox-setup-background"]',
    )
    expect(background?.textContent).toContain('Run in background')
    expect(background?.disabled).toBe(false)
    expect(body.querySelector<HTMLButtonElement>('[data-testid="sandbox-setup-continue"]')?.disabled)
      .toBe(true)

    background?.click()
    expect(onBackground).toHaveBeenCalledTimes(1)
  })

  it('keeps a retryable failure visible', () => {
    const body = mountDialog(false, 'verification_failed')

    expect(body.textContent).toContain('Live safety verification did not pass.')
    expect(body.querySelector('[data-testid="sandbox-setup-confirm"]')).toBeTruthy()
    expect(body.querySelector('[data-testid="sandbox-setup-continue"]')?.textContent).toContain('Retry')
  })
})
