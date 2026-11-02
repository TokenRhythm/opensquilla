import { randomBytes } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { request as httpsRequest, type RequestOptions } from 'node:https'
import type { AddressInfo } from 'node:net'
import { AccountError, equalToken, type TokenRhythmAccount } from './tokenrhythm-account.js'

export const ACCOUNT_BRIDGE_URL_ENV = 'OPENSQUILLA_DESKTOP_ACCOUNT_URL'
export const ACCOUNT_BRIDGE_TOKEN_ENV = 'OPENSQUILLA_DESKTOP_ACCOUNT_TOKEN'
const MAX_BODY = 64 * 1024 * 1024
const FORWARDED_HEADERS = new Set(['content-type', 'accept', 'x-opensquilla-session-id', 'x-opensquilla-task-id', 'x-opensquilla-project-id', 'x-opensquilla-install-id', 'x-opensquilla-execution-id', 'x-opensquilla-call-kind', 'x-opensquilla-client-version', 'x-opensquilla-turn-id', 'x-opensquilla-request-id', 'x-title', 'http-referer'])
export function allowedModelRoute(method: string, path: string): boolean {
  return method === 'GET' && (['/v1/models', '/api/models'].includes(path) || /^\/v1\/videos\/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(path))
    || method === 'POST' && ['/v1/chat/completions', '/v1/images/generations', '/v1/videos/generations'].includes(path)
}
function failure(res: ServerResponse, status: number, error: string): void {
  if (res.headersSent) { res.destroy(); return }
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  res.end(JSON.stringify({ error: { code: error, message: error, type: 'account_error' } }))
}

/** A fixed-upstream streaming proxy, separate from the renderer-facing Gateway. */
export class TokenRhythmModelProxy {
  private server: Server | null = null
  private controlToken = ''
  private url = ''
  private startPromise: Promise<NodeJS.ProcessEnv> | null = null
  private requests = new Set<() => void>()
  constructor(private readonly account: TokenRhythmAccount, private readonly upstreamRequest: typeof httpsRequest = httpsRequest) {}
  async start(): Promise<NodeJS.ProcessEnv> {
    if (this.server) return { [ACCOUNT_BRIDGE_URL_ENV]: this.url, [ACCOUNT_BRIDGE_TOKEN_ENV]: this.controlToken }
    if (this.startPromise) return this.startPromise
    this.startPromise = (async () => {
      this.controlToken = randomBytes(32).toString('base64url')
      const server = createServer((req, res) => { void this.handle(req, res).catch(error => failure(res, 503, error instanceof AccountError ? error.code : 'ACCOUNT_PROXY_UNAVAILABLE')) })
      server.headersTimeout = 15_000
      server.requestTimeout = 120_000
      await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve) })
      this.server = server
      this.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
      return { [ACCOUNT_BRIDGE_URL_ENV]: this.url, [ACCOUNT_BRIDGE_TOKEN_ENV]: this.controlToken }
    })().finally(() => { this.startPromise = null })
    return this.startPromise
  }
  invalidate(): void { for (const abort of this.requests) abort(); this.requests.clear() }
  async close(): Promise<void> {
    this.invalidate()
    const server = this.server
    this.server = null; this.controlToken = ''; this.url = ''
    if (server) { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())) }
  }
  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    res.setHeader('Cache-Control', 'no-store')
    // Browser origins cannot access this bridge, even if they discover its port.
    if (req.headers.origin || req.headers['sec-fetch-site']) { failure(res, 403, 'ACCOUNT_PROXY_FORBIDDEN'); return }
    const authorization = req.headers.authorization ?? ''
    const bearer = authorization.startsWith('Bearer ') ? authorization.slice(7) : ''
    if (req.method === 'GET' && req.url === '/state') {
      if (!equalToken(bearer, this.controlToken)) { failure(res, 401, 'ACCOUNT_PROXY_UNAUTHORIZED'); return }
      const state = await this.account.runtimeState()
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify(state)); return
    }
    const path = req.url ?? ''
    if (!allowedModelRoute(req.method ?? '', path)) { failure(res, 404, 'ACCOUNT_PROXY_ROUTE_FORBIDDEN'); return }
    const credential = await this.account.modelCredential(bearer)
    if (req.method === 'GET' && path.startsWith('/v1/videos/') && !this.account.ownsVideoJob(path.slice('/v1/videos/'.length))) { failure(res, 403, 'ACCOUNT_VIDEO_OWNERSHIP_REQUIRED'); return }
    const headers: Record<string, string> = { Authorization: `Bearer ${credential}` }
    for (const [name, value] of Object.entries(req.headers)) {
      if (FORWARDED_HEADERS.has(name) && typeof value === 'string') headers[name] = value
    }
    if (req.headers['content-length']) {
      const length = Number(req.headers['content-length'])
      if (!Number.isSafeInteger(length) || length < 0 || length > MAX_BODY) { failure(res, 413, 'ACCOUNT_PROXY_BODY_TOO_LARGE'); return }
      headers['content-length'] = String(length)
    }
    const options: RequestOptions = { method: req.method, headers }
    const upstream = this.upstreamRequest(new URL(path, this.account.origin), options, response => {
      const status = response.statusCode ?? 502
      if (status >= 300 && status < 400) { response.destroy(); failure(res, 502, 'ACCOUNT_PROXY_REDIRECT_FORBIDDEN'); return }
      res.statusCode = status
      for (const name of ['content-type', 'retry-after', 'x-request-id', 'x-trace-id']) {
        const value = response.headers[name]
        if (value) res.setHeader(name, value)
      }
      response.on('error', () => failure(res, 502, 'ACCOUNT_PROXY_UPSTREAM_INTERRUPTED'))
      if (req.method === 'POST' && path === '/v1/videos/generations' && status >= 200 && status < 300) {
        const chunks: Buffer[] = []
        let size = 0
        response.on('data', (chunk: Buffer) => { size += chunk.length; if (size > 1024 * 1024) { response.destroy(); failure(res, 502, 'ACCOUNT_VIDEO_RESPONSE_TOO_LARGE') } else chunks.push(chunk) })
        response.on('end', () => { void (async () => {
          const body = Buffer.concat(chunks)
          const result = JSON.parse(body.toString('utf8'))
          await this.account.recordVideoJob(result.id ?? result.data?.id, bearer)
          res.end(body)
        })().catch(() => failure(res, 502, 'ACCOUNT_VIDEO_OWNERSHIP_UNAVAILABLE')) })
      } else response.pipe(res)
      if (status === 401) void this.account.modelKeyRejected().catch(() => {})
    })
    const abort = () => { upstream.destroy(); res.destroy() }
    this.requests.add(abort)
    res.on('close', () => { this.requests.delete(abort); upstream.destroy() })
    req.on('aborted', abort)
    upstream.on('error', () => failure(res, 502, 'ACCOUNT_PROXY_UPSTREAM_UNAVAILABLE'))
    // This is an inactivity timeout; active streams are not cut off after a
    // fixed duration. No automatic replay of potentially billable requests.
    upstream.setTimeout(120_000, () => { upstream.destroy(); failure(res, 504, 'ACCOUNT_PROXY_UPSTREAM_TIMEOUT') })
    let received = 0
    req.on('data', chunk => {
      received += chunk.length
      if (received > MAX_BODY) { req.unpipe(upstream); upstream.destroy(); failure(res, 413, 'ACCOUNT_PROXY_BODY_TOO_LARGE') }
    })
    req.pipe(upstream)
  }
}
