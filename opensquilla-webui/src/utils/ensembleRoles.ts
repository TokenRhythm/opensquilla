export type PublicEnsembleMemberRole = 'proposer' | 'aggregator' | 'fallback'

export function normalizeEnsembleMemberRole(role: unknown): PublicEnsembleMemberRole {
  const normalized = String(role || '').trim().toLowerCase().replace(/\s+/g, '_')
  if (
    normalized === 'aggregator'
    || normalized === 'primary_aggregator'
    || normalized === 'fixed_aggregator'
  ) return 'aggregator'
  if (normalized === 'fallback' || normalized === 'fallback_single' || normalized === 'fixed_direct') {
    return 'fallback'
  }
  return 'proposer'
}

export function ensembleMemberRoleLabel(role: unknown): string {
  const normalized = normalizeEnsembleMemberRole(role)
  if (normalized === 'aggregator') return 'Aggregator'
  if (normalized === 'fallback') return 'Fallback'
  return 'Proposer'
}
