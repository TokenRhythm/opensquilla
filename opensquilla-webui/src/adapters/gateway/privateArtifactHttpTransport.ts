import type { ArtifactPayload } from '@/types/artifacts'
import type { ArtifactPreviewMode } from '@/modules/artifactWorkbench'
import type { PlatformId } from '@/platform/types'

const ARTIFACT_CONTENT_PATH = '/api/v1/artifacts/'
const ATTACHMENT_UPLOAD_PATH = '/api/v1/files/upload'
const ARTIFACT_PREVIEW_LEASE_PATH = '/api/v1/artifact-preview-leases/'
const DESKTOP_RENDERER_PROTOCOL = 'opensquilla-app:'
const DESKTOP_RENDERER_HOST = 'desktop'
const CREDENTIAL_QUERY_KEYS = /(token|session)/i
const WS_TOKEN_KEY = 'opensquilla.wsToken'
const DEFAULT_BASE_ORIGIN = 'http://localhost'

interface ArtifactBinaryHttpTransport<Response> {
  fetchExternalArtifact?(endpoint: string, signal?: AbortSignal): Promise<Response>
  requestBinary(endpoint: string, options?: {
    method?: 'GET' | 'POST'
    sessionKey?: string
    signal?: AbortSignal
    timeoutMs?: number
  }): Promise<Response>
}

interface ArtifactJsonHttpTransport {
  requestJson<T>(endpoint: string, options: {
    method: 'POST'
    form?: FormData
    json?: unknown
    sessionKey?: string
    timeoutMs?: number
  }): Promise<T>
}

interface ArtifactBlobHttpTransport {
  requestBlob(endpoint: string, options: {
    keepalive: true
    method: 'DELETE'
    sessionKey?: string
    timeoutMs?: number
  }): Promise<Blob>
}

type ArtifactHttpOriginPolicy =
  | 'allow-external'
  | 'same-origin'
  | 'trusted-origin'

interface ArtifactBinaryRequestOptions {
  readonly baseOrigin: string
  readonly policy: ArtifactHttpOriginPolicy
  readonly variant?: 'content' | 'thumbnail'
}

interface ArtifactBinaryExecutionOptions {
  readonly sessionKey?: string
  readonly signal?: AbortSignal
  readonly timeoutMs?: number
}

interface BoundArtifactBinaryRequest<Response> {
  readonly url: string
  execute(options?: ArtifactBinaryExecutionOptions): Promise<Response>
}

interface AttachmentBinaryRequestOptions {
  readonly baseOrigin: string
}

interface ArtifactPreviewHttpContext {
  readonly baseOrigin: string
  readonly sessionKey?: string
}

type ArtifactPreviewLaunchResolution =
  | { readonly ok: true; readonly url: string }
  | { readonly ok: false; readonly reason: 'syntax' | 'url' | 'origin' }

export function runtimeArtifactHttpBaseOrigin(): string {
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

export function runtimeAttachmentHttpBaseOrigin(): string {
  return typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : DEFAULT_BASE_ORIGIN
}

function urlsShareArtifactOrigin(candidate: URL, base: URL): boolean {
  if (candidate.origin !== 'null' || base.origin !== 'null') {
    return candidate.origin === base.origin
  }
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

function sameOrigin(url: string, baseOrigin: string): boolean {
  try {
    return urlsShareArtifactOrigin(new URL(url, baseOrigin), new URL(baseOrigin))
  } catch {
    return false
  }
}

function trustedOrigin(url: string, baseOrigin: string): boolean {
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

export function isSameArtifactHttpOrigin(url: string, baseOrigin: string): boolean {
  return sameOrigin(url, baseOrigin)
}

export function isTrustedArtifactHttpUrl(url: string, baseOrigin: string): boolean {
  return trustedOrigin(url, baseOrigin)
}

function artifactContentUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  variant: 'content' | 'thumbnail',
  absolute = false,
): string {
  const source = variant === 'thumbnail' && artifact.thumbnail_url
    ? { ...artifact, download_url: String(artifact.thumbnail_url) }
    : artifact
  let raw = source.download_url ? String(source.download_url) : ''
  if (!raw && source.id) raw = `${ARTIFACT_CONTENT_PATH}${encodeURIComponent(source.id)}`
  if (!raw) return ''
  try {
    const url = new URL(raw, baseOrigin)
    const base = new URL(baseOrigin)
    const local = urlsShareArtifactOrigin(url, base)
    if (local) {
      url.searchParams.delete('token')
      url.searchParams.delete('sessionKey')
      url.searchParams.delete('session_key')
    }
    if (!local || absolute) return url.toString()
    return url.pathname + url.search + url.hash
  } catch {
    return raw
  }
}

export function artifactHttpAccessUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
  options: { readonly absolute?: boolean } = {},
): string {
  return artifactContentUrl(artifact, baseOrigin, 'content', options.absolute === true)
}

export function artifactHttpThumbnailUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
): string {
  return artifactContentUrl(artifact, baseOrigin, 'thumbnail')
}

export function artifactHttpGatewayOpenUrl(
  artifact: ArtifactPayload,
  baseOrigin: string,
): string {
  const id = artifact.id ? String(artifact.id) : ''
  if (id) return `${ARTIFACT_CONTENT_PATH}${encodeURIComponent(id)}/open`
  const accessUrl = artifactHttpAccessUrl(artifact, baseOrigin)
  if (!accessUrl) return ''
  try {
    const url = new URL(accessUrl, baseOrigin)
    if (!sameOrigin(url.toString(), baseOrigin)) return ''
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/([^/]+)$/)
    return match ? `${ARTIFACT_CONTENT_PATH}${match[1]}/open` : ''
  } catch {
    return ''
  }
}

export function bindArtifactOpenRequest<Response>(
  http: Pick<ArtifactBinaryHttpTransport<Response>, 'requestBinary'>,
  artifact: ArtifactPayload,
  options: { readonly baseOrigin: string },
): BoundArtifactBinaryRequest<Response> | null {
  const url = artifactHttpGatewayOpenUrl(artifact, options.baseOrigin)
  if (!url) return null
  return {
    url,
    execute(execution = {}) {
      return http.requestBinary(url, {
        method: 'POST',
        sessionKey: execution.sessionKey,
        timeoutMs: execution.timeoutMs,
      })
    },
  }
}

export function artifactHttpAttachmentUrl(raw: unknown, baseOrigin: string): string {
  if (typeof raw !== 'string' || !raw.trim()) return ''
  try {
    const base = new URL(baseOrigin)
    const url = new URL(raw, base)
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.origin !== base.origin) {
      return ''
    }
    if (url.username || url.password) return ''
    for (const key of [...url.searchParams.keys()]) {
      if (CREDENTIAL_QUERY_KEYS.test(key)) url.searchParams.delete(key)
    }
    url.hash = ''
    return url.pathname + url.search
  } catch {
    return ''
  }
}

export function bindAttachmentBinaryRequest<Response>(
  http: Pick<ArtifactBinaryHttpTransport<Response>, 'requestBinary'>,
  raw: unknown,
  options: AttachmentBinaryRequestOptions,
): BoundArtifactBinaryRequest<Response> | null {
  const url = artifactHttpAttachmentUrl(raw, options.baseOrigin)
  if (!url) return null
  return {
    url,
    execute(execution = {}) {
      return http.requestBinary(url, {
        sessionKey: execution.sessionKey,
        signal: execution.signal,
        timeoutMs: execution.timeoutMs,
      })
    },
  }
}

export function uploadArtifactAttachment<T>(
  http: ArtifactJsonHttpTransport,
  form: FormData,
): Promise<T> {
  return http.requestJson<T>(ATTACHMENT_UPLOAD_PATH, {
    method: 'POST',
    form,
  })
}

export function normalizedArtifactHttpOrigin(baseOrigin: string): string {
  const fallback = typeof window === 'undefined'
    ? 'http://localhost'
    : window.location.origin
  return new URL(baseOrigin || fallback).origin
}

export function artifactHttpPreviewId(artifact: ArtifactPayload): string {
  const direct = typeof artifact.id === 'string' ? artifact.id.trim() : ''
  if (direct) return direct
  const downloadUrl = typeof artifact.download_url === 'string' ? artifact.download_url : ''
  const match = downloadUrl.match(/\/api\/v1\/artifacts\/([^/?#]+)/)
  if (!match) return ''
  try {
    return decodeURIComponent(match[1])
  } catch {
    return ''
  }
}

export function validArtifactPreviewLeaseId(leaseId: string): boolean {
  return !!leaseId && !/[\u0000-\u001f/\\]/.test(leaseId)
}

export function artifactHttpBrokerAuthToken(explicit?: string): string {
  if (explicit !== undefined) return explicit
  try {
    return globalThis.sessionStorage?.getItem(WS_TOKEN_KEY)?.trim() || ''
  } catch {
    return ''
  }
}

export function resolveArtifactPreviewLaunch(
  launchUrl: string,
  previewOrigin: string | null,
  baseOrigin: string,
): ArtifactPreviewLaunchResolution {
  let launch: URL
  try {
    launch = new URL(launchUrl, normalizedArtifactHttpOrigin(baseOrigin))
  } catch {
    return { ok: false, reason: 'syntax' }
  }
  if (
    (launch.protocol !== 'http:' && launch.protocol !== 'https:')
    || !!launch.username
    || !!launch.password
  ) return { ok: false, reason: 'url' }
  if (previewOrigin !== null && previewOrigin !== launch.origin) {
    return { ok: false, reason: 'origin' }
  }
  if (
    previewOrigin === null
    && baseOrigin
    && launch.origin !== normalizedArtifactHttpOrigin(baseOrigin)
  ) return { ok: false, reason: 'origin' }
  return { ok: true, url: launch.toString() }
}

export function createArtifactPreviewLeaseHttp<T>(
  http: ArtifactJsonHttpTransport,
  artifactId: string,
  mode: ArtifactPreviewMode,
  client: PlatformId,
  context: ArtifactPreviewHttpContext,
): Promise<T> {
  const url = new URL(
    `${ARTIFACT_CONTENT_PATH}${encodeURIComponent(artifactId)}/preview-leases`,
    normalizedArtifactHttpOrigin(context.baseOrigin),
  ).toString()
  return http.requestJson<T>(url, {
    method: 'POST',
    json: { version: 1, mode, client },
    sessionKey: context.sessionKey,
    timeoutMs: 0,
  })
}

function artifactPreviewLeaseControlUrl(
  leaseId: string,
  context: ArtifactPreviewHttpContext,
  suffix = '',
): string {
  return new URL(
    `${ARTIFACT_PREVIEW_LEASE_PATH}${encodeURIComponent(leaseId)}${suffix}`,
    normalizedArtifactHttpOrigin(context.baseOrigin),
  ).toString()
}

export function renewArtifactPreviewLeaseHttp<T>(
  http: ArtifactJsonHttpTransport,
  leaseId: string,
  context: ArtifactPreviewHttpContext,
): Promise<T> {
  return http.requestJson<T>(artifactPreviewLeaseControlUrl(leaseId, context, '/renew'), {
    method: 'POST',
    sessionKey: context.sessionKey,
    timeoutMs: 0,
  })
}

export async function revokeArtifactPreviewLeaseHttp(
  http: ArtifactBlobHttpTransport,
  leaseId: string,
  context: ArtifactPreviewHttpContext,
): Promise<void> {
  await http.requestBlob(artifactPreviewLeaseControlUrl(leaseId, context), {
    keepalive: true,
    method: 'DELETE',
    sessionKey: context.sessionKey,
    timeoutMs: 0,
  })
}

export function bindArtifactBinaryRequest<Response>(
  http: ArtifactBinaryHttpTransport<Response>,
  artifact: ArtifactPayload,
  options: ArtifactBinaryRequestOptions,
): BoundArtifactBinaryRequest<Response> | null {
  const url = artifactContentUrl(artifact, options.baseOrigin, options.variant ?? 'content')
  if (!url) return null
  const local = sameOrigin(url, options.baseOrigin)
  if (
    (options.policy === 'same-origin' && !local)
    || (options.policy === 'trusted-origin' && !trustedOrigin(url, options.baseOrigin))
  ) return null

  return {
    url,
    execute(execution = {}) {
      if (!local) {
        if (!http.fetchExternalArtifact) {
          throw new TypeError('External Artifact transport is unavailable.')
        }
        return http.fetchExternalArtifact(url, execution.signal)
      }
      return http.requestBinary(url, {
        sessionKey: execution.sessionKey,
        signal: execution.signal,
        timeoutMs: execution.timeoutMs,
      })
    },
  }
}
