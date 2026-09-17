/** Stable public taxonomy only; never infer a provider cause from prose or HTTP codes. */
const FAILURE_KINDS = new Set([
  'rate_limited', 'provider_overloaded', 'auth_invalid', 'context_overflow',
  'unsupported_feature', 'insufficient_credits', 'model_not_found', 'transport_transient',
  'policy_refusal', 'empty_response', 'malformed_response', 'bad_request',
])

export function providerFailureKind(value: unknown): string | undefined {
  return typeof value === 'string' && FAILURE_KINDS.has(value) ? value : undefined
}

export function diagnosticErrorId(value: unknown): string | undefined {
  return typeof value === 'string' && /^[0-9a-f]{8}$/.test(value) ? value : undefined
}

// These existing terminal codes have more specific, server-authored guidance.
const SPECIAL_TERMINALS = new Set([
  'timeout', 'llm_timeout', 'iteration_timeout', 'stream_idle_timeout', 'provider_output_truncated',
  'provider_request_too_large', 'provider_request_budget_exhausted', 'current_turn_context_exhausted',
  'model_repetition_loop_detected', 'tool_run_budget_exhausted', 'llm_budget_exhausted',
  'turn_llm_call_budget_exceeded', 'turn_input_token_budget_exceeded',
  'turn_output_token_budget_exceeded', 'turn_billed_cost_budget_exceeded', 'max_iterations',
  'sandbox_threshold_exceeded', 'tool_policy_denied', 'silent_reply_not_allowed',
])

export function localizedProviderFailureKind(kind: unknown, code: unknown, fallback: string): string | undefined {
  if (typeof code === 'string' && SPECIAL_TERMINALS.has(code)) return undefined
  // An explicit empty-response classification must not erase the engine's
  // more specific reasoning-only advice. No classification is inferred here.
  if (code === 'empty_response' && [
    'The model used its output budget for reasoning without returning a visible answer.',
    'The model returned reasoning without a visible answer.',
  ].some(prefix => fallback.startsWith(prefix))) return undefined
  return providerFailureKind(kind)
}
