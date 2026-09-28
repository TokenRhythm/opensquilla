import { inject, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { useToasts } from '@/composables/useToasts'
import {
  abortableClipboardPreparation,
  assertClipboardPreparationActive,
  IMAGE_CLIPBOARD_MAX_BYTES,
  IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS,
  imageClipboardSourceIdentity,
  ImageClipboardError,
  prepareClipboardBlob,
  SVG_CLIPBOARD_MAX_BYTES,
  writePreparedClipboard,
  type ClipboardImageSource,
  type ImageClipboardMode,
} from '@/utils/imageClipboard'

export { isClipboardImageCandidate, isSvgClipboardCandidate } from '@/utils/imageClipboard'
export type { ClipboardImageSource } from '@/utils/imageClipboard'

export function useImageClipboard(options: {
  source: () => ClipboardImageSource | null
  sessionKey: () => string
  loadBlob?: (signal: AbortSignal, maxBytes: number) => Promise<Blob>
}) {
  const workbench = inject(ARTIFACT_WORKBENCH_KEY, null)
  const gateway = inject(GATEWAY_ACCESS_KEY, null)
  const { t } = useI18n()
  const { pushToast } = useToasts()
  const busy = ref(false)
  let pending: AbortController | null = null
  let generation = 0
  let timeout: ReturnType<typeof setTimeout> | undefined

  function clearTimer() {
    if (timeout !== undefined) clearTimeout(timeout)
    timeout = undefined
  }

  function cancel() {
    generation++
    pending?.abort()
    pending = null
    clearTimer()
    busy.value = false
  }

  const identity = () => [options.sessionKey(), gateway?.subscriptionEpoch,
    ...imageClipboardSourceIdentity(options.source())]
  watch(identity, (next, previous) => {
    if (next.length !== previous.length || next.some((value, index) => value !== previous[index])) cancel()
  }, { flush: 'sync' })
  onBeforeUnmount(cancel)

  async function copy(mode: ImageClipboardMode): Promise<boolean> {
    const selected = options.source()
    if (!selected || busy.value) return false
    const source: ClipboardImageSource = selected.kind === 'artifact'
      ? { kind: 'artifact', artifact: { ...selected.artifact } }
      : { kind: 'attachment', attachment: { ...selected.attachment } }
    const descriptor = source.kind === 'artifact' ? source.artifact : source.attachment
    const sessionKey = options.sessionKey()
    const epoch = gateway?.subscriptionEpoch
    const attempt = ++generation
    const controller = new AbortController()
    pending = controller
    busy.value = true
    let preparationError: unknown
    const assertCurrent = () => {
      assertClipboardPreparationActive(controller.signal)
      if (attempt !== generation || sessionKey !== options.sessionKey()
        || epoch !== gateway?.subscriptionEpoch) throw new DOMException('Cancelled', 'AbortError')
    }
    timeout = setTimeout(() => controller.abort(new ImageClipboardError('timedOut')),
      IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS)
    try {
      // Do not await the fetch before writing: ClipboardItem owns the preparation
      // promise so the browser observes the original click's user activation.
      await abortableClipboardPreparation(writePreparedClipboard(mode, async () => {
        try {
          assertCurrent()
          const maxBytes = mode === 'image' ? IMAGE_CLIPBOARD_MAX_BYTES : SVG_CLIPBOARD_MAX_BYTES
          let blob: Blob
          if (options.loadBlob) {
            blob = await abortableClipboardPreparation(options.loadBlob(controller.signal, maxBytes), controller.signal)
          } else {
            if (!workbench) throw new ImageClipboardError('failed')
            const request = { sessionKey, signal: controller.signal, maxBytes }
            const result = await abortableClipboardPreparation(source.kind === 'artifact'
              ? workbench.content.fetchArtifact(source.artifact, request)
              : workbench.content.fetchAttachment(source.attachment, request), controller.signal)
            if (!result.ok) throw new ImageClipboardError(result.errorCode === 'too_large' ? 'tooLarge' : 'failed')
            blob = result.blob
          }
          assertCurrent()
          const prepared = await prepareClipboardBlob(blob, descriptor, mode, controller.signal)
          assertCurrent()
          return prepared
        } catch (error) {
          preparationError = error
          throw error
        }
      }), controller.signal)
      assertCurrent()
      pushToast(t(mode === 'image' ? 'imageClipboard.copiedImage' : 'imageClipboard.copiedSource'), { tone: 'ok' })
      return true
    } catch (error) {
      const failure = preparationError ?? error
      if (attempt === generation && !(failure instanceof DOMException && failure.name === 'AbortError')) {
        const kind = failure instanceof ImageClipboardError ? failure.kind : 'failed'
        pushToast(t(`imageClipboard.${kind}`), { tone: 'danger' })
      }
      return false
    } finally {
      controller.abort()
      if (attempt === generation) {
        clearTimer()
        pending = null
        busy.value = false
      }
    }
  }

  return { busy, copy, cancel }
}
