export const DESKTOP_DEEP_LINK_SCHEME = 'opensquilla'

export type DesktopDeepLinkAction = 'open'

const DESKTOP_DEEP_LINK_ACTIONS = new Set<DesktopDeepLinkAction>(['open'])

export function parseDesktopDeepLink(rawUrl: unknown): DesktopDeepLinkAction | null {
  if (typeof rawUrl !== 'string' || !rawUrl.trim()) return null

  let parsed: URL
  try {
    parsed = new URL(rawUrl.trim())
  } catch {
    return null
  }

  if (parsed.protocol !== `${DESKTOP_DEEP_LINK_SCHEME}:`) return null
  if (parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash) return null
  if (parsed.pathname !== '' && parsed.pathname !== '/') return null

  const action = parsed.hostname.toLowerCase() as DesktopDeepLinkAction
  return DESKTOP_DEEP_LINK_ACTIONS.has(action) ? action : null
}

export function desktopDeepLinkArguments(argv: readonly string[]): string[] {
  const prefix = `${DESKTOP_DEEP_LINK_SCHEME}:`
  return argv.filter((value) => (
    typeof value === 'string'
    && value.trim().toLowerCase().startsWith(prefix)
  ))
}
