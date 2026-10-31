import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n, { loadLocaleMessages, SUPPORTED_LOCALES } from '@/i18n'
import { chatErrorPresentation, type ChatErrorAction } from './chatErrorPresentation'
import { localizedChatErrorMessage } from './errors'

afterEach(() => {
  vi.restoreAllMocks()
  i18n.global.locale.value = 'en'
})

describe('Chat user error presentation', () => {
  const providerCases: [string, string, ChatErrorAction?][] = [
    ['rate_limited', 'rateLimited'],
    ['provider_overloaded', 'busy'],
    ['auth_invalid', 'credentials', 'open-provider-settings'],
    ['context_overflow', 'contextLimit', 'choose-model'],
    ['payload_too_large', 'requestTooLarge'],
    ['wire_payload_too_large', 'outboundRequestTooLarge'],
    ['unsupported_feature', 'unsupported', 'choose-model'],
    ['insufficient_credits', 'credits', 'open-provider-settings'],
    ['model_not_found', 'modelUnavailable', 'choose-model'],
    ['transport_transient', 'unavailable'],
    ['policy_refusal', 'refused'],
    ['empty_response', 'responseIncomplete'],
    ['malformed_response', 'responseIncomplete'],
    ['bad_request', 'invalidRequest'],
    ['unknown', 'unknown'],
  ]

  it.each(providerCases)('presents the stable %s provider cause', (failureKind, key, action) => {
    const expected = { messageKey: `chat.errorMessage.${key}`, ...(action ? { action } : {}) }
    expect(chatErrorPresentation({ code: 'provider_error', failureKind })).toEqual(expected)
    expect(chatErrorPresentation({ code: failureKind })).toEqual(expected)
    expect(chatErrorPresentation({ code: `provider_${failureKind}` })).toEqual(expected)
  })

  it.each([
    ['no_provider', 'noProvider', 'open-model-settings'],
    ['provider_request_too_large', 'contextLimit', 'choose-model'],
    ['provider_payload_too_large', 'requestTooLarge', undefined],
    ['provider_wire_payload_too_large', 'outboundRequestTooLarge', undefined],
    ['provider_request_budget_exhausted', 'contextLimit', 'choose-model'],
    ['current_turn_context_exhausted', 'contextLimit', 'choose-model'],
    ['attachment_capacity_too_large', 'contextLimit', 'choose-model'],
    ['image_input_unsupported', 'imageUnsupported', 'choose-model'],
    ['ensemble_multimodal_unsupported', 'ensembleImageUnsupported', 'choose-model'],
    ['max_iterations', 'runLimit', undefined],
    ['tool_run_budget_exhausted', 'runLimit', undefined],
    ['llm_budget_exhausted', 'runLimit', undefined],
    ['turn_input_token_budget_exceeded', 'runLimit', undefined],
    ['turn_output_token_budget_exceeded', 'runLimit', undefined],
    ['turn_billed_cost_budget_exceeded', 'runLimit', undefined],
    ['turn_cost_budget_exceeded', 'runLimit', undefined],
    ['provider_output_truncated', 'responseIncomplete', undefined],
    ['incomplete_tool_stream', 'responseIncomplete', undefined],
    ['provider_stream_incomplete', 'responseIncomplete', undefined],
    ['invalid_response', 'responseIncomplete', undefined],
    ['model_repetition_loop_detected', 'responseIncomplete', undefined],
    ['sandbox_threshold_exceeded', 'needsConfirmation', 'resume-sandbox'],
    ['tool_policy_denied', 'toolDenied', undefined],
    ['turn_tool_error_budget_exceeded', 'toolFailed', undefined],
    ['tool_failure_loop_exhausted', 'toolFailed', undefined],
  ] as const)('keeps the %s cause ahead of generic provider metadata', (code, key, action) => {
    expect(chatErrorPresentation({ code, failureKind: 'rate_limited' })).toEqual({
      messageKey: `chat.errorMessage.${key}`, ...(action ? { action } : {}),
    })
    expect(chatErrorPresentation({ reason: code, failureKind: 'rate_limited' })).toEqual({
      messageKey: `chat.errorMessage.${key}`, ...(action ? { action } : {}),
    })
  })

  it.each([
    'compaction_refused_empty_summary', 'context_unsalvageable', 'compaction_failed',
    'compaction_not_smaller', 'compaction_exhausted', 'compaction_deadline_exceeded',
  ])('keeps a known provider cause visible when context recovery ends with %s', code => {
    expect(chatErrorPresentation({ code })).toEqual({ messageKey: 'chat.errorMessage.contextUnavailable' })
    expect(chatErrorPresentation({ reason: code, failureKind: 'unknown' })).toEqual({ messageKey: 'chat.errorMessage.contextUnavailable' })
    expect(chatErrorPresentation({ code, failureKind: 'auth_invalid' })).toEqual({
      messageKey: 'chat.errorMessage.credentials', action: 'open-provider-settings',
    })
    expect(chatErrorPresentation({ reason: code, failureKind: 'rate_limited' })).toEqual({ messageKey: 'chat.errorMessage.rateLimited' })
    expect(chatErrorPresentation({ code, failureKind: 'transport_transient' })).toEqual({ messageKey: 'chat.errorMessage.unavailable' })
    expect(chatErrorPresentation({ code, terminalStatus: 'timeout', failureKind: 'auth_invalid' })).toEqual({ messageKey: 'chat.errorMessage.timeout' })
    expect(chatErrorPresentation({ code, terminalStatus: 'cancelled', failureKind: 'auth_invalid' })).toEqual({ messageKey: 'chat.errorMessage.stopped' })
  })

  it.each([
    ['timeout', 'timeout'],
    ['cancelled', 'stopped'],
    ['abandoned', 'interrupted'],
    ['interrupted', 'interrupted'],
  ])('preserves authoritative lifecycle status %s', (terminalStatus, key) => {
    expect(chatErrorPresentation({
      terminalStatus, code: 'no_provider', failureKind: 'auth_invalid', replaySafe: true,
    })).toEqual({ messageKey: `chat.errorMessage.${key}` })
  })

  it.each(['llm_timeout', 'iteration_timeout', 'stream_idle_timeout', 'hard_deadline_exceeded', 'agent_runtime_timeout'])(
    'explains timeout %s without internal timing details', code => {
      expect(chatErrorPresentation({ code, failureKind: 'transport_transient' }))
        .toEqual({ messageKey: 'chat.errorMessage.timeout' })
    },
  )

  it('keeps normalized interruption and budget outcomes meaningful without a code', () => {
    expect(chatErrorPresentation({ outcomeKind: 'interrupted', failureKind: 'auth_invalid' }))
      .toEqual({ messageKey: 'chat.errorMessage.interrupted' })
    expect(chatErrorPresentation({ outcomeKind: 'budgetLimited', failureKind: 'unknown' }))
      .toEqual({ messageKey: 'chat.errorMessage.runLimit' })
    expect(chatErrorPresentation({ outcomeKind: 'partial', failureKind: 'rate_limited' }))
      .toEqual({ messageKey: 'chat.errorMessage.rateLimited' })
  })

  it.each(['approval_pending', 'approval_required', 'human_decision_required'])(
    'keeps %s as waiting for approval without adding another action', code => {
      expect(chatErrorPresentation({ code })).toEqual({ messageKey: 'chat.errorMessage.approvalRequired' })
    },
  )

  it.each(['usage_accounting_busy', 'usage_accounting_unavailable'])(
    'offers replay only for %s with caller-validated proof', code => {
      expect(chatErrorPresentation({ code })).toEqual({ messageKey: 'chat.usageAccountingBlockedUnsafeMessage' })
      expect(chatErrorPresentation({ code, replaySafe: false })).toEqual({ messageKey: 'chat.usageAccountingBlockedUnsafeMessage' })
      expect(chatErrorPresentation({ code, replaySafe: true })).toEqual({
        messageKey: 'chat.usageAccountingBlockedMessage', action: 'retry-usage-replay',
      })
    },
  )

  it('distinguishes waiting budget from task timeout and usage limits from credits', () => {
    expect(chatErrorPresentation({ code: 'provider_retry_after_deadline', failureKind: 'rate_limited' })).toEqual({ messageKey: 'chat.errorMessage.rateLimitWaitEnded' })
    expect(chatErrorPresentation({ code: 'provider_overload_retry_after_deadline', failureKind: 'provider_overloaded' })).toEqual({ messageKey: 'chat.errorMessage.busyWaitEnded' })
    expect(chatErrorPresentation({ terminalStatus: 'timeout', code: 'provider_retry_after_deadline' })).toEqual({ messageKey: 'chat.errorMessage.timeout' })
    expect(chatErrorPresentation({ failureKind: 'insufficient_credits' })).toEqual({ messageKey: 'chat.errorMessage.credits', action: 'open-provider-settings' })
    expect(chatErrorPresentation({ failureKind: 'usage_limit' })).toEqual({ messageKey: 'chat.errorMessage.usageLimit' })
    for (const code of ['400', '401', '403', '404', '413', '429', '503']) expect(chatErrorPresentation({ code })).toEqual({ messageKey: 'chat.errorMessage.unknown' })
  })

  it('never uses a general retry hint to offer whole-turn replay', () => {
    for (const [failureKind] of providerCases) {
      expect(chatErrorPresentation({ failureKind, replaySafe: true }).action).not.toBe('retry-usage-replay')
    }
    expect(chatErrorPresentation({ code: 'provider_output_truncated', replaySafe: true }).action).toBeUndefined()
  })

  it('keeps a provider usage limit separate from the local execution budget', () => {
    for (const failureKind of [undefined, 'usage_limit']) {
      expect(chatErrorPresentation({ code: 'usage_limit_reached', failureKind })).toEqual({
        messageKey: 'chat.errorMessage.usageLimit',
      })
    }
    expect(chatErrorPresentation({ code: 'turn_billed_cost_budget_exceeded' }))
      .toEqual({ messageKey: 'chat.errorMessage.runLimit' })
  })

  it('keeps artifact admission fallback local and generic errors independent of artifact wording', () => {
    vi.spyOn(i18n.global, 't').mockImplementation(key => String(key))
    expect(localizedChatErrorMessage('DOCUMENT_CHANGED', 'raw upstream secret'))
      .toBe('The page changed. Refresh it before trying again.')
    expect(localizedChatErrorMessage('PERMISSION_DENIED', 'raw upstream secret'))
      .toBe('This request did not finish.')
  })

  it.each(['429', '401', 'future_code', '__proto__', 'constructor', 'Rate limited (ref: abcdef01)', null, {}])(
    'does not diagnose unknown code %s from arbitrary text', code => {
      expect(chatErrorPresentation({ code, failureKind: 'future_kind', reason: 'Authorization: Bearer private' }))
        .toEqual({ messageKey: 'chat.errorMessage.unknown' })
    },
  )

  it.each(SUPPORTED_LOCALES)('has local cause and action translations in %s', async locale => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const chat = i18n.global.getLocaleMessage(locale).chat
    expect(Object.keys(chat.errorMessage)).toEqual(Object.keys(i18n.global.getLocaleMessage('en').chat.errorMessage))
    expect(Object.keys(chat.errorAction)).toEqual(Object.keys(i18n.global.getLocaleMessage('en').chat.errorAction))
    for (const [failureKind] of providerCases) {
      const message = localizedChatErrorMessage('provider_error', 'secret upstream content (ref: abcdef01)', false, failureKind)
      expect(message).not.toMatch(/secret|abcdef01|chat\./)
      expect(message).not.toBe('')
    }
  })

  it('uses the exact concise Chinese fallback regardless of diagnostic text', async () => {
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    expect(localizedChatErrorMessage('unknown', 'secret upstream content (ref: abcdef01)'))
      .toBe('本次请求未完成')
    for (const fallback of ['No provider available', 'The task failed. (ref: abcdef01)', '']) {
      expect(localizedChatErrorMessage('no_provider', fallback)).toBe('当前没有可用模型')
    }
    expect(localizedChatErrorMessage(undefined, 'secret', false, undefined, 'failed', { reason: 'no_provider' }))
      .toBe('当前没有可用模型')
    expect(localizedChatErrorMessage('compaction_exhausted', 'debug ref: abcdef01'))
      .toBe('上下文整理未完成，本次无法继续，请稍后重试')
    expect(localizedChatErrorMessage('provider_request_too_large', 'debug ref: abcdef01'))
      .toBe('内容超出当前模型容量，请缩短输入或选择更大上下文的模型')
  })

  it.each(['chat.errorMessage.noProvider', ''])(
    'uses bundled English if localization returns a key or empty text: %s', translation => {
      vi.spyOn(i18n.global, 't').mockReturnValue(translation)
      expect(localizedChatErrorMessage('no_provider', 'raw upstream secret'))
        .toBe('No model is available.')
    },
  )
})
