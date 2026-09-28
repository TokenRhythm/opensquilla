export type CompactionSkippedLabelCode =
  | 'chat.compact.withinBudget'
  | 'chat.compact.noSafeHistory'
  | 'chat.compact.alreadyConcise'
  | 'chat.compact.skipped'

export function compactionCompletedLabelCode(durability: unknown):
  | 'chat.compact.temporarilyReduced'
  | 'chat.compact.compacted' {
  if (durability === 'request_scoped') return 'chat.compact.temporarilyReduced'
  return 'chat.compact.compacted'
}

const BENIGN_SKIP_REASONS = new Set([
  'within_budget',
  'within_compaction_budget',
])

// The backend found no older history outside its protected turn/tail range.
// This is an expected no-op, including an immediate repeat after compaction.
const NO_SAFE_HISTORY_REASONS = new Set([
  'no_entries',
  'no_safe_turn_boundary',
  'protected_tail_exhausts_compaction_window',
])

export function compactionSkippedLabelCode(reason: unknown): CompactionSkippedLabelCode {
  const normalized = String(reason || '').trim().toLowerCase()
  if (normalized === 'no_compression_benefit') return 'chat.compact.alreadyConcise'
  if (NO_SAFE_HISTORY_REASONS.has(normalized)) return 'chat.compact.noSafeHistory'
  return BENIGN_SKIP_REASONS.has(normalized)
    ? 'chat.compact.withinBudget'
    : 'chat.compact.skipped'
}

const INFORMATIONAL_SKIP_REASONS = new Set([
  ...BENIGN_SKIP_REASONS,
  ...NO_SAFE_HISTORY_REASONS,
  'no_compression_benefit',
  'already_attempted_this_turn',
  'already_compacted_this_turn',
  'stale_preimage',
  'structured_content_noop',
])

export function compactionSkipIsInformational(reason: unknown): boolean {
  return INFORMATIONAL_SKIP_REASONS.has(String(reason || '').trim().toLowerCase())
}

/** Translate known refusal causes without exposing provider exceptions or prompt data. */
export function compactionFailurePresentation(reason: unknown) {
  const normalized = String(reason || '').trim().toLowerCase()
  if (['summary_replay_incomplete', 'coverage_blocked', 'quality_gate_failed'].includes(normalized)) {
    return { title: 'chat.compact.failureIntegrity', detail: 'chat.compact.failureRejectedDetail', warning: false }
  }
  if (['summary_does_not_fit', 'consumer_admission_failed'].includes(normalized)) {
    return { title: 'chat.compact.failureCapacity', detail: 'chat.compact.failureRejectedDetail', warning: false }
  }
  if (['summary_target_unavailable', 'suffix_target_unavailable'].includes(normalized)) {
    return { title: 'chat.compact.failureUnavailable', detail: 'chat.compact.failureUnavailableDetail', warning: false }
  }
  if (['summary_failed', 'suffix_summary_failed'].includes(normalized)) {
    return { title: 'chat.compact.failureProvider', detail: 'chat.compact.failureRetryDetail', warning: false }
  }
  if (normalized === 'compaction_deadline_exceeded' || normalized === 'timed_out') {
    return { title: 'chat.compact.failureTimeout', detail: 'chat.compact.failureRetryDetail', warning: true }
  }
  if (['cancelled', 'owner_task_cancelled', 'gateway_restarted'].includes(normalized)) {
    return { title: 'chat.compact.cancelled', detail: 'chat.compact.failureRetryDetail', warning: true }
  }
  return { title: 'chat.compact.failed', detail: 'chat.compact.failureRetryDetail', warning: false }
}
