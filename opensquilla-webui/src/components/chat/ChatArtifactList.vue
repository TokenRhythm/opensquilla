<template>
  <div v-if="artifacts.length" class="msg-artifacts">
    <!-- Image artifacts: one unified media card (thumbnail hero + caption bar). -->
    <TransitionGroup v-if="visualArtifacts.length" name="artifact-card" tag="div" class="msg-media-cards">
      <figure
        v-for="artifact in visualArtifacts"
        :key="`media-${artifactKey(artifact)}`"
        class="msg-media-card"
        :aria-label="t('chat.artifactTitleSubtitle', { title: artifactFileTitle(artifact), subtitle: artifactFileSubtitle(artifact) })"
      >
        <!-- Reserved aspect-ratio box: the preview only fetches once this scrolls
             into view (lazy), shows progress/skeleton while loading, and degrades
             to a retry card on timeout/error. -->
        <button
          v-if="previewStateFor(artifact) === 'loaded' && thumbUrlFor(artifact)"
          type="button"
          class="msg-media-card__img"
          :aria-label="t('chat.openTitle', { title: artifactFileTitle(artifact) })"
          @click="openPreview(artifact)"
        >
          <img
            :src="thumbUrlFor(artifact)"
            :alt="artifactFileTitle(artifact)"
            :data-artifact-key="artifactKey(artifact)"
            decoding="async"
          />
          <span class="msg-media-card__zoom" aria-hidden="true">
            <Icon name="externalLink" :size="16" />
          </span>
        </button>

        <div
          v-else-if="previewStateFor(artifact) === 'timeout' || previewStateFor(artifact) === 'error'"
          class="msg-media-card__img msg-media-card__img--error"
          role="status"
          :data-state="previewStateFor(artifact)"
        >
          <p class="msg-media-card__error-text">
            {{ previewStateFor(artifact) === 'timeout' ? t('chat.previewTimedOutShort') : t('chat.previewFailedShort') }}
          </p>
          <span class="msg-media-card__error-actions">
            <button
              type="button"
              class="msg-media-card__retry"
              :aria-label="t('chat.retryPreviewFor', { title: artifactFileTitle(artifact) })"
              @click="retryPreview(artifact)"
            >
              <Icon name="refresh" :size="14" />
              <span>{{ t('chat.retry') }}</span>
            </button>
            <button
              type="button"
              class="msg-media-card__retry"
              :aria-label="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
              @click="$emit('download', artifact)"
            >
              <Icon name="download" :size="14" />
              <span>{{ t('chat.download') }}</span>
            </button>
          </span>
        </div>

        <div
          v-else
          :ref="el => registerObserver(artifact, el)"
          class="msg-media-card__img msg-media-card__img--loading"
          role="status"
          :aria-label="t('chat.loadingPreview')"
        >
          <div
            v-if="previewProgressFor(artifact) !== null"
            class="msg-media-card__progress"
            role="progressbar"
            :aria-label="t('chat.previewDownload')"
            :aria-valuenow="previewProgressFor(artifact) ?? 0"
            aria-valuemin="0"
            aria-valuemax="100"
          >
            <span class="msg-media-card__progress-bar" :style="{ width: `${previewProgressFor(artifact)}%` }" />
          </div>
          <span v-else class="msg-media-card__skeleton" aria-hidden="true" />
        </div>

        <figcaption class="msg-media-card__cap">
          <span class="msg-media-card__name">{{ artifactFileTitle(artifact) }}</span>
          <span class="msg-media-card__meta">{{ artifactFileSubtitle(artifact) }}</span>
          <span class="msg-media-card__spacer" />
          <button
            type="button"
            class="msg-media-card__download"
            :aria-label="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
            @click="$emit('download', artifact)"
          >
            <Icon name="download" :size="16" />
          </button>
        </figcaption>
      </figure>
    </TransitionGroup>

    <!-- Audio artifacts fetch authenticated bytes only after an explicit Play. -->
    <TransitionGroup v-if="audioArtifacts.length" name="artifact-chip" tag="div" class="msg-artifact-files">
      <AudioArtifactCard
        v-for="artifact in audioArtifacts"
        :key="artifactKey(artifact)"
        :artifact="artifact"
        :session-key="sessionKey"
        :auth-token="authToken"
        @download="$emit('download', $event)"
      />
    </TransitionGroup>

    <!-- Non-image/non-audio artifacts: file cards with explicit actions. -->
    <TransitionGroup v-if="fileArtifacts.length" name="artifact-chip" tag="div" class="msg-artifact-files">
      <ArtifactChip
        v-for="artifact in fileArtifacts"
        :key="artifactKey(artifact)"
        :artifact="artifact"
        :category="artifactCategory(artifact)"
        :icon-name="artifactIconName(artifact)"
        :title="artifactFileTitle(artifact)"
        :kind-pill="artifactKindPill(artifact)"
        :size="artifactSizeLabel(artifact)"
        :previewable="artifactCanOpen(artifact)"
        :action-label="artifactChipActionLabel(artifact)"
        @open="openFile($event)"
        @download="$emit('download', $event)"
      />
    </TransitionGroup>

    <!-- In-app image lightbox: Open shows the full image here, not a new tab. -->
    <div
      v-if="active"
      class="deliv-preview"
      role="dialog"
      aria-modal="true"
      :aria-label="t('chat.previewOf', { title: artifactFileTitle(active) })"
      @click.self="closePreview"
    >
      <!-- Always media mode: this lightbox only ever opens image artifacts. -->
      <div ref="lightboxPanel" class="deliv-preview__panel deliv-preview__panel--media">
        <header class="deliv-preview__head">
          <span class="deliv-preview__title">{{ artifactFileTitle(active) }}</span>
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
              aria-label="Preview download"
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
          <button type="button" class="btn btn--primary" @click="$emit('download', active)">
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
import ArtifactChip from '@/components/chat/ArtifactChip.vue'
import AudioArtifactCard from '@/components/chat/AudioArtifactCard.vue'
import type { ArtifactPayload } from '@/types/rpc'
import { useToasts } from '@/composables/useToasts'
import { useDialogLayer } from '@/composables/useDialogA11y'
import {
  createArtifactPreview,
  type ArtifactPreviewController,
  type ArtifactPreviewState,
} from '@/composables/chat/useArtifactPreview'
import {
  fetchArtifactBlob,
  isActiveDocumentArtifactCandidate,
  openArtifactBlobUrl,
  openArtifactViaGateway,
} from '@/utils/chat/artifactAccess'
import { usePlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import {
  artifactCategory,
  artifactDownloadUrl,
  artifactFileSubtitle,
  artifactFileTitle,
  artifactIconName,
  artifactKindPill,
  artifactSizeLabel,
  artifactThumbnailUrl,
  canPreview,
} from '@/utils/chat/artifacts'

const props = defineProps<{
  artifacts: ArtifactPayload[]
  navigationArtifacts?: ArtifactPayload[]
  sessionKey?: string
  authToken?: string
}>()

defineEmits<{
  download: [artifact: ArtifactPayload]
}>()

const { t } = useI18n()
const { pushToast } = useToasts()
const platform = usePlatform()
const rpcStore = useRpcStore()

const visualArtifacts = computed(() => props.artifacts.filter(artifact => artifactCategory(artifact) === 'visual'))
const audioArtifacts = computed(() => props.artifacts.filter(artifact => artifactCategory(artifact) === 'audio'))
const navigationVisualArtifacts = computed(() => {
  const source = props.navigationArtifacts?.length ? props.navigationArtifacts : props.artifacts
  return source.filter(artifact => artifactCategory(artifact) === 'visual')
})
const fileArtifacts = computed(() => props.artifacts.filter(artifact => {
  const category = artifactCategory(artifact)
  return category !== 'visual' && category !== 'audio'
}))

function artifactKey(artifact: ArtifactPayload): string {
  return String(artifact.id || artifact.download_url || artifact.name || '')
}

const webOwnerCanNativeOpen = computed(() => {
  if (platform.capabilities.isDesktop) return false
  const auth = rpcStore.auth
  const principal = auth && typeof auth === 'object' ? auth.principal : null
  return Boolean(
    principal &&
    typeof principal === 'object' &&
    (principal as Record<string, unknown>).isOwner === true,
  )
})

function artifactCanOpen(artifact: ArtifactPayload): boolean {
  if (!canPreview(artifact)) return false
  if (!isActiveDocumentArtifactCandidate(artifact)) return true
  if (platform.capabilities.canOpenArtifactsNatively && platform.files.openArtifact) return true
  return webOwnerCanNativeOpen.value
}

function artifactChipActionLabel(artifact: ArtifactPayload): string {
  return artifactCanOpen(artifact) ? 'Open' : 'Download'
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

// Per-card thumbnail controllers. Each fetches the small `variant=thumb` webp
// (or the full image when no thumbnail exists) only after the card scrolls into
// view, through the shared concurrency-capped queue. The controller renders the
// fetched bytes as a revocable blob via URL.createObjectURL(blob); the full
// image is fetched separately only when Open is invoked.
const controllers = new Map<string, ArtifactPreviewController>()

function controllerFor(artifact: ArtifactPayload): ArtifactPreviewController {
  const key = artifactKey(artifact)
  let controller = controllers.get(key)
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
    controllers.set(key, controller)
  }
  return controller
}

function registerObserver(artifact: ArtifactPayload, el: unknown) {
  controllerFor(artifact).observe(el instanceof Element ? el : null)
}

function previewStateFor(artifact: ArtifactPayload): ArtifactPreviewState {
  return controllerFor(artifact).state.value as ArtifactPreviewState
}

function previewProgressFor(artifact: ArtifactPayload): number | null {
  return controllerFor(artifact).progress.value ?? null
}

function thumbUrlFor(artifact: ArtifactPayload): string {
  return controllerFor(artifact).objectUrl.value || ''
}

function retryPreview(artifact: ArtifactPayload) {
  controllerFor(artifact).retry()
}

// Open the image in an in-app lightbox (role=dialog) rather than navigating
// away. The lightbox fetches the FULL download URL (never the thumbnail);
// Download stays fully decoupled from this preview path.
function openPreview(artifact: ArtifactPayload) {
  lightboxInvoker = document.activeElement instanceof HTMLElement ? document.activeElement : null
  active.value = artifact
  loadFull(artifact)
  document.addEventListener('keydown', onLightboxKeydown)
  nextTick(() => lightboxCloseBtn.value?.focus())
}

// Open a previewable non-image file (pdf/html/text).
//
// Desktop: `window.open` is denied by the Electron shell handler, so the blob
// popup path below can never succeed. Fetch the bytes (with auth) and hand them
// to the main process, which writes a temp file and opens it with the OS
// default app. Web keeps the in-browser new-tab path and its active-document
// guard.
async function openFile(artifact: ArtifactPayload) {
  if (platform.capabilities.canOpenArtifactsNatively && platform.files.openArtifact) {
    const fetched = await fetchArtifactBlob(artifact, {
      baseOrigin: window.location.origin,
      sessionKey: props.sessionKey,
      authToken: props.authToken,
    })
    if (!fetched.ok) {
      pushToast(fetched.message, { tone: 'danger' })
      return
    }
    const data = await fetched.blob.arrayBuffer()
    const result = await platform.files.openArtifact({
      data,
      name: String(artifact.name || artifactFileTitle(artifact) || 'artifact'),
      mime: fetched.blob.type || String(artifact.mime || ''),
    })
    if (!result.ok) {
      pushToast(result.message || t('chat.toast.artifactOpenFailed'), { tone: 'danger' })
    }
    return
  }

  if (isActiveDocumentArtifactCandidate(artifact)) {
    const result = await openArtifactViaGateway(artifact, {
      baseOrigin: window.location.origin,
      sessionKey: props.sessionKey,
      authToken: props.authToken,
    })
    if (result.ok) return
    pushToast(result.message, { tone: 'danger' })
    return
  }

  const result = await openArtifactBlobUrl(artifact, {
    baseOrigin: window.location.origin,
    sessionKey: props.sessionKey,
    authToken: props.authToken,
  })
  if (result.ok) return
  pushToast(result.message, { tone: 'danger' })
}

// ── In-app image lightbox ──────────────────────────────────────────────────
// The full-size image is fetched only when Open is invoked, through the shared
// LRU-bounded controller; the thumbnail path above never loads the full bytes.
const active = ref<ArtifactPayload | null>(null)
const lightboxIsTopmost = useDialogLayer(computed(() => active.value !== null))
const lightboxCloseBtn = ref<HTMLButtonElement | null>(null)
const lightboxPanel = ref<HTMLElement | null>(null)
let lightboxInvoker: HTMLElement | null = null

const activeImageIndex = computed(() => {
  const current = active.value
  if (!current) return -1
  const currentKey = artifactKey(current)
  return navigationVisualArtifacts.value.findIndex(artifact => artifactKey(artifact) === currentKey)
})
const canNavigateImages = computed(() => navigationVisualArtifacts.value.length > 1)
const canGoPreviousImage = computed(() => activeImageIndex.value > 0)
const canGoNextImage = computed(() =>
  activeImageIndex.value >= 0 && activeImageIndex.value < navigationVisualArtifacts.value.length - 1)

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
  const artifact = navigationVisualArtifacts.value[index]
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

function closePreview() {
  if (!active.value) return
  active.value = null
  disposeFull()
  document.removeEventListener('keydown', onLightboxKeydown)
  const invoker = lightboxInvoker
  lightboxInvoker = null
  nextTick(() => {
    if (invoker && document.contains(invoker)) invoker.focus()
  })
}

function trapLightboxFocus(event: KeyboardEvent) {
  const rootEl = lightboxPanel.value
  if (!rootEl) return
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

function onLightboxKeydown(event: KeyboardEvent) {
  if (!active.value || !lightboxIsTopmost.value) return
  if (event.key === 'Escape') {
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

function disposeStaleControllers() {
  const live = new Set(visualArtifacts.value.map(artifactKey))
  for (const [key, controller] of controllers) {
    if (!live.has(key)) {
      controller.dispose()
      controllers.delete(key)
    }
  }
}

// When the artifact set or auth changes, drop controllers whose card is gone so
// their blob URLs are revoked promptly.
watch(
  () => [visualArtifacts.value.map(artifactKey).join('|'), props.sessionKey || '', props.authToken || ''],
  () => { disposeStaleControllers() },
)

onUnmounted(() => {
  document.removeEventListener('keydown', onLightboxKeydown)
  disposeFull()
  for (const controller of controllers.values()) controller.dispose()
  controllers.clear()
})
</script>

<style scoped>
.msg-artifacts {
  margin: var(--sp-3) 0 var(--sp-3);
}

.msg-artifact-files {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  width: 100%;
  margin: 0 auto;
}

.msg-media-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}

.msg-media-card {
  display: flex;
  flex-direction: column;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
}

.msg-media-card__img {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  /* Reserved box so the large image decode never causes layout shift. */
  aspect-ratio: 4 / 3;
  max-height: 320px;
  padding: 0;
  border: 0;
  background: var(--bg);
  cursor: zoom-in;
  overflow: hidden;
}

.msg-media-card__img--loading,
.msg-media-card__img--error {
  flex-direction: column;
  gap: var(--sp-2);
  cursor: default;
}

.msg-media-card__img img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.msg-media-card__skeleton {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    100deg,
    var(--bg) 30%,
    var(--bg-hover) 50%,
    var(--bg) 70%
  );
  background-size: 220% 100%;
  animation: mediaSkeleton 1.4s ease-in-out infinite;
}

.msg-media-card__progress {
  width: 64%;
  height: var(--sp-1);
  overflow: hidden;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
}

.msg-media-card__progress-bar {
  display: block;
  height: 100%;
  border-radius: var(--radius-full);
  background: var(--accent);
  transition: width var(--dur-base) var(--ease-standard);
}

.msg-media-card__error-text {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.msg-media-card__error-actions {
  display: inline-flex;
  gap: var(--sp-1);
}

.msg-media-card__retry {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: var(--sp-8);
  padding: 0 var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  font-size: var(--fs-xs);
  font-weight: 500;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.msg-media-card__retry:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.msg-media-card__retry:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

.msg-media-card__zoom {
  position: absolute;
  top: var(--sp-2);
  right: var(--sp-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--sp-8);
  height: var(--sp-8);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg) 55%, transparent);
  color: var(--text);
  /* Faint at rest so touch devices (no hover) still see the tap affordance. */
  opacity: 0.3;
  transition: opacity var(--dur-fast) var(--ease-standard);
}

.msg-media-card__img:hover .msg-media-card__zoom,
.msg-media-card__img:focus-visible .msg-media-card__zoom {
  opacity: 1;
}

.msg-media-card__img:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring-inset);
}

.msg-media-card__cap {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border);
}

.msg-media-card__name {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.msg-media-card__meta {
  flex-shrink: 0;
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.msg-media-card__spacer {
  flex: 1;
}

.msg-media-card__download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: var(--sp-8);
  height: var(--sp-8);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.msg-media-card__download:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.msg-media-card__download:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

@keyframes mediaSkeleton {
  from { background-position: 180% 0; }
  to { background-position: -80% 0; }
}

/* ── Artifact enter transitions ────────────────────────────────────────
   Cards and chips fade in + slide up on arrival mid-stream.
   Leave is instant (no lingering ghost). The reserved aspect-ratio box
   on .msg-media-card__img is layout-only and is not affected. */
.artifact-card-enter-from,
.artifact-chip-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.artifact-card-enter-active,
.artifact-chip-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

@media (prefers-reduced-motion: reduce) {
  .msg-media-card__zoom,
  .msg-media-card__download,
  .msg-media-card__retry,
  .msg-media-card__progress-bar {
    transition: none;
  }

  .msg-media-card__skeleton {
    animation: none;
    background: var(--bg-hover);
  }

  .artifact-card-enter-active,
  .artifact-chip-enter-active {
    transition: none;
  }
}
</style>
