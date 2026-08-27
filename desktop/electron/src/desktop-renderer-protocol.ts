import { resolve, sep } from 'node:path'

export const DESKTOP_RENDERER_SCHEME = 'opensquilla-app'
export const DESKTOP_RENDERER_HOST = 'desktop'
export const DESKTOP_RENDERER_ORIGIN = `${DESKTOP_RENDERER_SCHEME}://${DESKTOP_RENDERER_HOST}`
export const DESKTOP_RENDERER_URL = `${DESKTOP_RENDERER_ORIGIN}/chat/new`
export const DESKTOP_RENDERER_ENTRY = 'desktop.html'

const DESKTOP_RENDERER_DOCUMENT_PATHS = new Set([
  '/',
  '/agents',
  '/approvals',
  '/channels',
  '/changelog',
  '/chat',
  '/chat/new',
  '/config',
  '/cron',
  '/health',
  '/logs',
  '/overview',
  '/sessions',
  '/settings',
  '/setup',
  '/skills',
  '/usage',
])

export type DesktopRendererRequestRoute =
  | { kind: 'gateway'; pathAndQuery: string }
  | { kind: 'file'; relativePath: string }
  | { kind: 'spa'; relativePath: typeof DESKTOP_RENDERER_ENTRY }
  | { kind: 'reject' }

function isDesktopRendererLocation(url: URL): boolean {
  // Node's URL implementation reports `origin === "null"` for custom schemes,
  // even though Electron treats this registered standard scheme as an origin.
  // Compare the parsed authority explicitly so the routing and trust checks have
  // identical behavior in Electron and in the pure Node tests.
  return url.protocol === `${DESKTOP_RENDERER_SCHEME}:`
    && url.hostname === DESKTOP_RENDERER_HOST
    && url.port === ''
    && url.username === ''
    && url.password === ''
}

export function isDesktopRendererUrl(rawUrl: string): boolean {
  try {
    return isDesktopRendererLocation(new URL(rawUrl))
  } catch {
    return false
  }
}

export function isDesktopRendererDocumentUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl)
    if (!isDesktopRendererLocation(url)) return false
    if (DESKTOP_RENDERER_DOCUMENT_PATHS.has(url.pathname)) return true
    return /^\/settings\/[^/]+\/?$/.test(url.pathname)
  } catch {
    return false
  }
}

export function routeDesktopRendererRequest(
  rawUrl: string,
  method = 'GET',
): DesktopRendererRequestRoute {
  let url: URL
  try {
    url = new URL(rawUrl)
  } catch {
    return { kind: 'reject' }
  }
  if (!isDesktopRendererLocation(url)) return { kind: 'reject' }

  const upperMethod = method.toUpperCase()
  if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
    return { kind: 'gateway', pathAndQuery: `${url.pathname}${url.search}` }
  }
  if (url.pathname.startsWith('/static/img/')) {
    if (upperMethod !== 'GET' && upperMethod !== 'HEAD') return { kind: 'reject' }
    return { kind: 'gateway', pathAndQuery: `${url.pathname}${url.search}` }
  }
  if (upperMethod !== 'GET' && upperMethod !== 'HEAD') return { kind: 'reject' }

  let pathname: string
  try {
    pathname = decodeURIComponent(url.pathname)
  } catch {
    return { kind: 'reject' }
  }
  if (pathname.includes('\0') || pathname.includes('\\')) return { kind: 'reject' }
  const parts = pathname.split('/').filter(Boolean)
  if (parts.includes('.') || parts.includes('..')) return { kind: 'reject' }

  if (pathname.startsWith('/static/dist/')) {
    return { kind: 'file', relativePath: pathname.slice('/static/dist/'.length) }
  }
  if (
    pathname.startsWith('/assets/')
    || pathname.startsWith('/music/')
    || pathname === '/opensquilla-mark.png'
    || pathname === '/desktop.html'
    || pathname === '/webui-artifact-manifest.json'
  ) {
    return { kind: 'file', relativePath: pathname.slice(1) }
  }
  return { kind: 'spa', relativePath: DESKTOP_RENDERER_ENTRY }
}

export function resolveDesktopRendererFile(root: string, relativePath: string): string | null {
  const resolvedRoot = resolve(root)
  const candidate = resolve(resolvedRoot, relativePath)
  if (candidate === resolvedRoot || candidate.startsWith(`${resolvedRoot}${sep}`)) return candidate
  return null
}
