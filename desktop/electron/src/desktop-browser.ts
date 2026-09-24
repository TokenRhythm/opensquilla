import { randomBytes, timingSafeEqual } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { DesktopBrowserMcp } from './desktop-browser-mcp.js'

export const DESKTOP_BROWSER_URL_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_URL'
export const DESKTOP_BROWSER_TOKEN_ENV = 'OPENSQUILLA_DESKTOP_BROWSER_TOKEN'
const MAX_REQUEST_BYTES = 64 * 1024
// The authenticated MCP channel can carry bounded, Gateway-resolved attachments.
const MAX_MCP_REQUEST_BYTES = 16 * 1024 * 1024
const MAX_RESPONSE_BYTES = 12 * 1024 * 1024
const MAX_TIMEOUT_MS = 60_000

export type DesktopBrowserOperation = 'list' | 'open' | 'snapshot' | 'act' | 'screenshot' | 'reload'
  | 'observe' | 'batch' | 'dialog' | 'tab'
export interface DesktopBrowserAction {
  action?: 'click' | 'fill' | 'press' | 'scroll' | 'hover' | 'select' | 'hold' | 'drag'
    | 'upload' | 'cancelUpload' | 'download'
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

export function parseDesktopBrowserRequest(value: unknown): DesktopBrowserRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Expected a browser request.', 400)
  }
  const body = value as Record<string, unknown>
  const operation = body.operation as DesktopBrowserOperation
  if (!['list', 'open', 'snapshot', 'act', 'screenshot', 'reload', 'observe', 'batch', 'dialog', 'tab'].includes(operation)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Unknown browser operation.', 400)
  }
  const allowed = ['sessionKey', 'operation', ...(operation === 'list' ? [] : ['targetRef']),
    ...(operation === 'open' ? ['url', 'contextTargetRef'] : []),
    ...(operation === 'act' ? ['action', 'ref', 'text', 'key', 'direction', 'amount',
      'button', 'durationMs', 'endRef', 'fileId', 'chooserId'] : []),
    ...(operation === 'snapshot' ? ['ref', 'maxChars', 'downloadId'] : []),
    ...(operation === 'observe' ? ['observationMode'] : []),
    ...(operation === 'batch' ? ['actions', 'observationMode'] : []),
    ...(operation === 'dialog' ? ['dialogId', 'accept', 'promptText', 'observationMode'] : []),
    ...(operation === 'tab' ? ['tabAction'] : [])]
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
  if (body.contextTargetRef !== undefined) {
    if (body.targetRef !== undefined) throw new DesktopBrowserError('INVALID_REQUEST', 'A related tab cannot also navigate an existing target.', 400)
    request.contextTargetRef = boundedString(body.contextTargetRef, 128, 'context target reference')
  }
  if (operation === 'snapshot') {
    if (body.ref !== undefined && body.downloadId !== undefined) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Read an element or a downloaded artifact, not both.', 400)
    }
    if (body.ref !== undefined) request.ref = boundedString(body.ref, 128, 'element reference')
    if (body.downloadId !== undefined) request.downloadId = boundedString(body.downloadId, 128, 'download identity')
    if (body.maxChars !== undefined) {
      if (typeof body.maxChars !== 'number' || !Number.isInteger(body.maxChars) || body.maxChars < 1 || body.maxChars > 65536) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'maxChars must be an integer from 1 to 65536.', 400)
      }
      if (!request.ref && !request.downloadId) throw new DesktopBrowserError('INVALID_REQUEST', 'maxChars requires an element or download identity.', 400)
      request.maxChars = body.maxChars
    }
  }
  if (body.observationMode !== undefined) {
    if (typeof body.observationMode !== 'string' || !['auto', 'dom'].includes(body.observationMode)) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid observation mode.', 400)
    }
    request.observationMode = body.observationMode as DesktopBrowserRequest['observationMode']
  }
  if (operation === 'batch') {
    if (!Array.isArray(body.actions) || body.actions.length < 1 || body.actions.length > 3) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'A batch requires one to three actions.', 400)
    }
    request.actions = body.actions.map(value => parseBrowserAction(value, request.sessionKey, request.targetRef!))
    // A changed page needs a new observation before the next action. Only
    // known form fields can share one batch; submitting ends it.
    if (request.actions.slice(0, -1).some(action => action.action !== 'fill' && action.action !== 'select')) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Only form fills or selections may precede the final action.', 400)
    }
  }
  if (operation === 'dialog') {
    request.dialogId = boundedString(body.dialogId, 128, 'dialog identity')
    if (typeof body.accept !== 'boolean') throw new DesktopBrowserError('INVALID_REQUEST', 'Specify whether to accept the dialog.', 400)
    request.accept = body.accept
    if (body.promptText !== undefined) {
      if (typeof body.promptText !== 'string' || body.promptText.length > 16384 || body.promptText.includes('\0')) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid prompt text.', 400)
      }
      request.promptText = body.promptText
    }
  }
  if (operation === 'tab') {
    if (body.tabAction !== 'switch' && body.tabAction !== 'close') {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid tab action.', 400)
    }
    request.tabAction = body.tabAction
  }
  if (operation === 'act') {
    if (!['click', 'fill', 'press', 'scroll', 'hover', 'select', 'hold', 'drag', 'upload', 'cancelUpload', 'download'].includes(body.action as string)) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Unknown browser action.', 400)
    }
    request.action = body.action as DesktopBrowserRequest['action']
    if (['click', 'fill', 'hover', 'select', 'hold', 'drag', 'download'].includes(request.action!)) {
      request.ref = boundedString(body.ref, 128, 'element reference')
    } else if (body.ref !== undefined) {
      request.ref = boundedString(body.ref, 128, 'element reference')
    }
    if (body.button !== undefined) {
      if (!['click', 'hold', 'drag'].includes(request.action!) || typeof body.button !== 'string'
        || !['left', 'right', 'middle'].includes(body.button)) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'button is only valid for a click, hold or drag.', 400)
      }
      request.button = body.button as DesktopBrowserAction['button']
    }
    if (request.action === 'hold' || body.durationMs !== undefined) {
      if (request.action !== 'hold' || typeof body.durationMs !== 'number' || !Number.isInteger(body.durationMs)
        || body.durationMs < 1 || body.durationMs > 10000) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'A hold requires durationMs from 1 to 10000.', 400)
      }
      request.durationMs = body.durationMs
    }
    if (request.action === 'drag') request.endRef = boundedString(body.endRef, 128, 'drag destination reference')
    else if (body.endRef !== undefined) throw new DesktopBrowserError('INVALID_REQUEST', 'endRef requires a drag.', 400)
    if (request.action === 'upload') {
      request.fileId = boundedString(body.fileId, 512, 'attachment identity')
      if ((body.ref === undefined) === (body.chooserId === undefined)) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Upload requires either an input ref or a pending chooser identity.', 400)
      }
      if (body.chooserId !== undefined) request.chooserId = boundedString(body.chooserId, 128, 'file chooser identity')
    } else if (request.action === 'cancelUpload') {
      if (body.ref !== undefined) throw new DesktopBrowserError('INVALID_REQUEST', 'Cancel the specific file chooser by identity.', 400)
      request.chooserId = boundedString(body.chooserId, 128, 'file chooser identity')
    }
    if (request.action !== 'upload' && body.fileId !== undefined
      || !['upload', 'cancelUpload'].includes(request.action!) && body.chooserId !== undefined) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'File fields require the corresponding file action.', 400)
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
      if (typeof (body.amount ?? 600) !== 'number' || !Number.isInteger(request.amount)
        || request.amount < 1 || request.amount > 10_000) {
        throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid scroll amount.', 400)
      }
    }
  }
  return request
}

function parseBrowserAction(value: unknown, sessionKey: string, targetRef: string): DesktopBrowserAction {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid batch action.', 400)
  }
  const action = value as Record<string, unknown>
  const fields = ['action', 'ref', 'text', 'key', 'direction', 'amount', 'observationId', 'imageId', 'x', 'y',
    'button', 'durationMs', 'endRef', 'toX', 'toY']
  if (Object.keys(action).some(key => !fields.includes(key))) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Unexpected batch action field.', 400)
  }
  if (['upload', 'cancelUpload', 'download'].includes(String(action.action))) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'File actions must run individually.', 400)
  }
  const coordinate = ['x', 'y', 'imageId', 'observationId'].some(key => action[key] !== undefined)
  if (!coordinate) {
    const { sessionKey: _session, operation: _operation, targetRef: _target, ...parsed } =
      parseDesktopBrowserRequest({ ...action, sessionKey, targetRef, operation: 'act' })
    return parsed
  }
  if (action.ref !== undefined || action.endRef !== undefined || !['click', 'hover', 'scroll', 'hold', 'drag'].includes(String(action.action))) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Coordinates support click, hover, scroll, hold or drag without element refs.', 400)
  }
  if (action.action !== 'drag' && (action.toX !== undefined || action.toY !== undefined)) {
    throw new DesktopBrowserError('INVALID_REQUEST', 'Destination coordinates require a drag.', 400)
  }
  for (const key of action.action === 'drag' ? ['x', 'y', 'toX', 'toY'] : ['x', 'y']) {
    if (typeof action[key] !== 'number' || !Number.isFinite(action[key]) || action[key] < 0 || action[key] > 100000) {
      throw new DesktopBrowserError('INVALID_REQUEST', 'Invalid image coordinate.', 400)
    }
  }
  const { x, y, toX, toY, imageId, observationId, ...rest } = action
  const { sessionKey: _session, operation: _operation, targetRef: _target, ref: _ref, endRef: _endRef, ...parsed } =
    parseDesktopBrowserRequest({ ...rest, ...(['click', 'hover', 'hold', 'drag'].includes(String(action.action)) ? { ref: 'coordinate' } : {}),
      ...(action.action === 'drag' ? { endRef: 'coordinate-destination' } : {}),
      sessionKey, targetRef, operation: 'act' })
  return { ...parsed, x: x as number, y: y as number,
    ...(action.action === 'drag' ? { toX: toX as number, toY: toY as number } : {}),
    imageId: boundedString(imageId, 128, 'image identity'), observationId: boundedString(observationId, 128, 'observation identity') }
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
