import type { RouterCandidate } from '@/utils/chat/routerCandidates'

/** Disambiguate display names without changing candidate identity or order. */
export function routerCandidateLabels(candidates: readonly RouterCandidate[]): string[] {
  const shortNames = candidates.map(candidate => (
    candidate.model.slice(candidate.model.lastIndexOf('/') + 1) || candidate.model
  ))
  const modelsByShortName = new Map<string, Set<string>>()
  const modelCounts = new Map<string, number>()
  for (const [index, candidate] of candidates.entries()) {
    const shortName = shortNames[index]!
    const models = modelsByShortName.get(shortName) || new Set<string>()
    models.add(candidate.model)
    modelsByShortName.set(shortName, models)
    modelCounts.set(candidate.model, (modelCounts.get(candidate.model) || 0) + 1)
  }

  const labels = candidates.map((candidate, index) => {
    if ((modelCounts.get(candidate.model) || 0) > 1) {
      return `${candidate.model} · ${candidate.provider || tierLabel(candidate)}`
    }
    return (modelsByShortName.get(shortNames[index]!)?.size || 0) > 1
      ? candidate.model
      : shortNames[index]!
  })
  const counts = new Map<string, number>()
  for (const label of labels) counts.set(label, (counts.get(label) || 0) + 1)
  return labels.map((label, index) => (counts.get(label) || 0) > 1
    ? `${label} · ${tierLabel(candidates[index]!)}`
    : label)
}

function tierLabel(candidate: RouterCandidate): string {
  return candidate.tiers.map(tier => tier.toUpperCase()).join('/') || candidate.tier.toUpperCase()
}
