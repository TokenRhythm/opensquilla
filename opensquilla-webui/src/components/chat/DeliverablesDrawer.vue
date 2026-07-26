<template>
  <div v-if="open" class="deliv-overlay" @click.self="emit('close')">
    <aside
      ref="drawerRef"
      class="deliv-drawer"
      role="dialog"
      aria-modal="true"
      :aria-label="t('chat.deliverablesCount', { count: artifacts.length })"
    >
      <header class="deliv-head">
        <h3 class="deliv-head__title">{{ t('chat.deliverables') }}</h3>
        <span class="deliv-head__count" aria-hidden="true">{{ artifacts.length }}</span>
        <button
          ref="closeBtn"
          type="button"
          class="btn btn--icon btn--ghost"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          @click="emit('close')"
        >
          <Icon name="x" :size="16" />
        </button>
      </header>

      <div class="deliv-body" :aria-label="t('chat.sessionDeliverables')">
        <p v-if="artifacts.length === 0" class="deliv-empty">{{ t('chat.noDeliverables') }}</p>
        <ul v-else class="deliv-grid">
          <li v-for="artifact in artifacts" :key="artifactKey(artifact)" class="deliv-tile-wrap">
            <button
              type="button"
              class="deliv-tile"
              :title="artifactFileTitle(artifact)"
              :aria-label="t('chat.openArtifact', { title: artifactFileTitle(artifact), subtitle: artifactFileSubtitle(artifact) })"
              @click="openPreview(artifact)"
            >
              <span class="deliv-tile__thumb" :data-kind="artifactCategory(artifact)">
                <!-- Thumbnail is lazy + concurrency-capped: it only fetches once
                     the tile scrolls into view. Non-image / failed thumbs fall
                     back to the category glyph. -->
                <img
                  v-if="isVisual(artifact) && tileThumbState(artifact) === 'loaded' && tileThumbUrl(artifact)"
                  :src="tileThumbUrl(artifact)"
                  :alt="artifactFileTitle(artifact)"
                  decoding="async"
                />
                <span
                  v-else-if="isVisual(artifact) && tileThumbState(artifact) === 'loading'"
                  :ref="el => registerTileThumb(artifact, el)"
                  class="deliv-tile__thumb-skeleton"
                  aria-hidden="true"
                />
                <Icon
                  v-else
                  :ref="el => registerTileThumb(artifact, el)"
                  :name="artifactIconName(artifact)"
                  :size="26"
                />
              </span>
              <span class="deliv-tile__name">{{ artifactFileTitle(artifact) }}</span>
              <span class="deliv-tile__meta">{{ artifactFileSubtitle(artifact) }}</span>
            </button>
          </li>
        </ul>
      </div>
    </aside>

    <!-- Larger preview: image lightbox, or metadata + download for non-images -->
    <div
      v-if="active"
      class="deliv-preview"
      role="dialog"
      aria-modal="true"
      :aria-label="t('chat.previewOf', { title: artifactFileTitle(active) })"
      @click.self="closePreview"
    >
      <div class="deliv-preview__panel" :class="{ 'deliv-preview__panel--media': isVisual(active) }">
        <header class="deliv-preview__head">
          <span class="deliv-preview__title">{{ artifactFileTitle(active) }}</span>
          <button
            ref="previewCloseBtn"
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
            v-if="isVisual(active) && fullState === 'loaded' && fullUrl"
            class="deliv-preview__image"
            :src="fullUrl"
            :alt="artifactFileTitle(active)"
            decoding="async"
          />
          <div
            v-else-if="isVisual(active) && (fullState === 'timeout' || fullState === 'error')"
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
            v-else-if="isVisual(active)"
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
          <div v-else class="deliv-preview__file">
            <span class="deliv-preview__icon" :data-kind="artifactCategory(active)" aria-hidden="true">
              <Icon :name="artifactIconName(active)" :size="40" />
            </span>
            <p class="deliv-preview__meta">{{ artifactFileSubtitle(active) }}</p>
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
          <button type="button" class="btn btn--primary" @click="emit('download', active)">
            <Icon name="download" :size="14" />
            <span>{{ t('chat.download') }}</span>
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ArtifactPayload } from '@/types/rpc'
import { useDialogLayer } from '@/composables/useDialogA11y'

const { t } = useI18n()
import {
  createArtifactPreview,
  type ArtifactPreviewController,
  type ArtifactPreviewState,
} from '@/composables/chat/useArtifactPreview'
import {
  artifactCategory,
  artifactDownloadUrl,
  artifactFileSubtitle,
  artifactFileTitle,
  artifactIconName,
  artifactThumbnailUrl,
} from '@/utils/chat/artifacts'

const props = defineProps<{
  open: boolean
  artifacts: ArtifactPayload[]
  sessionKey?: string
  authToken?: string
}>()

const emit = defineEmits<{
  close: []
  download: [artifact: ArtifactPayload]
}>()

const drawerRef = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)
const previewCloseBtn = ref<HTMLButtonElement | null>(null)
const active = ref<ArtifactPayload | null>(null)
const drawerIsTopmost = useDialogLayer(computed(() => props.open))
const previewIsTopmost = useDialogLayer(computed(() => props.open && active.value !== null))

let invokerEl: HTMLElement | null = null

const visualArtifacts = computed(() => props.artifacts.filter(isVisual))
const activeImageIndex = computed(() => {
  const current = active.value
  if (!isVisual(current)) return -1
  const currentKey = artifactKey(current)
  return visualArtifacts.value.findIndex(artifact => artifactKey(artifact) === currentKey)
})
const canNavigateImages = computed(() => isVisual(active.value) && visualArtifacts.value.length > 1)
const canGoPreviousImage = computed(() => activeImageIndex.value > 0)
const canGoNextImage = computed(() =>
  activeImageIndex.value >= 0 && activeImageIndex.value < visualArtifacts.value.length - 1)

function artifactKey(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.download_url || artifact.name || '')
}

function isVisual(artifact: ArtifactPayload | null): artifact is ArtifactPayload {
  return !!artifact && artifactCategory(artifact) === 'visual'
}

function sameOrigin(url: string): boolean {
  try {
    return new URL(url, window.location.origin).origin === window.location.origin
  } catch { return false }
}

function previewHeaders(url: string): Record<string, string> {
  if (!sameOrigin(url)) return {}
  const headers: Record<string, string> = {}
  if (props.sessionKey) headers['x-opensquilla-session-key'] = props.sessionKey
  if (props.authToken) headers.Authorization = `Bearer ${props.authToken}`
  return headers
}

/* ── Tile thumbnails: lazy + capped, small bytes per tile ──────────────────
   Each visual tile fetches the `variant=thumb` webp (or the full image when no
   thumbnail exists) only when it scrolls into view. Bytes are rendered as a
   revocable blob via URL.createObjectURL(blob) inside the shared controller. */

const tileControllers = new Map<string, ArtifactPreviewController>()

function tileController(artifact: ArtifactPayload): ArtifactPreviewController {
  const key = artifactKey(artifact)
  let controller = tileControllers.get(key)
  if (!controller) {
    controller = createArtifactPreview({
      resolveUrl: () => artifactThumbnailUrl(artifact, window.location.origin, {
        sessionKey: props.sessionKey,
        includeSessionKey: false,
      }),
      headers: () => previewHeaders(artifactThumbnailUrl(artifact, window.location.origin, {
        sessionKey: props.sessionKey,
        includeSessionKey: false,
      })),
      sameOrigin,
      fullSize: false,
    })
    tileControllers.set(key, controller)
  }
  return controller
}

function registerTileThumb(artifact: ArtifactPayload, el: unknown) {
  const target = el && typeof el === 'object' && '$el' in el
    ? (el as { $el: unknown }).$el
    : el
  tileController(artifact).observe(target instanceof Element ? target : null)
}

function tileThumbState(artifact: ArtifactPayload): ArtifactPreviewState {
  return tileController(artifact).state.value as ArtifactPreviewState
}

function tileThumbUrl(artifact: ArtifactPayload): string {
  return tileController(artifact).objectUrl.value || ''
}

function disposeTileControllers() {
  for (const controller of tileControllers.values()) controller.dispose()
  tileControllers.clear()
}

/* ── Full image (lightbox): fetched only on Open, bounded by the LRU ──────── */

let fullController: ArtifactPreviewController | null = null
const fullState = ref<ArtifactPreviewState>('idle')
const fullProgress = ref<number | null>(null)
const fullUrl = ref<string>('')

let stopFullState: (() => void) | null = null

function disposeFull() {
  stopFullState?.()
  stopFullState = null
  fullController?.dispose()
  fullController = null
  fullState.value = 'idle'
  fullProgress.value = null
  fullUrl.value = ''
}

function loadFull(artifact: ArtifactPayload) {
  disposeFull()
  fullController = createArtifactPreview({
    resolveUrl: () => artifactDownloadUrl(artifact, window.location.origin, {
      sessionKey: props.sessionKey,
      includeSessionKey: false,
    }),
    headers: () => previewHeaders(artifactDownloadUrl(artifact, window.location.origin, {
      sessionKey: props.sessionKey,
      includeSessionKey: false,
    })),
    sameOrigin,
    fullSize: true,
  })
  const ctrl = fullController
  stopFullState = watch(
    [ctrl.state, ctrl.progress, ctrl.objectUrl],
    ([s, p, u]) => {
      fullState.value = s as ArtifactPreviewState
      fullProgress.value = (p as number | null) ?? null
      fullUrl.value = (u as string) || ''
    },
    { immediate: true },
  )
  ctrl.load()
}

function retryFull() {
  fullController?.retry()
}

function showImageAt(index: number) {
  const artifact = visualArtifacts.value[index]
  if (!artifact) return
  active.value = artifact
  loadFull(artifact)
}

function showPreviousImage() {
  if (!canGoPreviousImage.value) return
  showImageAt(activeImageIndex.value - 1)
}

function showNextImage() {
  if (!canGoNextImage.value) return
  showImageAt(activeImageIndex.value + 1)
}

/* ── Preview (lightbox / metadata) ─────────────────────────────────────── */

function openPreview(artifact: ArtifactPayload) {
  active.value = artifact
  if (isVisual(artifact)) loadFull(artifact)
  else disposeFull()
  nextTick(() => previewCloseBtn.value?.focus())
}

function closePreview() {
  active.value = null
  disposeFull()
  nextTick(() => closeBtn.value?.focus())
}

/* ── Dialog a11y: focus trap, Escape, focus return ─────────────────────── */

function trapFocus(event: KeyboardEvent, rootEl: HTMLElement | null) {
  if (event.key !== 'Tab' || !rootEl) return
  const focusables = Array.from(rootEl.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const activeEl = document.activeElement as HTMLElement | null
  const inside = !!activeEl && rootEl.contains(activeEl)
  if (event.shiftKey && (!inside || activeEl === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || activeEl === last)) {
    event.preventDefault()
    first.focus()
  }
}

function onDocumentKeydown(event: KeyboardEvent) {
  if (!props.open) return
  if (active.value ? !previewIsTopmost.value : !drawerIsTopmost.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    if (active.value) closePreview()
    else emit('close')
    return
  }
  if (active.value && event.key === 'ArrowLeft') {
    if (canGoPreviousImage.value) {
      event.preventDefault()
      showPreviousImage()
    }
    return
  }
  if (active.value && event.key === 'ArrowRight') {
    if (canGoNextImage.value) {
      event.preventDefault()
      showNextImage()
    }
    return
  }
  // Trap focus inside whichever dialog is on top.
  if (active.value) {
    const panel = drawerRef.value?.parentElement?.querySelector<HTMLElement>('.deliv-preview__panel') || null
    trapFocus(event, panel)
  } else {
    trapFocus(event, drawerRef.value)
  }
}

watch(
  () => props.open,
  (open, wasOpen) => {
    if (open && !wasOpen) {
      invokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
      document.addEventListener('keydown', onDocumentKeydown)
      nextTick(() => closeBtn.value?.focus())
    } else if (!open && wasOpen) {
      document.removeEventListener('keydown', onDocumentKeydown)
      active.value = null
      disposeFull()
      disposeTileControllers()
      if (invokerEl && document.contains(invokerEl)) invokerEl.focus()
      invokerEl = null
    }
  },
)

const visualKeys = computed(() => visualArtifacts.value.map(artifactKey).join('|'))

// Drop tile controllers when the open drawer's artifact set or auth changes so
// their blob URLs are revoked promptly.
watch(
  () => [visualKeys.value, props.sessionKey || '', props.authToken || ''],
  () => { if (props.open) disposeTileControllers() },
)

onUnmounted(() => {
  document.removeEventListener('keydown', onDocumentKeydown)
  disposeFull()
  disposeTileControllers()
})
</script>
