import { createHmac, randomUUID } from 'node:crypto'
import type { BigIntStats } from 'node:fs'
import { open, rename, unlink, lstat, realpath } from 'node:fs/promises'
import { basename, dirname, isAbsolute, join, relative, sep, extname } from 'node:path'

export interface SaveArtifactRequest { data: ArrayBuffer; name: string; mime: string }
export interface SourceFileActionRequest {
  gatewayInstanceId: string
  sessionKey: string
  documentId: string
  pagePath?: string
  action: 'open' | 'reveal'
}
export interface WorkspaceFileActionRequest {
  gatewayInstanceId: string
  sessionKey: string
  path: string
  workspaceBinding: string
  action: 'open' | 'reveal'
}
export interface SourceGatewayConnection {
  instanceId: string
  profile: string
  url: string
  authToken: string
  /** Process-ownership nonce used only for native workspace metadata requests. */
  nonce?: string
}

const NATIVE_WORKSPACE_METADATA_SIGNING_CONTEXT = 'opensquilla-native-workspace-file-v1\n'

function boundedString(value: unknown, limit = 512): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= limit
    && value === value.trim() && !/[\u0000-\u001f\u007f]/.test(value)
}

function htmlPage(value: unknown): value is string {
  return boundedString(value, 4096) && !/[\\:%?#]/.test(value)
    && value.split('/').every(part => part && part !== '.' && part !== '..')
    && /\.(html?|xhtml)$/i.test(value)
}

export async function saveArtifactFile(
  payload: SaveArtifactRequest,
  choosePath: (name: string) => Promise<string | null>,
  io = { open, rename, unlink },
): Promise<{ status: 'saved' | 'cancelled' }> {
  const raw: unknown = payload?.data
  if (!(raw instanceof ArrayBuffer) && !ArrayBuffer.isView(raw)) {
    throw new Error('Invalid file data')
  }
  const bytes = raw instanceof ArrayBuffer
    ? new Uint8Array(raw)
    : new Uint8Array(raw.buffer, raw.byteOffset, raw.byteLength)
  const name = basename(String(payload.name || 'file').replace(/\\/g, '/'))
    .replace(/[\u0000-\u001f\u007f<>:"|?*]/g, '_').replace(/^\.+/, '') || 'file'
  const destination = await choosePath(name)
  if (!destination) return { status: 'cancelled' }
  const temporary = join(dirname(destination), `.opensquilla-save-${randomUUID()}.part`)
  const file = await io.open(temporary, 'wx', 0o600)
  try {
    await file.writeFile(bytes)
    await file.sync()
    await file.close()
    await io.rename(temporary, destination)
  } catch (error) {
    await file.close().catch(() => {})
    await io.unlink(temporary).catch(() => {})
    throw error
  }
  return { status: 'saved' }
}

export async function performSourceFileAction(
  payload: SourceFileActionRequest,
  deps: {
    connection: () => SourceGatewayConnection | null
    fetch?: typeof fetch
    openPath: (path: string) => Promise<string>
    reveal: (path: string) => void
  },
): Promise<void> {
  if (!payload || Object.keys(payload).some(key => ![
    'gatewayInstanceId', 'sessionKey', 'documentId', 'pagePath', 'action',
  ].includes(key)) || !boundedString(payload.gatewayInstanceId)
    || !boundedString(payload.sessionKey) || !/^doc_[\w-]+$/.test(payload.documentId)
    || !['open', 'reveal'].includes(payload.action)
    || (payload.pagePath !== undefined && !htmlPage(payload.pagePath))) {
    throw new Error('Invalid source file request')
  }
  const connection = deps.connection()
  if (!connection || connection.instanceId !== payload.gatewayInstanceId) {
    throw new Error('Local source file access is unavailable')
  }
  const assertCurrent = () => {
    const current = deps.connection()
    if (!current || current.instanceId !== connection.instanceId
      || current.profile !== connection.profile || current.url !== connection.url) {
      throw new Error('Gateway changed; reopen the file menu')
    }
  }
  const base = new URL(connection.url)
  if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
    || base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
    throw new Error('Invalid owned Gateway')
  }
  const url = new URL(`/api/v1/artifact-documents/${encodeURIComponent(payload.documentId)}/working-file`, base)
  url.searchParams.set('format', 'metadata')
  if (payload.pagePath !== undefined) url.searchParams.set('pagePath', payload.pagePath)
  const response = await (deps.fetch ?? fetch)(url, {
    headers: { Authorization: `Bearer ${connection.authToken}`, 'x-opensquilla-session-key': payload.sessionKey },
    redirect: 'error', signal: AbortSignal.timeout(15_000),
  })
  if (!response.ok) throw new Error(`Source file unavailable (${response.status})`)
  const reader = response.body?.getReader()
  if (!reader) throw new Error('Missing source file metadata')
  let text = ''
  const decoder = new TextDecoder()
  try {
    let size = 0
    for (;;) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > 64 * 1024) throw new Error('Source metadata is too large')
      text += decoder.decode(next.value, { stream: true })
    }
    text += decoder.decode()
  } finally { await reader.cancel().catch(() => {}) }
  assertCurrent()
  const metadata = JSON.parse(text)
  if (metadata?.documentId !== payload.documentId || !htmlPage(metadata.pagePath)
    || (payload.pagePath !== undefined && metadata.pagePath !== payload.pagePath)
    || !boundedString(metadata.sourcePath, 32768) || !boundedString(metadata.workspace, 32768)
    || !isAbsolute(metadata.sourcePath) || !isAbsolute(metadata.workspace)
    || !['text/html', 'application/xhtml+xml'].includes(metadata.mime)
    || !['.html', '.htm', '.xhtml'].includes(extname(metadata.sourcePath).toLowerCase())) {
    throw new Error('Invalid source file metadata')
  }
  const workspace = await realpath(metadata.workspace)
  const source = await realpath(metadata.sourcePath)
  const location = relative(workspace, source)
  if (workspace !== metadata.workspace || source !== metadata.sourcePath || !location
    || isAbsolute(location) || location === '..' || location.startsWith(`..${sep}`)
    || !(await lstat(source)).isFile()) throw new Error('Source file identity changed')
  assertCurrent()
  if (payload.action === 'reveal') deps.reveal(source)
  else {
    const error = await deps.openPath(source)
    if (error) throw new Error(error)
  }
}

function workspaceRelativePath(value: unknown): value is string {
  return boundedString(value, 4096) && !/[\\:\u0000-\u001f\u007f]/.test(value)
    && !isAbsolute(value) && value.split('/').every(part => part && part !== '.' && part !== '..')
}

export function workspaceFileIdentityMatches(
  value: unknown,
  info: Pick<BigIntStats, 'dev' | 'ino' | 'size' | 'mtimeNs' | 'ctimeNs'>,
  platform = process.platform,
): boolean {
  if (!value || typeof value !== 'object') return false
  const identity = value as Record<string, unknown>
  const properties = ['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs'] as const
  if (properties.some(key => typeof identity[key] !== 'string' || !/^\d+$/.test(identity[key] as string))) return false
  // CPython still reports creation time as Windows lstat ctime; libuv reports
  // change time. dev is normalized to libuv's low DWORD by the Gateway.
  return properties.every(key => (platform === 'win32' && key === 'ctimeNs')
    || info[key].toString() === identity[key])
}

/** Open or reveal a file that still belongs to the currently owned workspace. */
export async function performWorkspaceFileAction(
  payload: WorkspaceFileActionRequest,
  deps: {
    connection: () => SourceGatewayConnection | null
    fetch?: typeof fetch
    openPath: (path: string) => Promise<string>
    reveal: (path: string) => void
  },
): Promise<{ ok: boolean; message?: string }> {
  if (!payload || Object.keys(payload).some(key => ![
    'gatewayInstanceId', 'sessionKey', 'path', 'workspaceBinding', 'action',
  ].includes(key)) || !boundedString(payload.gatewayInstanceId)
    || !boundedString(payload.sessionKey) || !workspaceRelativePath(payload.path)
    || !boundedString(payload.workspaceBinding) || !['open', 'reveal'].includes(payload.action)) {
    throw new Error('Invalid workspace file request')
  }
  const connection = deps.connection()
  if (!connection || connection.instanceId !== payload.gatewayInstanceId
    || !boundedString(connection.nonce, 256)) {
    throw new Error('Local source file access is unavailable')
  }
  const assertCurrent = () => {
    const current = deps.connection()
    if (!current || current.instanceId !== connection.instanceId
      || current.profile !== connection.profile || current.url !== connection.url
      || current.nonce !== connection.nonce) {
      throw new Error('Gateway changed; reopen the file menu')
    }
  }
  const base = new URL(connection.url)
  if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)
    || base.username || base.password || base.pathname !== '/' || base.search || base.hash) {
    throw new Error('Invalid owned Gateway')
  }
  const url = new URL('/api/v1/workspace-files/metadata', base)
  url.searchParams.set('path', payload.path)
  url.searchParams.set('workspaceBinding', payload.workspaceBinding)
  const signaturePayload = JSON.stringify({
    v: 1,
    instanceId: payload.gatewayInstanceId,
    sessionKey: payload.sessionKey,
    path: payload.path,
    workspaceBinding: payload.workspaceBinding,
  })
  const signature = createHmac('sha256', connection.nonce)
    .update(NATIVE_WORKSPACE_METADATA_SIGNING_CONTEXT + signaturePayload)
    .digest('hex')
  const response = await (deps.fetch ?? fetch)(url, {
    headers: {
      Authorization: `Bearer ${connection.authToken}`,
      'x-opensquilla-session-key': payload.sessionKey,
      'x-opensquilla-native-signature': signature,
    },
    redirect: 'error', signal: AbortSignal.timeout(15_000),
  })
  if (!response.ok) throw new Error(`Workspace file unavailable (${response.status})`)
  const reader = response.body?.getReader()
  if (!reader) throw new Error('Missing workspace file metadata')
  let text = ''
  const decoder = new TextDecoder('utf-8', { fatal: true })
  try {
    let size = 0
    for (;;) {
      const next = await reader.read()
      if (next.done) break
      size += next.value.byteLength
      if (size > 64 * 1024) throw new Error('Workspace metadata is too large')
      text += decoder.decode(next.value, { stream: true })
    }
    text += decoder.decode()
  } finally { await reader.cancel().catch(() => {}) }
  const metadata = JSON.parse(text) as Record<string, unknown>
  assertCurrent()
  if (metadata?.workspaceBinding !== payload.workspaceBinding
    || metadata?.relativePath !== payload.path
    || !boundedString(metadata.sourcePath, 32768) || !boundedString(metadata.workspace, 32768)
    || !isAbsolute(metadata.sourcePath) || !isAbsolute(metadata.workspace)) {
    throw new Error('Invalid workspace file metadata')
  }
  const workspace = await realpath(metadata.workspace)
  const source = await realpath(metadata.sourcePath)
  const location = relative(workspace, source)
  if (workspace !== metadata.workspace || source !== metadata.sourcePath || !location
    || isAbsolute(location) || location === '..' || location.startsWith(`..${sep}`)
    || location.split(sep).join('/') !== payload.path
    || !(await lstat(source)).isFile()) throw new Error('Workspace file identity changed')
  let ancestor = workspace
  for (const segment of ['', ...payload.path.split('/')]) {
    if (segment) ancestor = join(ancestor, segment)
    if ((await lstat(ancestor)).isSymbolicLink()) throw new Error('Workspace file identity changed')
  }
  const info = await lstat(source, { bigint: true })
  if (!info.isFile() || !workspaceFileIdentityMatches(metadata.identity, info)) {
    throw new Error('Workspace file identity changed')
  }
  assertCurrent()
  if (payload.action === 'reveal') deps.reveal(source)
  else {
    const error = await deps.openPath(source)
    if (error) throw new Error(error)
  }
  return { ok: true }
}
