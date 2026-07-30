import type { RouteLocationNormalized, RouteLocationRaw } from 'vue-router'

export type ChannelHashTarget = { kind: 'edit'; name: string } | { kind: 'new' } | null

export function parseChannelHash(hash: unknown): ChannelHashTarget {
  if (typeof hash !== 'string') return null
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const prefix = 'channel-'
  if (!raw.startsWith(prefix)) return null
  const name = raw.slice(prefix.length).trim()
  if (!name) return null
  if (name === 'new') return { kind: 'new' }
  try {
    return { kind: 'edit', name: decodeURIComponent(name) }
  } catch {
    return { kind: 'edit', name }
  }
}

// Legacy channel deep links: channel setup moved from the Settings dialog to
// the /channels workspace, and the old `#channel-…` hash contract moved with
// it. This safety net rewrites stale bookmarks; live code mints /channels
// URLs directly.
//
//   /settings/channels#channel-new     → /channels?compose=1
//   /settings/channels#channel-<name>  → /channels?channel=<name>&tab=configuration&edit=1
//
// Bare and unrecognized-hash variants still redirect, preserving any query
// that a previous client put on the legacy path. A recognized hash is the
// authoritative old contract and replaces conflicting query state exactly.
export function legacyChannelHashRedirect(
  to: Pick<RouteLocationNormalized, 'path'>
    & Partial<Pick<RouteLocationNormalized, 'hash' | 'query'>>,
): RouteLocationRaw | null {
  if (to.path !== '/settings/channels') return null
  const target = parseChannelHash(to.hash)
  if (target?.kind === 'new') {
    return { path: '/channels', query: { compose: '1' }, replace: true }
  }
  if (target?.kind === 'edit') {
    return {
      path: '/channels',
      query: { channel: target.name, tab: 'configuration', edit: '1' },
      replace: true,
    }
  }
  return { path: '/channels', query: { ...(to.query || {}) }, replace: true }
}
