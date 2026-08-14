import type { Attachment, DisplayAttachment } from '@/types/chat'
import type { ChatHistoryAttachmentPayload, ChatSendAttachmentPayload } from '@/types/rpc'

export type SendableAttachment = Attachment & (
  | { kind: 'inline'; data: string }
  | { kind: 'staged'; file_uuid: string }
)

export function isAttachmentBusy(attachment: Attachment): boolean {
  return attachment.kind === 'inline_pending' || attachment.kind === 'uploading'
}

/**
 * Collect the files carried by a paste event's DataTransfer. Every clipboard
 * file is a candidate attachment — the attachment pipeline applies its own
 * type and size policy — so this deliberately does not filter by MIME type.
 * Falls back to `files` for engines that expose pasted files there without
 * listing them in `items`.
 */
export function collectClipboardFiles(data: DataTransfer | null): File[] {
  const files: File[] = []
  if (!data) return files
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind !== 'file') continue
    const file = item.getAsFile()
    if (file) files.push(file)
  }
  if (files.length === 0 && data.files) files.push(...Array.from(data.files))
  return files
}

/**
 * Decide whether a document-level file paste belongs to the chat composer.
 * Pastes aimed at another editable surface — clarify/approval inputs, the
 * command palette, settings fields — keep their default behavior, and an open
 * dialog layer owns the clipboard entirely, mirroring the document keydown
 * guard.
 */
export function shouldCaptureFilePaste(
  target: EventTarget | null,
  options: { composerTextareaFocused: boolean, dialogLayerOpen: boolean },
): boolean {
  if (options.dialogLayerOpen) return false
  if (target instanceof HTMLTextAreaElement) return options.composerTextareaFocused
  if (target instanceof HTMLInputElement) return false
  if (target instanceof HTMLElement && target.isContentEditable) return false
  return true
}

export function isMimeLike(value: unknown): value is string {
  return typeof value === 'string' && /^[^/\s]+\/[^/\s;]+(?:\s*;.*)?$/.test(value.trim())
}

export function normalizeAttachmentMimeValue(value: unknown, fallback = 'application/octet-stream'): string {
  return isMimeLike(value) ? value.trim().toLowerCase() : fallback
}

export function displayAttachmentMime(attachment: Record<string, unknown>): string {
  for (const key of ['mime', 'mime_type', 'media_type', 'type']) {
    const value = attachment[key]
    if (isMimeLike(value)) return value.trim().toLowerCase()
  }
  return 'application/octet-stream'
}

export function isImageAttachmentMime(mime: unknown): boolean {
  const normalized = normalizeAttachmentMimeValue(mime, '')
  const essence = normalized.split(';', 1)[0].trim()
  // SVG is an active document format. Keep it download-only even though its
  // media type starts with image/ so user-provided markup never enters the DOM.
  return essence.startsWith('image/') && essence !== 'image/svg+xml'
}

export function isImageDisplayAttachment(attachment: Pick<DisplayAttachment, 'mime'> | Pick<Attachment, 'mime'>): boolean {
  return isImageAttachmentMime(attachment.mime)
}

const MODEL_INPUT_IMAGE_MIMES = new Set([
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
])

/**
 * Return the canonical MIME type for images the Gateway turns into model image
 * blocks. Keep this narrower than the preview helper above: formats such as
 * TIFF and SVG may be attachments, but they do not enter the model as images.
 */
export function normalizeModelInputImageMime(mime: unknown): string {
  const essence = normalizeAttachmentMimeValue(mime, '').split(';', 1)[0].trim()
  return essence === 'image/jpg' ? 'image/jpeg' : essence
}

export function isModelInputImageMime(mime: unknown): boolean {
  return MODEL_INPUT_IMAGE_MIMES.has(normalizeModelInputImageMime(mime))
}

function safeImageDataUrl(value: unknown, declaredMime: string): string | undefined {
  if (typeof value !== 'string') return undefined
  const match = value.match(/^data:([^;,]+)(?:;[^,]*)?;base64,[a-z0-9+/=\s]*$/i)
  if (!match) return undefined
  const embeddedMime = normalizeAttachmentMimeValue(match[1], '')
  const declaredEssence = normalizeAttachmentMimeValue(declaredMime, '').split(';', 1)[0].trim()
  return isImageAttachmentMime(embeddedMime) && embeddedMime === declaredEssence
    ? value
    : undefined
}

export function isSendableAttachment(attachment: Attachment): attachment is SendableAttachment {
  if (attachment.kind === 'inline') return Boolean(attachment.data)
  if (attachment.kind === 'staged') return Boolean(attachment.file_uuid)
  return false
}

export function isSendableModelInputImageAttachment(
  attachment: Attachment,
): attachment is SendableAttachment {
  return isSendableAttachment(attachment) && isModelInputImageMime(attachment.mime)
}

export function hasSendableModelInputImageAttachment(attachments: readonly Attachment[]): boolean {
  return attachments.some(isSendableModelInputImageAttachment)
}

export function serializeSendableAttachment(attachment: SendableAttachment): ChatSendAttachmentPayload {
  if (attachment.kind === 'staged') {
    return {
      type: attachment.mime,
      file_uuid: attachment.file_uuid,
      mime: attachment.mime,
      name: attachment.name,
    }
  }
  return {
    type: attachment.mime || 'image/png',
    data: attachment.data,
    mime: attachment.mime,
    name: attachment.name,
  }
}

export function serializeDisplayAttachment(attachment: SendableAttachment): DisplayAttachment {
  const displayId = `local:${attachment.local_id}`
  const base = {
    displayId,
    renderKey: displayId,
    name: attachment.name,
    mime: attachment.mime,
    size: attachment.size,
  }
  if (attachment.kind === 'staged') {
    return { ...base, kind: 'staged', localFile: attachment.file }
  }
  const isImage = isImageDisplayAttachment(attachment)
  return {
    ...base,
    kind: 'inline',
    data: isImage ? attachment.data : undefined,
    dataUrl: isImage ? attachment.dataUrl : undefined,
    downloadData: isImage ? undefined : attachment.data,
    localFile: attachment.file,
  }
}

export function normalizeDisplayAttachment(
  raw: Attachment | DisplayAttachment | ChatHistoryAttachmentPayload,
  options: { messageId?: string, index?: number } = {},
): DisplayAttachment {
  const record = raw as Record<string, unknown>
  const mime = displayAttachmentMime(record)
  const image = isImageAttachmentMime(mime)
  const name = typeof record.name === 'string' && record.name.trim()
    ? record.name.trim()
    : typeof record.filename === 'string' && record.filename.trim()
      ? record.filename.trim()
      : 'attachment'
  const sha = typeof record.sha256_ref === 'string' && record.sha256_ref.trim()
    ? record.sha256_ref.trim()
    : ''
  const existingDisplayId = typeof record.displayId === 'string' && record.displayId.trim()
    ? record.displayId.trim()
    : ''
  const localId = typeof record.local_id === 'number' && Number.isFinite(record.local_id)
    ? `local:${record.local_id}`
    : ''
  const index = typeof options.index === 'number' ? options.index : 0
  const messagePart = options.messageId || 'history'
  const displayId = existingDisplayId || localId || (sha ? `sha:${sha}:${index}` : `${messagePart}:att:${index}`)
  const size = typeof record.size === 'number' && Number.isFinite(record.size)
    ? record.size
    : typeof record.size === 'string' && Number.isFinite(Number(record.size))
      ? Number(record.size)
      : undefined
  const data = typeof record.data === 'string' ? record.data : undefined
  const downloadData = typeof record.downloadData === 'string'
    ? record.downloadData
    : image
      ? undefined
      : data
  const dataUrl = safeImageDataUrl(
    typeof record.dataUrl === 'string' ? record.dataUrl : record.data_url,
    mime,
  )
  const rawKind = typeof record.kind === 'string' ? record.kind : ''
  const kind: DisplayAttachment['kind'] = rawKind === 'staged' || sha
    ? 'staged'
    : rawKind === 'inline' || data || dataUrl
      ? 'inline'
      : 'file'

  return {
    kind,
    displayId,
    renderKey: typeof record.renderKey === 'string' && record.renderKey.trim()
      ? record.renderKey.trim()
      : displayId,
    name,
    mime,
    size,
    data: image ? data : undefined,
    dataUrl: image ? dataUrl : undefined,
    downloadData,
    localFile: typeof File !== 'undefined' && record.localFile instanceof File
      ? record.localFile
      : typeof File !== 'undefined' && record.file instanceof File
        ? record.file
        : undefined,
    download_url: typeof record.download_url === 'string' ? record.download_url : undefined,
    sha256_ref: sha || undefined,
  }
}

export function normalizeDisplayAttachments(
  attachments: Array<Attachment | DisplayAttachment | ChatHistoryAttachmentPayload> | undefined,
  options: { messageId?: string } = {},
): DisplayAttachment[] {
  return (attachments || []).map((attachment, index) =>
    normalizeDisplayAttachment(attachment, { messageId: options.messageId, index }),
  )
}
