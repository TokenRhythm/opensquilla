import type { WorkbenchItem } from './types'

export const BROWSER_WORKBENCH_OPEN_EVENT = 'opensquilla:open-side-browser'

export type BrowserWorkbenchOpenEventDetail =
  | { url: string }
  | { action: 'reveal'; sessionId: string }

function urlDigest(value: string): string {
  const bytes = new TextEncoder().encode(value)
  let hash = 0xcbf29ce484222325n
  for (const byte of bytes) {
    hash ^= BigInt(byte)
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return hash.toString(16).padStart(16, '0')
}

export function normalizeBrowserUrl(value: string): string {
  const trimmed = value.trim()
  if (!trimmed || trimmed.length > 8192 || /[\u0000-\u001f\u007f]/.test(trimmed)) return ''
  try {
    const url = new URL(trimmed)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
    url.username = ''
    url.password = ''
    return url.toString()
  } catch {
    return ''
  }
}

/** Address-bar input may omit the scheme; tool URLs remain strictly explicit. */
export function normalizeBrowserAddress(value: string): string {
  const trimmed = value.trim()
  if (/^(localhost|127(?:\.\d{1,3}){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(trimmed)) {
    return normalizeBrowserUrl(`http://${trimmed}`)
  }
  if (/^(?:[^/:?#\s.]+(?:\.[^/:?#\s.]+)+|\[[a-f\d:]+\]):\d+(?:[/?#]|$)/i.test(trimmed)) {
    return normalizeBrowserUrl(`https://${trimmed}`)
  }
  if (/^[a-z][a-z\d+.-]*:/i.test(trimmed)) return normalizeBrowserUrl(trimmed)
  return trimmed ? normalizeBrowserUrl(`https://${trimmed}`) : ''
}

export function createBrowserWorkbenchItem(options: {
  scopeId: string
  url: string
  instanceId?: string
}): WorkbenchItem | null {
  const url = normalizeBrowserUrl(options.url)
  if (!url) return null
  const scopeId = options.scopeId.trim()
  if (!scopeId) return null
  const parsed = new URL(url)
  return {
    id: `browser:${urlDigest(scopeId)}:${urlDigest(url)}${options.instanceId
      ? `:${urlDigest(options.instanceId)}` : ''}`,
    kind: 'browser',
    title: parsed.hostname,
    scope: { type: 'session', id: scopeId },
    hostKind: 'native-webcontents',
    retention: 'keep-alive',
    payload: {
      initialUrl: url,
      scopeId,
    },
  }
}

export function browserUrlFromWorkbenchItem(item: WorkbenchItem): string {
  if (item.kind !== 'browser') return ''
  return normalizeBrowserUrl(
    typeof item.payload.initialUrl === 'string' ? item.payload.initialUrl : '',
  )
}

export function requestBrowserWorkbenchOpen(url: string): boolean {
  const normalized = normalizeBrowserUrl(url)
  if (!normalized || typeof window === 'undefined') return false
  window.dispatchEvent(new CustomEvent<BrowserWorkbenchOpenEventDetail>(
    BROWSER_WORKBENCH_OPEN_EVENT,
    { detail: { url: normalized } },
  ))
  return true
}

export function requestBrowserWorkbenchReveal(sessionId: string): boolean {
  if (!sessionId || typeof window === 'undefined') return false
  window.dispatchEvent(new CustomEvent<BrowserWorkbenchOpenEventDetail>(
    BROWSER_WORKBENCH_OPEN_EVENT,
    { detail: { action: 'reveal', sessionId } },
  ))
  return true
}
