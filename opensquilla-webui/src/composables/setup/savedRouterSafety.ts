/** Old gateways cannot authorize conflict choices. Prove activation safe from
 * saved execution evidence or leave the mode unchanged until an upgrade. */
export function savedRouterActivationSafe(
  providerId: string,
  crossProvider: unknown,
  tiers: unknown,
  roles: unknown,
  ensembleEnabled: boolean,
): boolean {
  if (!providerId || typeof crossProvider !== 'boolean') return false
  if (crossProvider) return true
  // Switching away from global Ensemble changes dormant roles; its saved role
  // map cannot prove the target Router mode's execution dependencies.
  if (ensembleEnabled) return false
  if (!tiers || typeof tiers !== 'object' || !roles || typeof roles !== 'object') return false
  const rows = Object.entries(tiers).filter(([name]) => name !== 'image')
  if (!rows.length) return false
  const roleMap = roles as Record<string, unknown>
  return rows.every(([name, tier]) => {
    const role = roleMap[name]
    if (role === 'dormant_draft' || role === 'blocked') return true
    if (role !== 'direct' && role !== 'dynamic_member') return false
    const provider = String((tier as { provider?: unknown })?.provider || '').trim().toLowerCase()
    return provider === providerId.toLowerCase()
  })
}
