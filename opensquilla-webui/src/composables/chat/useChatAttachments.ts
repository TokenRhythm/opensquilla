import { computed, ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { Attachment } from '@/types/chat'
import type { ArtifactContentAccess } from '@/modules/artifactWorkbench'

const INLINE_THRESHOLD_BYTES = 2_000_000
const ATTACHMENT_TEXT_HARD_CAP_BYTES = INLINE_THRESHOLD_BYTES
const ATTACHMENT_IMAGE_HARD_CAP_BYTES = 5 * 1024 * 1024
const ATTACHMENT_PDF_HARD_CAP_BYTES = 30 * 1024 * 1024
const ATTACHMENT_OFFICE_HARD_CAP_BYTES = 30 * 1024 * 1024
// Text above the inline threshold routes through the staged upload path (the
// gateway proves the whole payload is UTF-8 before honoring this ceiling).
const ATTACHMENT_STAGED_TEXT_HARD_CAP_BYTES = 30 * 1024 * 1024
// Opaque types (archives, binaries, audio/video, unknown formats) stage up to
// this ceiling; their bytes land in the agent workspace, never in the prompt.
const ATTACHMENT_OPAQUE_HARD_CAP_BYTES = 30 * 1024 * 1024
const MAX_ATTACHMENTS = 10
const MAX_TOTAL_ATTACHMENT_BYTES = 60 * 1024 * 1024
const STAGED_UPLOAD_REFRESH_GRACE_MS = 30_000
// Email is held to the text cap (bounded text is extracted; large emails are
// large only due to attachments we never read), so it inlines and never stages.
const ATTACHMENT_EMAIL_HARD_CAP_BYTES = ATTACHMENT_TEXT_HARD_CAP_BYTES

type UploadResponseMeta = {
  fileUuid: string
  expiresAt?: number
  ttlSeconds?: number
}

export type AttachmentPreparationOptions = {
  ownership: 'composer'
  isCurrent?: () => boolean
  /** A send snapshot can remain composer-owned even though it is a cloned array. */
  attachments?: Attachment[]
} | {
  ownership: 'detached'
  /** Detached callers own cancellation; composer retirement must not invalidate them. */
  isCurrent: () => boolean
  attachments: Attachment[]
}

// Per-addAttachments-call state so batch-wide rejections (the aggregate size
// cap) toast once instead of once per rejected file.
type AttachmentBatch = {
  generation: number
  totalSizeToastShown: boolean
}

const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
const PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
const EML_MIME = 'message/rfc822'
const MBOX_MIME = 'application/mbox'
const MSG_MIME = 'application/vnd.ms-outlook'

const ATTACHMENT_IMAGE_MIMES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const ATTACHMENT_TEXT_MIMES = ['text/plain', 'text/markdown', 'text/html', 'text/csv', 'application/json']
const ATTACHMENT_OFFICE_MIMES = [DOCX_MIME, XLSX_MIME, PPTX_MIME]
const ATTACHMENT_EMAIL_MIMES = [EML_MIME, MBOX_MIME, MSG_MIME]
const ATTACHMENT_ALLOWED_MIMES = [...ATTACHMENT_IMAGE_MIMES, 'application/pdf', ...ATTACHMENT_TEXT_MIMES, ...ATTACHMENT_OFFICE_MIMES, ...ATTACHMENT_EMAIL_MIMES]
const ATTACHMENT_EXTENSION_MIMES: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif',
  webp: 'image/webp', pdf: 'application/pdf', txt: 'text/plain', md: 'text/markdown',
  markdown: 'text/markdown', html: 'text/html', htm: 'text/html', csv: 'text/csv', json: 'application/json',
  docx: DOCX_MIME, xlsx: XLSX_MIME, pptx: PPTX_MIME,
  eml: EML_MIME, mbox: MBOX_MIME, msg: MSG_MIME,
}

function isAllowedAttachmentMime(mime: string): boolean {
  return typeof mime === 'string' && ATTACHMENT_ALLOWED_MIMES.includes(mime)
}

function isImageAttachmentMime(mime: string): boolean {
  return typeof mime === 'string' && ATTACHMENT_IMAGE_MIMES.includes(mime)
}

function canStageAttachmentMime(mime: string): boolean {
  // Email is capped at the text limit, so it inlines and is never staged.
  // Everything else — rendered or opaque — has a staged path.
  return !ATTACHMENT_EMAIL_MIMES.includes(mime)
}

function attachmentHardCapBytes(mime: string): number {
  if (mime === 'application/pdf') return ATTACHMENT_PDF_HARD_CAP_BYTES
  if (isImageAttachmentMime(mime)) return ATTACHMENT_IMAGE_HARD_CAP_BYTES
  if (ATTACHMENT_OFFICE_MIMES.includes(mime)) return ATTACHMENT_OFFICE_HARD_CAP_BYTES
  if (ATTACHMENT_EMAIL_MIMES.includes(mime)) return ATTACHMENT_EMAIL_HARD_CAP_BYTES
  if (ATTACHMENT_TEXT_MIMES.includes(mime)) return ATTACHMENT_STAGED_TEXT_HARD_CAP_BYTES
  return ATTACHMENT_OPAQUE_HARD_CAP_BYTES
}

function resolveAttachmentMime(file: File): string {
  const name = file.name || ''
  const ext = name.includes('.') ? name.split('.').pop()?.toLowerCase() || '' : ''
  const extensionMime = ATTACHMENT_EXTENSION_MIMES[ext]
  if (file.type && isAllowedAttachmentMime(file.type)) return file.type
  return extensionMime || file.type || 'application/octet-stream'
}

// Unknown-but-textual uploads degrade to text/plain so the gateway's UTF-8
// fallback is reachable from the WebUI (the gateway re-validates). Bounded to
// the text cap range; binary (NUL byte / invalid UTF-8) stays rejected.
const TEXT_FALLBACK_MAX_SNIFF_BYTES = 4_000_000
async function fileLooksLikeUtf8Text(file: File): Promise<boolean> {
  if (file.size === 0 || file.size > TEXT_FALLBACK_MAX_SNIFF_BYTES) return false
  try {
    const bytes = new Uint8Array(await file.arrayBuffer())
    if (bytes.includes(0)) return false
    new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    return true
  } catch {
    return false
  }
}

export function useChatAttachments(artifactContent?: ArtifactContentAccess) {
  const { pushToast } = useToasts()
  const pendingAttachments = ref<Attachment[]>([])
  const nextAttachmentId = ref(1)
  const refreshInFlightByCollection = new WeakMap<Attachment[], Set<Attachment>>()
  const composerRefreshInFlightCount = ref(0)
  const attachmentIntakeInFlightCount = ref(0)
  let attachmentGeneration = 0
  // Composer send snapshots clone both the array and its attachment objects.
  // Exclude them at the draft-generation boundary; detached queue/handoff
  // collections intentionally keep the per-collection identity lane below.
  let composerPreparationInFlight: { generation: number } | null = null
  const attachmentWorkBusy = computed(() =>
    attachmentIntakeInFlightCount.value > 0
    || composerRefreshInFlightCount.value > 0
    || pendingAttachments.value.some(
      attachment => attachment.kind === 'inline_pending' || attachment.kind === 'uploading',
    ),
  )

  function onFileInputChange(e: Event) {
    const target = e.target as HTMLInputElement
    if (target.files) {
      void addAttachments(Array.from(target.files))
      target.value = ''
    }
  }

  async function addAttachments(files: File[]) {
    const batch: AttachmentBatch = {
      generation: attachmentGeneration,
      totalSizeToastShown: false,
    }
    for (const file of files) {
      if (!isAttachmentGenerationCurrent(batch.generation)) return
      // One toast for the whole batch when the count cap is hit — a per-file
      // repeat would only evict more useful toasts.
      if (activeAttachmentCount() >= MAX_ATTACHMENTS) {
        pushToast(i18n.global.t('chat.toast.tooManyAttachments', { max: MAX_ATTACHMENTS }), { tone: 'danger' })
        return
      }
      await addAttachmentFile(file, batch)
      if (!isAttachmentGenerationCurrent(batch.generation)) return
    }
  }

  async function addAttachment(file: File) {
    await addAttachments([file])
  }

  async function addAttachmentFile(file: File, batch: AttachmentBatch) {
    if (!isAttachmentGenerationCurrent(batch.generation)) return
    const fileName = file.name || 'Untitled file'
    if (file.size === 0) {
      pushToast(i18n.global.t('chat.toast.emptyFile', { name: fileName }), { tone: 'danger' })
      return
    }

    let mime = resolveAttachmentMime(file)
    const requiresMimeSniff = !isAllowedAttachmentMime(mime)
    if (requiresMimeSniff) attachmentIntakeInFlightCount.value += 1
    try {
      if (requiresMimeSniff) {
        const looksLikeText = await fileLooksLikeUtf8Text(file)
        if (!isAttachmentGenerationCurrent(batch.generation)) return
        if (looksLikeText) {
          // Unknown-but-textual uploads degrade to text/plain so the gateway's
          // UTF-8 fallback is reachable from the WebUI (the gateway re-validates).
          mime = 'text/plain'
        }
        // Anything else is an opaque attachment: it uploads under its resolved
        // label and the gateway stages the bytes for the agent workspace.
      }
      const hardCap = attachmentHardCapBytes(mime)
      if (file.size > hardCap) {
        pushToast(i18n.global.t('chat.toast.fileTooLarge', { name: fileName, cap: formatMiB(hardCap) }), { tone: 'danger' })
        return
      }
      if (!canAcceptAttachment(fileName, file.size, batch)) return

      const localId = allocateAttachmentId()

      if (file.size <= INLINE_THRESHOLD_BYTES) {
        const placeholder: Attachment = {
          kind: 'inline_pending',
          local_id: localId,
          name: fileName,
          mime,
          size: file.size,
          file,
        }
        pendingAttachments.value.push(placeholder)
        const reader = new FileReader()
        reader.onload = (e) => {
          if (!isAttachmentGenerationCurrent(batch.generation)) return
          const dataUrl = e.target?.result as string
          const b64 = dataUrl?.split(',')[1] || ''
          const idx = pendingAttachments.value.indexOf(placeholder)
          if (idx >= 0) {
            pendingAttachments.value[idx] = { kind: 'inline', local_id: localId, name: fileName, mime, size: file.size, data: b64, dataUrl, file }
          }
        }
        reader.onerror = () => {
          if (!isAttachmentGenerationCurrent(batch.generation)) return
          const message = i18n.global.t('chat.toast.couldNotReadFile', { name: fileName })
          markAttachmentFailed(localId, file, mime, message, pendingAttachments.value, placeholder)
          pushToast(message, { tone: 'danger' })
        }
        reader.readAsDataURL(file)
        return
      }

      if (!canStageAttachmentMime(mime)) {
        pushToast(i18n.global.t('chat.toast.fileTooLarge', { name: fileName, cap: formatMiB(hardCap) }), { tone: 'danger' })
        return
      }

      const placeholder: Attachment = {
        kind: 'uploading',
        local_id: localId,
        name: fileName,
        mime,
        size: file.size,
        file,
      }
      pendingAttachments.value.push(placeholder)
      uploadAttachmentStaged(file, mime, placeholder, batch.generation).catch((err) => {
        if (!isAttachmentGenerationCurrent(batch.generation)) return
        const message = uploadFailureMessage(err)
        markAttachmentFailed(localId, file, mime, message, pendingAttachments.value, placeholder)
        pushToast(`${i18n.global.t('chat.toast.uploadFailed', { name: fileName })}: ${message}`, { tone: 'danger' })
      })
    } finally {
      if (requiresMimeSniff && isAttachmentGenerationCurrent(batch.generation)) {
        attachmentIntakeInFlightCount.value = Math.max(0, attachmentIntakeInFlightCount.value - 1)
      }
    }
  }

  async function uploadAttachmentStaged(
    file: File,
    mime: string,
    placeholder: Attachment,
    generation: number,
  ) {
    const meta = await uploadAttachmentFile(file, mime)
    if (!isAttachmentGenerationCurrent(generation)) return
    const idx = pendingAttachments.value.indexOf(placeholder)
    if (idx >= 0) {
      pendingAttachments.value[idx] = {
        kind: 'staged',
        local_id: placeholder.local_id,
        name: file.name || 'Untitled file',
        mime,
        size: file.size,
        file_uuid: meta.fileUuid,
        expires_at: meta.expiresAt,
        ttl_seconds: meta.ttlSeconds,
        file,
      }
    }
  }

  async function uploadAttachmentFile(file: File, mime: string): Promise<UploadResponseMeta> {
    if (!artifactContent) throw new Error('Attachment upload is unavailable.')
    return artifactContent.uploadAttachment(file, mime)
  }

  function removeAttachment(index: number) {
    pendingAttachments.value.splice(index, 1)
  }

  function retireAttachments() {
    attachmentGeneration += 1
    refreshInFlightByCollection.delete(pendingAttachments.value)
    composerPreparationInFlight = null
    pendingAttachments.value = []
    composerRefreshInFlightCount.value = 0
    attachmentIntakeInFlightCount.value = 0
  }

  async function retryAttachment(index: number) {
    const attachment = pendingAttachments.value[index]
    if (!attachment || attachment.kind !== 'failed') return
    if (!attachment.file) {
      pushToast(`Cannot retry ${attachment.name}: select the file again`, { tone: 'danger' })
      return
    }
    pendingAttachments.value.splice(index, 1)
    await addAttachment(attachment.file)
  }

  function markAttachmentFailed(
    localId: number,
    file: File,
    mime: string,
    error: string,
    attachments: Attachment[] = pendingAttachments.value,
    expectedAttachment?: Attachment,
  ) {
    const idx = expectedAttachment
      ? attachments.indexOf(expectedAttachment)
      : attachments.findIndex(attachment => attachment.local_id === localId)
    if (idx >= 0) {
      attachments[idx] = {
        kind: 'failed',
        local_id: localId,
        name: file.name || 'Untitled file',
        mime,
        size: file.size,
        error,
        file,
      }
    }
  }

  function hasPendingAttachmentWork(): boolean {
    return attachmentWorkBusy.value
  }

  async function prepareAttachmentsForSend(
    options: AttachmentPreparationOptions = { ownership: 'composer' },
  ): Promise<boolean> {
    const isCurrent = options.isCurrent ?? (() => true)
    const composerOwned = options.ownership !== 'detached'
    const generation = attachmentGeneration
    const preparationIsCurrent = () => (
      (!composerOwned || isAttachmentGenerationCurrent(generation)) && isCurrent()
    )
    const attachments = options.attachments ?? pendingAttachments.value
    const composerPreparation = composerOwned ? { generation } : null
    if (
      composerPreparation
      && composerPreparationInFlight?.generation === generation
    ) return false
    if (composerPreparation) composerPreparationInFlight = composerPreparation
    let refreshInFlightAttachments = refreshInFlightByCollection.get(attachments)
    if (!refreshInFlightAttachments) {
      refreshInFlightAttachments = new Set<Attachment>()
      refreshInFlightByCollection.set(attachments, refreshInFlightAttachments)
    }
    try {
      const staged = [...attachments].filter(stagedUploadNeedsRefresh)
      for (const attachment of staged) {
        if (!preparationIsCurrent()) return false
        if (refreshInFlightAttachments.has(attachment)) return false
        const idx = attachments.indexOf(attachment)
        if (idx < 0 || attachments[idx].kind !== 'staged') continue
        if (!attachment.file) {
          attachments[idx] = {
            kind: 'failed',
            local_id: attachment.local_id,
            name: attachment.name,
            mime: attachment.mime,
            size: attachment.size,
            error: 'Upload expired; select the file again',
          }
          pushToast(`Upload expired for ${attachment.name}: select the file again`, { tone: 'danger' })
          return false
        }
        refreshInFlightAttachments.add(attachment)
        if (composerOwned) composerRefreshInFlightCount.value += 1
        try {
          const meta = await uploadAttachmentFile(attachment.file, attachment.mime)
          if (!preparationIsCurrent()) return false
          const currentIdx = attachments.indexOf(attachment)
          if (currentIdx < 0 || attachments[currentIdx].kind !== 'staged') continue
          attachments[currentIdx] = {
            kind: 'staged',
            local_id: attachment.local_id,
            name: attachment.name,
            mime: attachment.mime,
            size: attachment.size,
            file_uuid: meta.fileUuid,
            expires_at: meta.expiresAt,
            ttl_seconds: meta.ttlSeconds,
            file: attachment.file,
          }
        } catch (err: unknown) {
          if (!preparationIsCurrent()) return false
          const message = uploadFailureMessage(err)
          markAttachmentFailed(
            attachment.local_id,
            attachment.file,
            attachment.mime,
            message,
            attachments,
            attachment,
          )
          pushToast(`${i18n.global.t('chat.toast.uploadFailed', { name: attachment.name })}: ${message}`, { tone: 'danger' })
          return false
        } finally {
          const removed = refreshInFlightAttachments.delete(attachment)
          if (composerOwned && removed && isAttachmentGenerationCurrent(generation)) {
            composerRefreshInFlightCount.value = Math.max(0, composerRefreshInFlightCount.value - 1)
          }
        }
      }
      return true
    } finally {
      if (composerPreparationInFlight === composerPreparation) {
        composerPreparationInFlight = null
      }
    }
  }

  function activeAttachmentCount(): number {
    return pendingAttachments.value.filter(attachmentCountsTowardLimits).length
  }

  function allocateAttachmentId(): number {
    const currentIds = new Set(pendingAttachments.value.map(attachment => attachment.local_id))
    while (currentIds.has(nextAttachmentId.value)) nextAttachmentId.value += 1
    return nextAttachmentId.value++
  }

  function canAcceptAttachment(fileName: string, size: number, batch: AttachmentBatch): boolean {
    if (!isAttachmentGenerationCurrent(batch.generation)) return false
    const activeAttachments = pendingAttachments.value.filter(attachmentCountsTowardLimits)
    if (activeAttachments.length >= MAX_ATTACHMENTS) {
      pushToast(i18n.global.t('chat.toast.tooManyAttachments', { max: MAX_ATTACHMENTS }), { tone: 'danger' })
      return false
    }
    const totalBytes = activeAttachments.reduce((sum, attachment) => sum + (attachment.size || 0), 0) + size
    if (totalBytes > MAX_TOTAL_ATTACHMENT_BYTES) {
      // Every file is still evaluated (a smaller later file may fit under the
      // total), but the rejection toasts once per batch.
      if (!batch.totalSizeToastShown) {
        batch.totalSizeToastShown = true
        pushToast(
          i18n.global.t('chat.toast.attachmentsTotalTooLarge', { name: fileName, max: formatMiB(MAX_TOTAL_ATTACHMENT_BYTES) }),
          { tone: 'danger' },
        )
      }
      return false
    }
    return true
  }

  function isAttachmentGenerationCurrent(generation: number): boolean {
    return generation === attachmentGeneration
  }

  return {
    pendingAttachments,
    attachmentWorkBusy,
    onFileInputChange,
    addAttachments,
    addAttachment,
    removeAttachment,
    retireAttachments,
    retryAttachment,
    hasPendingAttachmentWork,
    prepareAttachmentsForSend,
  }
}

function uploadFailureMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

function stagedUploadNeedsRefresh(attachment: Attachment): boolean {
  if (attachment.kind !== 'staged') return false
  if (typeof attachment.expires_at !== 'number' || !Number.isFinite(attachment.expires_at)) return false
  return attachment.expires_at * 1000 <= Date.now() + STAGED_UPLOAD_REFRESH_GRACE_MS
}

function attachmentCountsTowardLimits(attachment: Attachment): boolean {
  return attachment.kind !== 'failed'
}

function formatMiB(bytes: number): string {
  // Floor to one decimal for caps that are not whole MiB (the 2,000,000-byte
  // email cap) so the stated limit never exceeds the enforced one.
  const mib = bytes / 1024 / 1024
  return `${Number.isInteger(mib) ? mib : Math.floor(mib * 10) / 10} MiB`
}
