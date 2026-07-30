<template>
  <Teleport to="body">
    <div
      v-if="active"
      class="deliv-preview"
      role="dialog"
      aria-modal="true"
      :aria-label="t('chat.previewOf', { title: artifactFileTitle(active) })"
      @click.self="closePreview"
    >
      <div ref="lightboxPanel" class="deliv-preview__panel deliv-preview__panel--media">
        <header class="deliv-preview__head">
          <span
            class="deliv-preview__title"
            aria-live="polite"
            aria-atomic="true"
          >
            {{ artifactFileTitle(active) }}
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
            :alt="artifactFileTitle(active)"
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
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  createArtifactPreview,
  type ArtifactPreviewController,
  type ArtifactPreviewState,
} from '@/composables/chat/useArtifactPreview'
import { useArtifactImageLightbox } from '@/composables/chat/useArtifactImageLightbox'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useToasts } from '@/composables/useToasts'
import type { ArtifactPayload } from '@/types/rpc'
import { fetchArtifactBlob } from '@/utils/chat/artifactAccess'
import {
  artifactCategory,
  artifactDownloadUrl,
  artifactFileTitle,
} from '@/utils/chat/artifacts'
import { downloadBlob } from '@/utils/browser'

const { t } = useI18n()
const { pushToast } = useToasts()
const controller = useArtifactImageLightbox()
const active = computed(() => controller.request.value?.artifact ?? null)
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

const navigationVisualArtifacts = computed(() => {
  const request = controller.request.value
  if (!request) return []
  const seen = new Set<string>()
  const images: ArtifactPayload[] = []
  for (const artifact of request.navigationArtifacts) {
    if (artifactCategory(artifact) !== 'visual') continue
    const key = artifactKey(artifact)
    if (!key || seen.has(key)) continue
    seen.add(key)
    images.push(artifact)
  }
  if (!seen.has(artifactKey(request.artifact))) images.push(request.artifact)
  return images
})

const activeImageIndex = computed(() => {
  if (!active.value) return -1
  const key = artifactKey(active.value)
  return navigationVisualArtifacts.value.findIndex(artifact => artifactKey(artifact) === key)
})
const canNavigateImages = computed(() => navigationVisualArtifacts.value.length > 1)
const canGoPreviousImage = computed(() => activeImageIndex.value > 0)
const canGoNextImage = computed(() =>
  activeImageIndex.value >= 0
  && activeImageIndex.value < navigationVisualArtifacts.value.length - 1)

function readAuthToken(): string {
  if (typeof sessionStorage === 'undefined') return ''
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

function sameOrigin(url: string): boolean {
  try {
    return new URL(url, window.location.origin).origin === window.location.origin
  } catch {
    return false
  }
}

function previewHeaders(url: string, sessionKey: string): Record<string, string> {
  if (!sameOrigin(url)) return {}
  const headers: Record<string, string> = {}
  if (sessionKey) headers['x-opensquilla-session-key'] = sessionKey
  const authToken = readAuthToken()
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return headers
}

function disposeFull() {
  stopFullState?.()
  stopFullState = null
  fullController?.dispose()
  fullController = null
  fullState.value = 'idle'
  fullProgress.value = null
  fullUrl.value = ''
}

function loadFull(artifact: ArtifactPayload, sessionKey: string) {
  disposeFull()
  const url = artifactDownloadUrl(artifact, window.location.origin, {
    sessionKey,
    includeSessionKey: false,
  })
  fullController = createArtifactPreview({
    resolveUrl: () => url,
    headers: () => previewHeaders(url, sessionKey),
    sameOrigin,
    fullSize: true,
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
  const result = await fetchArtifactBlob(request.artifact, {
    authToken: readAuthToken(),
    baseOrigin: window.location.origin,
    sessionKey: request.sessionKey,
  })
  if (!result.ok) {
    pushToast(result.message || t('chat.toast.downloadFailed'), { tone: 'danger' })
    return
  }
  downloadBlob(result.blob, String(request.artifact.name || artifactFileTitle(request.artifact)))
}

const activeResourceSignature = computed(() => {
  const request = controller.request.value
  if (!request) return ''
  return [
    request.sessionKey,
    artifactKey(request.artifact),
    String(request.artifact.download_url || ''),
  ].join('\u0000')
})

watch(
  activeResourceSignature,
  (signature, previousSignature) => {
    const request = controller.request.value
    if (!signature || !request) {
      disposeFull()
      return
    }
    loadFull(request.artifact, request.sessionKey)
    if (!previousSignature) nextTick(() => lightboxCloseBtn.value?.focus())
  },
  { immediate: true },
)

useDocumentEvent('keydown', onLightboxKeydown)
onUnmounted(() => {
  disposeFull()
})
</script>
