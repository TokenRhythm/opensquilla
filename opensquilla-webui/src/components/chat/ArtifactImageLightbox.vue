<template>
  <Teleport to="body">
    <div
      v-if="active"
      class="deliv-preview"
      role="dialog"
      aria-modal="true"
      :aria-label="t('chat.previewOf', { title: imageTitle(active) })"
      @click.self="closePreview"
    >
      <div ref="lightboxPanel" class="deliv-preview__panel deliv-preview__panel--media">
        <header class="deliv-preview__head">
          <span
            class="deliv-preview__title"
            aria-live="polite"
            aria-atomic="true"
          >
            {{ imageTitle(active) }}
          </span>
          <button
            ref="lightboxCloseBtn"
            type="button"
            class="btn btn--icon btn--ghost"
            :aria-label="t('chat.closePreview')"
            :title="t('chat.closePreview')"
            @click="closePreview"
          >
            <Icon name="x" :size="16" />
          </button>
        </header>
        <div class="deliv-preview__body">
          <button
            v-if="canNavigateImages"
            type="button"
            class="deliv-preview__nav deliv-preview__nav--prev"
            :aria-label="t('chat.previousImage')"
            :title="t('chat.previousImage')"
            :disabled="!canGoPreviousImage"
            @click="showPreviousImage"
          >
            <Icon name="chevronRight" :size="22" />
          </button>
          <img
            v-if="fullState === 'loaded' && fullUrl"
            class="deliv-preview__image"
            :src="fullUrl"
            :alt="imageTitle(active)"
            decoding="async"
          />
          <div
            v-else-if="fullState === 'timeout' || fullState === 'error'"
            class="deliv-preview__file"
            role="status"
          >
            <p class="deliv-preview__meta">
              {{ fullState === 'timeout' ? t('chat.previewTimedOut') : t('chat.previewFailed') }}
            </p>
            <button type="button" class="btn btn--ghost" @click="retryFull">
              <Icon name="refresh" :size="14" />
              <span>{{ t('chat.retry') }}</span>
            </button>
          </div>
          <div
            v-else
            class="deliv-preview__loading"
            role="status"
            :aria-label="t('chat.loadingPreview')"
          >
            <div
              v-if="fullProgress !== null"
              class="deliv-preview__progress"
              role="progressbar"
              :aria-label="t('chat.previewDownload')"
              :aria-valuenow="fullProgress ?? 0"
              aria-valuemin="0"
              aria-valuemax="100"
            >
              <span class="deliv-preview__progress-bar" :style="{ width: `${fullProgress}%` }" />
            </div>
            <span v-else class="deliv-preview__progress-shimmer" aria-hidden="true" />
          </div>
          <button
            v-if="canNavigateImages"
            type="button"
            class="deliv-preview__nav deliv-preview__nav--next"
            :aria-label="t('chat.nextImage')"
            :title="t('chat.nextImage')"
            :disabled="!canGoNextImage"
            @click="showNextImage"
          >
            <Icon name="chevronRight" :size="22" />
          </button>
        </div>
        <footer class="deliv-preview__actions">
          <button type="button" class="btn btn--primary" @click="downloadActive">
            <Icon name="download" :size="14" />
            <span>{{ t('chat.download') }}</span>
          </button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useArtifactImageLightbox, type ImageLightboxItem } from '@/composables/chat/useArtifactImageLightbox'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useToasts } from '@/composables/useToasts'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  ARTIFACT_WORKBENCH_KEY,
  type ArtifactPreviewController,
  type ArtifactPreviewState,
} from '@/modules/artifactWorkbench'
import { isImageAttachmentMime, isImageDisplayAttachment } from '@/utils/chat/attachments'
import {
  artifactCategory,
  artifactFileTitle,
} from '@/utils/chat/artifacts'
import { downloadBlob } from '@/utils/browser'

const { t } = useI18n()
const { pushToast } = useToasts()
const controller = useArtifactImageLightbox()
const injectedArtifactWorkbench = inject(ARTIFACT_WORKBENCH_KEY)
if (!injectedArtifactWorkbench) throw new Error('ArtifactWorkbench was not provided')
const artifactWorkbench = injectedArtifactWorkbench
const active = computed(() => controller.request.value?.image ?? null)
const isOpen = computed(() => active.value !== null)
const lightboxIsTopmost = useDialogLayer(isOpen)
const lightboxCloseBtn = ref<HTMLButtonElement | null>(null)
const lightboxPanel = ref<HTMLElement | null>(null)

let fullController: ArtifactPreviewController | null = null
const fullState = ref<ArtifactPreviewState>('idle')
const fullProgress = ref<number | null>(null)
const fullUrl = ref('')
let stopFullState: (() => void) | null = null

function artifactKey(artifact: ArtifactPayload): string {
  return String(
    artifact.id
      || artifact.key
      || artifact.download_url
      || `${artifact.name || 'artifact'}:${artifact.mime || ''}:${artifact.size || ''}`,
  )
}

function imageKey(image: ImageLightboxItem): string {
  return image.kind === 'artifact'
    ? `artifact:${artifactKey(image.artifact)}`
    : `attachment:${image.attachment.renderKey}`
}

function imageTitle(image: ImageLightboxItem): string {
  return image.kind === 'artifact' ? artifactFileTitle(image.artifact) : image.attachment.name
}

const navigationVisualArtifacts = computed(() => {
  const request = controller.request.value
  if (!request) return []
  const seen = new Set<string>()
  const images: ImageLightboxItem[] = []
  for (const image of request.navigationImages) {
    if (image.kind === 'artifact'
      ? artifactCategory(image.artifact) !== 'visual'
      : !isImageDisplayAttachment(image.attachment)) continue
    const key = imageKey(image)
    if (!key || seen.has(key)) continue
    seen.add(key)
    images.push(image)
  }
  if (!seen.has(imageKey(request.image))) images.push(request.image)
  return images
})

const activeImageIndex = computed(() => {
  if (!active.value) return -1
  const key = imageKey(active.value)
  return navigationVisualArtifacts.value.findIndex(image => imageKey(image) === key)
})
const canNavigateImages = computed(() => navigationVisualArtifacts.value.length > 1)
const canGoPreviousImage = computed(() => activeImageIndex.value > 0)
const canGoNextImage = computed(() =>
  activeImageIndex.value >= 0
  && activeImageIndex.value < navigationVisualArtifacts.value.length - 1)

function disposeFull() {
  stopFullState?.()
  stopFullState = null
  fullController?.dispose()
  fullController = null
  fullState.value = 'idle'
  fullProgress.value = null
  fullUrl.value = ''
}

function loadFull(image: ImageLightboxItem, sessionKey: string) {
  disposeFull()
  fullController = artifactWorkbench.previews.create({
    sessionKey: () => sessionKey,
    variant: 'content',
    fullSize: true,
    ...(image.kind === 'attachment' ? {
      loadBlob: async (signal: AbortSignal) => {
        const result = await artifactWorkbench.content.fetchAttachment(image.attachment, {
          sessionKey,
          signal,
        })
        if (!result.ok) throw new Error(result.message)
        if (result.source === 'local-file'
          && (!result.blob.type || result.blob.type === 'application/octet-stream')) {
          return result.blob.slice(0, result.blob.size, image.attachment.mime)
        }
        return result.blob
      },
      acceptBlob: (blob: Blob) => isImageAttachmentMime(blob.type),
    } : { artifact: () => image.artifact }),
  })
  const preview = fullController
  stopFullState = watch(
    [preview.state, preview.progress, preview.objectUrl],
    ([state, progress, objectUrl]) => {
      fullState.value = state as ArtifactPreviewState
      fullProgress.value = (progress as number | null) ?? null
      fullUrl.value = (objectUrl as string) || ''
    },
    { immediate: true },
  )
  preview.load()
}

function retryFull() {
  fullController?.retry()
}

function showImageAt(index: number) {
  const artifact = navigationVisualArtifacts.value[index]
  if (artifact) controller.show(artifact)
}

function showPreviousImage() {
  if (canGoPreviousImage.value) showImageAt(activeImageIndex.value - 1)
}

function showNextImage() {
  if (canGoNextImage.value) showImageAt(activeImageIndex.value + 1)
}

function closePreview() {
  const invoker = controller.request.value?.invoker ?? null
  controller.close()
  disposeFull()
  nextTick(() => {
    if (invoker && document.contains(invoker)) invoker.focus()
  })
}

function trapLightboxFocus(event: KeyboardEvent) {
  const root = lightboxPanel.value
  if (!root) return
  const focusables = Array.from(root.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const activeElement = document.activeElement as HTMLElement | null
  const inside = !!activeElement && root.contains(activeElement)
  if (event.shiftKey && (!inside || activeElement === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || activeElement === last)) {
    event.preventDefault()
    first.focus()
  }
}

function onLightboxKeydown(event: KeyboardEvent) {
  if (!active.value || !lightboxIsTopmost.value) return
  if (event.key === 'Escape') {
    event.stopPropagation()
    event.preventDefault()
    closePreview()
    return
  }
  if (event.key === 'ArrowLeft') {
    if (canGoPreviousImage.value) {
      event.preventDefault()
      showPreviousImage()
    }
    return
  }
  if (event.key === 'ArrowRight') {
    if (canGoNextImage.value) {
      event.preventDefault()
      showNextImage()
    }
    return
  }
  if (event.key === 'Tab') trapLightboxFocus(event)
}

async function downloadActive() {
  const request = controller.request.value
  if (!request) return
  const options = {
    sessionKey: request.sessionKey,
  }
  const result = request.image.kind === 'artifact'
    ? await artifactWorkbench.content.fetchArtifact(request.image.artifact, options)
    : await artifactWorkbench.content.fetchAttachment(request.image.attachment, options)
  if (!result.ok) {
    pushToast(result.message || t('chat.toast.downloadFailed'), { tone: 'danger' })
    return
  }
  const filename = 'filename' in result && typeof result.filename === 'string'
    ? result.filename
    : imageTitle(request.image)
  downloadBlob(result.blob, filename)
}

const activeResource = computed(() => {
  const request = controller.request.value
  return request?.image ?? null
})

watch(
  activeResource,
  (image, previousImage) => {
    const request = controller.request.value
    if (!image || !request) {
      disposeFull()
      return
    }
    loadFull(image, request.sessionKey)
    if (!previousImage) nextTick(() => lightboxCloseBtn.value?.focus())
  },
  { immediate: true },
)

useDocumentEvent('keydown', onLightboxKeydown)
onUnmounted(() => {
  disposeFull()
})
</script>
