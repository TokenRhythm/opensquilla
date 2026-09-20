import type { DisplayAttachment } from '@/types/chat'
import { isImageAttachmentMime } from '@/utils/chat/attachments'
import type {
  ArtifactAccessRequest,
  ArtifactContentAccess,
  AttachmentUploadReceipt,
} from '@/modules/artifactWorkbench'
import {
  HttpTransportError,
} from './privateHttpTransport'
import {
  assertBinaryReadActive,
  assertBinarySize,
  BinaryBodyTooLargeError,
  readBinaryBlob,
  type ReadableBinaryBody,
} from './boundedBinaryBody'
import {
  artifactHttpAttachmentUrl,
  bindAttachmentBinaryRequest,
  runtimeAttachmentHttpBaseOrigin,
  uploadArtifactAttachment,
} from './privateArtifactHttpTransport'

interface AttachmentBinaryResponse extends ReadableBinaryBody {
  readonly metadata: ReadableBinaryBody['metadata'] & {
    readonly filename?: string
    readonly status: number
  }
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
  maxBytes?: number
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
      source: 'none' | 'local-file' | 'inline' | 'staged'
      url: string
      message: string
      errorCode?: 'too_large'
    }

export function attachmentAccessUrl(raw: unknown, baseOrigin: string): string {
  return artifactHttpAttachmentUrl(raw, baseOrigin)
}

function resolveBaseOrigin(baseOrigin?: string): string {
  if (baseOrigin) return baseOrigin
  return runtimeAttachmentHttpBaseOrigin()
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

function base64Bytes(value: string, maxBytes?: number): Uint8Array | null {
  const compact = value.replace(/\s+/g, '')
  if (!compact || compact.length % 4 === 1 || !/^[A-Za-z0-9+/]*={0,2}$/.test(compact)) return null
  const padding = compact.endsWith('==') ? 2 : compact.endsWith('=') ? 1 : 0
  assertBinarySize(Math.floor(compact.length * 3 / 4) - padding, maxBytes)
  let decoded: string
  try {
    decoded = atob(compact)
  } catch {
    return null
  }
  assertBinarySize(decoded.length, maxBytes)
  const bytes = new Uint8Array(decoded.length)
  for (let i = 0; i < decoded.length; i += 1) bytes[i] = decoded.charCodeAt(i)
  return bytes
}

function attachmentTooLarge(source: 'local-file' | 'inline' | 'staged', url = ''): AttachmentDownloadResult {
  return { ok: false, status: 0, source, url, errorCode: 'too_large',
    message: 'Attachment exceeds the requested byte limit.' }
}

export async function fetchDisplayAttachmentBlob(
  http: Pick<AttachmentHttpTransport, 'requestBinary'>,
  attachment: DisplayAttachment,
  options: AttachmentDownloadOptions = {},
): Promise<AttachmentDownloadResult> {
  assertBinaryReadActive(options.signal)
  const filename = safeFilename(attachment.name)
  if (attachment.localFile instanceof Blob) {
    try { assertBinarySize(attachment.localFile.size, options.maxBytes) }
    catch (error) {
      if (error instanceof BinaryBodyTooLargeError) return attachmentTooLarge('local-file')
      throw error
    }
    return {
      ok: true,
      status: 200,
      source: 'local-file',
      url: '',
      blob: attachment.localFile,
      filename,
    }
  }

  const dataUrl = attachment.dataUrl?.match(/^data:(image\/[^;,]+);base64,([\s\S]*)$/i)
  const imageData = dataUrl && isImageAttachmentMime(dataUrl[1]) ? dataUrl[2] : undefined
  const encoded = attachment.downloadData || attachment.data || imageData
  if (encoded) {
    let bytes: Uint8Array | null
    try { bytes = base64Bytes(encoded, options.maxBytes) }
    catch (error) {
      if (error instanceof BinaryBodyTooLargeError) return attachmentTooLarge('inline')
      throw error
    }
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
  const request = bindAttachmentBinaryRequest(http, attachment.download_url, { baseOrigin })
  if (!request) {
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
  const url = request.url

  try {
    const response = await request.execute({
      sessionKey: options.sessionKey,
      signal: options.signal,
      timeoutMs: 0,
    })
    return {
      ok: true,
      status: response.metadata.status,
      source: 'staged',
      url,
      blob: await readBinaryBlob(response, options),
      filename: safeFilename(response.metadata.filename || filename),
    }
  } catch (error) {
    if (error instanceof BinaryBodyTooLargeError) return attachmentTooLarge('staged', url)
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
    baseOrigin: runtimeAttachmentHttpBaseOrigin(),
    sessionKey: request.sessionKey,
    signal: request.signal,
    maxBytes: request.maxBytes,
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
      return uploadResponseMeta(await uploadArtifactAttachment(http, form))
    },
  }
}
