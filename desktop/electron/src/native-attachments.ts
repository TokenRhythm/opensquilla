import { createHash, createHmac, randomUUID } from 'node:crypto'
import { constants, type BigIntStats } from 'node:fs'
import { lstat, open, realpath } from 'node:fs/promises'
import { basename, extname, isAbsolute } from 'node:path'

const MAX_FILE_BYTES = 30 * 1024 * 1024
const SELECTION_TTL_MS = 120_000
const MAX_SELECTIONS = 40
const SIGNATURE_CONTEXT = 'opensquilla-native-attachment-v1\n'

export interface NativeAttachmentContext {
  gatewayInstanceId: string; sessionKey: string; sessionId: string; sessionEpoch: number
}
export interface NativeAttachmentConnection {
  instanceId: string; profile: string; url: string; authToken: string; nonce: string
}
export interface NativeAttachmentSelection {
  token: string; name: string; mime: string; size: number; previewDataUrl?: string
}
interface FileIdentity {
  size: number; dev: string; ino: string; mtimeNs: string; ctimeNs: string; birthtimeNs: string
}
interface Selection extends NativeAttachmentSelection {
  senderId: number; context: NativeAttachmentContext; connection: NativeAttachmentConnection
  path: string; identity: FileIdentity; sha256: string; expiresAt: number; generation: number
}
export interface NativeAttachmentReceipt {
  name: string; mime: string; size: number; previewDataUrl?: string
  file_uuid?: string; expires_at?: number; ttl_seconds?: number
  workspaceFile?: Record<string, unknown>
}

function bounded(value: unknown, limit = 512): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= limit
    && value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value)
}
export function parseNativeAttachmentContext(value: unknown): NativeAttachmentContext {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid attachment context')
  const raw = value as Record<string, unknown>
  if (Object.keys(raw).some(key => !['gatewayInstanceId', 'sessionKey', 'sessionId', 'sessionEpoch'].includes(key))
    || !bounded(raw.gatewayInstanceId) || !bounded(raw.sessionKey) || !bounded(raw.sessionId)
    || !Number.isSafeInteger(raw.sessionEpoch) || (raw.sessionEpoch as number) < 0) {
    throw new Error('Invalid attachment context')
  }
  return { gatewayInstanceId: raw.gatewayInstanceId, sessionKey: raw.sessionKey,
    sessionId: raw.sessionId, sessionEpoch: raw.sessionEpoch as number }
}
function identity(info: BigIntStats): FileIdentity {
  return { size: Number(info.size), dev: String(info.dev), ino: String(info.ino),
    mtimeNs: String(info.mtimeNs), ctimeNs: String(info.ctimeNs), birthtimeNs: String(info.birthtimeNs) }
}
function sameIdentity(left: FileIdentity, right: FileIdentity): boolean {
  return Object.keys(left).every(key => left[key as keyof FileIdentity] === right[key as keyof FileIdentity])
}
function mimeFor(name: string, bytes: Buffer): string {
  const extension = extname(name).toLowerCase()
  const mapped: Record<string, string> = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
    '.pdf': 'application/pdf', '.txt': 'text/plain', '.md': 'text/markdown', '.markdown': 'text/markdown',
    '.html': 'text/html', '.htm': 'text/html', '.csv': 'text/csv', '.json': 'application/json',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.eml': 'message/rfc822', '.mbox': 'application/mbox', '.msg': 'application/vnd.ms-outlook',
  }
  if (mapped[extension]) return mapped[extension]!
  if (bytes.length <= 4_000_000 && !bytes.includes(0)) {
    try { new TextDecoder('utf-8', { fatal: true }).decode(bytes); return 'text/plain' } catch { /* binary */ }
  }
  return 'application/octet-stream'
}
function assertSize(size: number, mime?: string): void {
  const limit = mime?.startsWith('image/') ? 5 * 1024 * 1024
    : ['message/rfc822', 'application/mbox', 'application/vnd.ms-outlook'].includes(mime || '')
      ? 2_000_000 : MAX_FILE_BYTES
  if (!Number.isSafeInteger(size) || size <= 0 || size > limit) throw new Error('Attachment exceeds its file size limit or is empty')
}

/** Bounded, regular-file-only read; compare identity on the open handle and path. */
async function readSelectedFile(path: string, expected?: FileIdentity): Promise<{
  bytes: Buffer; identity: FileIdentity; sha256: string
}> {
  const before = await lstat(path, { bigint: true })
  if (!before.isFile()) throw new Error('Select a regular file')
  const snapshot = identity(before)
  assertSize(snapshot.size)
  if (expected && !sameIdentity(expected, snapshot)) throw new Error('Selected file changed; select it again')
  const file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0) | (constants.O_NONBLOCK || 0))
  try {
    const opened = await file.stat({ bigint: true })
    if (!opened.isFile() || !sameIdentity(snapshot, identity(opened))) throw new Error('Selected file identity changed')
    const bytes = Buffer.alloc(snapshot.size)
    let offset = 0
    while (offset < bytes.length) {
      const result = await file.read(bytes, offset, bytes.length - offset, offset)
      if (!result.bytesRead) throw new Error('Selected file changed while reading')
      offset += result.bytesRead
    }
    const extra = await file.read(Buffer.alloc(1), 0, 1, offset)
    const after = await file.stat({ bigint: true })
    const pathAfter = await lstat(path, { bigint: true })
    if (extra.bytesRead || !after.isFile() || !pathAfter.isFile()
      || !sameIdentity(snapshot, identity(after)) || !sameIdentity(snapshot, identity(pathAfter))) {
      throw new Error('Selected file changed while reading')
    }
    return { bytes, identity: snapshot, sha256: createHash('sha256').update(bytes).digest('hex') }
  } finally { await file.close() }
}

export class NativeAttachmentSelections {
  private readonly selections = new Map<string, Selection>()
  private readonly generations = new Map<number, number>()
  constructor(private readonly deps: {
    connection: (senderId: number) => NativeAttachmentConnection | null
    fetch?: typeof fetch
    now?: () => number
  }) {}
  private now(): number { return this.deps.now?.() ?? Date.now() }
  private generation(senderId: number): number { return this.generations.get(senderId) ?? 0 }
  cancel(senderId: number): void {
    this.generations.set(senderId, this.generation(senderId) + 1)
    for (const [token, value] of this.selections) if (value.senderId === senderId) this.selections.delete(token)
  }
  private current(senderId: number, context: NativeAttachmentContext): NativeAttachmentConnection {
    const connection = this.deps.connection(senderId)
    if (!connection || connection.instanceId !== context.gatewayInstanceId) throw new Error('Attachment Gateway changed or is unavailable')
    const base = new URL(connection.url)
    if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
      || base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
      throw new Error('Invalid owned attachment Gateway')
    }
    return connection
  }
  private assertCurrent(senderId: number, context: NativeAttachmentContext, connection: NativeAttachmentConnection, generation: number): void {
    const current = this.current(senderId, context)
    if (this.generation(senderId) !== generation || current.profile !== connection.profile
      || current.url !== connection.url || current.nonce !== connection.nonce || current.authToken !== connection.authToken) {
      throw new Error('Attachment selection expired; select the file again')
    }
  }
  async choose(senderId: number, request: unknown, picker: () => Promise<string[]>): Promise<NativeAttachmentSelection[]> {
    const context = parseNativeAttachmentContext(request)
    const connection = this.current(senderId, context)
    const generation = this.generation(senderId)
    const paths = await picker()
    this.assertCurrent(senderId, context, connection, generation)
    if (paths.length > 10) throw new Error('Select at most 10 attachments')
    const selected: NativeAttachmentSelection[] = []
    for (const path of paths) {
      selected.push(await this.select(senderId, context, path))
      this.assertCurrent(senderId, context, connection, generation)
    }
    return selected
  }
  /** Called only by trusted main picker or preload webUtils.getPathForFile(File). */
  async select(senderId: number, request: unknown, selectedPath: unknown): Promise<NativeAttachmentSelection> {
    const context = parseNativeAttachmentContext(request)
    const connection = this.current(senderId, context)
    const generation = this.generation(senderId)
    if (!bounded(selectedPath, 32768) || !isAbsolute(selectedPath)) throw new Error('Invalid selected file')
    // Refuse a final symlink. Parent aliases resolve once, and all later reads bind to this canonical target.
    const selectedInfo = await lstat(selectedPath, { bigint: true })
    if (!selectedInfo.isFile()) throw new Error('Select a regular file')
    const path = await realpath(selectedPath)
    this.assertCurrent(senderId, context, connection, generation)
    const read = await readSelectedFile(path, identity(selectedInfo))
    this.assertCurrent(senderId, context, connection, generation)
    const name = basename(path)
    const mime = mimeFor(name, read.bytes)
    assertSize(read.bytes.length, mime)
    for (const [token, value] of this.selections) if (value.expiresAt <= this.now()) this.selections.delete(token)
    if (this.selections.size >= MAX_SELECTIONS) throw new Error('Too many pending attachment selections')
    const token = randomUUID()
    const metadata = { token, name, mime, size: read.bytes.length,
      ...(mime.startsWith('image/') ? { previewDataUrl: `data:${mime};base64,${read.bytes.toString('base64')}` } : {}),
    }
    this.selections.set(token, { ...metadata, senderId, context, connection, path, identity: read.identity,
      sha256: read.sha256, expiresAt: this.now() + SELECTION_TTL_MS, generation })
    return metadata
  }
  async import(senderId: number, request: unknown, token: unknown): Promise<NativeAttachmentReceipt> {
    const context = parseNativeAttachmentContext(request)
    if (!bounded(token)) throw new Error('Invalid attachment selection')
    const selection = this.selections.get(token)
    if (!selection || selection.senderId !== senderId || selection.context.sessionKey !== context.sessionKey
      || selection.context.gatewayInstanceId !== context.gatewayInstanceId
      || selection.context.sessionId !== context.sessionId || selection.context.sessionEpoch !== context.sessionEpoch) throw new Error('Attachment was not selected for this session')
    this.selections.delete(token) // single use, including failed or concurrent imports
    if (selection.expiresAt <= this.now()) throw new Error('Attachment selection expired; select the file again')
    const check = () => this.assertCurrent(senderId, context, selection.connection, selection.generation)
    check()
    const read = await readSelectedFile(selection.path, selection.identity)
    check()
    if (read.sha256 !== selection.sha256) throw new Error('Selected file contents changed; select it again')
    const capability = Buffer.from(JSON.stringify({
      v: 1, id: token, instanceId: context.gatewayInstanceId, sessionKey: context.sessionKey, senderId,
      sessionId: context.sessionId, sessionEpoch: context.sessionEpoch,
      path: selection.path, name: selection.name, mime: selection.mime, size: selection.size,
      dev: selection.identity.dev, ino: selection.identity.ino, mtimeNs: selection.identity.mtimeNs,
      ctimeNs: selection.identity.ctimeNs, birthtimeNs: selection.identity.birthtimeNs, sha256: selection.sha256, expiresAt: selection.expiresAt,
    })).toString('base64url')
    const signature = createHmac('sha256', selection.connection.nonce).update(SIGNATURE_CONTEXT + capability).digest('hex')
    const headers = { Authorization: `Bearer ${selection.connection.authToken}`,
      'x-opensquilla-session-key': context.sessionKey }
    const send = this.deps.fetch ?? fetch
    let response = await send(new URL('/api/v1/files/native-import', selection.connection.url), {
      method: 'POST', headers: { ...headers, 'Content-Type': 'application/json',
        'x-opensquilla-native-signature': signature }, body: JSON.stringify({ selection: capability }),
      redirect: 'error', signal: AbortSignal.timeout(30_000),
    })
    check()
    let result = await readResponse(response)
    check()
    // Only an explicit capability response permits bytes fallback. In particular,
    // 401/403, missing files, changed workspaces and malformed replies never do.
    if (response.status === 501 && result.code === 'native_import_unsupported') {
      const form = new FormData()
      form.append('file', new Blob([new Uint8Array(read.bytes)], { type: selection.mime }), selection.name)
      form.append('mime', selection.mime)
      check()
      response = await send(new URL('/api/v1/files/upload', selection.connection.url), {
        method: 'POST', headers, body: form, redirect: 'error', signal: AbortSignal.timeout(30_000),
      })
      check()
      result = await readResponse(response)
      check()
    }
    if (!response.ok) throw new Error(typeof result.error === 'string' ? result.error : `Attachment import failed (${response.status})`)
    if (typeof result.file_uuid !== 'string' && (!result.workspaceFile || typeof result.workspaceFile !== 'object')) {
      throw new Error('Invalid attachment import receipt')
    }
    if ((result.size !== undefined && result.size !== selection.size)
      || (result.sha256 !== undefined && result.sha256 !== selection.sha256)) throw new Error('Attachment import integrity mismatch')
    return { ...result, name: selection.name, mime: selection.mime, size: selection.size,
      ...(selection.previewDataUrl ? { previewDataUrl: selection.previewDataUrl } : {}),
    } as NativeAttachmentReceipt
  }
}
async function readResponse(response: Response): Promise<Record<string, unknown>> {
  const reader = response.body?.getReader()
  if (!reader) throw new Error('Missing attachment import response')
  const chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > 64 * 1024) throw new Error('Attachment import response is too large')
      chunks.push(next.value)
    }
  } finally { await reader.cancel().catch(() => {}) }
  const result = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('Invalid attachment import response')
  return result
}
