import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'
import type { Attachment, ChatMessage } from '@/types/chat'
import {
  isImageAttachmentMime,
  isModelInputImageMime,
  normalizeAttachmentMimeValue,
  normalizeDisplayAttachment,
  normalizeWorkspaceFileReferences,
} from './attachments'

// Match chat intake ceilings. Historical material must be read with a bound,
// and large files go through the existing staging API rather than the RPC body.
// Apply the inline threshold to the whole recovered batch: base64 expansion
// must not turn many individually small files into an oversized RPC frame.
const INLINE_BYTES = 2_000_000
const IMAGE_BYTES = 5 * 1024 * 1024
const FILE_BYTES = 50 * 1024 * 1024
const TOTAL_BYTES = 60 * 1024 * 1024
const MAX_ATTACHMENTS = 16
const EMAIL_MIMES = new Set(['message/rfc822', 'application/mbox', 'application/vnd.ms-outlook'])
let replayAttachmentId = -1

function assertActive(signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
}

function readBase64(file: File, signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    const abort = () => { reader.abort(); reject(new DOMException('Aborted', 'AbortError')) }
    const cleanup = () => signal.removeEventListener('abort', abort)
    reader.onload = () => {
      cleanup()
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      if (comma < 0) reject(new Error('Could not read original attachment'))
      else resolve(result.slice(comma + 1))
    }
    reader.onerror = () => { cleanup(); reject(reader.error || new Error('Could not read original attachment')) }
    reader.onabort = () => { cleanup(); reject(new DOMException('Aborted', 'AbortError')) }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) { abort(); return }
    reader.readAsDataURL(file)
  })
}

/** Recover bytes from the source session, never reuse expired upload tokens. */
export async function recoverMessageAttachments(
  content: Pick<ArtifactContentAccess, 'fetchAttachment' | 'uploadAttachment'>,
  message: ChatMessage,
  sessionKey: string,
  signal: AbortSignal,
): Promise<Attachment[]> {
  const originals = message.attachments || []
  assertActive(signal)
  if (originals.length > MAX_ATTACHMENTS) throw new Error('Too many original attachments')
  const attachments: Attachment[] = []
  let total = 0
  let inlineBytes = 0
  for (const original of originals) {
    assertActive(signal)
    // Previewable formats such as TIFF/BMP remain ordinary opaque files at
    // admission; only the four model-image formats use the smaller image cap.
    const fileMaxBytes = isModelInputImageMime(original.mime) ? IMAGE_BYTES : FILE_BYTES
    const retained = original.localFile || original.downloadData || original.data || original.dataUrl
      || original.download_url || original.sha256_ref || original.fileSnapshotRef
    // Native imports already carry a typed, workspace-bound input reference.
    // Keep it only when no retained snapshot bytes exist; the existing admission
    // path remains responsible for workspace ownership and file validation.
    if (original.workspaceFile && !retained) {
      const workspaceFile = normalizeWorkspaceFileReferences([original.workspaceFile])[0]
      if (!workspaceFile) throw new Error('Original workspace attachment is invalid')
      const size = workspaceFile.size ?? original.size ?? 0
      if (!Number.isSafeInteger(size) || size < 0) throw new Error('Original attachment size is invalid')
      if (size > fileMaxBytes) throw new Error('Original attachment is too large')
      // Match native intake: a bound workspace reference does not upload or
      // retain file bytes, so its declared size is not part of that budget.
      attachments.push({ kind: 'workspace', local_id: replayAttachmentId--,
        name: original.name, mime: original.mime, size, workspaceFile })
      continue
    }
    const mimeEssence = normalizeAttachmentMimeValue(original.mime).split(';', 1)[0].trim()
    // Email snapshots retain their transport ceiling even when freshly staged.
    // Workspace references above carry no bytes and keep the native-file cap.
    const payloadMaxBytes = EMAIL_MIMES.has(mimeEssence) ? INLINE_BYTES : fileMaxBytes
    const maxBytes = Math.min(payloadMaxBytes, TOTAL_BYTES - total)
    const result = await content.fetchAttachment(original, { sessionKey, signal, maxBytes })
    assertActive(signal)
    if (!result.ok) throw new Error(result.message)
    // Enforce the bounds even when a content provider returns retained memory.
    if (result.blob.size > maxBytes) throw new Error('Original attachment is too large')
    total += result.blob.size
    const mime = original.mime || result.blob.type || 'application/octet-stream'
    const file = new File([result.blob], original.name || result.filename, { type: mime })
    const base = {
      local_id: replayAttachmentId--, name: file.name, mime, size: file.size, file,
      ...(original.origin ? { origin: original.origin } : {}),
    }
    if (file.size <= INLINE_BYTES - inlineBytes) {
      const data = await readBase64(file, signal)
      assertActive(signal)
      inlineBytes += file.size
      attachments.push({ ...base, kind: 'inline', data,
        ...(isImageAttachmentMime(mime) ? { dataUrl: `data:${file.type};base64,${data}` } : {}) })
    } else {
      // Keep small images visible when the aggregate inline budget stages them.
      // Large staged files follow ordinary intake: reuse a safe retained preview
      // if present, but do not encode the whole file just to add a thumbnail.
      let dataUrl = normalizeDisplayAttachment({ ...original, mime }).dataUrl
      if (!dataUrl && isImageAttachmentMime(mime) && file.size <= INLINE_BYTES) {
        const data = await readBase64(file, signal)
        assertActive(signal)
        dataUrl = `data:${file.type};base64,${data}`
      }
      const receipt = await content.uploadAttachment(file, mime)
      assertActive(signal)
      if (!receipt.fileUuid) throw new Error('Original attachment upload failed')
      attachments.push({ ...base, kind: 'staged', file_uuid: receipt.fileUuid,
        expires_at: receipt.expiresAt, ttl_seconds: receipt.ttlSeconds,
        ...(dataUrl ? { dataUrl } : {}) })
    }
  }
  return attachments
}
