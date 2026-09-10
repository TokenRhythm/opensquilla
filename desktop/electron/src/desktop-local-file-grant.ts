import { randomBytes } from 'node:crypto'
import { lstatSync, realpathSync, statSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'

export const DESKTOP_LOCAL_FILE_GRANT_PROTOCOL_VERSION = 1 as const
export const DESKTOP_LOCAL_FILE_GRANT_TTL_MS = 2 * 60 * 1000

export type DesktopLocalFileGrantFailureCode =
  | 'unavailable'
  | 'invalid-request'
  | 'file-unavailable'
  | 'expired'
  | 'instance-changed'
  | 'file-changed'

export interface DesktopLocalFileGrantGatewayState {
  owned: boolean
  status: 'starting' | 'ready' | 'stopped' | 'error'
  instanceId: string | null
}

export interface DesktopLocalFileGrantIssueRequest {
  path: string
  name?: string
  mime?: string
  size?: number
  subject: string
  executionEnvironment: string
  now?: number
}

export interface DesktopLocalFileGrantDescriptor {
  version: typeof DESKTOP_LOCAL_FILE_GRANT_PROTOCOL_VERSION
  grant: string
  name: string
  mime: string
  size: number
  expiresAt: number
}

export interface DesktopLocalFileGrantResolveContext {
  grant: string
  subject: string
  executionEnvironment: string
  now?: number
}

export type DesktopLocalFileGrantIssueResult =
  | { ok: true, value: DesktopLocalFileGrantDescriptor }
  | { ok: false, code: DesktopLocalFileGrantFailureCode, message: string }

interface FileIdentity {
  realPath: string
  dev: number
  ino: number
  size: number
  mtimeMs: number
}

interface GrantRecord {
  descriptor: DesktopLocalFileGrantDescriptor
  path: string
  identity: FileIdentity
  instanceId: string
  subject: string
  executionEnvironment: string
}

function validText(value: unknown, max = 256): value is string {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= max
    && !/[\u0000-\u001f\u007f]/.test(value)
}

function safeName(value: unknown, fallback: string): string {
  if (!validText(value, 512)) return fallback
  const name = value.split(/[/\\]/).pop()?.trim() ?? ''
  return name && name !== '.' && name !== '..' ? name : fallback
}

function identityFor(path: string): FileIdentity | null {
  try {
    const lstat = lstatSync(path)
    if (!lstat.isFile() || lstat.isSymbolicLink()) return null
    const stats = statSync(path)
    if (!stats.isFile()) return null
    return {
      realPath: realpathSync.native(path),
      dev: Number(stats.dev),
      ino: Number(stats.ino),
      size: stats.size,
      mtimeMs: stats.mtimeMs,
    }
  } catch {
    return null
  }
}

function sameIdentity(expected: FileIdentity, path: string): boolean {
  const current = identityFor(path)
  return current !== null
    && current.realPath === expected.realPath
    && current.dev === expected.dev
    && current.ino === expected.ino
    && current.size === expected.size
    && current.mtimeMs === expected.mtimeMs
}

/**
 * Main-process-only local file capability. The absolute path is retained in
 * this process and is never part of the renderer-facing descriptor. A future
 * Gateway consumer must call resolve() immediately before opening the file.
 */
export class DesktopLocalFileGrantManager {
  private readonly grants = new Map<string, GrantRecord>()

  constructor(
    private readonly getGatewayState: () => DesktopLocalFileGrantGatewayState,
    private readonly ttlMs = DESKTOP_LOCAL_FILE_GRANT_TTL_MS,
    private readonly consumerAvailable: () => boolean = () => false,
  ) {}

  capabilities(): { version: 1, available: boolean, reason?: string } {
    const state = this.getGatewayState()
    if (!state.owned || state.status !== 'ready' || !validText(state.instanceId, 256)) {
      return { version: 1, available: false, reason: 'owned-gateway-required' }
    }
    if (!this.consumerAvailable()) {
      return { version: 1, available: false, reason: 'gateway-consumer-unavailable' }
    }
    return { version: 1, available: true }
  }

  issue(request: DesktopLocalFileGrantIssueRequest): DesktopLocalFileGrantIssueResult {
    const state = this.getGatewayState()
    if (!state.owned || state.status !== 'ready' || !validText(state.instanceId, 256)) {
      return { ok: false, code: 'unavailable', message: 'Local file references require an owned Desktop Gateway.' }
    }
    if (!this.consumerAvailable()) {
      return { ok: false, code: 'unavailable', message: 'The Gateway cannot consume local file grants.' }
    }
    if (
      !isAbsolute(request.path)
      || !validText(request.subject, 256)
      || !validText(request.executionEnvironment, 256)
      || (request.size !== undefined && (!Number.isSafeInteger(request.size) || request.size < 1))
    ) {
      return { ok: false, code: 'invalid-request', message: 'The local file grant request is invalid.' }
    }
    const identity = identityFor(resolve(request.path))
    if (!identity) {
      return { ok: false, code: 'file-unavailable', message: 'The local file is unavailable.' }
    }
    if (request.size !== undefined && request.size !== identity.size) {
      return { ok: false, code: 'file-changed', message: 'The local file changed before authorization.' }
    }
    const now = Number.isFinite(request.now) ? Number(request.now) : Date.now()
    const grant = randomBytes(32).toString('base64url')
    const descriptor: DesktopLocalFileGrantDescriptor = {
      version: DESKTOP_LOCAL_FILE_GRANT_PROTOCOL_VERSION,
      grant,
      name: safeName(request.name, 'attachment'),
      mime: validText(request.mime, 256) ? request.mime : 'application/octet-stream',
      size: identity.size,
      expiresAt: now + Math.max(1_000, this.ttlMs),
    }
    this.grants.set(grant, {
      descriptor,
      path: identity.realPath,
      identity,
      instanceId: state.instanceId!,
      subject: request.subject,
      executionEnvironment: request.executionEnvironment,
    })
    return { ok: true, value: descriptor }
  }

  resolve(
    request: DesktopLocalFileGrantResolveContext,
  ): { ok: true, path: string } | { ok: false, code: DesktopLocalFileGrantFailureCode, message: string } {
    const record = this.grants.get(request.grant)
    if (!record) return { ok: false, code: 'invalid-request', message: 'The local file grant is invalid.' }
    const now = Number.isFinite(request.now) ? Number(request.now) : Date.now()
    if (now >= record.descriptor.expiresAt) {
      this.grants.delete(request.grant)
      return { ok: false, code: 'expired', message: 'The local file grant has expired.' }
    }
    const state = this.getGatewayState()
    if (state.instanceId !== record.instanceId || !state.owned || state.status !== 'ready') {
      return { ok: false, code: 'instance-changed', message: 'The Desktop Gateway instance changed.' }
    }
    if (
      request.subject !== record.subject
      || request.executionEnvironment !== record.executionEnvironment
    ) return { ok: false, code: 'invalid-request', message: 'The local file grant context is invalid.' }
    if (!sameIdentity(record.identity, record.path)) {
      return { ok: false, code: 'file-changed', message: 'The local file changed after authorization.' }
    }
    return { ok: true, path: record.path }
  }

  revoke(grant: string): void {
    this.grants.delete(grant)
  }

  clear(): void {
    this.grants.clear()
  }
}
