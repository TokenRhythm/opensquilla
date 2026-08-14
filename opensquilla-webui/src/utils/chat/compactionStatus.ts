export type CompactionSkippedLabelCode =
  | 'chat.compact.withinBudget'
  | 'chat.compact.skipped'

const BENIGN_SKIP_REASONS = new Set([
  'within_budget',
  'within_compaction_budget',
])

export function compactionSkippedLabelCode(reason: unknown): CompactionSkippedLabelCode {
  const normalized = String(reason || '').trim().toLowerCase()
  return BENIGN_SKIP_REASONS.has(normalized)
    ? 'chat.compact.withinBudget'
    : 'chat.compact.skipped'
}

const INFORMATIONAL_SKIP_REASONS = new Set([
  ...BENIGN_SKIP_REASONS,
  'already_attempted_this_turn',
  'already_compacted_this_turn',
  'no_entries',
  'stale_preimage',
  'structured_content_noop',
])

export function compactionSkipIsInformational(reason: unknown): boolean {
  return INFORMATIONAL_SKIP_REASONS.has(String(reason || '').trim().toLowerCase())
}
