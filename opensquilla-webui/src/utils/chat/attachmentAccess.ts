import type { DisplayAttachment } from '@/types/chat'
import { filenameFromContentDisposition } from '@/utils/browser'

export interface AttachmentDownloadOptions {
  baseOrigin?: string
  sessionKey?: string
  authToken?: string
  fetchImpl?: typeof fetch
  signal?: AbortSignal
}

export type AttachmentDownloadResult =
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
// Treat any token/session-named query field as a credential. Gateway versions
// and reverse proxies have used variants such as access_token and session_id;
// retaining an unknown variant in a copied staged URL would defeat the header-
// only authentication boundary.
const CREDENTIAL_QUERY_KEYS = /(token|session)/i

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
  return !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
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

export function attachmentAccessUrl(raw: unknown, baseOrigin: string): string {
  if (typeof raw !== 'string' || !raw.trim()) return ''
  try {
    const base = new URL(baseOrigin)
    const url = new URL(raw, base)
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.origin !== base.origin) return ''
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

export function attachmentAccessHeaders(options: AttachmentDownloadOptions = {}): Record<string, string> {
  const headers: Record<string, string> = {}
  if (options.sessionKey) headers['x-opensquilla-session-key'] = options.sessionKey
  if (options.authToken) headers.Authorization = `Bearer ${options.authToken}`
  return headers
}

export async function fetchDisplayAttachmentBlob(
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

  const fetchImpl = options.fetchImpl || (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null)
  if (!fetchImpl) {
    return { ok: false, status: 0, source: 'staged', url, message: 'Attachment download is unavailable.' }
  }
  try {
    const response = await fetchImpl(url, {
      method: 'GET',
      headers: attachmentAccessHeaders(options),
      credentials: 'same-origin',
      redirect: 'error',
      signal: options.signal,
    })
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        source: 'staged',
        url,
        message: `Attachment download failed (HTTP ${response.status}).`,
      }
    }
    return {
      ok: true,
      status: response.status,
      source: 'staged',
      url,
      blob: await response.blob(),
      filename: safeFilename(
        filenameFromContentDisposition(response.headers.get('content-disposition')) || filename,
      ),
    }
  } catch (error) {
    if (isAbortError(error)) throw error
    return { ok: false, status: 0, source: 'staged', url, message: 'Attachment download failed.' }
  }
}
