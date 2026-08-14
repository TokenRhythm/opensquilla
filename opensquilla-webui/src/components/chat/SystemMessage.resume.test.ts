// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { normalizeTurnOutcome } from '@/utils/chat/turnOutcome'
import SystemMessage from './SystemMessage.vue'

function errorMessage(overrides: Partial<ChatRenderedMessage> = {}): ChatRenderedMessage {
  return {
    role: 'error',
    displayRole: 'error',
    roleLabel: 'Error',
    text: 'Automatic execution paused after repeated sandbox denials.',
    timeStr: '',
    ts: null,
    showHeader: true,
    ...overrides,
  }
}

async function mountMsg(
  message: ChatRenderedMessage,
  onResume?: () => void,
  onRetry?: (
    message: ChatRenderedMessage,
    settle: (accepted: boolean) => void,
  ) => void,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SystemMessage, {
    message,
    subagentSummary: (t: string) => t,
    subagentBody: (t: string) => t,
    onResume,
    onRetry,
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('SystemMessage sandbox resume', () => {
  it('renders a Resume button for a sandbox-pause error and emits resume once on click', async () => {
    const onResume = vi.fn()
    const { app, el } = await mountMsg(
      errorMessage({ errorCode: 'sandbox_threshold_exceeded' }),
      onResume,
    )
    const btn = el.querySelector<HTMLButtonElement>('.msg-error-card__resume')
    expect(btn).not.toBeNull()
    expect(btn?.textContent).toContain('Resume execution')

    btn?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledTimes(1)
    // Disabled after one click so a repeated click cannot fire duplicate resumes.
    expect(btn?.disabled).toBe(true)
    btn?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('does not render a Resume button for other terminal error codes', async () => {
    const { app, el } = await mountMsg(errorMessage({ errorCode: 'iteration_timeout' }))
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })

  it('does not render a Resume button when the error carries no code', async () => {
    const { app, el } = await mountMsg(errorMessage())
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })

  it('does not render a Resume button on a non-error system message', async () => {
    const { app, el } = await mountMsg(
      errorMessage({ role: 'system', displayRole: 'system', errorCode: 'sandbox_threshold_exceeded' }),
    )
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })

  it('locks safe retry only after the parent accepts it', async () => {
    let accepted = false
    const onRetry = vi.fn((
      _message: ChatRenderedMessage,
      settle: (accepted: boolean) => void,
    ) => settle(accepted))
    const message = errorMessage({
      errorCode: 'usage_accounting_busy',
      text: 'The provider request was not sent.',
      turnOutcome: {
        turnId: 'turn-usage',
        status: 'failed',
        usageCallIndex: 1,
        noPriorProviderDispatch: true,
        replaySafe: true,
      },
    })
    const { app, el } = await mountMsg(message, undefined, onRetry)

    expect(el.querySelector('.msg-error-card__heading')?.textContent).toContain(
      'Usage accounting temporarily unavailable',
    )
    const btn = el.querySelector<HTMLButtonElement>('.msg-error-card__resume')
    expect(btn?.textContent).toContain('Retry')
    btn?.click()
    await nextTick()
    expect(onRetry).toHaveBeenCalledOnce()
    expect(onRetry.mock.calls[0]?.[0]).toBe(message)
    expect(btn?.disabled).toBe(false)

    accepted = true
    btn?.click()
    await nextTick()
    expect(onRetry).toHaveBeenCalledTimes(2)
    expect(btn?.disabled).toBe(true)
    btn?.click()
    await nextTick()
    expect(onRetry).toHaveBeenCalledTimes(2)
    app.unmount()
  })

  it('does not offer whole-turn retry without an explicit replay-safe proof', async () => {
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'usage_accounting_busy',
      turnOutcome: {
        turnId: 'turn-usage',
        status: 'failed',
        retryable: true,
        usageCallIndex: 2,
        noPriorProviderDispatch: false,
        replaySafe: false,
      },
    }))

    expect(el.querySelector('.msg-error-card__heading')?.textContent).toContain(
      'Usage accounting temporarily unavailable',
    )
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })

  it.each([
    [
      'string index',
      { usage_call_index: '1', no_prior_provider_dispatch: true, replay_safe: true },
      false,
    ],
    ['null index', { usage_call_index: null }, undefined],
    [
      'zero index',
      { usage_call_index: 0, no_prior_provider_dispatch: true, replay_safe: true },
      false,
    ],
    [
      'NaN index',
      { usage_call_index: Number.NaN, no_prior_provider_dispatch: true, replay_safe: true },
      false,
    ],
    ['conflicting index', {
      usage_call_index: 2,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
  ])('fails closed for an invalid usage replay proof: %s', async (
    _label,
    proof,
    expectedReplaySafe,
  ) => {
    const turnOutcome = normalizeTurnOutcome({
      turn_id: 'turn-usage',
      status: 'failed',
      ...proof,
    })
    expect(turnOutcome?.replaySafe).toBe(expectedReplaySafe)

    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'usage_accounting_busy',
      turnOutcome,
    }))
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })
})
