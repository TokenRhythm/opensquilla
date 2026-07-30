import type { ArtifactPayload } from '@/types/rpc'
import { artifactDownloadUrl, artifactFileTitle } from '@/utils/chat/artifacts'

export interface ArtifactAuthContext {
  baseOrigin?: string
  sessionKey?: string
  authToken?: string
}

export interface ArtifactFetchOptions extends ArtifactAuthContext {
  fetchImpl?: typeof fetch
  signal?: AbortSignal
  /** Require authenticated same-origin HTTP(S) bytes and reject redirects. */
  requireSameOrigin?: boolean
}

type ArtifactWindowHandle = Pick<Window, 'close'> & {
  opener: unknown
  location: Pick<Location, 'href'>
}

export interface ArtifactOpenOptions extends ArtifactFetchOptions {
  createObjectUrl?: (blob: Blob) => string
  revokeObjectUrl?: (url: string) => void
  openWindow?: (url: string, target: string, features: string) => ArtifactWindowHandle | null
  scheduleRevoke?: (url: string, revoke: () => void) => void
}

export type ArtifactFetchResult =
  | { ok: true; status: number; url: string; blob: Blob }
  | { ok: false; status: number; url: string; message: string }

export type ArtifactOpenResult =
  | { ok: true; status: number; url: string; objectUrl: string }
  | { ok: false; status: number; url: string; message: string }

export type ArtifactGatewayOpenResult =
  | { ok: true; status: number; url: string }
  | { ok: false; status: number; url: string; message: string }

const DEFAULT_BASE_ORIGIN = 'http://localhost'
const BLOB_REVOKE_DELAY_MS = 60000

function resolveBaseOrigin(baseOrigin?: string): string {
  if (baseOrigin) return baseOrigin
  if (typeof window !== 'undefined' && window.location && window.location.origin) {
    return window.location.origin
  }
  return DEFAULT_BASE_ORIGIN
}

function resolveFetch(fetchImpl?: typeof fetch): typeof fetch | null {
  if (fetchImpl) return fetchImpl
  if (typeof fetch !== 'undefined') return fetch.bind(globalThis)
  return null
}

function isAbortError(error: unknown): boolean {
  return !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
}

function safeTitle(artifact: ArtifactPayload): string {
  return artifactFileTitle(artifact) || 'artifact'
}

function closeOpenedWindow(opened: ArtifactWindowHandle) {
  try {
    opened.close()
  } catch {}
}

function isolateOpenedWindow(opened: ArtifactWindowHandle): boolean {
  try {
    opened.opener = null
    return opened.opener === null
  } catch {
    return false
  }
}

function normalizedMime(value: unknown): string {
  return typeof value === 'string' ? value.split(';', 1)[0].trim().toLowerCase() : ''
}

function artifactNameForSafety(artifact: ArtifactPayload): string {
  return typeof artifact.name === 'string' ? artifact.name.trim().toLowerCase() : ''
}

function hasActiveDocumentExtension(artifact: ArtifactPayload): boolean {
  const name = artifactNameForSafety(artifact)
  return name.endsWith('.html') || name.endsWith('.htm') || name.endsWith('.xhtml')
}

export function isActiveDocumentArtifactCandidate(artifact: ArtifactPayload): boolean {
  const artifactMime = normalizedMime(artifact.mime)
  return artifactMime === 'text/html' || artifactMime === 'application/xhtml+xml' ||
    hasActiveDocumentExtension(artifact)
}

export function isActiveDocumentArtifact(artifact: ArtifactPayload, blob: Blob): boolean {
  const responseMime = normalizedMime(blob.type)
  return responseMime === 'text/html' || responseMime === 'application/xhtml+xml' ||
    isActiveDocumentArtifactCandidate(artifact)
}

export function isSameOriginArtifactUrl(url: string, baseOrigin: string): boolean {
  try {
    return new URL(url, baseOrigin).origin === new URL(baseOrigin).origin
  } catch {
    return false
  }
}

function isSameOriginHttpArtifactUrl(url: string, baseOrigin: string): boolean {
  try {
    const resolved = new URL(url, baseOrigin)
    return (resolved.protocol === 'http:' || resolved.protocol === 'https:')
      && resolved.origin === new URL(baseOrigin).origin
  } catch {
    return false
  }
}

export function artifactAccessUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: { absolute?: boolean } = {},
): string {
  return artifactDownloadUrl(artifact, baseOrigin, {
    absolute: options.absolute === true,
    includeSessionKey: false,
  })
}

export function artifactAccessHeaders(url: string, options: ArtifactAuthContext = {}): Record<string, string> {
  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  if (!isSameOriginArtifactUrl(url, baseOrigin)) return {}
  const headers: Record<string, string> = {}
  if (options.sessionKey) headers['x-opensquilla-session-key'] = options.sessionKey
  if (options.authToken) headers.Authorization = `Bearer ${options.authToken}`
  return headers
}

export function artifactGatewayOpenUrl(artifact: ArtifactPayload, baseOrigin: string): string {
  const rawId = artifact?.id ? String(artifact.id) : ''
  if (rawId) return `/api/v1/artifacts/${encodeURIComponent(rawId)}/open`
  const accessUrl = artifactAccessUrl(artifact, baseOrigin)
  if (!accessUrl) return ''
  try {
    const url = new URL(accessUrl, baseOrigin)
    if (url.origin !== new URL(baseOrigin).origin) return ''
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/([^/]+)$/)
    return match ? `/api/v1/artifacts/${match[1]}/open` : ''
  } catch {
    return ''
  }
}

export function artifactOpenFailureMessage(status: number, title: string): string {
  if (status === 401 || status === 403) {
    return 'Artifact open is not authorized. Refresh the page and try again.'
  }
  if (status === 404) {
    return `Artifact is unavailable in this session: ${title}`
  }
  return `Artifact open failed. Use Download instead: ${title}`
}

export async function openArtifactViaGateway(
  artifact: ArtifactPayload,
  options: ArtifactFetchOptions = {},
): Promise<ArtifactGatewayOpenResult> {
  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  const url = artifactGatewayOpenUrl(artifact, baseOrigin)
  const title = safeTitle(artifact)
  if (!url) {
    return { ok: false, status: 0, url: '', message: artifactOpenFailureMessage(0, title) }
  }

  const fetchImpl = resolveFetch(options.fetchImpl)
  if (!fetchImpl) {
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }

  try {
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: artifactAccessHeaders(url, options),
      credentials: 'same-origin',
    })
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        url,
        message: artifactOpenFailureMessage(response.status, title),
      }
    }
    return { ok: true, status: response.status, url }
  } catch {
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }
}

export async function fetchArtifactBlob(
  artifact: ArtifactPayload,
  options: ArtifactFetchOptions = {},
): Promise<ArtifactFetchResult> {
  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  const url = artifactAccessUrl(artifact, baseOrigin)
  const title = safeTitle(artifact)
  if (!url) {
    return { ok: false, status: 0, url: '', message: artifactOpenFailureMessage(0, title) }
  }

  const fetchImpl = resolveFetch(options.fetchImpl)
  if (!fetchImpl) {
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }

  const sameOrigin = isSameOriginArtifactUrl(url, baseOrigin)
  if (options.requireSameOrigin && !isSameOriginHttpArtifactUrl(url, baseOrigin)) {
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }
  try {
    const response = await fetchImpl(url, {
      method: 'GET',
      headers: artifactAccessHeaders(url, options),
      credentials: sameOrigin ? 'same-origin' : 'omit',
      signal: options.signal,
      ...(options.requireSameOrigin ? { redirect: 'error' as const } : {}),
    })
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        url,
        message: artifactOpenFailureMessage(response.status, title),
      }
    }
    return { ok: true, status: response.status, url, blob: await response.blob() }
  } catch (error) {
    if (isAbortError(error)) throw error
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }
}

export async function openArtifactBlobUrl(
  artifact: ArtifactPayload,
  options: ArtifactOpenOptions = {},
): Promise<ArtifactOpenResult> {
  const createObjectUrl = options.createObjectUrl || ((blob: Blob) => URL.createObjectURL(blob))
  const revokeObjectUrl = options.revokeObjectUrl || ((url: string) => URL.revokeObjectURL(url))
  const openWindow = options.openWindow || ((url: string, target: string, features: string) => {
    if (typeof window === 'undefined') return null
    return window.open(url, target, features)
  })
  const scheduleRevoke = options.scheduleRevoke || ((_url: string, revoke: () => void) => {
    if (typeof window === 'undefined') return
    window.setTimeout(revoke, BLOB_REVOKE_DELAY_MS)
  })

  const opened = openWindow('', '_blank', '')
  if (opened === null) {
    return {
      ok: false,
      status: 0,
      url: artifactAccessUrl(artifact, resolveBaseOrigin(options.baseOrigin)),
      message: artifactOpenFailureMessage(0, safeTitle(artifact)),
    }
  }
  if (!isolateOpenedWindow(opened)) {
    closeOpenedWindow(opened)
    return {
      ok: false,
      status: 0,
      url: artifactAccessUrl(artifact, resolveBaseOrigin(options.baseOrigin)),
      message: artifactOpenFailureMessage(0, safeTitle(artifact)),
    }
  }

  const fetched = await fetchArtifactBlob(artifact, options)
  if (!fetched.ok) {
    closeOpenedWindow(opened)
    return fetched
  }
  if (isActiveDocumentArtifact(artifact, fetched.blob)) {
    closeOpenedWindow(opened)
    return {
      ok: false,
      status: 0,
      url: fetched.url,
      message: artifactOpenFailureMessage(0, safeTitle(artifact)),
    }
  }

  const objectUrl = createObjectUrl(fetched.blob)
  try {
    opened.location.href = objectUrl
  } catch {
    try {
      revokeObjectUrl(objectUrl)
    } catch {}
    closeOpenedWindow(opened)
    return {
      ok: false,
      status: 0,
      url: fetched.url,
      message: artifactOpenFailureMessage(0, safeTitle(artifact)),
    }
  }
  scheduleRevoke(objectUrl, () => {
    try {
      revokeObjectUrl(objectUrl)
    } catch {}
  })
  return { ok: true, status: fetched.status, url: fetched.url, objectUrl }
}
