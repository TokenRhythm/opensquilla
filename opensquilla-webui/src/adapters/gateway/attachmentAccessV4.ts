import type { DisplayAttachment } from '@/types/chat'
import type {
  ArtifactAccessRequest,
  ArtifactContentAccess,
  AttachmentUploadReceipt,
} from '@/modules/artifactWorkbench'
import {
  HttpTransportError,
} from './privateHttpTransport'

interface AttachmentBinaryResponse {
  readonly metadata: {
    readonly filename?: string
    readonly status: number
  }
  blob(): Promise<Blob>
}

interface AttachmentHttpTransport {
  requestBinary(endpoint: string, options?: {
    sessionKey?: string
    signal?: AbortSignal
    timeoutMs?: number
  }): Promise<AttachmentBinaryResponse>
  requestJson<T>(endpoint: string, options: {
    method: 'POST'
    form: FormData
  }): Promise<T>
}

interface AttachmentDownloadOptions {
  baseOrigin?: string
  sessionKey?: string
  signal?: AbortSignal
}

type AttachmentDownloadResult =
  | {
      ok: true
      status: number
      source: 'local-file' | 'inline' | 'staged'
      url: string
      blob: Blob
      filename: string
    }
  | {
      ok: false
      status: number
      source: 'none' | 'inline' | 'staged'
      url: string
      message: string
    }

const DEFAULT_BASE_ORIGIN = 'http://localhost'
const CREDENTIAL_QUERY_KEYS = /(token|session)/i

export function attachmentAccessUrl(raw: unknown, baseOrigin: string): string {
  if (typeof raw !== 'string' || !raw.trim()) return ''
  try {
    const base = new URL(baseOrigin)
    const url = new URL(raw, base)
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.origin !== base.origin) {
      return ''
    }
    if (url.username || url.password) return ''
    for (const key of [...url.searchParams.keys()]) {
      if (CREDENTIAL_QUERY_KEYS.test(key)) url.searchParams.delete(key)
    }
    url.hash = ''
    return url.pathname + url.search
  } catch {
    return ''
  }
}

function resolveBaseOrigin(baseOrigin?: string): string {
  if (baseOrigin) return baseOrigin
  if (typeof window !== 'undefined' && window.location?.origin) return window.location.origin
  return DEFAULT_BASE_ORIGIN
}

function safeFilename(value: unknown): string {
  const raw = typeof value === 'string' ? value : ''
  const basename = raw.split(/[/\\]/).pop()?.replace(/[\u0000-\u001f\u007f]/g, '').trim() || ''
  return basename && basename !== '.' && basename !== '..' ? basename : 'attachment'
}

function isAbortError(error: unknown): boolean {
  return error instanceof HttpTransportError
    ? error.kind === 'aborted'
    : !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
}

function base64Bytes(value: string): Uint8Array | null {
  const compact = value.replace(/\s+/g, '')
  if (!compact || compact.length % 4 === 1 || !/^[A-Za-z0-9+/]*={0,2}$/.test(compact)) return null
  try {
    const decoded = atob(compact)
    const bytes = new Uint8Array(decoded.length)
    for (let i = 0; i < decoded.length; i += 1) bytes[i] = decoded.charCodeAt(i)
    return bytes
  } catch {
    return null
  }
}

export async function fetchDisplayAttachmentBlob(
  http: Pick<AttachmentHttpTransport, 'requestBinary'>,
  attachment: DisplayAttachment,
  options: AttachmentDownloadOptions = {},
): Promise<AttachmentDownloadResult> {
  const filename = safeFilename(attachment.name)
  if (attachment.localFile instanceof Blob) {
    return {
      ok: true,
      status: 200,
      source: 'local-file',
      url: '',
      blob: attachment.localFile,
      filename,
    }
  }

  const encoded = attachment.downloadData || attachment.data
  if (encoded) {
    const bytes = base64Bytes(encoded)
    if (!bytes) {
      return { ok: false, status: 0, source: 'inline', url: '', message: 'Attachment data is invalid.' }
    }
    const buffer = new ArrayBuffer(bytes.byteLength)
    new Uint8Array(buffer).set(bytes)
    return {
      ok: true,
      status: 200,
      source: 'inline',
      url: '',
      blob: new Blob([buffer], { type: attachment.mime || 'application/octet-stream' }),
      filename,
    }
  }

  const baseOrigin = resolveBaseOrigin(options.baseOrigin)
  const url = attachmentAccessUrl(attachment.download_url, baseOrigin)
  if (!url) {
    return {
      ok: false,
      status: 0,
      source: attachment.download_url ? 'staged' : 'none',
      url: '',
      message: attachment.download_url
        ? 'Attachment download URL is not allowed.'
        : 'Attachment is no longer available.',
    }
  }

  try {
    const response = await http.requestBinary(url, {
      sessionKey: options.sessionKey,
      signal: options.signal,
      timeoutMs: 0,
    })
    return {
      ok: true,
      status: response.metadata.status,
      source: 'staged',
      url,
      blob: await response.blob(),
      filename: safeFilename(response.metadata.filename || filename),
    }
  } catch (error) {
    if (isAbortError(error)) {
      if (error instanceof HttpTransportError) {
        throw new DOMException('Aborted', 'AbortError')
      }
      throw error
    }
    if (
      error instanceof HttpTransportError
      && error.kind === 'http-status'
      && typeof error.status === 'number'
    ) {
      return {
        ok: false,
        status: error.status,
        source: 'staged',
        url,
        message: `Attachment download failed (HTTP ${error.status}).`,
      }
    }
    return { ok: false, status: 0, source: 'staged', url, message: 'Attachment download failed.' }
  }
}

function uploadResponseMeta(value: unknown): AttachmentUploadReceipt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Attachment upload returned an invalid response.')
  }
  const raw = value as Record<string, unknown>
  const fileUuid = typeof raw.file_uuid === 'string'
    ? raw.file_uuid.trim()
    : typeof raw.fileUuid === 'string' ? raw.fileUuid.trim() : ''
  if (!fileUuid) throw new Error('Attachment upload did not return a file identifier.')
  const expiresAt = Number(raw.expires_at ?? raw.expiresAt)
  const ttlSeconds = Number(raw.ttl_seconds ?? raw.ttlSeconds)
  return {
    fileUuid,
    ...(Number.isFinite(expiresAt) ? { expiresAt } : {}),
    ...(Number.isFinite(ttlSeconds) ? { ttlSeconds } : {}),
  }
}

function runtimeOptions(request: ArtifactAccessRequest = {}): AttachmentDownloadOptions {
  return {
    baseOrigin: typeof window !== 'undefined' && window.location?.origin
      ? window.location.origin
      : DEFAULT_BASE_ORIGIN,
    sessionKey: request.sessionKey,
    signal: request.signal,
  }
}

export function createV4AttachmentContentAccess(http: AttachmentHttpTransport): Pick<
  ArtifactContentAccess,
  'fetchAttachment' | 'uploadAttachment'
> {
  return {
    fetchAttachment: (attachment, request) => (
      fetchDisplayAttachmentBlob(http, attachment, runtimeOptions(request))
    ),
    async uploadAttachment(file, mime) {
      const form = new FormData()
      form.append('file', file, file.name)
      form.append('mime', mime)
      return uploadResponseMeta(await http.requestJson('/api/v1/files/upload', {
        method: 'POST',
        form,
      }))
    },
  }
}
