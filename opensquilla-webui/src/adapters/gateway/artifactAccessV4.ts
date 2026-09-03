import type { ArtifactPayload } from '@/types/artifacts'
import type {
  ArtifactAccessRequest,
  ArtifactContentAccess,
} from '@/modules/artifactWorkbench'
import { artifactFileTitle } from '@/utils/chat/artifacts'
import { isActiveDocumentArtifact } from '@/utils/chat/artifactAccess'

export {
  isActiveDocumentArtifact,
  isActiveDocumentArtifactCandidate,
} from '@/utils/chat/artifactAccess'
import {
  HttpTransportError,
} from './privateHttpTransport'

interface ArtifactBinaryResponse {
  readonly metadata: { readonly status: number }
  blob(): Promise<Blob>
}

interface ArtifactAccessHttpTransport {
  clearPreviewOrigin(previewOrigin: string): Promise<void>
  fetchExternalArtifact(endpoint: string, signal?: AbortSignal): Promise<ArtifactBinaryResponse>
  requestBinary(endpoint: string, options?: {
    method?: 'GET' | 'POST'
    sessionKey?: string
    signal?: AbortSignal
    timeoutMs?: number
  }): Promise<ArtifactBinaryResponse>
}

interface ArtifactFetchOptions {
  baseOrigin?: string
  sessionKey?: string
  signal?: AbortSignal
  /** Require authenticated same-origin HTTP(S) bytes and reject redirects. */
  requireSameOrigin?: boolean
}

type ArtifactWindowHandle = Pick<Window, 'close'> & {
  opener: unknown
  location: Pick<Location, 'href'>
}

interface ArtifactOpenOptions extends ArtifactFetchOptions {
  createObjectUrl?: (blob: Blob) => string
  revokeObjectUrl?: (url: string) => void
  openWindow?: (url: string, target: string, features: string) => ArtifactWindowHandle | null
  scheduleRevoke?: (url: string, revoke: () => void) => void
}

type ArtifactFetchResult =
  | { ok: true; status: number; url: string; blob: Blob }
  | { ok: false; status: number; url: string; message: string }

type ArtifactOpenResult =
  | { ok: true; status: number; url: string; objectUrl: string }
  | { ok: false; status: number; url: string; message: string }

type ArtifactGatewayOpenResult =
  | { ok: true; status: number; url: string }
  | { ok: false; status: number; url: string; message: string }

const DEFAULT_BASE_ORIGIN = 'http://localhost'
const BLOB_REVOKE_DELAY_MS = 60000
const DESKTOP_RENDERER_PROTOCOL = 'opensquilla-app:'
const DESKTOP_RENDERER_HOST = 'desktop'

export function runtimeArtifactBaseOrigin(): string {
  if (
    typeof window !== 'undefined'
    && window.location?.protocol === DESKTOP_RENDERER_PROTOCOL
    && window.location.hostname === DESKTOP_RENDERER_HOST
  ) {
    return `${DESKTOP_RENDERER_PROTOCOL}//${DESKTOP_RENDERER_HOST}`
  }
  if (
    typeof window !== 'undefined'
    && window.location?.origin
    && window.location.origin !== 'null'
  ) return window.location.origin
  return DEFAULT_BASE_ORIGIN
}

function runtimeOptions(request: ArtifactAccessRequest = {}): ArtifactFetchOptions {
  return {
    baseOrigin: runtimeArtifactBaseOrigin(),
    requireSameOrigin: request.requireSameOrigin,
    sessionKey: request.sessionKey,
    signal: request.signal,
  }
}

function urlsShareArtifactOrigin(candidate: URL, base: URL): boolean {
  if (candidate.origin !== 'null' || base.origin !== 'null') {
    return candidate.origin === base.origin
  }
  // Opaque URL origins all serialize as "null". Compare the one supported
  // custom transport by authority so an unrelated custom scheme is never
  // mistaken for the privileged Desktop API proxy.
  return candidate.protocol === DESKTOP_RENDERER_PROTOCOL
    && base.protocol === DESKTOP_RENDERER_PROTOCOL
    && candidate.hostname === DESKTOP_RENDERER_HOST
    && base.hostname === DESKTOP_RENDERER_HOST
    && candidate.port === base.port
    && !candidate.username
    && !candidate.password
    && !base.username
    && !base.password
}

function resolveBaseOrigin(baseOrigin?: string): string {
  if (baseOrigin) return baseOrigin
  return runtimeArtifactBaseOrigin()
}

function isAbortError(error: unknown): boolean {
  return error instanceof HttpTransportError
    ? error.kind === 'aborted'
    : !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
}

function httpErrorStatus(error: unknown): number {
  return error instanceof HttpTransportError
    && error.kind === 'http-status'
    && typeof error.status === 'number'
    ? error.status
    : 0
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

export function isSameOriginArtifactUrl(url: string, baseOrigin: string): boolean {
  try {
    return urlsShareArtifactOrigin(new URL(url, baseOrigin), new URL(baseOrigin))
  } catch {
    return false
  }
}

export function isTrustedArtifactTransportUrl(url: string, baseOrigin: string): boolean {
  try {
    const resolved = new URL(url, baseOrigin)
    const base = new URL(baseOrigin)
    if (!urlsShareArtifactOrigin(resolved, base)) return false
    return resolved.protocol === 'http:'
      || resolved.protocol === 'https:'
      || (
        resolved.protocol === DESKTOP_RENDERER_PROTOCOL
        && resolved.hostname === DESKTOP_RENDERER_HOST
      )
  } catch {
    return false
  }
}

interface ArtifactUrlOptions {
  readonly absolute?: boolean
}

function artifactDownloadUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: ArtifactUrlOptions = {},
): string {
  let raw = artifact?.download_url ? String(artifact.download_url) : ''
  if (!raw && artifact?.id) raw = `/api/v1/artifacts/${encodeURIComponent(artifact.id)}`
  if (!raw) return ''
  try {
    const url = new URL(raw, baseOrigin)
    const base = new URL(baseOrigin)
    const sameOrigin = urlsShareArtifactOrigin(url, base)
    if (sameOrigin) {
      url.searchParams.delete('token')
      url.searchParams.delete('sessionKey')
      url.searchParams.delete('session_key')
    }
    if (!sameOrigin || options.absolute) return url.toString()
    return url.pathname + url.search + url.hash
  } catch {
    return raw
  }
}

export function artifactAccessUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: { absolute?: boolean } = {},
): string {
  return artifactDownloadUrl(artifact, baseOrigin, {
    absolute: options.absolute === true,
  })
}

export function artifactThumbnailAccessUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
): string {
  const thumbnailUrl = artifact?.thumbnail_url ? String(artifact.thumbnail_url) : ''
  return artifactDownloadUrl(
    thumbnailUrl ? { ...artifact, download_url: thumbnailUrl } : artifact,
    baseOrigin,
  )
}

export function artifactGatewayOpenUrl(artifact: ArtifactPayload, baseOrigin: string): string {
  const rawId = artifact?.id ? String(artifact.id) : ''
  if (rawId) return `/api/v1/artifacts/${encodeURIComponent(rawId)}/open`
  const accessUrl = artifactAccessUrl(artifact, baseOrigin)
  if (!accessUrl) return ''
  try {
    const url = new URL(accessUrl, baseOrigin)
    if (!isSameOriginArtifactUrl(url.toString(), baseOrigin)) return ''
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
  http: Pick<ArtifactAccessHttpTransport, 'requestBinary'>,
  artifact: ArtifactPayload,
  options: ArtifactFetchOptions = {},
): Promise<ArtifactGatewayOpenResult> {
  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  const url = artifactGatewayOpenUrl(artifact, baseOrigin)
  const title = safeTitle(artifact)
  if (!url) {
    return { ok: false, status: 0, url: '', message: artifactOpenFailureMessage(0, title) }
  }

  try {
    const response = await http.requestBinary(url, {
      method: 'POST',
      sessionKey: options.sessionKey,
      timeoutMs: 0,
    })
    await response.blob()
    return { ok: true, status: response.metadata.status, url }
  } catch (error) {
    const status = httpErrorStatus(error)
    return { ok: false, status, url, message: artifactOpenFailureMessage(status, title) }
  }
}

export async function fetchArtifactBlob(
  http: Pick<ArtifactAccessHttpTransport, 'fetchExternalArtifact' | 'requestBinary'>,
  artifact: ArtifactPayload,
  options: ArtifactFetchOptions = {},
): Promise<ArtifactFetchResult> {
  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  const url = artifactAccessUrl(artifact, baseOrigin)
  const title = safeTitle(artifact)
  if (!url) {
    return { ok: false, status: 0, url: '', message: artifactOpenFailureMessage(0, title) }
  }

  if (options.requireSameOrigin && !isTrustedArtifactTransportUrl(url, baseOrigin)) {
    return { ok: false, status: 0, url, message: artifactOpenFailureMessage(0, title) }
  }
  try {
    const sameOrigin = isSameOriginArtifactUrl(url, baseOrigin)
    const response = sameOrigin
      ? await http.requestBinary(url, {
        sessionKey: options.sessionKey,
        signal: options.signal,
        timeoutMs: 0,
      })
      : await http.fetchExternalArtifact(url, options.signal)
    return {
      ok: true,
      status: response.metadata.status,
      url,
      blob: await response.blob(),
    }
  } catch (error) {
    if (isAbortError(error)) {
      if (error instanceof HttpTransportError) throw new DOMException('Aborted', 'AbortError')
      throw error
    }
    const status = httpErrorStatus(error)
    return { ok: false, status, url, message: artifactOpenFailureMessage(status, title) }
  }
}

export async function openArtifactBlobUrl(
  http: Pick<ArtifactAccessHttpTransport, 'fetchExternalArtifact' | 'requestBinary'>,
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

  const fetched = await fetchArtifactBlob(http, artifact, options)
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

export function createV4ArtifactContentAccess(http: ArtifactAccessHttpTransport): Pick<
  ArtifactContentAccess,
  'fetchArtifact' | 'openArtifact' | 'openArtifactBlob' | 'clearPreviewStorage'
> {
  return {
    fetchArtifact: (artifact, request) => fetchArtifactBlob(http, artifact, runtimeOptions(request)),
    openArtifact: (artifact, request) => openArtifactViaGateway(http, artifact, runtimeOptions(request)),
    openArtifactBlob: (artifact, request) => openArtifactBlobUrl(http, artifact, runtimeOptions(request)),
    async clearPreviewStorage(previewOrigin) {
      try {
        await http.clearPreviewOrigin(previewOrigin)
      } catch {}
    },
  }
}
