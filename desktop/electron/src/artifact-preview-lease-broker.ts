export type ArtifactPreviewLeaseMode = 'full' | 'offline'

export interface ArtifactPreviewLeaseCreateRequest {
  version: 1
  artifactId: string
  scopeId: string
  mode: ArtifactPreviewLeaseMode
  authToken?: string
}

export interface ArtifactPreviewLeaseControlRequest {
  version: 1
  leaseId: string
  scopeId: string
  authToken?: string
}

export interface ArtifactPreviewLeaseSource {
  kind: 'bundle' | 'single_file'
  collection_status: 'complete' | 'partial' | 'not_applicable'
  file_count: number
  total_bytes: number
  warning_codes: string[]
}

export interface ArtifactPreviewLeasePayload {
  version: 1
  lease_id: string
  effective_mode: ArtifactPreviewLeaseMode
  launch_url: string
  entrypoint: string
  expires_at: string
  preview_origin: string
  idle_timeout_seconds: number
  source: ArtifactPreviewLeaseSource
}

export interface ArtifactPreviewLeaseRenewalPayload {
  version: 1
  lease_id: string
  expires_at: string
}

export type ArtifactPreviewLeaseBrokerResult<T = undefined> = {
  ok: true
  status: number
  payload: T
} | {
  ok: false
  status: number
  code: string
  message: string
}

type ArtifactPreviewLeaseBrokerFailure = Extract<
  ArtifactPreviewLeaseBrokerResult<unknown>,
  { ok: false }
>

export interface ArtifactPreviewSurfaceGrant {
  launchUrl: string
  expectedOrigin: string
  scopeId: string
  mode: ArtifactPreviewLeaseMode
}

export interface ArtifactPreviewSurfacePin {
  currentGrant(): ArtifactPreviewSurfaceGrant
  ensureCurrent(): Promise<ArtifactPreviewSurfaceGrant | null>
  release(): Promise<void>
}

interface IssuedArtifactPreview {
  leaseId: string
  artifactId: string
  gatewayOrigin: string
  launchUrl: string
  expectedOrigin: string
  scopeId: string
  authToken?: string
  mode: ArtifactPreviewLeaseMode
  expiresAtMs: number
  bindingPins: number
  revokePending: boolean
  renewTimer: ReturnType<typeof setInterval> | null
  renewInFlight: boolean
  replacementAttempted: boolean
  replacementInFlight: Promise<boolean> | null
  retiredLeaseIds: Set<string>
}

interface ArtifactPreviewLeaseBrokerOptions {
  getOwnedGatewayUrl: () => string | null
  fetchImpl?: typeof fetch
  now?: () => number
  timeoutMs?: number
}

const ARTIFACT_ID_PATTERN = /^art-[A-Za-z0-9_-]{1,200}$/
const LEASE_ID_PATTERN = /^apl-[A-Za-z0-9_-]{1,240}$/
const PREVIEW_HOST_PATTERN = /^p-[a-f0-9]{32}\.localhost$/i
const MAX_RESPONSE_BYTES = 1024 * 1024
const MAX_CREDENTIAL_BYTES = 16 * 1024
const DEFAULT_REQUEST_TIMEOUT_MS = 15_000
const PINNED_LEASE_RENEW_INTERVAL_MS = 15 * 60 * 1000

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function exactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const allowed = new Set([...required, ...optional])
  return required.every(key => Object.hasOwn(value, key))
    && Object.keys(value).every(key => allowed.has(key))
}

function parseBoundedString(
  value: unknown,
  label: string,
  maximumLength: number,
): string {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > maximumLength
    || value !== value.trim()
    || /[\u0000-\u001f\u007f]/.test(value)
  ) {
    throw new Error(`${label} is invalid.`)
  }
  return value
}

function parseScopeId(value: unknown): string {
  return parseBoundedString(value, 'The preview scope', 512)
}

function parseAuthToken(value: unknown): string | undefined {
  if (value === undefined || value === '') return undefined
  return parseBoundedString(value, 'The preview credential', MAX_CREDENTIAL_BYTES)
}

export function parseArtifactPreviewLeaseCreateRequest(
  value: unknown,
): ArtifactPreviewLeaseCreateRequest {
  const raw = objectRecord(value)
  if (
    !raw
    || !exactKeys(raw, ['version', 'artifactId', 'scopeId', 'mode'], ['authToken'])
    || raw.version !== 1
    || typeof raw.version === 'boolean'
    || !ARTIFACT_ID_PATTERN.test(String(raw.artifactId ?? ''))
    || (raw.mode !== 'full' && raw.mode !== 'offline')
  ) {
    throw new Error('The artifact preview lease request is invalid.')
  }
  const authToken = parseAuthToken(raw.authToken)
  return {
    version: 1,
    artifactId: String(raw.artifactId),
    scopeId: parseScopeId(raw.scopeId),
    mode: raw.mode,
    ...(authToken ? { authToken } : {}),
  }
}

export function parseArtifactPreviewLeaseControlRequest(
  value: unknown,
): ArtifactPreviewLeaseControlRequest {
  const raw = objectRecord(value)
  if (
    !raw
    || !exactKeys(raw, ['version', 'leaseId', 'scopeId'], ['authToken'])
    || raw.version !== 1
    || typeof raw.version === 'boolean'
    || !LEASE_ID_PATTERN.test(String(raw.leaseId ?? ''))
  ) {
    throw new Error('The artifact preview lease control request is invalid.')
  }
  const authToken = parseAuthToken(raw.authToken)
  return {
    version: 1,
    leaseId: String(raw.leaseId),
    scopeId: parseScopeId(raw.scopeId),
    ...(authToken ? { authToken } : {}),
  }
}

function parseOwnedGatewayOrigin(value: string | null): string | null {
  if (!value) return null
  try {
    const parsed = new URL(value)
    const hostname = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase()
    if (
      parsed.protocol !== 'http:'
      || !parsed.port
      || parsed.username
      || parsed.password
      || parsed.search
      || parsed.hash
      || (parsed.pathname !== '/' && parsed.pathname !== '')
      || !['127.0.0.1', 'localhost', '::1'].includes(hostname)
    ) return null
    return parsed.origin
  } catch {
    return null
  }
}

function safeErrorText(value: unknown, fallback: string): string {
  if (typeof value !== 'string') return fallback
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim()
  return normalized ? normalized.slice(0, 1000) : fallback
}

function parsePositiveInteger(value: unknown, label: string, allowZero = false): number {
  if (
    typeof value !== 'number'
    || !Number.isSafeInteger(value)
    || (allowZero ? value < 0 : value <= 0)
  ) throw new Error(`${label} is invalid.`)
  return value
}

function parseExpiresAt(value: unknown): { value: string; milliseconds: number } {
  const text = parseBoundedString(value, 'The preview lease expiry', 100)
  const milliseconds = Date.parse(text)
  if (!Number.isFinite(milliseconds)) {
    throw new Error('The preview lease expiry is invalid.')
  }
  return { value: text, milliseconds }
}

function parseLeaseSource(value: unknown): ArtifactPreviewLeaseSource {
  const raw = objectRecord(value)
  if (
    !raw
    || !exactKeys(
      raw,
      ['kind', 'collection_status', 'file_count', 'total_bytes', 'warning_codes'],
    )
    || (raw.kind !== 'bundle' && raw.kind !== 'single_file')
    || !['complete', 'partial', 'not_applicable'].includes(String(raw.collection_status))
    || !Array.isArray(raw.warning_codes)
  ) throw new Error('The preview lease source is invalid.')
  const warningCodes = raw.warning_codes.map((value) =>
    parseBoundedString(value, 'The preview warning code', 200))
  return {
    kind: raw.kind,
    collection_status: raw.collection_status as ArtifactPreviewLeaseSource['collection_status'],
    file_count: parsePositiveInteger(raw.file_count, 'The preview file count'),
    total_bytes: parsePositiveInteger(raw.total_bytes, 'The preview byte count', true),
    warning_codes: warningCodes,
  }
}

function parseLeasePayload(
  value: unknown,
  requestedMode: ArtifactPreviewLeaseMode,
): { payload: ArtifactPreviewLeasePayload; expiresAtMs: number } {
  const raw = objectRecord(value)
  if (
    !raw
    || !exactKeys(raw, [
      'version',
      'lease_id',
      'effective_mode',
      'launch_url',
      'entrypoint',
      'expires_at',
      'preview_origin',
      'idle_timeout_seconds',
      'source',
    ])
    || raw.version !== 1
    || typeof raw.version === 'boolean'
    || !LEASE_ID_PATTERN.test(String(raw.lease_id ?? ''))
    || (raw.effective_mode !== 'full' && raw.effective_mode !== 'offline')
    || (requestedMode === 'offline' && raw.effective_mode !== 'offline')
  ) throw new Error('The preview lease response is invalid.')

  const launchUrl = parseBoundedString(raw.launch_url, 'The preview launch address', 8192)
  const previewOrigin = parseBoundedString(raw.preview_origin, 'The preview origin', 1024)
  let launch: URL
  try {
    launch = new URL(launchUrl)
  } catch {
    throw new Error('The preview launch address is invalid.')
  }
  if (
    launch.protocol !== 'http:'
    || !launch.port
    || launch.username
    || launch.password
    || launch.search
    || launch.hash
    || !PREVIEW_HOST_PATTERN.test(launch.hostname)
    || launch.origin !== previewOrigin
  ) throw new Error('The preview lease response is not a trusted loopback preview.')

  const expiry = parseExpiresAt(raw.expires_at)
  return {
    expiresAtMs: expiry.milliseconds,
    payload: {
      version: 1,
      lease_id: String(raw.lease_id),
      effective_mode: raw.effective_mode,
      launch_url: launch.href,
      entrypoint: parseBoundedString(raw.entrypoint, 'The preview entrypoint', 4096),
      expires_at: expiry.value,
      preview_origin: previewOrigin,
      idle_timeout_seconds: parsePositiveInteger(
        raw.idle_timeout_seconds,
        'The preview idle timeout',
      ),
      source: parseLeaseSource(raw.source),
    },
  }
}

function parseRenewalPayload(
  value: unknown,
  expectedLeaseId: string,
): { payload: ArtifactPreviewLeaseRenewalPayload; expiresAtMs: number } {
  const raw = objectRecord(value)
  if (
    !raw
    || !exactKeys(raw, ['version', 'lease_id', 'expires_at'])
    || raw.version !== 1
    || typeof raw.version === 'boolean'
    || raw.lease_id !== expectedLeaseId
  ) throw new Error('The preview lease renewal response is invalid.')
  const expiry = parseExpiresAt(raw.expires_at)
  return {
    expiresAtMs: expiry.milliseconds,
    payload: {
      version: 1,
      lease_id: expectedLeaseId,
      expires_at: expiry.value,
    },
  }
}

function failure(
  status: number,
  code: string,
  message: string,
): ArtifactPreviewLeaseBrokerFailure {
  return { ok: false, status, code, message }
}

export class ArtifactPreviewLeaseBroker {
  private readonly fetchImpl: typeof fetch
  private readonly now: () => number
  private readonly timeoutMs: number
  private readonly issued = new Map<string, IssuedArtifactPreview>()
  private generation = 0

  constructor(private readonly options: ArtifactPreviewLeaseBrokerOptions) {
    this.fetchImpl = options.fetchImpl ?? fetch
    this.now = options.now ?? Date.now
    this.timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS
  }

  clear(): void {
    this.generation += 1
    for (const lease of this.issued.values()) this.stopPinnedRenewal(lease)
    this.issued.clear()
  }

  /**
   * Revoke every lease owned by the current renderer generation.
   *
   * Local authority is removed synchronously before any network request. The
   * snapshot also keeps leases created by a replacement renderer out of this
   * cleanup, even while the old Gateway DELETE requests are still pending.
   */
  async revokeAll(): Promise<void> {
    this.generation += 1
    const issued = [...this.issued.entries()]
    for (const [leaseId, lease] of issued) {
      if (lease.bindingPins > 0) {
        lease.revokePending = true
      } else {
        this.stopPinnedRenewal(lease)
        this.issued.delete(leaseId)
      }
    }
    await Promise.allSettled(issued.map(([leaseId, lease]) => {
      if (lease.bindingPins > 0) return Promise.resolve()
      if (parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl()) !== lease.gatewayOrigin) {
        return Promise.resolve()
      }
      return this.request(
        new URL(
          `/api/v1/artifact-preview-leases/${encodeURIComponent(leaseId)}`,
          lease.gatewayOrigin,
        ),
        'DELETE',
        lease.scopeId,
        lease.authToken,
      )
    }))
  }

  async create(
    value: unknown,
  ): Promise<ArtifactPreviewLeaseBrokerResult<ArtifactPreviewLeasePayload>> {
    const generation = this.generation
    let request: ArtifactPreviewLeaseCreateRequest
    try {
      request = parseArtifactPreviewLeaseCreateRequest(value)
    } catch (error) {
      return failure(
        400,
        'INVALID_REQUEST',
        error instanceof Error ? error.message : 'The preview request is invalid.',
      )
    }
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (!gatewayOrigin) {
      return failure(
        503,
        'OWNED_GATEWAY_UNAVAILABLE',
        'The Desktop-owned Gateway is unavailable.',
      )
    }
    const url = new URL(
      `/api/v1/artifacts/${encodeURIComponent(request.artifactId)}/preview-leases`,
      gatewayOrigin,
    )
    const response = await this.requestJson(
      url,
      'POST',
      request.scopeId,
      request.authToken,
      JSON.stringify({ version: 1, mode: request.mode, client: 'desktop' }),
    )
    if (!response.ok) return response
    if (response.status !== 201) {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
    try {
      const parsed = parseLeasePayload(response.payload, request.mode)
      if (
        generation !== this.generation
        || parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl()) !== gatewayOrigin
      ) {
        if (parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl()) === gatewayOrigin) {
          await this.request(
            new URL(
              `/api/v1/artifact-preview-leases/${encodeURIComponent(parsed.payload.lease_id)}`,
              gatewayOrigin,
            ),
            'DELETE',
            request.scopeId,
            request.authToken,
          )
        }
        return failure(
          409,
          'PREVIEW_LEASE_RETIRED',
          'The Desktop preview request was retired.',
        )
      }
      if (parsed.expiresAtMs <= this.now()) {
        return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an expired preview lease.')
      }
      this.issued.set(parsed.payload.lease_id, {
        leaseId: parsed.payload.lease_id,
        artifactId: request.artifactId,
        gatewayOrigin,
        launchUrl: parsed.payload.launch_url,
        expectedOrigin: parsed.payload.preview_origin,
        scopeId: request.scopeId,
        authToken: request.authToken,
        mode: parsed.payload.effective_mode,
        expiresAtMs: parsed.expiresAtMs,
        bindingPins: 0,
        revokePending: false,
        renewTimer: null,
        renewInFlight: false,
        replacementAttempted: false,
        replacementInFlight: null,
        retiredLeaseIds: new Set(),
      })
      return { ok: true, status: response.status, payload: parsed.payload }
    } catch {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
  }

  async renew(
    value: unknown,
  ): Promise<ArtifactPreviewLeaseBrokerResult<ArtifactPreviewLeaseRenewalPayload>> {
    let request: ArtifactPreviewLeaseControlRequest
    try {
      request = parseArtifactPreviewLeaseControlRequest(value)
    } catch (error) {
      return failure(
        400,
        'INVALID_REQUEST',
        error instanceof Error ? error.message : 'The preview request is invalid.',
      )
    }
    const issued = this.currentIssuedLease(request.leaseId, request.scopeId)
    if (!issued) {
      return failure(404, 'BROKER_LEASE_NOT_FOUND', 'The Desktop preview lease is unavailable.')
    }
    const response = await this.requestJson(
      new URL(
        `/api/v1/artifact-preview-leases/${encodeURIComponent(request.leaseId)}/renew`,
        issued.gatewayOrigin,
      ),
      'POST',
      request.scopeId,
      request.authToken,
    )
    if (!response.ok) {
      if (response.status === 404 || response.status === 410) {
        this.stopPinnedRenewal(issued)
        if (issued.bindingPins === 0) this.issued.delete(request.leaseId)
      }
      return response
    }
    if (response.status !== 200) {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
    try {
      const parsed = parseRenewalPayload(response.payload, request.leaseId)
      if (parsed.expiresAtMs <= this.now()) {
        this.issued.delete(request.leaseId)
        return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an expired preview lease.')
      }
      issued.expiresAtMs = parsed.expiresAtMs
      return { ok: true, status: response.status, payload: parsed.payload }
    } catch {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
  }

  async revoke(
    value: unknown,
  ): Promise<ArtifactPreviewLeaseBrokerResult<undefined>> {
    let request: ArtifactPreviewLeaseControlRequest
    try {
      request = parseArtifactPreviewLeaseControlRequest(value)
    } catch (error) {
      return failure(
        400,
        'INVALID_REQUEST',
        error instanceof Error ? error.message : 'The preview request is invalid.',
      )
    }
    const issued = this.currentIssuedLease(request.leaseId, request.scopeId)
      ?? this.retiredIssuedLease(request.leaseId, request.scopeId)
    if (!issued) {
      return failure(404, 'BROKER_LEASE_NOT_FOUND', 'The Desktop preview lease is unavailable.')
    }
    if (issued.bindingPins > 0) {
      issued.revokePending = true
      return { ok: true, status: 202, payload: undefined }
    }
    // Revoke local authority before the network request. A failed Gateway call
    // must never leave a renderer-revoked launch URL eligible for a new surface.
    this.stopPinnedRenewal(issued)
    this.issued.delete(request.leaseId)
    const response = await this.request(
      new URL(
        `/api/v1/artifact-preview-leases/${encodeURIComponent(request.leaseId)}`,
        issued.gatewayOrigin,
      ),
      'DELETE',
      request.scopeId,
      request.authToken,
    )
    if (!response.ok) return response
    if (response.status !== 204) {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
    return { ok: true, status: response.status, payload: undefined }
  }

  pinSurface(grant: ArtifactPreviewSurfaceGrant): ArtifactPreviewSurfacePin | null {
    const lease = this.issuedLeaseForSurface(grant)
    if (!lease) return null
    const [, issued] = lease
    issued.bindingPins += 1
    this.startPinnedRenewal(issued)
    let released = false
    return {
      currentGrant: () => this.surfaceGrant(issued),
      ensureCurrent: async () => {
        if (released || issued.bindingPins < 1) return null
        if (issued.replacementInFlight) await issued.replacementInFlight
        if (
          this.issued.get(issued.leaseId) !== issued
          || issued.expiresAtMs <= this.now()
        ) {
          if (!await this.replacePinnedLease(issued)) return null
        }
        return this.issued.get(issued.leaseId) === issued
          && issued.expiresAtMs > this.now()
          ? this.surfaceGrant(issued)
          : null
      },
      release: async () => {
        if (released) return
        released = true
        issued.bindingPins = Math.max(0, issued.bindingPins - 1)
        if (issued.bindingPins === 0) this.stopPinnedRenewal(issued)
        if (issued.bindingPins !== 0 || !issued.revokePending) return
        this.issued.delete(issued.leaseId)
        if (parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl()) !== issued.gatewayOrigin) return
        await this.request(
          new URL(
            `/api/v1/artifact-preview-leases/${encodeURIComponent(issued.leaseId)}`,
            issued.gatewayOrigin,
          ),
          'DELETE',
          issued.scopeId,
          issued.authToken,
        )
      },
    }
  }

  private startPinnedRenewal(issued: IssuedArtifactPreview): void {
    if (issued.renewTimer) return
    issued.renewTimer = setInterval(() => {
      if (
        issued.bindingPins < 1
        || issued.renewInFlight
        || this.issued.get(issued.leaseId) !== issued
      ) return
      issued.renewInFlight = true
      void this.renew({
        version: 1,
        leaseId: issued.leaseId,
        scopeId: issued.scopeId,
        ...(issued.authToken ? { authToken: issued.authToken } : {}),
      }).then(async result => {
        if (!result.ok && (result.status === 404 || result.status === 410)) {
          await this.replacePinnedLease(issued)
        }
      }).finally(() => {
        issued.renewInFlight = false
      })
    }, PINNED_LEASE_RENEW_INTERVAL_MS)
    issued.renewTimer.unref?.()
  }

  private surfaceGrant(issued: IssuedArtifactPreview): ArtifactPreviewSurfaceGrant {
    return {
      launchUrl: issued.launchUrl,
      expectedOrigin: issued.expectedOrigin,
      scopeId: issued.scopeId,
      mode: issued.mode,
    }
  }

  private async replacePinnedLease(issued: IssuedArtifactPreview): Promise<boolean> {
    if (issued.replacementInFlight) return await issued.replacementInFlight
    if (issued.replacementAttempted || issued.bindingPins < 1) return false
    issued.replacementAttempted = true
    const replacement = this.replacePinnedLeaseNow(issued)
    issued.replacementInFlight = replacement
    try {
      return await replacement
    } finally {
      issued.replacementInFlight = null
    }
  }

  private async replacePinnedLeaseNow(issued: IssuedArtifactPreview): Promise<boolean> {
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (!gatewayOrigin || gatewayOrigin !== issued.gatewayOrigin) return false
    const response = await this.requestJson(
      new URL(
        `/api/v1/artifacts/${encodeURIComponent(issued.artifactId)}/preview-leases`,
        issued.gatewayOrigin,
      ),
      'POST',
      issued.scopeId,
      issued.authToken,
      JSON.stringify({ version: 1, mode: issued.mode, client: 'desktop' }),
    )
    if (!response.ok || response.status !== 201) return false
    let parsed: ReturnType<typeof parseLeasePayload>
    try {
      parsed = parseLeasePayload(response.payload, issued.mode)
    } catch {
      return false
    }
    const replacementId = parsed.payload.lease_id
    const replacementCurrent = (
      issued.bindingPins > 0
      && parsed.expiresAtMs > this.now()
      && parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl()) === issued.gatewayOrigin
      && (!this.issued.has(replacementId) || this.issued.get(replacementId) === issued)
    )
    if (!replacementCurrent) {
      await this.request(
        new URL(
          `/api/v1/artifact-preview-leases/${encodeURIComponent(replacementId)}`,
          issued.gatewayOrigin,
        ),
        'DELETE',
        issued.scopeId,
        issued.authToken,
      )
      return false
    }
    const retiredLeaseId = issued.leaseId
    if (this.issued.get(retiredLeaseId) === issued) this.issued.delete(retiredLeaseId)
    issued.retiredLeaseIds.add(retiredLeaseId)
    issued.leaseId = replacementId
    issued.launchUrl = parsed.payload.launch_url
    issued.expectedOrigin = parsed.payload.preview_origin
    issued.mode = parsed.payload.effective_mode
    issued.expiresAtMs = parsed.expiresAtMs
    this.issued.set(replacementId, issued)
    this.startPinnedRenewal(issued)
    return true
  }

  private stopPinnedRenewal(issued: IssuedArtifactPreview): void {
    if (!issued.renewTimer) return
    clearInterval(issued.renewTimer)
    issued.renewTimer = null
  }

  private issuedLeaseForSurface(
    grant: ArtifactPreviewSurfaceGrant,
  ): [string, IssuedArtifactPreview] | null {
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (!gatewayOrigin) return null
    for (const entry of this.issued) {
      const issued = entry[1]
      if (
        issued.gatewayOrigin === gatewayOrigin
        && issued.launchUrl === grant.launchUrl
        && issued.expectedOrigin === grant.expectedOrigin
        && issued.scopeId === grant.scopeId
        && issued.mode === grant.mode
        && issued.expiresAtMs > this.now()
      ) return entry
    }
    return null
  }

  authorizesSurface(grant: ArtifactPreviewSurfaceGrant): boolean {
    return this.resolveSurfaceArtifactId(grant) !== null
  }

  /**
   * Resolve the immutable artifact identity attached to an exact lease grant.
   * The renderer never supplies this value to the surface contract: Desktop
   * derives it only from the authenticated lease issuance it performed.
   */
  resolveSurfaceArtifactId(grant: ArtifactPreviewSurfaceGrant): string | null {
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (!gatewayOrigin) return null
    for (const [leaseId, issued] of this.issued) {
      if (
        issued.expiresAtMs <= this.now()
        || issued.gatewayOrigin !== gatewayOrigin
      ) {
        this.stopPinnedRenewal(issued)
        this.issued.delete(leaseId)
        continue
      }
      if (
        issued.launchUrl === grant.launchUrl
        && issued.expectedOrigin === grant.expectedOrigin
        && issued.scopeId === grant.scopeId
        && issued.mode === grant.mode
      ) return issued.artifactId
    }
    return null
  }

  private currentIssuedLease(
    leaseId: string,
    scopeId: string,
  ): IssuedArtifactPreview | null {
    const issued = this.issued.get(leaseId)
    if (!issued) return null
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (
      !gatewayOrigin
      || gatewayOrigin !== issued.gatewayOrigin
      || issued.scopeId !== scopeId
      || issued.expiresAtMs <= this.now()
    ) {
      this.stopPinnedRenewal(issued)
      this.issued.delete(leaseId)
      return null
    }
    return issued
  }

  private retiredIssuedLease(
    leaseId: string,
    scopeId: string,
  ): IssuedArtifactPreview | null {
    const gatewayOrigin = parseOwnedGatewayOrigin(this.options.getOwnedGatewayUrl())
    if (!gatewayOrigin) return null
    for (const issued of new Set(this.issued.values())) {
      if (
        issued.gatewayOrigin === gatewayOrigin
        && issued.scopeId === scopeId
        && issued.retiredLeaseIds.has(leaseId)
        && issued.bindingPins > 0
      ) return issued
    }
    return null
  }

  private async requestJson(
    url: URL,
    method: 'POST',
    scopeId: string,
    authToken?: string,
    body?: string,
  ): Promise<ArtifactPreviewLeaseBrokerResult<unknown>> {
    const response = await this.request(url, method, scopeId, authToken, body)
    if (!response.ok) return response
    try {
      const contentType = response.response.headers.get('content-type') || ''
      if (!/^application\/json(?:\s*;|$)/i.test(contentType)) {
        return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
      }
      const contentLength = Number(response.response.headers.get('content-length') || '0')
      if (Number.isFinite(contentLength) && contentLength > MAX_RESPONSE_BYTES) {
        return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
      }
      const text = await response.response.text()
      if (Buffer.byteLength(text, 'utf8') > MAX_RESPONSE_BYTES) {
        return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
      }
      return {
        ok: true,
        status: response.status,
        payload: JSON.parse(text) as unknown,
      }
    } catch {
      return failure(502, 'INVALID_RESPONSE', 'The Gateway returned an invalid preview response.')
    }
  }

  private async request(
    url: URL,
    method: 'POST' | 'DELETE',
    scopeId: string,
    authToken?: string,
    body?: string,
  ): Promise<
    | ArtifactPreviewLeaseBrokerFailure
    | { ok: true; status: number; response: Response }
  > {
    let response: Response
    try {
      response = await this.fetchImpl(url, {
        method,
        headers: {
          'x-opensquilla-session-key': scopeId,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
          ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        },
        ...(body === undefined ? {} : { body }),
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        signal: AbortSignal.timeout(this.timeoutMs),
      })
    } catch {
      return failure(
        503,
        'PREVIEW_BROKER_UNAVAILABLE',
        'The Desktop preview service is unavailable.',
      )
    }
    if (!response.ok) {
      let payload: Record<string, unknown> | null = null
      try {
        const text = await response.text()
        if (Buffer.byteLength(text, 'utf8') <= MAX_RESPONSE_BYTES) {
          payload = objectRecord(JSON.parse(text))
        }
      } catch {}
      const code = typeof payload?.code === 'string'
        ? safeErrorText(payload.code, '')
        : ''
      const message = safeErrorText(
        payload?.detail ?? payload?.message ?? payload?.error,
        `Artifact preview request failed (${response.status}).`,
      )
      return failure(response.status, code, message)
    }
    return { ok: true, status: response.status, response }
  }
}
