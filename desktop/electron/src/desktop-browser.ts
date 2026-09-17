import { randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'

export const DESKTOP_BROWSER_URL_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_URL'
export const DESKTOP_BROWSER_TOKEN_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_TOKEN'
const MAX_REQUEST_BYTES = 64 * 1024
const MAX_RESPONSE_BYTES = 12 * 1024 * 1024
const MAX_TIMEOUT_MS = 60_000

export type DesktopBrowserOperation = 'list' | 'open' | 'snapshot' | 'act' | 'screenshot' | 'reload'
export interface DesktopBrowserRequest {
  sessionKey: string
  operation: DesktopBrowserOperation
  targetRef?: string
  url?: string
  action?: 'click' | 'fill' | 'press' | 'scroll' | 'hover' | 'select'
  ref?: string
  text?: string
  key?: string
  direction?: 'up' | 'down' | 'left' | 'right'
  amount?: number
}

export class DesktopBrowserError extends Error {
  constructor(readonly code: string, message: string, readonly status = 409) {
    super(message)
  }
}

function boundedString(value: unknown, max: number, label: string): string {
  if (typeof value !== 'string' || !value || value.length > max || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new DesktopBrowserError('INVALID_REQUEST', `Invalid ${label}.`, 400)
  }
  return value
}

export function parseDesktopBrowserRequest(value: unknown): DesktopBrowserRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Expected a browser request.', 400)
  }
  const body = value as Record<string, unknown>
  const operation = body.operation as DesktopBrowserOperation
  if (!['list', 'open', 'snapshot', 'act', 'screenshot', 'reload'].includes(operation)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Unknown browser operation.', 400)
  }
  const allowed = ['sessionKey', 'operation', ...(operation === 'list' ? [] : ['targetRef']),
    ...(operation === 'open' ? ['url'] : []),
    ...(operation === 'act' ? ['action', 'ref', 'text', 'key', 'direction', 'amount'] : [])]
  if (Object.keys(body).some(key => !allowed.includes(key))) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Unexpected browser request field.', 400)
  }
  const request: DesktopBrowserRequest = {
    sessionKey: boundedString(body.sessionKey, 512, 'session key'), operation,
  }
  if (operation !== 'list' && (operation !== 'open' || body.targetRef !== undefined)) {
    request.targetRef = boundedString(body.targetRef, 128, 'target reference')
  }
  if (operation === 'open') request.url = boundedString(body.url, 8192, 'URL')
  if (operation === 'act') {
    if (!['click', 'fill', 'press', 'scroll', 'hover', 'select'].includes(body.action as string)) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Unknown browser action.', 400)
    }
    request.action = body.action as DesktopBrowserRequest['action']
    if (['click', 'fill', 'hover', 'select'].includes(request.action!)) {
      request.ref = boundedString(body.ref, 128, 'element reference')
    } else if (body.ref !== undefined) {
      request.ref = boundedString(body.ref, 128, 'element reference')
    }
    if (request.action === 'fill' || request.action === 'select') {
      if (typeof body.text !== 'string' || body.text.length > 16_384 || body.text.includes('\0')) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid input text.', 400)
      }
      request.text = body.text
    }
    if (request.action === 'press') request.key = boundedString(body.key, 40, 'key')
    if (request.action === 'scroll') {
      if (!['up', 'down', 'left', 'right'].includes(body.direction as string)) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid scroll direction.', 400)
      }
      request.direction = body.direction as DesktopBrowserRequest['direction']
      request.amount = body.amount === undefined ? 600 : Number(body.amount)
      if (typeof (body.amount ?? 600) !== 'number' || !Number.isFinite(request.amount)
        || request.amount < 1 || request.amount > 10_000) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid scroll amount.', 400)
      }
    }
  }
  return request
}

export interface DesktopBrowserAudit {
  event: 'desktop_browser_request'
  operation: string
  outcome: 'allowed' | 'rejected'
  code: string
  durationMs: number
}

/** Authenticated process-local transport; credentials are passed only to the owned Gateway. */
export class DesktopBrowserServer {
  private server: Server | null = null
  private credentials: NodeJS.ProcessEnv | null = null
  private starting: Promise<NodeJS.ProcessEnv> | null = null
  private epoch = 0
  private readonly requests = new Set<AbortController>()

  constructor(
    private readonly execute: (request: DesktopBrowserRequest, signal: AbortSignal) => Promise<unknown>,
    private readonly audit?: (entry: DesktopBrowserAudit) => void,
  ) {}

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
      if (request.method !== 'POST' || request.url !== '/v1/browser') {
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
        const declared = request.headers['content-length']
        if (declared !== undefined && (!/^\d+$/.test(declared) || Number(declared) > MAX_REQUEST_BYTES)) {
          throw new DesktopBrowserError('INVALID_REQUEST', 'Browser request is too large.', 413)
        }
        let size = 0
        const chunks: Buffer[] = []
        for await (const chunk of request) {
          size += chunk.length
          if (size > MAX_REQUEST_BYTES) throw new DesktopBrowserError('INVALID_REQUEST', 'Browser request is too large.', 413)
          chunks.push(Buffer.from(chunk))
        }
        let body: unknown
        try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch {
          throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid JSON.', 400)
        }
        const parsed = parseDesktopBrowserRequest(body)
        operation = parsed.operation
        if (controller.signal.aborted) throw new DesktopBrowserError('TIMEOUT', 'Browser request ended.', 504)
        return await this.execute(parsed, controller.signal)
      })()
      const result = await Promise.race([work, aborted])
      const serialized = JSON.stringify(result)
      if (Buffer.byteLength(serialized) > MAX_RESPONSE_BYTES) {
        throw new DesktopBrowserError('RESPONSE_TOO_LARGE', 'Browser result exceeds the response limit.')
      }
      this.reply(response, 200, serialized)
      outcome = 'allowed'
      code = 'OK'
    } catch (error) {
      const failure = error instanceof DesktopBrowserError ? error
        : new DesktopBrowserError('BROWSER_UNAVAILABLE', 'The requested browser operation could not complete.')
      code = failure.code
      this.reply(response, failure.status, JSON.stringify({ ok: false, code, message: failure.message }))
    } finally {
      if (timeout) clearTimeout(timeout)
      this.requests.delete(controller)
      request.off('aborted', cancel)
      response.off('close', cancel)
      try { this.audit?.({ event: 'desktop_browser_request', operation, outcome, code, durationMs: Date.now() - started }) } catch {}
    }
  }

  private reply(response: ServerResponse, status: number, serialized: string): void {
    if (response.destroyed || response.writableEnded) return
    response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', Connection: 'close' })
    response.end(serialized)
  }
}
