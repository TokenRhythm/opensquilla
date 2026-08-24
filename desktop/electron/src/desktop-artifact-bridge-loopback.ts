import { randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

import {
  DESKTOP_ARTIFACT_BRIDGE_METHODS,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
  DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS,
  type DesktopArtifactBridgeMethod,
} from './desktop-artifact-bridge-contract.js'
import {
  DesktopArtifactBridge,
  type DesktopArtifactBridgeResult,
} from './desktop-artifact-bridge.js'

export const DESKTOP_ARTIFACT_BRIDGE_URL_ENV = 'OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_URL'
export const DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV = 'OPENSQUILLA_DESKTOP_ARTIFACT_BRIDGE_TOKEN'

const LOOPBACK_HOST = '127.0.0.1'
const MAX_REQUEST_BYTES = 64 * 1024
const MAX_DEADLINE_MS = 60_000
const MIN_DEADLINE_MS = 25
const DEADLINE_HEADER = 'x-opensquilla-deadline-at-ms'

type AuditOutcome = 'allowed' | 'rejected' | 'failed'

export interface DesktopArtifactBridgeLoopbackAudit {
  event: 'desktop_artifact_bridge_transport'
  operation: 'capabilities' | 'bindingAcquire' | 'bindingRelease' | DesktopArtifactBridgeMethod | 'unknown'
  outcome: AuditOutcome
  code: string
  durationMs: number
}

export interface DesktopArtifactBridgeLoopbackOptions {
  audit?(entry: DesktopArtifactBridgeLoopbackAudit): void
  now?: () => number
}

export interface DesktopArtifactBridgeLoopbackEnvironment extends NodeJS.ProcessEnv {
  [DESKTOP_ARTIFACT_BRIDGE_URL_ENV]: string
  [DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV]: string
}

interface TransportErrorBody {
  ok: false
  code: string
  message: string
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}

function authorizationMatches(header: string | undefined, token: Buffer): boolean {
  if (!header?.startsWith('Bearer ')) return false
  const supplied = Buffer.from(header.slice('Bearer '.length), 'utf8')
  return supplied.length === token.length && timingSafeEqual(supplied, token)
}

function isLoopbackPeer(request: IncomingMessage): boolean {
  const address = request.socket.remoteAddress
  return address === LOOPBACK_HOST || address === `::ffff:${LOOPBACK_HOST}`
}

function contentTypeIsJson(request: IncomingMessage): boolean {
  const value = request.headers['content-type']
  if (typeof value !== 'string') return false
  return value.split(';', 1)[0]?.trim().toLowerCase() === 'application/json'
}

async function readJsonBody(request: IncomingMessage): Promise<unknown> {
  const declaredLength = request.headers['content-length']
  if (declaredLength !== undefined) {
    const length = Number(declaredLength)
    if (!Number.isSafeInteger(length) || length < 0) {
      throw new TransportRequestError(400, 'invalid-request', 'The request length is invalid.')
    }
    if (length > MAX_REQUEST_BYTES) {
      throw new TransportRequestError(413, 'request-too-large', 'The request is too large.')
    }
  }

  let size = 0
  const chunks: Buffer[] = []
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
    size += bytes.length
    if (size > MAX_REQUEST_BYTES) {
      throw new TransportRequestError(413, 'request-too-large', 'The request is too large.')
    }
    chunks.push(bytes)
  }
  if (size === 0) {
    throw new TransportRequestError(400, 'invalid-request', 'A JSON request body is required.')
  }
  try {
    return JSON.parse(Buffer.concat(chunks, size).toString('utf8')) as unknown
  } catch {
    throw new TransportRequestError(400, 'invalid-request', 'The JSON request body is invalid.')
  }
}

function writeJson(response: ServerResponse, status: number, payload: unknown): void {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  response.writeHead(status, {
    'cache-control': 'no-store',
    connection: 'close',
    'content-length': String(body.length),
    'content-type': 'application/json; charset=utf-8',
    'x-content-type-options': 'nosniff',
  })
  response.end(body)
}

async function writeJsonSettled(
  response: ServerResponse,
  status: number,
  payload: unknown,
): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      response.off('finish', onFinish)
      response.off('close', onClose)
      response.off('error', onError)
    }
    const onFinish = () => {
      cleanup()
      resolve()
    }
    const onClose = () => {
      cleanup()
      if (response.writableFinished) resolve()
      else reject(new Error('The Desktop binding response closed before delivery.'))
    }
    const onError = (error: Error) => {
      cleanup()
      reject(error)
    }
    response.once('finish', onFinish)
    response.once('close', onClose)
    response.once('error', onError)
    try {
      writeJson(response, status, payload)
    } catch (error) {
      cleanup()
      reject(error)
    }
  })
}

class TransportRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

function publicTransportError(error: unknown): TransportRequestError {
  if (error instanceof TransportRequestError) return error
  return new TransportRequestError(
    500,
    'transport-failed',
    'The Desktop artifact bridge request failed.',
  )
}

function bridgeMethod(value: unknown): DesktopArtifactBridgeMethod | null {
  return DESKTOP_ARTIFACT_BRIDGE_METHODS.includes(value as DesktopArtifactBridgeMethod)
    ? value as DesktopArtifactBridgeMethod
    : null
}

function wireBridgeResult<M extends DesktopArtifactBridgeMethod>(
  result: DesktopArtifactBridgeResult<M>,
): unknown {
  if (!result.ok || result.method !== 'screenshot') return result
  const value = result.value as {
    mime: 'image/png'
    data: Uint8Array
    width: number
    height: number
  }
  return {
    ok: true,
    method: result.method,
    value: {
      mime: value.mime,
      dataBase64: Buffer.from(value.data).toString('base64'),
      width: value.width,
      height: value.height,
    },
  }
}

/**
 * Main-process-only HTTP transport for the desktop-managed Gateway.
 *
 * It binds IPv4 loopback on an ephemeral port and authenticates every request
 * with one process-lifetime 256-bit bearer token. The endpoint and token are
 * returned solely for injection into the child Gateway environment; this
 * module has no renderer or IPC integration.
 */
export class DesktopArtifactBridgeLoopbackTransport {
  private server: Server | null = null
  private endpoint: string | null = null
  private tokenText: string | null = null
  private startPromise: Promise<DesktopArtifactBridgeLoopbackEnvironment> | null = null
  /** Invalidates a bind that is still racing with application shutdown. */
  private lifecycleEpoch = 0

  constructor(
    private readonly bridge: DesktopArtifactBridge,
    private readonly options: DesktopArtifactBridgeLoopbackOptions = {},
  ) {}

  start(): Promise<DesktopArtifactBridgeLoopbackEnvironment> {
    if (this.endpoint && this.tokenText) return Promise.resolve(this.environment())
    if (this.startPromise) return this.startPromise
    this.startPromise = this.startServer(this.lifecycleEpoch)
    return this.startPromise
  }

  environment(): DesktopArtifactBridgeLoopbackEnvironment {
    if (!this.endpoint || !this.tokenText) {
      throw new Error('The Desktop artifact bridge transport has not started.')
    }
    return {
      [DESKTOP_ARTIFACT_BRIDGE_URL_ENV]: this.endpoint,
      [DESKTOP_ARTIFACT_BRIDGE_TOKEN_ENV]: this.tokenText,
    }
  }

  /** Process-local credential for the Gateway's candidate materialization RPC. */
  token(): string | null {
    return this.tokenText
  }

  async close(): Promise<void> {
    this.lifecycleEpoch += 1
    const server = this.server
    this.server = null
    this.endpoint = null
    this.tokenText = null
    this.startPromise = null
    await this.bridge.releaseAllBindings()
    if (!server) return
    await new Promise<void>((resolve) => server.close(() => resolve()))
  }

  private async startServer(
    lifecycleEpoch: number,
  ): Promise<DesktopArtifactBridgeLoopbackEnvironment> {
    const tokenText = randomBytes(32).toString('base64url')
    const tokenBytes = Buffer.from(tokenText, 'utf8')
    const server = createServer((request, response) => {
      void this.handle(request, response, tokenBytes)
    })
    server.requestTimeout = MAX_DEADLINE_MS + 5_000
    server.headersTimeout = 10_000
    server.keepAliveTimeout = 1

    try {
      await new Promise<void>((resolve, reject) => {
        const onError = (error: Error) => {
          server.off('listening', onListening)
          reject(error)
        }
        const onListening = () => {
          server.off('error', onError)
          resolve()
        }
        server.once('error', onError)
        server.once('listening', onListening)
        server.listen(0, LOOPBACK_HOST)
      })
      if (lifecycleEpoch !== this.lifecycleEpoch) {
        await new Promise<void>(resolve => server.close(() => resolve()))
        throw new Error('The Desktop artifact bridge transport was closed while starting.')
      }
      const address = server.address() as AddressInfo | null
      if (!address || address.address !== LOOPBACK_HOST || address.family !== 'IPv4') {
        throw new Error('Desktop artifact bridge failed to bind IPv4 loopback.')
      }
      server.unref()
      this.server = server
      this.endpoint = `http://${LOOPBACK_HOST}:${address.port}`
      this.tokenText = tokenText
      return this.environment()
    } catch (error) {
      try { server.close() } catch {}
      if (lifecycleEpoch === this.lifecycleEpoch) this.startPromise = null
      throw error
    }
  }

  private async handle(
    request: IncomingMessage,
    response: ServerResponse,
    expectedToken: Buffer,
  ): Promise<void> {
    const startedAt = this.now()
    let operation: DesktopArtifactBridgeLoopbackAudit['operation'] = 'unknown'
    let outcome: AuditOutcome = 'rejected'
    let code = 'invalid-request'
    try {
      if (!isLoopbackPeer(request)) {
        throw new TransportRequestError(403, 'forbidden', 'The request is not allowed.')
      }
      if (!authorizationMatches(request.headers.authorization, expectedToken)) {
        throw new TransportRequestError(401, 'unauthorized', 'Desktop bridge authentication failed.')
      }
      if (request.method !== 'POST') {
        throw new TransportRequestError(405, 'method-not-allowed', 'Use POST for this endpoint.')
      }
      if (!contentTypeIsJson(request)) {
        throw new TransportRequestError(415, 'unsupported-media-type', 'Use application/json.')
      }
      const deadlineAt = this.parseDeadline(request)
      const body = await readJsonBody(request)

      if (request.url === '/v1/capabilities') {
        operation = 'capabilities'
        if (
          !isObjectRecord(body)
          || !exactKeys(body, ['version'])
          || !DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS.includes(
            body.version as (typeof DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS)[number],
          )
        ) {
          throw new TransportRequestError(400, 'invalid-request', 'The capabilities request is invalid.')
        }
        this.assertBeforeDeadline(deadlineAt)
        outcome = 'allowed'
        code = 'ok'
        writeJson(response, 200, {
          ok: true,
          value: this.bridge.getCapabilities(
            body.version as (typeof DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS)[number],
          ),
        })
        return
      }

      if (request.url === '/v1/bindings/acquire') {
        operation = 'bindingAcquire'
        if (!isObjectRecord(body) || !exactKeys(body, ['version']) || body.version !== DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION) {
          throw new TransportRequestError(400, 'invalid-request', 'The binding request is invalid.')
        }
        const binding = await this.bridge.acquireBinding()
        if (!binding) throw new TransportRequestError(409, 'unavailable', 'No editable Desktop artifact surface is available.')
        try {
          this.assertBeforeDeadline(deadlineAt)
          await writeJsonSettled(response, 201, {
            ok: true,
            value: {
              version: DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION,
              ...binding,
            },
          })
          outcome = 'allowed'
          code = 'ok'
        } catch (error) {
          await this.bridge.releaseBinding(binding.bindingToken)
          throw error
        }
        return
      }

      if (request.url === '/v1/bindings/release') {
        operation = 'bindingRelease'
        if (!isObjectRecord(body) || !exactKeys(body, ['version', 'bindingToken']) || body.version !== DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION || typeof body.bindingToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(body.bindingToken)) {
          throw new TransportRequestError(400, 'invalid-request', 'The binding release request is invalid.')
        }
        await this.bridge.releaseBinding(body.bindingToken)
        outcome = 'allowed'
        code = 'ok'
        writeJson(response, 200, { ok: true, value: { released: true } })
        return
      }

      if (request.url !== '/v1/invoke') {
        throw new TransportRequestError(404, 'not-found', 'The Desktop bridge endpoint was not found.')
      }
      const isV5 = isObjectRecord(body) && body.version === DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSION
      if (!isObjectRecord(body) || !exactKeys(body, isV5 ? ['version', 'method', 'request', 'bindingToken'] : ['version', 'method', 'request'])) {
        throw new TransportRequestError(400, 'invalid-request', 'The bridge invocation is invalid.')
      }
      // The envelope and typed request must negotiate the same protocol.  A
      // mismatched pair could otherwise smuggle a v4-only candidate method
      // through a v3 envelope during a rolling upgrade.
      if (!isObjectRecord(body.request) || body.request.version !== body.version) {
        throw new TransportRequestError(400, 'invalid-request', 'The bridge invocation is invalid.')
      }
      const method = bridgeMethod(body.method)
      if (
        !DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS.includes(
          body.version as (typeof DESKTOP_ARTIFACT_BRIDGE_PROTOCOL_VERSIONS)[number],
        )
        || method === null
      ) {
        throw new TransportRequestError(400, 'invalid-request', 'The bridge invocation is invalid.')
      }
      operation = method
      this.assertBeforeDeadline(deadlineAt)
      const invocationController = new AbortController()
      const result = await this.beforeDeadline(
        isV5
          ? this.invokeBoundBridge(method, body.request, body.bindingToken, invocationController.signal)
          : this.invokeBridge(method, body.request, invocationController.signal),
        deadlineAt,
        invocationController,
      )
      outcome = result.ok ? 'allowed' : 'failed'
      code = result.ok ? 'ok' : result.code
      writeJson(response, 200, wireBridgeResult(result))
    } catch (error) {
      const safe = publicTransportError(error)
      outcome = safe.status >= 500 ? 'failed' : 'rejected'
      code = safe.code
      const payload: TransportErrorBody = {
        ok: false,
        code: safe.code,
        message: safe.message,
      }
      if (!response.headersSent && !response.destroyed) writeJson(response, safe.status, payload)
    } finally {
      this.audit({
        event: 'desktop_artifact_bridge_transport',
        operation,
        outcome,
        code,
        durationMs: Math.max(0, this.now() - startedAt),
      })
    }
  }

  private parseDeadline(request: IncomingMessage): number {
    const value = request.headers[DEADLINE_HEADER]
    if (typeof value !== 'string' || !/^\d{13}$/.test(value)) {
      throw new TransportRequestError(400, 'invalid-deadline', 'A valid request deadline is required.')
    }
    const deadlineAt = Number(value)
    const remaining = deadlineAt - this.now()
    if (!Number.isSafeInteger(deadlineAt) || remaining < MIN_DEADLINE_MS) {
      throw new TransportRequestError(408, 'deadline-exceeded', 'The request deadline expired.')
    }
    if (remaining > MAX_DEADLINE_MS) {
      throw new TransportRequestError(400, 'invalid-deadline', 'The request deadline is too far away.')
    }
    return deadlineAt
  }

  private assertBeforeDeadline(deadlineAt: number): void {
    if (deadlineAt - this.now() < 1) {
      throw new TransportRequestError(408, 'deadline-exceeded', 'The request deadline expired.')
    }
  }

  private async beforeDeadline<T>(
    operation: Promise<T>,
    deadlineAt: number,
    controller: AbortController,
  ): Promise<T> {
    const remaining = deadlineAt - this.now()
    if (remaining < 1) {
      throw new TransportRequestError(408, 'deadline-exceeded', 'The request deadline expired.')
    }
    let timeout: NodeJS.Timeout | undefined
    try {
      return await Promise.race([
        operation,
        new Promise<never>((_resolve, reject) => {
          timeout = setTimeout(() => {
            controller.abort()
            reject(new TransportRequestError(408, 'deadline-exceeded', 'The request deadline expired.'))
          }, remaining)
          timeout.unref()
        }),
      ])
    } finally {
      if (timeout) clearTimeout(timeout)
    }
  }

  private invokeBridge<M extends DesktopArtifactBridgeMethod>(
    method: M,
    request: unknown,
    signal: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<M>> {
    return this.bridge[method](request, signal) as Promise<DesktopArtifactBridgeResult<M>>
  }

  private invokeBoundBridge<M extends DesktopArtifactBridgeMethod>(
    method: M,
    request: unknown,
    bindingToken: unknown,
    signal: AbortSignal,
  ): Promise<DesktopArtifactBridgeResult<M>> {
    if (typeof bindingToken !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(bindingToken)) {
      return Promise.resolve({ ok: false, method, code: 'invalid-request', message: 'The Desktop artifact binding token is invalid.' })
    }
    return this.bridge.invokeBound(method, request, bindingToken, signal)
  }

  private now(): number {
    return Math.floor((this.options.now ?? Date.now)())
  }

  private audit(entry: DesktopArtifactBridgeLoopbackAudit): void {
    try {
      this.options.audit?.(entry)
    } catch {
      // Observability must never change bridge authorization or execution.
    }
  }
}
