// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, shallowRef } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import i18n, { loadLocaleMessages } from '@/i18n'
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
  onResume?: (message: ChatRenderedMessage) => void,
  onRetry?: (
    message: ChatRenderedMessage,
    settle: (accepted: boolean) => void,
  ) => void,
  retryAvailable = false,
  resumeAvailable = false,
  hasPartialAnswer = false,
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const currentMessage = shallowRef(message)
  const resumeEnabled = ref(resumeAvailable)
  const app = createApp({ render: () => h(SystemMessage, {
    message: currentMessage.value,
    subagentSummary: (t: string) => t,
    subagentBody: (t: string) => t,
    onResume,
    onRetry,
    retryAvailable,
    resumeAvailable: resumeEnabled.value,
    hasPartialAnswer,
  }) })
  app.use(i18n)
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  app.use(router)
  await router.push('/')
  await router.isReady()
  app.mount(el)
  await nextTick()
  return {
    app, el, router,
    setResumeAvailable: (available: boolean) => { resumeEnabled.value = available },
    setMessage: (next: ChatRenderedMessage) => { currentMessage.value = next },
  }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

afterEach(() => {
  i18n.global.locale.value = 'en'
})

describe('SystemMessage runtime errors', () => {
  it('keeps the partial note and safe action without an additional error timestamp', async () => {
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'no_provider',
      ts: 1_800_000_000_000,
    }), undefined, undefined, false, false, true)
    const notice = el.querySelector('.msg-error')!
    expect(notice.querySelector('time')).toBeNull()
    expect(notice.querySelector('.msg-error__note')?.textContent).toBe(i18n.global.t('chat.partialFailureNote'))
    expect(notice.querySelectorAll('a,button')).toHaveLength(1)
    app.unmount()
  })

  it('keeps timestamps for ordinary system messages', async () => {
    const { app, el } = await mountMsg(errorMessage({
      role: 'system', displayRole: 'system', text: 'Session created', ts: 1_800_000_000_000,
    }))
    expect(el.querySelector('.msg-system time')).not.toBeNull()
    app.unmount()
  })

  it.each([
    ['DOCUMENT_CHANGED', 'The page changed. Refresh it before trying again.', '页面已更新。请刷新后再试。'],
    ['PREVIEW_CAPABILITY_EXPIRED', 'The preview needs to be reopened.', '需要重新打开预览。'],
  ])('preserves existing localized %s admission guidance without raw text or task retry', async (errorCode, english, chinese) => {
    await loadLocaleMessages('zh-Hans')
    const { app, el } = await mountMsg(errorMessage({ errorCode, text: 'PRIVATE_PROVIDER_DETAIL' }))
    try {
      expect(el.querySelector('.msg-error__text')?.textContent).toBe(english)
      i18n.global.locale.value = 'zh-Hans'
      await nextTick()
      expect(el.querySelector('.msg-error__text')?.textContent).toBe(chinese)
      expect(el.textContent).not.toContain('PRIVATE_PROVIDER_DETAIL')
      expect(el.querySelectorAll('a, button')).toHaveLength(0)
    } finally {
      app.unmount()
    }
  })

  it('updates an existing error reason, action, and partial note when the display language changes', async () => {
    await loadLocaleMessages('zh-Hans')
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'no_provider',
      text: 'Old provider text (ref: abcdef01)',
    }), undefined, undefined, false, false, true)
    try {
      expect(el.querySelector('.msg-error__text')?.textContent).toBe('No model is available.')
      expect(el.querySelector('a')?.textContent).toBe('Open model settings')
      expect(el.querySelector('.msg-error__note')?.textContent).toBe('Partial results were preserved.')

      i18n.global.locale.value = 'zh-Hans'
      await nextTick()

      expect(el.querySelector('.msg-error__text')?.textContent).toBe('当前没有可用模型')
      expect(el.querySelector('a')?.textContent).toBe('打开模型设置')
      expect(el.querySelector('.msg-error__note')?.textContent).toBe('已保留部分结果')
      expect(el.querySelector('a')?.getAttribute('href')).toBe('/settings/modelStrategy')
      expect(el.querySelectorAll('a, button')).toHaveLength(1)
      expect(el.textContent).not.toMatch(/Old provider|abcdef01|chat\./)
    } finally {
      app.unmount()
    }
  })

  it('links a capacity failure to the exact provider and punctuation-containing model without retrying', async () => {
    const onRetry = vi.fn()
    const target = { provider: 'custom_anthropic', model: 'example.vendor/model.v1:latest', contextWindow: 8192, source: 'default' as const }
    const { app, el } = await mountMsg(errorMessage({ errorCode: 'provider_request_too_large', modelCapacity: target }), undefined, onRetry, true)
    const href = el.querySelector<HTMLAnchorElement>('.msg-error__capacity')?.getAttribute('href')
    const url = new URL(href!, 'https://capacity.invalid')
    expect(url.pathname).toBe('/settings/modelStrategy')
    expect(url.searchParams.get('capacityProvider')).toBe(target.provider)
    expect(url.searchParams.get('capacityModel')).toBe(target.model)
    expect(el.querySelector('button')).toBeNull()
    expect(onRetry).not.toHaveBeenCalled()
    app.unmount()
  })

  it('shows one capacity action without raw limits or diagnostic content', async () => {
    const onRetry = vi.fn()
    const text = 'The request exceeds the system default of 8,192 tokens.'
    const { app, el } = await mountMsg(errorMessage({
      text, errorCode: 'provider_request_budget_exhausted', turnId: 'capacity-turn',
      modelCapacity: { provider: 'custom', model: 'example/model.v1:latest', contextWindow: 8192, source: 'default' },
      turnOutcome: { turnId: 'capacity-turn', status: 'failed', failureKind: 'context_overflow', errorId: 'abcdef01', retryable: true },
    }), undefined, onRetry, true)
    expect(el.querySelector('.msg-error__text')?.textContent).not.toBe(text)
    expect(el.querySelector('.msg-error__capacity')).not.toBeNull()
    expect(el.querySelector('.msg-error__copy')).toBeNull()
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    expect(el.querySelectorAll('a, button')).toHaveLength(1)
    expect(el.textContent).not.toContain('abcdef01')
    expect(el.textContent).not.toContain('8,192')
    expect(el.textContent).not.toContain('example/model.v1:latest')
    expect(onRetry).not.toHaveBeenCalled()
    app.unmount()
  })

  it('keeps lifecycle timeout text ahead of a preserved provider classification', async () => {
    const { app, el } = await mountMsg(errorMessage({
      text: 'The task timed out before it could finish.', errorCode: '429', turnId: 't',
      turnOutcome: { turnId: 't', status: 'timeout', statusSource: 'task', failureKind: 'rate_limited' },
    }))
    expect(el.querySelector('.msg-error__text')?.textContent).toContain('timed out')
    expect(el.querySelectorAll('a, button')).toHaveLength(0)
    app.unmount()
  })

  it('never offers a retry button merely because a provider failure is retryable', async () => {
    const { app, el } = await mountMsg(errorMessage({
      text: 'safe fallback', errorCode: '429', turnId: 't',
      turnOutcome: { turnId: 't', status: 'failed', failureKind: 'rate_limited', errorId: 'abcdef01', retryable: true },
    }), undefined, undefined, true)
    expect(el.textContent).toContain('The model service is busy.')
    expect(el.textContent).not.toContain('abcdef01')
    expect(el.querySelectorAll('a, button')).toHaveLength(0)
    app.unmount()
  })

  it.each([undefined, null, 'INVALID1', 'abcdef01'])('shows the same safe unknown message with diagnostic reference %s', async errorId => {
    const raw = 'Authorization: Bearer secret-token https://private.invalid/prompt provider=private-model (ref: abcdef01)'
    const { app, el } = await mountMsg(errorMessage({
      text: raw, errorCode: 'unrecognized-provider-prose', turnId: 't',
      turnOutcome: { turnId: 't', status: 'failed', errorId },
    }))
    expect(el.querySelector('.msg-error__copy')).toBeNull()
    expect(el.querySelector('details, pre')).toBeNull()
    expect(el.querySelectorAll('a, button')).toHaveLength(0)
    expect(el.textContent).not.toContain('secret-token')
    expect(el.textContent).not.toContain('private.invalid')
    expect(el.textContent).not.toContain('private-model')
    expect(el.textContent).not.toContain('abcdef01')
    expect(el.textContent).not.toContain('ref:')
    expect(el.textContent).not.toContain('unrecognized-provider-prose')
    expect(el.querySelector('.msg-error__text')?.textContent?.trim()).toBeTruthy()
    app.unmount()
  })

  it.each([
    ['no_provider', undefined, '/settings/modelStrategy'],
    ['provider_error', 'auth_invalid', '/settings/provider'],
    ['provider_error', 'model_not_found', '/settings/modelStrategy'],
    ['image_input_unsupported', undefined, '/settings/modelStrategy'],
  ])('offers one settings link for %s / %s without changing or retrying the model', async (errorCode, failureKind, path) => {
    const onRetry = vi.fn()
    const onResume = vi.fn()
    const { app, el, router } = await mountMsg(errorMessage({
      errorCode,
      text: 'private fallback text',
      turnOutcome: { turnId: 'settings-turn', status: 'failed', failureKind, retryable: true },
    }), onResume, onRetry, true, true)
    const link = el.querySelector<HTMLAnchorElement>('a')
    expect(link?.getAttribute('href')).toBe(path)
    expect(el.querySelectorAll('a, button')).toHaveLength(1)
    expect(el.querySelector('button')).toBeNull()
    const navigation = new Promise<void>((resolve) => {
      const remove = router.afterEach(() => { remove(); resolve() })
    })
    link?.click()
    await navigation
    expect(router.currentRoute.value.path).toBe(path)
    expect(router.currentRoute.value.query).toEqual({})
    expect(onRetry).not.toHaveBeenCalled()
    expect(onResume).not.toHaveBeenCalled()
    app.unmount()
  })

  it.each(['timeout', 'cancelled', 'abandoned', 'interrupted'])('gives %s lifecycle precedence over provider and resume actions', async status => {
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'sandbox_threshold_exceeded',
      text: 'raw private lifecycle text',
      turnOutcome: { turnId: 'stopped-turn', status, failureKind: 'auth_invalid', retryable: true },
    }), vi.fn(), vi.fn(), true, true)
    expect(el.querySelectorAll('a, button')).toHaveLength(0)
    expect(el.textContent).not.toContain('raw private lifecycle text')
    expect(el.querySelector('.msg-error')?.getAttribute('role')).toBe(status === 'timeout' ? 'alert' : 'status')
    app.unmount()
  })

  it('retains a brief partial-result note without adding another action', async () => {
    const { app, el } = await mountMsg(errorMessage({ errorCode: 'no_provider' }), undefined, undefined, false, false, true)
    expect(el.querySelector('.msg-error__note')?.textContent).toBe(i18n.global.t('chat.partialFailureNote'))
    expect(el.querySelectorAll('a, button')).toHaveLength(1)
    app.unmount()
  })

  it('uses the durable error class when the live message has no code', async () => {
    const { app, el } = await mountMsg(errorMessage({
      text: 'The task failed before it could finish. (ref: abcdef01)',
      turnOutcome: { turnId: 'history-turn', status: 'failed', errorClass: 'no_provider', errorId: 'abcdef01' },
    }))
    expect(el.querySelector<HTMLAnchorElement>('a')?.getAttribute('href')).toBe('/settings/modelStrategy')
    expect(el.textContent).not.toContain('abcdef01')
    expect(el.textContent).not.toContain('The task failed before it could finish.')
    app.unmount()
  })

  it('does not attach old capacity identity to a credentials settings link', async () => {
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'provider_auth_invalid',
      modelCapacity: { provider: 'old-provider', model: 'old-model', contextWindow: 8192, source: 'default' },
      turnOutcome: { turnId: 'credentials-turn', status: 'failed', failureKind: 'auth_invalid' },
    }))
    expect(el.querySelector<HTMLAnchorElement>('a')?.getAttribute('href')).toBe('/settings/provider')
    expect(el.textContent).not.toContain('old-provider')
    expect(el.textContent).not.toContain('old-model')
    app.unmount()
  })

  it.each(['timeout', 'cancelled', 'abandoned'])('does not replay a %s turn even with an earlier usage proof', async status => {
    const onRetry = vi.fn()
    const { app, el } = await mountMsg(errorMessage({
      errorCode: 'usage_accounting_busy',
      turnOutcome: {
        turnId: 'ended-turn', status, usageCallIndex: 1,
        noPriorProviderDispatch: true, replaySafe: true,
      },
    }), undefined, onRetry, true)
    expect(el.querySelectorAll('a, button')).toHaveLength(0)
    expect(onRetry).not.toHaveBeenCalled()
    app.unmount()
  })

  it('renders a Resume button for a sandbox-pause error and emits resume once on click', async () => {
    const onResume = vi.fn()
    const message = errorMessage({ errorCode: 'sandbox_threshold_exceeded' })
    const { app, el } = await mountMsg(
      message,
      onResume, undefined, false, true,
    )
    const btn = el.querySelector<HTMLButtonElement>('.msg-error__resume')
    expect(btn).not.toBeNull()
    expect(btn?.textContent).toBe('Resume')

    btn?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledTimes(1)
    expect(onResume).toHaveBeenCalledWith(message)
    // Disabled after one click so a repeated click cannot fire duplicate resumes.
    expect(btn?.disabled).toBe(true)
    btn?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledTimes(1)
    app.unmount()
  })

  it('does not resume a sandbox failure without current-turn ownership from its parent', async () => {
    const onResume = vi.fn()
    const { app, el } = await mountMsg(errorMessage({ errorCode: 'sandbox_threshold_exceeded' }), onResume)
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    expect(onResume).not.toHaveBeenCalled()
    app.unmount()
  })

  it('allows another explicit resume after its parent rejects the request and restores ownership', async () => {
    const onResume = vi.fn()
    const { app, el, setResumeAvailable } = await mountMsg(
      errorMessage({ errorCode: 'sandbox_threshold_exceeded' }), onResume, undefined, false, true,
    )
    el.querySelector<HTMLButtonElement>('.msg-error__resume')?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledOnce()
    setResumeAvailable(false)
    await nextTick()
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    setResumeAvailable(true)
    await nextTick()
    const retry = el.querySelector<HTMLButtonElement>('.msg-error__resume')
    expect(retry?.disabled).toBe(false)
    retry?.click()
    await nextTick()
    expect(onResume).toHaveBeenCalledTimes(2)
    setResumeAvailable(false)
    await nextTick()
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    app.unmount()
  })

  it('does not inherit a previous turn resume lock when the same component renders a new paused turn', async () => {
    const onResume = vi.fn()
    const { app, el, setMessage } = await mountMsg(
      errorMessage({ errorCode: 'sandbox_threshold_exceeded', turnId: 'old-turn' }), onResume, undefined, false, true,
    )
    el.querySelector<HTMLButtonElement>('.msg-error__resume')?.click()
    await nextTick()
    const next = errorMessage({ errorCode: 'sandbox_threshold_exceeded', turnId: 'new-turn' })
    setMessage(next)
    await nextTick()
    const button = el.querySelector<HTMLButtonElement>('.msg-error__resume')
    expect(button?.disabled).toBe(false)
    button?.click()
    expect(onResume).toHaveBeenLastCalledWith(next)
    app.unmount()
  })

  it('does not render a Resume button for other terminal error codes', async () => {
    const { app, el } = await mountMsg(errorMessage({ errorCode: 'iteration_timeout' }))
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    app.unmount()
  })

  it('does not render a Resume button when the error carries no code', async () => {
    const { app, el } = await mountMsg(errorMessage())
    expect(el.querySelector('.msg-error__resume')).toBeNull()
    app.unmount()
  })

  it('does not render a Resume button on a non-error system message', async () => {
    const { app, el } = await mountMsg(
      errorMessage({ role: 'system', displayRole: 'system', errorCode: 'sandbox_threshold_exceeded' }),
    )
    expect(el.querySelector('.msg-error__resume')).toBeNull()
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

    expect(el.querySelector('.msg-error__text')?.textContent).toContain(
      'The provider request was not sent',
    )
    const btn = el.querySelector<HTMLButtonElement>('.msg-error__resume')
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

    expect(el.querySelector('.msg-error__text')?.textContent).toContain(
      'Earlier work in this turn',
    )
    expect(el.querySelector('.msg-error__resume')).toBeNull()
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
    expect(el.querySelector('.msg-error__resume')).toBeNull()
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

    expect(el.querySelector('.msg-error__resume')).toBeNull()
    app.unmount()
  })
})
