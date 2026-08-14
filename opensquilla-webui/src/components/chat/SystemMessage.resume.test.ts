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
  retryAvailable = false,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(SystemMessage, {
    message,
    subagentSummary: (t: string) => t,
    subagentBody: (t: string) => t,
    onResume,
    onRetry,
    retryAvailable,
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
    const { app, el } = await mountMsg(message, undefined, onRetry, true)

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
    }), undefined, undefined, true)

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
    ['null index', { usage_call_index: null }, false],
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
    ['top index 2 conflicts with nested index 1', {
      usage_call_index: 2,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['top index 1 conflicts with nested index 2', {
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        usage_call_index: 2,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['top no-prior true conflicts with nested false', {
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: false,
        replay_safe: true,
      },
    }, false],
    ['top replay-safe false conflicts with nested true', {
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: false,
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['barrier code conflicts with nested error class', {
      error_class: 'usage_accounting_busy',
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        error_class: 'provider_error',
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['turn id conflicts with nested turn id', {
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        turn_id: 'turn-other',
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['primary user id conflicts with nested id', {
      user_message_id: 'user-primary',
      usage_call_index: 1,
      no_prior_provider_dispatch: true,
      replay_safe: true,
      outcome: {
        user_message_id: 'user-steer',
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['safe outcome conflicts with unsafe turn_outcome', {
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
      turn_outcome: {
        usage_call_index: 2,
        no_prior_provider_dispatch: false,
        replay_safe: false,
      },
    }, false],
    ['unsafe outcome conflicts with safe turn_outcome', {
      outcome: {
        usage_call_index: 2,
        no_prior_provider_dispatch: false,
        replay_safe: false,
      },
      turn_outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['same-container camel null invalidates snake proof', {
      outcome: {
        usage_call_index: 1,
        usageCallIndex: null,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['same-container invalid camel boolean invalidates snake proof', {
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        noPriorProviderDispatch: 'true',
        replay_safe: true,
      },
    }, false],
    ['same-container null error alias invalidates the barrier proof', {
      outcome: {
        error_class: 'usage_accounting_busy',
        errorClass: null,
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
    }, false],
    ['an explicit invalid sibling container invalidates a safe proof', {
      outcome: {
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
      turn_outcome: null,
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
    }), undefined, undefined, true)
    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })

  it('drops replay identity when outcome containers conflict on identity and code', () => {
    const turnOutcome = normalizeTurnOutcome({
      turn_id: 'turn-usage',
      status: 'failed',
      outcome: {
        error_class: 'usage_accounting_busy',
        user_message_id: 'user-primary',
        usage_call_index: 1,
        no_prior_provider_dispatch: true,
        replay_safe: true,
      },
      turnOutcome: {
        errorClass: 'provider_error',
        userMessageId: 'user-other',
        usageCallIndex: 1,
        noPriorProviderDispatch: true,
        replaySafe: true,
      },
    })

    expect(turnOutcome).toMatchObject({
      turnId: 'turn-usage',
      status: 'failed',
      errorClass: 'usage_accounting_busy',
      replaySafe: false,
    })
    expect(turnOutcome?.userMessageId).toBeUndefined()
  })

  it('preserves ordinary outcome presentation when an unused container is null', () => {
    expect(normalizeTurnOutcome({
      turn_id: 'turn-complete',
      status: 'completed',
      outcome: null,
    })).toEqual({
      turnId: 'turn-complete',
      status: 'completed',
    })
  })

  it('hides a proven-safe retry when its durable same-turn user is unavailable', async () => {
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'usage_accounting_busy',
      turnOutcome: {
        turnId: 'turn-missing-user',
        status: 'failed',
        usageCallIndex: 1,
        noPriorProviderDispatch: true,
        replaySafe: true,
      },
    }))

    expect(el.querySelector('.msg-error-card__resume')).toBeNull()
    app.unmount()
  })
})
