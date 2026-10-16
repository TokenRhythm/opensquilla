// Only an explicit application ID is remembered, within this page's verified
// Gateway identity. Paths, file versions and app catalogs are never persisted.
const preferences = new Map<string, string>()
const MAX_IDENTITIES = 32

export function readWorkspaceAppPreference(identity: string | null | undefined): string | null {
  return identity ? preferences.get(identity) ?? null : null
}

export function rememberWorkspaceAppPreference(identity: string | null | undefined, appId: string): void {
  if (!identity || !/^[a-z-]{1,32}$/.test(appId)) return
  preferences.delete(identity)
  preferences.set(identity, appId)
  if (preferences.size > MAX_IDENTITIES) preferences.delete(preferences.keys().next().value!)
}
