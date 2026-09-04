import type { PlatformId } from '@/platform/types'
import {
  ArtifactPreviewLeaseError,
  type ArtifactPreviewLease,
  type ArtifactPreviewLeaseRenewal,
  type ArtifactPreviewLeaseRequest,
  type ArtifactPreviewLeaseSource,
  type ArtifactPreviewMode,
} from '@/modules/artifactWorkbench'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  HttpTransportError,
} from './privateHttpTransport'
import {
  artifactHttpBrokerAuthToken,
  artifactHttpPreviewId,
  createArtifactPreviewLeaseHttp,
  renewArtifactPreviewLeaseHttp,
  resolveArtifactPreviewLaunch,
  revokeArtifactPreviewLeaseHttp,
  validArtifactPreviewLeaseId,
} from './privateArtifactHttpTransport'

interface ArtifactPreviewLeaseHttpTransport {
  requestJson<T>(endpoint: string, options: {
    method: 'POST'
    json?: unknown
    sessionKey?: string
    timeoutMs?: number
  }): Promise<T>
  requestBlob(endpoint: string, options: {
    keepalive: true
    method: 'DELETE'
    sessionKey?: string
    timeoutMs?: number
  }): Promise<Blob>
}

interface ArtifactPreviewLeaseContext extends ArtifactPreviewLeaseRequest {
  authToken?: string
  baseOrigin: string
}

export function artifactPreviewId(artifact: ArtifactPayload): string {
  return artifactHttpPreviewId(artifact)
}

function resolvedAuthToken(context: ArtifactPreviewLeaseContext): string {
  return artifactHttpBrokerAuthToken(context.authToken)
}

function transportError(error: HttpTransportError): ArtifactPreviewLeaseError {
  let code = ''
  const status = typeof error.status === 'number' ? error.status : 0
  let message = status > 0
    ? `Artifact preview request failed (${status}).`
    : 'Artifact preview request failed.'
  const payload = error.payload
  if (payload && typeof payload === 'object') {
    const raw = payload as {
      code?: unknown
      detail?: unknown
      error?: unknown
      message?: unknown
    }
    if (typeof raw.code === 'string') code = raw.code
    const detail = typeof raw.detail === 'string'
      ? raw.detail
      : typeof raw.message === 'string' ? raw.message : ''
    const payloadError = typeof raw.error === 'string' ? raw.error : ''
    if (detail || payloadError) message = detail || payloadError
  }
  return new ArtifactPreviewLeaseError(message, status, code)
}

function translateTransportError(error: unknown): never {
  if (error instanceof HttpTransportError) throw transportError(error)
  throw error
}

function stringField(raw: Record<string, unknown>, key: string): string {
  return typeof raw[key] === 'string' ? String(raw[key]) : ''
}

function parseSource(value: unknown): ArtifactPreviewLeaseSource {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const warnings = Array.isArray(raw.warning_codes)
    ? raw.warning_codes.filter((value): value is string => typeof value === 'string')
    : []
  return {
    kind: raw.kind === 'bundle' ? 'bundle' : 'single_file',
    collection_status: raw.collection_status === 'complete'
      || raw.collection_status === 'partial'
      ? raw.collection_status
      : 'not_applicable',
    file_count: typeof raw.file_count === 'number' && Number.isFinite(raw.file_count)
      ? Math.max(1, Math.floor(raw.file_count))
      : 1,
    total_bytes: typeof raw.total_bytes === 'number' && Number.isFinite(raw.total_bytes)
      ? Math.max(0, Math.floor(raw.total_bytes))
      : 0,
    warning_codes: warnings,
  }
}

export function parseArtifactPreviewLease(
  value: unknown,
  baseOrigin = typeof window === 'undefined' ? '' : window.location.origin,
): ArtifactPreviewLease {
  if (!value || typeof value !== 'object') {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid lease.', 502)
  }
  const raw = value as Record<string, unknown>
  if (raw.effective_mode !== 'full' && raw.effective_mode !== 'offline') {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid lease.', 502)
  }
  const effectiveMode = raw.effective_mode
  const launchUrl = stringField(raw, 'launch_url')
  const leaseId = stringField(raw, 'lease_id')
  const entrypoint = stringField(raw, 'entrypoint')
  const expiresAt = stringField(raw, 'expires_at')
  const previewOrigin = typeof raw.preview_origin === 'string' && raw.preview_origin
    ? raw.preview_origin
    : null
  const launch = resolveArtifactPreviewLaunch(launchUrl, previewOrigin, baseOrigin)
  if (!launch.ok && launch.reason === 'syntax') {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid launch URL.', 502)
  }
  if (
    raw.version !== 1
    || !leaseId
    || !entrypoint
    || !expiresAt
    || (!launch.ok && launch.reason === 'url')
  ) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid lease.', 502)
  }
  if (!launch.ok) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid origin.', 502)
  }
  return {
    version: 1,
    lease_id: leaseId,
    effective_mode: effectiveMode,
    launch_url: launch.url,
    entrypoint,
    expires_at: expiresAt,
    preview_origin: previewOrigin,
    idle_timeout_seconds: typeof raw.idle_timeout_seconds === 'number'
      && Number.isFinite(raw.idle_timeout_seconds)
      ? Math.max(1, Math.floor(raw.idle_timeout_seconds))
      : 28_800,
    source: parseSource(raw.source),
  }
}

export function parseArtifactPreviewLeaseRenewal(
  value: unknown,
): ArtifactPreviewLeaseRenewal {
  if (!value || typeof value !== 'object') {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid renewal.', 502)
  }
  const raw = value as Record<string, unknown>
  const leaseId = stringField(raw, 'lease_id')
  const expiresAt = stringField(raw, 'expires_at')
  if (raw.version !== 1 || !leaseId || !expiresAt) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid renewal.', 502)
  }
  return {
    version: 1,
    lease_id: leaseId,
    expires_at: expiresAt,
  }
}

function brokerError(
  result: { status?: unknown; code?: unknown; message?: unknown },
): ArtifactPreviewLeaseError {
  return new ArtifactPreviewLeaseError(
    typeof result.message === 'string' && result.message
      ? result.message
      : 'The Desktop preview service is unavailable.',
    typeof result.status === 'number' && Number.isFinite(result.status)
      ? Math.max(0, Math.floor(result.status))
      : 0,
    typeof result.code === 'string' ? result.code : 'PREVIEW_BROKER_UNAVAILABLE',
  )
}

function desktopBrokerUnavailable(): ArtifactPreviewLeaseError {
  return new ArtifactPreviewLeaseError(
    'Update OpenSquilla Desktop to use browser-grade Artifact previews.',
    0,
    'DESKTOP_PREVIEW_BROKER_UNAVAILABLE',
  )
}

export async function createArtifactPreviewLease(
  http: Pick<ArtifactPreviewLeaseHttpTransport, 'requestJson'>,
  artifact: ArtifactPayload,
  mode: ArtifactPreviewMode,
  client: PlatformId,
  context: ArtifactPreviewLeaseContext,
): Promise<ArtifactPreviewLease> {
  if (client === 'desktop') {
    const artifactId = artifactPreviewId(artifact)
    if (!artifactId) {
      throw new ArtifactPreviewLeaseError('Artifact preview is unavailable.', 0)
    }
    const broker = context.nativeBroker?.createArtifactPreviewLease
    if (!broker) throw desktopBrokerUnavailable()
    const authToken = resolvedAuthToken(context)
    let result
    try {
      result = await broker({
        version: 1,
        artifactId,
        mode,
        scopeId: context.sessionKey || '',
        ...(authToken ? { authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok) throw brokerError(result)
    return parseArtifactPreviewLease(result.payload, context.baseOrigin)
  }
  const artifactId = artifactPreviewId(artifact)
  if (!artifactId) {
    throw new ArtifactPreviewLeaseError('Artifact preview is unavailable.', 0)
  }
  try {
    const payload = await createArtifactPreviewLeaseHttp<unknown>(
      http,
      artifactId,
      mode,
      client,
      context,
    )
    return parseArtifactPreviewLease(payload, context.baseOrigin)
  } catch (error) {
    translateTransportError(error)
  }
}

export async function renewArtifactPreviewLease(
  http: Pick<ArtifactPreviewLeaseHttpTransport, 'requestJson'>,
  leaseId: string,
  context: ArtifactPreviewLeaseContext,
): Promise<ArtifactPreviewLeaseRenewal> {
  const broker = context.nativeBroker?.renewArtifactPreviewLease
  if (context.nativeBroker) {
    if (!broker) throw desktopBrokerUnavailable()
    const authToken = resolvedAuthToken(context)
    let result
    try {
      result = await broker({
        version: 1,
        leaseId,
        scopeId: context.sessionKey || '',
        ...(authToken ? { authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok) throw brokerError(result)
    return parseArtifactPreviewLeaseRenewal(result.payload)
  }
  if (!validArtifactPreviewLeaseId(leaseId)) {
    throw new ArtifactPreviewLeaseError('Artifact preview lease is invalid.', 0)
  }
  try {
    const payload = await renewArtifactPreviewLeaseHttp<unknown>(http, leaseId, context)
    return parseArtifactPreviewLeaseRenewal(payload)
  } catch (error) {
    translateTransportError(error)
  }
}

export async function revokeArtifactPreviewLease(
  http: Pick<ArtifactPreviewLeaseHttpTransport, 'requestBlob'>,
  leaseId: string,
  context: ArtifactPreviewLeaseContext,
): Promise<void> {
  const broker = context.nativeBroker?.revokeArtifactPreviewLease
  if (context.nativeBroker) {
    if (!broker) throw desktopBrokerUnavailable()
    const authToken = resolvedAuthToken(context)
    let result
    try {
      result = await broker({
        version: 1,
        leaseId,
        scopeId: context.sessionKey || '',
        ...(authToken ? { authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok && result.status !== 404 && result.status !== 410) {
      throw brokerError(result)
    }
    return
  }
  if (!validArtifactPreviewLeaseId(leaseId)) {
    throw new ArtifactPreviewLeaseError('Artifact preview lease is invalid.', 0)
  }
  try {
    await revokeArtifactPreviewLeaseHttp(http, leaseId, context)
  } catch (error) {
    if (
      error instanceof HttpTransportError
      && error.kind === 'http-status'
      && (error.status === 404 || error.status === 410)
    ) return
    translateTransportError(error)
  }
}
