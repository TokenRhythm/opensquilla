// Persist + restore the last-viewed view so the app reopens where you left off
// instead of always landing on the default (Sessions on desktop / Chat on
// mobile). Only stable, known views are restorable; the route path is stored
// without query/hash so a stale session key can never reopen a dead chat, and
// an unknown/removed path falls back to the default.

export const LAST_ROUTE_KEY = 'opensquilla-last-route'

// Top-level views that are safe to reopen on launch. Deliberately excludes '/'
// (would redirect-loop), '/chat/new' (a fresh draft — restoring it every launch
// would spawn empty drafts), and the '/settings' overlay (a modal rendered over
// the default view, not a standalone page — restoring it reopens the dialog on
// cold boot with nothing behind it and traps close in a '/' → '/settings'
// redirect loop). Chat restores to the chat surface, not a specific session,
// since the query is dropped.
const RESTORABLE = new Set<string>([
  '/chat',
  '/sessions',
  '/channels',
  '/cron',
  '/skills',
  '/overview',
  '/usage',
  '/logs',
])

export function isRestorableRoute(path: string): boolean {
  if (!path || typeof path !== 'string') return false
  return RESTORABLE.has(path)
}

/**
 * Persist the last-viewed route. Pass `route.path` (no query/hash). Only
 * restorable views are stored, so the saved value is always a valid restore
 * target.
 */
export function saveLastRoute(path: string): void {
  try {
    if (isRestorableRoute(path)) localStorage.setItem(LAST_ROUTE_KEY, path)
  } catch {
    // localStorage unavailable (private mode) — restore just won't happen
  }
}

/** The saved route to restore on launch, re-validated, or null to use the default. */
export function readLastRoute(): string | null {
  try {
    const saved = localStorage.getItem(LAST_ROUTE_KEY)
    return saved && isRestorableRoute(saved) ? saved : null
  } catch {
    return null
  }
}
