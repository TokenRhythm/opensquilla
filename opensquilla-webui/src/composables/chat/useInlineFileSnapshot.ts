import { computed, inject, onScopeDispose, readonly, ref, watch } from 'vue'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import type { ToolFileSnapshot } from '@/types/chat'
import { boundedTextPreview } from '@/utils/chat/toolContentPreview'
import { readInlineToolContent, reserveInlineToolPreview, type InlineToolPreviewReservation } from './inlineToolContentBudget'

/** Mounted only inside an expanded tool row; closing it cancels its read. */
export function useInlineFileSnapshot(options: {
  sessionKey: () => string | undefined
  toolId: () => string | undefined
  snapshot: () => ToolFileSnapshot | undefined
  enabled: () => boolean
}) {
  const workbench = inject(ARTIFACT_WORKBENCH_KEY, null)
  const content = ref<string | null>(null)
  const truncated = ref(false)
  const loading = ref(false)
  const error = ref(false)
  const source = computed(() => ({ sessionKey: options.sessionKey()?.trim() || '',
    toolId: options.toolId() || '', snapshot: options.snapshot(), enabled: options.enabled() }))
  const key = computed(() => JSON.stringify([source.value.sessionKey, source.value.toolId,
    source.value.snapshot?.sha256_ref, source.value.snapshot?.legacy_ref, source.value.snapshot?.bytes,
    source.value.snapshot?.path, source.value.enabled]))
  const required = computed(() => Boolean(source.value.enabled
    && (source.value.snapshot?.sha256_ref || source.value.snapshot?.legacy_ref?.sha256)))
  const previewReady = computed(() => content.value !== null)
  let controller: AbortController | null = null
  let generation = 0
  let disposed = false
  let pending: Promise<boolean> | null = null
  let reservation: InlineToolPreviewReservation | null = null

  function cancel() {
    generation += 1
    controller?.abort()
    controller = null
    pending = null
    reservation?.release()
    reservation = null
    content.value = null
    truncated.value = false
    loading.value = false
    error.value = false
  }
  async function retry(): Promise<boolean> {
    cancel()
    content.value = null
    error.value = false
    loading.value = false
    if (disposed || !required.value) return false
    const { sessionKey, snapshot } = source.value
    const digest = snapshot?.sha256_ref || snapshot?.legacy_ref?.sha256
    if (!workbench?.content.readAttachmentPreview || !sessionKey || !snapshot || !digest) { error.value = true; return false }
    const legacy = snapshot.sha256_ref ? undefined : snapshot.legacy_ref
    const previewReservation = reserveInlineToolPreview()
    reservation = previewReservation
    const request = new AbortController()
    controller = request
    const requestGeneration = generation
    const requestKey = key.value
    const current = () => !disposed && !request.signal.aborted && generation === requestGeneration && key.value === requestKey
    loading.value = true
    const read = async () => {
      try {
        const capacity = previewReservation.prepare(request.signal)
        if (capacity) await capacity
        if (!current()) return false
        const body = await readInlineToolContent(request.signal, () => {
          if (!current()) throw new DOMException('Aborted', 'AbortError')
          return workbench.content.readAttachmentPreview!({ kind: 'file',
            displayId: digest, renderKey: digest, name: snapshot.path,
            mime: 'text/plain', size: snapshot.bytes, sha256_ref: digest,
            ...(legacy ? { fileSnapshotRef: { messageId: legacy.message_id, toolUseId: legacy.tool_use_id } } : {}) },
          { sessionKey, signal: request.signal })
        })
        if (body === null || !current()) return false
        if (snapshot.bytes !== undefined && body.totalBytes !== snapshot.bytes) throw new Error('Incomplete file snapshot')
        const preview = boundedTextPreview(body.content)
        content.value = preview.content
        truncated.value = body.truncated || preview.truncated
        return true
      } catch {
        if (current()) error.value = true
        return false
      } finally {
        if (current()) {
          previewReservation.retain(content.value || '')
          loading.value = false
        }
      }
    }
    pending = read()
    return pending
  }
  async function ensurePreview(): Promise<boolean> {
    if (!required.value) return true
    if (content.value !== null) return true
    return pending ? pending : false
  }
  async function ensureComplete(): Promise<boolean> {
    return (await ensurePreview()) && !truncated.value
  }
  watch(key, () => { void retry() }, { immediate: true })
  onScopeDispose(() => { disposed = true; cancel() })
  return { content: readonly(content), truncated: readonly(truncated), previewReady,
    loading: readonly(loading), error: readonly(error), required, retry, ensurePreview, ensureComplete }
}
