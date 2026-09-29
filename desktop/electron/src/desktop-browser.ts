import { randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { DesktopBrowserMcp } from './desktop-browser-mcp.js'
import { BrowserArgumentValidationError, BROWSER_ARGUMENT_CONTRACT_VERSION, parseBrowserArguments,
  type BrowserArgumentIssue, type BrowserActionName, type BrowserOperationName } from './browser-action-contract.js'

export const DESKTOP_BROWSER_URL_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_URL'
export const DESKTOP_BROWSER_TOKEN_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_TOKEN'
const MAX_REQUEST_BYTES = 64 * 1024
// The authenticated MCP channel can carry bounded, Gateway-resolved attachments.
const MAX_MCP_REQUEST_BYTES = 16 * 1024 * 1024
const MAX_RESPONSE_BYTES = 12 * 1024 * 1024
const MAX_TIMEOUT_MS = 60_000

export type DesktopBrowserOperation = BrowserOperationName
export interface DesktopBrowserAction {
  action?: BrowserActionName
  ref?: string
  text?: string
  key?: string
  direction?: 'up' | 'down' | 'left' | 'right'
  amount?: number
  observationId?: string
  imageId?: string
  x?: number
  y?: number
  button?: 'left' | 'right' | 'middle'
  durationMs?: number
  endRef?: string
  toX?: number
  toY?: number
  fileId?: string
  chooserId?: string
}
/** Authenticated Gateway attachment bytes, never model-supplied file paths. */
export interface BrowserUploadFile {
  fileId: string
  name: string
  mimeType: string
  dataBase64: string
}
export interface DesktopBrowserRequest extends DesktopBrowserAction {
  sessionKey: string
  operation: DesktopBrowserOperation
  targetRef?: string
  url?: string
  actions?: DesktopBrowserAction[]
  observationMode?: 'auto' | 'dom' | 'hybrid'
  dialogId?: string
  accept?: boolean
  promptText?: string
  tabAction?: 'switch' | 'close'
  contextTargetRef?: string
  maxChars?: number
  downloadId?: string
  uploadFile?: BrowserUploadFile
}

export type DesktopBrowserObservationReason = 'observation_missing' | 'observation_mismatch'
  | 'document_changed' | 'generation_changed' | 'viewport_changed' | 'scroll_changed'
  | 'target_pixels_changed' | 'validation_unstable'

export interface DesktopBrowserFailureDetails {
  phase?: 'argument_validation'
  contractVersion?: typeof BROWSER_ARGUMENT_CONTRACT_VERSION
  issues?: BrowserArgumentIssue[]
  targetRef?: string
  operation?: DesktopBrowserOperation
  pageState?: string
  navigation?: { url?: string; code: string; errorCode?: number }
  outcome?: 'not_started' | 'unknown' | 'completed'
  retryable?: boolean
  recovery?: string
  observationReason?: DesktopBrowserObservationReason
  diagnostic?: 'element_obscured' | 'element_detached'
}

export class DesktopBrowserError extends Error {
  constructor(readonly code: string, message: string, readonly status = 409,
    readonly details: DesktopBrowserFailureDetails = {}) {
    super(message)
  }
}

function boundedString(value: unknown, max: number, label: string): string {
  if (typeof value !== 'string' || !value || value.length > max || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new DesktopBrowserError('INVALID_REQUEST', `Invalid ${label}.`, 400)
  }
  return value
}

export function browserArgumentFailure(error: BrowserArgumentValidationError): DesktopBrowserError {
  return new DesktopBrowserError('INVALID_REQUEST', error.message, 400, {
    phase: 'argument_validation', contractVersion: BROWSER_ARGUMENT_CONTRACT_VERSION,
    outcome: 'not_started', retryable: false, issues: error.issues,
  })
}

export function parseDesktopBrowserRequest(value: unknown): DesktopBrowserRequest {
  try {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return parseBrowserArguments(value) as DesktopBrowserRequest
    const { sessionKey, ...arguments_ } = value as Record<string, unknown>
    // Session identity belongs to the authenticated transport, not the public contract.
    const session = boundedString(sessionKey, 512, 'session key')
    return { sessionKey: session, ...parseBrowserArguments(arguments_) }
  } catch (error) {
    if (error instanceof BrowserArgumentValidationError) throw browserArgumentFailure(error)
    throw error
  }
}

export interface DesktopBrowserAudit {
  event: 'desktop_browser_request'
  operation: string
  outcome: 'allowed' | 'rejected'
  code: string
  durationMs: number
  operationId?: string
  targetRef?: string
  navigationCode?: string
}

// Audit only bounded opaque identities and error codes, never page URLs, input
// text, session identities, or a browser's raw error message.
function auditIdentifier(value: unknown): string | undefined {
  return typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) ? value : undefined
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined
}

/** Authenticated process-local transport; credentials are passed only to the owned Gateway. */
export class DesktopBrowserServer {
  private server: Server | null = null
  private credentials: NodeJS.ProcessEnv | null = null
  private starting: Promise<NodeJS.ProcessEnv> | null = null
  private epoch = 0
  private readonly requests = new Set<AbortController>()
  private readonly mcp: DesktopBrowserMcp | null

  constructor(
    private readonly execute: (request: DesktopBrowserRequest, signal: AbortSignal) => Promise<unknown>,
    private readonly audit?: (entry: DesktopBrowserAudit) => void,
    executeMcp?: (request: DesktopBrowserRequest, signal: AbortSignal) => Promise<unknown>,
  ) { this.mcp = executeMcp ? new DesktopBrowserMcp(executeMcp) : null }

  start(): Promise<NodeJS.ProcessEnv> {
    if (this.credentials) return Promise.resolve({ ...this.credentials })
    if (this.starting) return this.starting
    const epoch = this.epoch
    this.starting = this.listen(epoch).finally(() => {
      if (epoch === this.epoch) this.starting = null
    })
    return this.starting
  }

  private async listen(epoch: number): Promise<NodeJS.ProcessEnv> {
    const token = randomBytes(32).toString('base64url')
    const server = createServer((request, response) => {
      void this.handle(request, response, Buffer.from(token))
    })
    server.requestTimeout = MAX_TIMEOUT_MS + 5000
    server.headersTimeout = 10_000
    server.keepAliveTimeout = 1
    try {
      await new Promise<void>((resolve, reject) => {
        server.once('error', reject)
        server.listen(0, '127.0.0.1', () => { server.off('error', reject); resolve() })
      })
      if (epoch !== this.epoch) throw new Error('Desktop browser server closed during startup.')
      const address = server.address() as AddressInfo
      this.server = server
      server.unref()
      this.credentials = {
        [DESKTOP_BROWSER_URL_ENV]: `http://127.0.0.1:${address.port}/v1/browser`,
        [DESKTOP_BROWSER_TOKEN_ENV]: token,
      }
      return { ...this.credentials }
    } catch (error) {
      server.close()
      throw error
    }
  }

  async close(): Promise<void> {
    this.epoch += 1
    this.credentials = null
    this.starting = null
    for (const request of this.requests) request.abort()
    this.mcp?.clear()
    const server = this.server
    this.server = null
    if (server) {
      server.closeAllConnections()
      await new Promise<void>(resolve => server.close(() => resolve()))
    }
  }

  private async handle(request: IncomingMessage, response: ServerResponse, token: Buffer): Promise<void> {
    const started = Date.now()
    let operation = 'unknown'
    let outcome: DesktopBrowserAudit['outcome'] = 'rejected'
    let code = 'INVALID_REQUEST'
    const diagnostics: Pick<DesktopBrowserAudit, 'operationId' | 'targetRef' | 'navigationCode'> = {}
    const controller = new AbortController()
    this.requests.add(controller)
    let timeout: NodeJS.Timeout | undefined
    const cancel = () => { if (!response.writableFinished) controller.abort() }
    request.once('aborted', cancel)
    response.once('close', cancel)
    try {
      if (request.socket.remoteAddress !== '127.0.0.1'
        || request.headers.origin !== undefined || request.headers['sec-fetch-site'] !== undefined) {
        throw new DesktopBrowserError('FORBIDDEN', 'Browser-origin requests are not allowed.', 403)
      }
      const expectedHost = `127.0.0.1:${request.socket.localPort}`
      if (request.headers.host !== expectedHost) throw new DesktopBrowserError('FORBIDDEN', 'Invalid host.', 403)
      const supplied = Buffer.from((request.headers.authorization ?? '').replace(/^Bearer /, ''))
      if (!request.headers.authorization?.startsWith('Bearer ') || supplied.length !== token.length
        || !timingSafeEqual(supplied, token)) {
        throw new DesktopBrowserError('UNAUTHORIZED', 'Desktop browser authentication failed.', 401)
      }
      const isMcp = request.url === '/v1/browser/mcp' && this.mcp !== null
      if (isMcp && request.method === 'GET') {
        this.reply(response, 405, JSON.stringify({ error: 'Streaming is not supported.' }))
        return
      }
      if (request.method !== 'POST' || (request.url !== '/v1/browser' && !isMcp)) {
        throw new DesktopBrowserError('NOT_FOUND', 'Unknown browser endpoint.', 404)
      }
      if (request.headers['content-type']?.split(';')[0]?.trim() !== 'application/json') {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Use application/json.', 415)
      }
      const header = request.headers['x-opensquilla-deadline-at-ms']
      const deadline = header === undefined ? started + 30_000 : Number(header)
      if (!Number.isSafeInteger(deadline) || deadline <= started || deadline > started + MAX_TIMEOUT_MS) {
        throw new DesktopBrowserError('TIMEOUT', 'Invalid or expired browser deadline.', 504)
      }
      const aborted = new Promise<never>((_resolve, reject) => controller.signal.addEventListener('abort', () => {
        reject(new DesktopBrowserError('TIMEOUT', 'Desktop browser request ended.', 504))
      }, { once: true }))
      timeout = setTimeout(() => controller.abort(), deadline - started)
      timeout.unref()
      const work = (async () => {
        const requestLimit = isMcp ? MAX_MCP_REQUEST_BYTES : MAX_REQUEST_BYTES
        const declared = request.headers['content-length']
        if (declared !== undefined && (!/^\d+$/.test(declared) || Number(declared) > requestLimit)) {
          throw new DesktopBrowserError('INVALID_REQUEST', 'Browser request is too large.', 413)
        }
        let size = 0
        const chunks: Buffer[] = []
        for await (const chunk of request) {
          size += chunk.length
          if (size > requestLimit) throw new DesktopBrowserError('INVALID_REQUEST', 'Browser request is too large.', 413)
          chunks.push(Buffer.from(chunk))
        }
        let body: unknown
        try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch {
          throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid JSON.', 400)
        }
        if (isMcp) {
          operation = 'mcp'
          const message = record(body)
          const params = record(message?.params)
          const name = auditIdentifier(params?.name)
          if (message?.method === 'tools/call' && name?.startsWith('browser_')) operation = name
          diagnostics.operationId = auditIdentifier(record(params?._meta)?.operationId)
          return await this.mcp!.handle(body, controller.signal)
        }
        const parsed = parseDesktopBrowserRequest(body)
        if (!['list', 'open', 'snapshot', 'act', 'screenshot', 'reload'].includes(parsed.operation)
          || parsed.contextTargetRef !== undefined || parsed.downloadId !== undefined
          || parsed.operation === 'snapshot' && parsed.ref !== undefined
          || parsed.action && !['click', 'fill', 'press', 'scroll', 'hover', 'select'].includes(parsed.action)
          || parsed.button !== undefined) {
          throw new DesktopBrowserError('INVALID_REQUEST', 'This browser operation requires the MCP endpoint.', 400)
        }
        operation = parsed.operation
        if (controller.signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'Browser request ended.', 504)
        return await this.execute(parsed, controller.signal)
      })()
      let result: unknown
      try {
        result = await Promise.race([work, aborted])
      } catch (error) {
        if (!isMcp || !controller.signal.aborted) throw error
        // Native cancellation can return a retained target and a known
        // navigation outcome. Give it a short, bounded cleanup window rather
        // than discard that result at the same instant we signal cancellation.
        let settle: NodeJS.Timeout | undefined
        try {
          result = await Promise.race([work, new Promise<never>((_resolve, reject) => {
            settle = setTimeout(() => reject(error), 250)
          })])
        } finally { if (settle) clearTimeout(settle) }
      }
      if (isMcp && result === null) {
        this.reply(response, 202, '')
        outcome = 'allowed'
        code = 'OK'
        return
      }
      const serialized = JSON.stringify(result)
      if (Buffer.byteLength(serialized) > MAX_RESPONSE_BYTES) {
        throw new DesktopBrowserError('RESPONSE_TOO_LARGE', 'Browser result exceeds the response limit.')
      }
      this.reply(response, 200, serialized)
      const envelope = isMcp ? record(result) : undefined
      const tool = record(envelope?.result)
      const failure = record(tool?.structuredContent) ?? record(envelope?.error)
      if (envelope?.error || tool?.isError === true) {
        outcome = 'rejected'
        code = auditIdentifier(failure?.code) ?? auditIdentifier(record(failure?.data)?.code) ?? 'MCP_ERROR'
      } else {
        outcome = 'allowed'
        code = 'OK'
      }
      const details = record(tool?.structuredContent)
      diagnostics.operationId ??= auditIdentifier(details?.operationId)
      diagnostics.targetRef = auditIdentifier(details?.targetRef)
      diagnostics.navigationCode = auditIdentifier(record(details?.navigation)?.code)
    } catch (error) {
      const failure = error instanceof DesktopBrowserError ? error
        : new DesktopBrowserError('BROWSER_UNAVAILABLE', 'The requested browser operation could not complete.')
      code = failure.code
      diagnostics.targetRef = auditIdentifier(failure.details.targetRef)
      diagnostics.navigationCode = auditIdentifier(failure.details.navigation?.code)
      this.reply(response, failure.status, JSON.stringify({ ...failure.details, ok: false, code, message: failure.message }))
    } finally {
      if (timeout) clearTimeout(timeout)
      this.requests.delete(controller)
      request.off('aborted', cancel)
      response.off('close', cancel)
      try { this.audit?.({ event: 'desktop_browser_request', operation, outcome, code, ...diagnostics,
        durationMs: Date.now() - started }) } catch {}
    }
  }

  private reply(response: ServerResponse, status: number, serialized: string): void {
    if (response.destroyed || response.writableEnded) return
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', Connection: 'close' })
    response.end(serialized)
  }
}
