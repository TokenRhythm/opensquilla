export const DESKTOP_DEEP_LINK_SCHEME = 'opensquilla'

export type DesktopDeepLinkAction = 'open'

export interface DesktopDeepLinkTarget {
  action: DesktopDeepLinkAction
  sessionKey?: string
}

const DESKTOP_DEEP_LINK_ACTIONS = new Set<DesktopDeepLinkAction>(['open'])

/**
 * Parse the desktop protocol without ever returning credentials or an URL to
 * the renderer.  Session targets are deliberately one path segment so an
 * incoming link cannot smuggle a route, query, or filesystem path.
 */
export function parseDesktopDeepLinkTarget(rawUrl: unknown): DesktopDeepLinkTarget | null {
  if (
    typeof rawUrl !== 'string'
    || !rawUrl
    || rawUrl.length > 2048
    || /[\u0000-\u0020\u007f?#]/.test(rawUrl)
  ) return null
  // URL normalizes dot segments before exposing pathname. Reject them in the
  // original path first so an encoded ``..`` cannot collapse the target to
  // the legacy ``opensquilla://open`` action.
  const rawPath = rawUrl.trim().split(/[?#]/, 1)[0] ?? ''
  if (/(?:^|\/)(?:\.|%2e){1,2}(?:\/|$)/i.test(rawPath)) return null

  let parsed: URL
  try {
    parsed = new URL(rawUrl.trim())
  } catch {
    return null
  }

  if (parsed.protocol.toLowerCase() !== `${DESKTOP_DEEP_LINK_SCHEME}:`) return null
  if (parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash) return null

  const action = parsed.hostname.toLowerCase() as DesktopDeepLinkAction
  if (!DESKTOP_DEEP_LINK_ACTIONS.has(action)) return null

  if (parsed.pathname === '' || parsed.pathname === '/') return { action }
  const segments = parsed.pathname.split('/')
  if (segments.length !== 3 || segments[0] !== '' || segments[1] !== 'session' || !segments[2]) return null
  let sessionKey: string
  try {
    sessionKey = decodeURIComponent(segments[2])
  } catch {
    return null
  }
  // Keep the payload bounded and reject encoded separators/control characters.
  if (
    !sessionKey
    || sessionKey.length > 512
    || sessionKey === '.'
    || sessionKey === '..'
    || /[\u0000-\u001f\u007f]/.test(sessionKey)
    || sessionKey.includes('/')
    || sessionKey.includes('\\')
  ) return null
  return { action, sessionKey }
}

export function parseDesktopDeepLink(rawUrl: unknown): DesktopDeepLinkAction | null {
  return parseDesktopDeepLinkTarget(rawUrl)?.action ?? null
}

export function desktopDeepLinkArguments(argv: readonly string[]): string[] {
  const prefix = `${DESKTOP_DEEP_LINK_SCHEME}:`
  return argv.filter((value) => (
    typeof value === 'string'
    && value.trim().toLowerCase().startsWith(prefix)
  ))
}
