import type en from '@/locales/en.json'
import { isUsageAccountingBarrier } from '@/utils/chat/usageAccountingFailure'

type ChatErrorMessageName = keyof typeof en.chat.errorMessage
export type ChatErrorMessageKey =
  | `chat.errorMessage.${ChatErrorMessageName}`
  | 'chat.usageAccountingBlockedMessage'
  | 'chat.usageAccountingBlockedUnsafeMessage'

export type ChatErrorAction =
  | 'open-model-settings'
  | 'open-provider-settings'
  | 'choose-model'
  | 'resume-sandbox'
  | 'retry-usage-replay'

export interface ChatErrorPresentationInput {
  code?: unknown
  failureKind?: unknown
  terminalStatus?: unknown
  reason?: unknown
  cancellationSource?: unknown
  outcomeKind?: unknown
  /** Caller must validate the existing strict usage barrier replay proof. */
  replaySafe?: boolean
}

export interface ChatErrorPresentation {
  messageKey: ChatErrorMessageKey
  action?: ChatErrorAction
}

function presentation(name: ChatErrorMessageName, action?: ChatErrorAction): ChatErrorPresentation {
  return { messageKey: `chat.errorMessage.${name}`, ...(action ? { action } : {}) }
}

function stableValue(value: unknown): string {
  return typeof value === 'string' ? value.trim().toLowerCase() : ''
}

const TIMEOUTS = new Set([
  'timeout', 'llm_timeout', 'iteration_timeout', 'stream_idle_timeout',
  'hard_deadline_exceeded', 'provider_retry_after_deadline', 'agent_runtime_timeout',
])
const STOPPED = new Set(['cancelled', 'cancelled_before_start', 'user_cancelled'])
const INTERRUPTED = new Set(['abandoned', 'interrupted', 'shutdown_timeout', 'dropped_by_overflow'])
const CONTEXT_LIMITS = new Set([
  'context_overflow', 'context_length_exceeded', 'provider_request_too_large',
  'provider_request_budget_exhausted', 'current_turn_context_exhausted',
  'provider_system_prompt_too_large', 'provider_tool_schema_too_large',
  'provider_protected_context_too_large', 'attachment_capacity_too_large',
])
const RUN_LIMITS = new Set([
  'max_iterations', 'tool_run_budget_exhausted', 'llm_budget_exhausted',
  'turn_llm_call_budget_exceeded', 'turn_input_token_budget_exceeded',
  'turn_output_token_budget_exceeded', 'turn_billed_cost_budget_exceeded',
  'provider_output_limit', 'turn_cost_budget_exceeded',
])
const INCOMPLETE_RESPONSES = new Set([
  'output_truncated', 'provider_output_truncated', 'provider_pretext_buffer_exhausted',
  'empty_response', 'malformed_response', 'reasoning_only', 'stream_incomplete', 'provider_stream_incomplete',
  'incomplete_stream', 'incomplete_tool_call', 'incomplete_tool_stream',
  'invalid_json', 'invalid_response', 'invalid_response_status', 'invalid_stream_frame',
  'invalid_stream_order', 'provider_protocol_error', 'response_incomplete',
  'silent_reply_not_allowed', 'model_repetition_loop_detected',
])
const PROVIDER_PRESENTATIONS = new Map<string, ChatErrorPresentation>([
  ['rate_limited', presentation('busy')],
  ['provider_overloaded', presentation('busy')],
  ['auth_invalid', presentation('credentials', 'open-provider-settings')],
  ['context_overflow', presentation('contextLimit', 'choose-model')],
  ['unsupported_feature', presentation('unsupported', 'choose-model')],
  ['insufficient_credits', presentation('credits', 'open-provider-settings')],
  ['usage_limit_reached', presentation('credits', 'open-provider-settings')],
  ['model_not_found', presentation('modelUnavailable', 'choose-model')],
  ['transport_transient', presentation('unavailable')],
  ['policy_refusal', presentation('refused')],
  ['empty_response', presentation('responseIncomplete')],
  ['malformed_response', presentation('responseIncomplete')],
  ['bad_request', presentation('invalidRequest')],
])

/** User copy is selected only from stable facts, never upstream prose or HTTP status. */
export function chatErrorPresentation(input: ChatErrorPresentationInput): ChatErrorPresentation {
  const status = stableValue(input.terminalStatus)
  const code = stableValue(input.code)
  const reason = stableValue(input.reason)
  const kind = stableValue(input.outcomeKind)
  const causes = [code, reason]

  // Task lifecycle remains authoritative when a provider failure is also present.
  if (status === 'timeout') return presentation('timeout')
  if (status === 'cancelled') return presentation('stopped')
  if (status === 'abandoned' || status === 'interrupted') return presentation('interrupted')
  if (causes.some(value => TIMEOUTS.has(value))) return presentation('timeout')
  if (causes.some(value => STOPPED.has(value))) return presentation('stopped')
  if (causes.some(value => INTERRUPTED.has(value))) return presentation('interrupted')
  if (kind === 'interrupted') return presentation('interrupted')

  if (causes.some(isUsageAccountingBarrier)) {
    return input.replaySafe === true
      ? { messageKey: 'chat.usageAccountingBlockedMessage', action: 'retry-usage-replay' }
      : { messageKey: 'chat.usageAccountingBlockedUnsafeMessage' }
  }
  if (causes.includes('no_provider')) return presentation('noProvider', 'open-model-settings')
  if (causes.includes('sandbox_threshold_exceeded')) return presentation('needsConfirmation', 'resume-sandbox')
  if (causes.some(value => ['approval_required', 'approval_pending', 'human_decision_required'].includes(value))) {
    return presentation('approvalRequired')
  }
  if (causes.includes('tool_policy_denied')) return presentation('toolDenied')
  if (causes.some(value => ['turn_tool_error_budget_exceeded', 'tool_failure_loop_exhausted'].includes(value))) {
    return presentation('toolFailed')
  }
  if (causes.includes('ensemble_multimodal_unsupported')) return presentation('ensembleImageUnsupported', 'choose-model')
  if (causes.includes('image_input_unsupported')) return presentation('imageUnsupported', 'choose-model')
  if (causes.some(value => CONTEXT_LIMITS.has(value))) return presentation('contextLimit', 'choose-model')
  if (causes.some(value => ['attachment_capacity_unknown', 'attachment_capacity_unavailable'].includes(value))) {
    return presentation('modelUnavailable', 'open-model-settings')
  }
  if (causes.some(value => [
    'compaction_refused_empty_summary', 'context_unsalvageable', 'compaction_failed',
    'compaction_not_smaller', 'compaction_exhausted', 'compaction_deadline_exceeded',
  ].includes(value))) {
    return presentation('contextUnavailable')
  }
  if (causes.some(value => RUN_LIMITS.has(value))) return presentation('runLimit')
  if (causes.some(value => INCOMPLETE_RESPONSES.has(value))) return presentation('responseIncomplete')
  if (kind === 'budgetlimited') return presentation('runLimit')

  const providerKind = stableValue(input.failureKind)
  const providerPresentation = PROVIDER_PRESENTATIONS.get(providerKind)
  if (providerPresentation) return { ...providerPresentation }
  for (const value of causes) {
    // Gateway also emits the bounded provider_<failure_kind> code form.
    const classified = PROVIDER_PRESENTATIONS.get(value)
      ?? (value.startsWith('provider_') ? PROVIDER_PRESENTATIONS.get(value.slice('provider_'.length)) : undefined)
    if (classified) return { ...classified }
  }
  return presentation('unknown')
}
