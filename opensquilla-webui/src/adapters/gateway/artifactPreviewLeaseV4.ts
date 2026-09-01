import type { NativeWorkbenchApi, PlatformId } from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import { artifactAccessHeaders } from '@/utils/chat/artifactAccess'

export type ArtifactPreviewMode = 'full' | 'offline'
export type ArtifactPreviewCollectionStatus = 'complete' | 'partial' | 'not_applicable'

export interface ArtifactPreviewLeaseSource {
  kind: 'bundle' | 'single_file'
  collection_status: ArtifactPreviewCollectionStatus
  file_count: number
  total_bytes: number
  warning_codes: string[]
}

export interface ArtifactPreviewLease {
  version: 1
  lease_id: string
  effective_mode: ArtifactPreviewMode
  launch_url: string
  entrypoint: string
  expires_at: string
  preview_origin: string | null
  idle_timeout_seconds: number
  source: ArtifactPreviewLeaseSource
}

export interface ArtifactPreviewLeaseRenewal {
  version: 1
  lease_id: string
  expires_at: string
}

export type ArtifactPreviewNativeBroker = Pick<
  NativeWorkbenchApi,
  | 'createArtifactPreviewLease'
  | 'renewArtifactPreviewLease'
  | 'revokeArtifactPreviewLease'
>

export interface ArtifactPreviewLeaseContext {
  authToken?: string
  baseOrigin: string
  fetchImpl?: typeof fetch
  nativeBroker?: ArtifactPreviewNativeBroker
  sessionKey?: string
}

export class ArtifactPreviewLeaseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = '',
  ) {
    super(message)
    this.name = 'ArtifactPreviewLeaseError'
  }
}

export function artifactPreviewId(artifact: ArtifactPayload): string {
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

function normalizedBaseOrigin(baseOrigin: string): string {
  const fallback = typeof window === 'undefined'
    ? 'http://localhost'
    : window.location.origin
  return new URL(baseOrigin || fallback).origin
}

function fetcher(context: ArtifactPreviewLeaseContext): typeof fetch {
  if (context.fetchImpl) return context.fetchImpl
  if (typeof fetch !== 'function') {
    throw new ArtifactPreviewLeaseError('Preview leases are unavailable.', 0)
  }
  return fetch.bind(globalThis)
}

function controlHeaders(
  url: string,
  context: ArtifactPreviewLeaseContext,
  includeJson = false,
): Record<string, string> {
  return {
    ...artifactAccessHeaders(url, context),
    ...(includeJson ? { 'Content-Type': 'application/json' } : {}),
  }
}

function previewLeaseUrl(
  artifact: ArtifactPayload,
  context: ArtifactPreviewLeaseContext,
): string {
  const id = artifactPreviewId(artifact)
  if (!id) throw new ArtifactPreviewLeaseError('Artifact preview is unavailable.', 0)
  return new URL(
    `/api/v1/artifacts/${encodeURIComponent(id)}/preview-leases`,
    normalizedBaseOrigin(context.baseOrigin),
  ).toString()
}

function leaseControlUrl(
  leaseId: string,
  context: ArtifactPreviewLeaseContext,
  suffix = '',
): string {
  if (!leaseId || /[\u0000-\u001f/\\]/.test(leaseId)) {
    throw new ArtifactPreviewLeaseError('Artifact preview lease is invalid.', 0)
  }
  return new URL(
    `/api/v1/artifact-preview-leases/${encodeURIComponent(leaseId)}${suffix}`,
    normalizedBaseOrigin(context.baseOrigin),
  ).toString()
}

async function responseError(response: Response): Promise<ArtifactPreviewLeaseError> {
  let code = ''
  let message = `Artifact preview request failed (${response.status}).`
  try {
    const payload = await response.json() as {
      code?: unknown
      detail?: unknown
      error?: unknown
      message?: unknown
    }
    if (typeof payload.code === 'string') code = payload.code
    const detail = typeof payload.detail === 'string'
      ? payload.detail
      : typeof payload.message === 'string' ? payload.message : ''
    const error = typeof payload.error === 'string' ? payload.error : ''
    if (detail || error) message = detail || error
  } catch {}
  return new ArtifactPreviewLeaseError(message, response.status, code)
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
  let launch: URL
  try {
    launch = new URL(launchUrl, normalizedBaseOrigin(baseOrigin))
  } catch {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid launch URL.', 502)
  }
  if (
    raw.version !== 1
    || !leaseId
    || !entrypoint
    || !expiresAt
    || !['http:', 'https:'].includes(launch.protocol)
    || launch.username
    || launch.password
  ) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid lease.', 502)
  }
  const previewOrigin = typeof raw.preview_origin === 'string' && raw.preview_origin
    ? raw.preview_origin
    : null
  if (previewOrigin !== null && previewOrigin !== launch.origin) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid origin.', 502)
  }
  if (
    previewOrigin === null
    && baseOrigin
    && launch.origin !== normalizedBaseOrigin(baseOrigin)
  ) {
    throw new ArtifactPreviewLeaseError('Artifact preview returned an invalid origin.', 502)
  }
  return {
    version: 1,
    lease_id: leaseId,
    effective_mode: effectiveMode,
    launch_url: launch.toString(),
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
    let result
    try {
      result = await broker({
        version: 1,
        artifactId,
        mode,
        scopeId: context.sessionKey || '',
        ...(context.authToken ? { authToken: context.authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok) throw brokerError(result)
    return parseArtifactPreviewLease(result.payload, context.baseOrigin)
  }
  const url = previewLeaseUrl(artifact, context)
  const response = await fetcher(context)(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: controlHeaders(url, context, true),
    body: JSON.stringify({ version: 1, mode, client }),
  })
  if (!response.ok) throw await responseError(response)
  return parseArtifactPreviewLease(await response.json(), context.baseOrigin)
}

export async function renewArtifactPreviewLease(
  leaseId: string,
  context: ArtifactPreviewLeaseContext,
): Promise<ArtifactPreviewLeaseRenewal> {
  const broker = context.nativeBroker?.renewArtifactPreviewLease
  if (context.nativeBroker) {
    if (!broker) throw desktopBrokerUnavailable()
    let result
    try {
      result = await broker({
        version: 1,
        leaseId,
        scopeId: context.sessionKey || '',
        ...(context.authToken ? { authToken: context.authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok) throw brokerError(result)
    return parseArtifactPreviewLeaseRenewal(result.payload)
  }
  const url = leaseControlUrl(leaseId, context, '/renew')
  const response = await fetcher(context)(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: controlHeaders(url, context),
  })
  if (!response.ok) throw await responseError(response)
  return parseArtifactPreviewLeaseRenewal(await response.json())
}

export async function revokeArtifactPreviewLease(
  leaseId: string,
  context: ArtifactPreviewLeaseContext,
): Promise<void> {
  const broker = context.nativeBroker?.revokeArtifactPreviewLease
  if (context.nativeBroker) {
    if (!broker) throw desktopBrokerUnavailable()
    let result
    try {
      result = await broker({
        version: 1,
        leaseId,
        scopeId: context.sessionKey || '',
        ...(context.authToken ? { authToken: context.authToken } : {}),
      })
    } catch {
      throw desktopBrokerUnavailable()
    }
    if (!result.ok && result.status !== 404 && result.status !== 410) {
      throw brokerError(result)
    }
    return
  }
  const url = leaseControlUrl(leaseId, context)
  const response = await fetcher(context)(url, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: controlHeaders(url, context),
    keepalive: true,
  })
  if (!response.ok && response.status !== 404 && response.status !== 410) {
    throw await responseError(response)
  }
}
