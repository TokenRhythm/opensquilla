<template>
  <figure
    class="msg-video-card"
    :data-state="cardState"
    :aria-label="t('chat.artifactTitleSubtitle', { title, subtitle })"
  >
    <div class="msg-video-card__viewport">
      <video
        v-if="previewState === 'loaded' && previewUrl && !playbackFailed"
        ref="videoElement"
        class="msg-video-card__video msg-video-card__player"
        :src="previewUrl"
        :aria-label="t('chat.previewOf', { title })"
        controls
        playsinline
        preload="metadata"
        @loadedmetadata="playbackFailed = false"
        @error="playbackFailed = true"
      >
        {{ t('chat.videoUnsupported') }}
      </video>

      <div
        v-else-if="playbackFailed || previewState === 'timeout' || previewState === 'error'"
        class="msg-video-card__fallback"
        role="status"
      >
        <p class="msg-video-card__status">
          {{ fallbackMessage }}
        </p>
        <span class="msg-video-card__fallback-actions">
          <button
            type="button"
            class="msg-video-card__retry msg-video-card__action"
            :aria-label="t('chat.retryPreviewFor', { title })"
            @click="retryPreview"
          >
            <Icon name="refresh" :size="14" />
            <span>{{ t('chat.retry') }}</span>
          </button>
          <button
            type="button"
            class="msg-video-card__retry"
            :aria-label="t('chat.downloadTitle', { title })"
            @click="emit('download', artifact)"
          >
            <Icon name="download" :size="14" />
            <span>{{ t('chat.download') }}</span>
          </button>
        </span>
      </div>

      <div
        v-else-if="previewState === 'idle'"
        class="msg-video-card__ready"
      >
        <button
          type="button"
          class="msg-video-card__load msg-video-card__action"
          :aria-label="t('chat.loadVideoPreviewFor', { title })"
          @click="loadPreview"
        >
          <span class="msg-video-card__play" aria-hidden="true" />
          <span>{{ t('chat.loadVideoPreview') }}</span>
        </button>
      </div>

      <div
        v-else
        class="msg-video-card__loading"
        role="status"
        :aria-label="t('chat.loadingPreview')"
      >
        <div
          v-if="previewProgress !== null"
          class="msg-video-card__progress"
          role="progressbar"
          :aria-label="t('chat.previewDownload')"
          :aria-valuenow="previewProgress"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <span class="msg-video-card__progress-bar" :style="{ width: `${previewProgress}%` }" />
        </div>
        <span v-else class="msg-video-card__skeleton" aria-hidden="true" />
        <p class="msg-video-card__status msg-video-card__loading-copy">
          {{ t('chat.videoPreviewLoading') }}
        </p>
        <button
          type="button"
          class="msg-video-card__retry msg-video-card__loading-cancel"
          data-testid="video-preview-cancel"
          @click="unloadPreview"
        >
          {{ t('chat.cancelVideoPreview') }}
        </button>
      </div>
    </div>

    <figcaption class="msg-video-card__caption">
      <span class="msg-video-card__name">{{ title }}</span>
      <span class="msg-video-card__meta">{{ subtitle }}</span>
      <span class="msg-video-card__spacer" />
      <button
        v-if="previewState === 'loaded'"
        type="button"
        class="msg-video-card__unload"
        data-testid="video-preview-unload"
        :aria-label="t('chat.unloadVideoPreviewFor', { title })"
        @click="unloadPreview"
      >
        {{ t('chat.unloadVideoPreview') }}
      </button>
      <button
        type="button"
        class="msg-video-card__download"
        :aria-label="t('chat.downloadTitle', { title })"
        @click="emit('download', artifact)"
      >
        <Icon name="download" :size="16" />
      </button>
    </figcaption>
  </figure>
</template>

<script lang="ts">
const VIDEO_PREVIEW_MAX_BYTES = 128 * 1024 * 1024
const VIDEO_PREVIEW_TIMEOUT_MS = 120_000
const RETAINED_VIDEO_PREVIEW_LIMIT = 2

type RetainedVideoPreview = { token: string; release: () => void }
const retainedVideoPreviews: RetainedVideoPreview[] = []
let nextVideoPreviewToken = 0

function forgetRetainedVideoPreview(token: string): void {
  const index = retainedVideoPreviews.findIndex(entry => entry.token === token)
  if (index >= 0) retainedVideoPreviews.splice(index, 1)
}

function retainVideoPreview(entry: RetainedVideoPreview): void {
  forgetRetainedVideoPreview(entry.token)
  retainedVideoPreviews.push(entry)
  while (retainedVideoPreviews.length > RETAINED_VIDEO_PREVIEW_LIMIT) {
    retainedVideoPreviews.shift()?.release()
  }
}
</script>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import {
  createArtifactPreview,
  type ArtifactPreviewState,
} from '@/composables/chat/useArtifactPreview'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactDownloadUrl,
  artifactFileSubtitle,
  artifactFileTitle,
} from '@/utils/chat/artifacts'

const props = defineProps<{
  artifact: ArtifactPayload
  sessionKey?: string
  authToken?: string
}>()

const emit = defineEmits<{
  download: [artifact: ArtifactPayload]
}>()

const { t } = useI18n()
const playbackFailed = ref(false)
const videoElement = ref<HTMLVideoElement | null>(null)
const title = computed(() => artifactFileTitle(props.artifact))
const subtitle = computed(() => artifactFileSubtitle(props.artifact))
const artifactIdentity = computed(() => [
  props.artifact.id,
  props.artifact.key,
  props.artifact.download_url,
  props.artifact.name,
  props.artifact.mime,
  props.artifact.size,
  props.sessionKey,
  props.authToken,
].map(value => String(value || '')).join('\u0000'))

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

function supportedByBrowser(blob: Blob): boolean {
  const responseMime = String(blob.type || '').split(';', 1)[0].trim().toLowerCase()
  const declaredMime = String(props.artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
  const mime = responseMime.startsWith('video/') ? responseMime : declaredMime
  if (!mime.startsWith('video/')) return true
  try {
    const probe = document.createElement('video')
    if (typeof probe.canPlayType !== 'function' || probe.canPlayType(mime) !== '') return true
    // DOM test environments and some conservative browsers report an empty
    // capability for standard containers before metadata is available. Keep
    // those native formats loadable, while rejecting clearly unknown types.
    return ['video/mp4', 'video/webm', 'video/ogg'].includes(mime)
  } catch {
    return true
  }
}

// A video fetch starts only after an explicit preview request. Using a fetched
// Blob URL keeps session/auth credentials out of the media URL while avoiding
// bandwidth and memory cost for videos the user never chooses to watch.
const controller = createArtifactPreview({
  resolveUrl: () => artifactDownloadUrl(props.artifact, window.location.origin, {
    sessionKey: props.sessionKey,
    includeSessionKey: false,
  }),
  headers: () => previewHeaders(artifactDownloadUrl(props.artifact, window.location.origin, {
    sessionKey: props.sessionKey,
    includeSessionKey: false,
  })),
  sameOrigin,
  fullSize: false,
  timeoutMs: VIDEO_PREVIEW_TIMEOUT_MS,
  maxBytes: VIDEO_PREVIEW_MAX_BYTES,
  requireSameOrigin: true,
  acceptBlob: supportedByBrowser,
})

const previewToken = `video-preview-${(nextVideoPreviewToken += 1)}`

const previewState = computed(() => controller.state.value as ArtifactPreviewState)
const previewProgress = computed(() => controller.progress.value ?? null)
const previewUrl = computed(() => controller.objectUrl.value || '')
const cardState = computed(() => {
  if (playbackFailed.value || controller.errorCode.value === 'unsupported') return 'unsupported'
  if (previewState.value === 'loaded') return 'ready'
  return previewState.value
})
const fallbackMessage = computed(() => {
  if (playbackFailed.value || controller.errorCode.value === 'unsupported') {
    return t('chat.videoUnsupported')
  }
  if (controller.errorCode.value === 'too_large') {
    const megabytes = new Intl.NumberFormat().format(VIDEO_PREVIEW_MAX_BYTES / (1024 * 1024))
    return t('chat.videoPreviewTooLarge', { size: `${megabytes} MB` })
  }
  if (previewState.value === 'timeout') return t('chat.videoPreviewTimedOut')
  return t('chat.previewFailed')
})

function loadPreview() {
  controller.load()
}

function retryPreview() {
  playbackFailed.value = false
  if (controller.state.value === 'loaded') {
    // A decoder error can be transient; remount the native player without
    // downloading a second copy. Network/load errors use the shared retry path.
    return
  }
  controller.load()
}

function unloadPreview() {
  playbackFailed.value = false
  forgetRetainedVideoPreview(previewToken)
  controller.release()
}

function releaseRetainedPreview() {
  playbackFailed.value = false
  controller.release()
}

watch(
  () => controller.state.value,
  state => {
    if (state === 'loaded') {
      retainVideoPreview({ token: previewToken, release: releaseRetainedPreview })
      void nextTick(() => {
        const playback = videoElement.value?.play()
        if (playback && typeof playback.catch === 'function') {
          void playback.catch(() => undefined)
        }
      })
    } else {
      forgetRetainedVideoPreview(previewToken)
    }
  },
  { flush: 'sync' },
)

watch(artifactIdentity, (_next, previous) => {
  if (previous) unloadPreview()
})

onUnmounted(() => {
  forgetRetainedVideoPreview(previewToken)
  controller.dispose()
})
</script>

<style scoped>
.msg-video-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
}

.msg-video-card__viewport {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  aspect-ratio: 16 / 9;
  max-height: 420px;
  overflow: hidden;
  background: var(--bg);
}

.msg-video-card__video {
  display: block;
  width: 100%;
  height: 100%;
  background: var(--bg);
  object-fit: contain;
}

.msg-video-card__loading,
.msg-video-card__ready,
.msg-video-card__fallback {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
}

.msg-video-card__load {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  min-height: var(--sp-10);
  padding: 0 var(--sp-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.msg-video-card__play {
  width: 0;
  height: 0;
  margin-left: var(--sp-1);
  border-top: 0.35rem solid transparent;
  border-bottom: 0.35rem solid transparent;
  border-left: 0.55rem solid currentColor;
}

.msg-video-card__skeleton {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    100deg,
    var(--bg) 30%,
    var(--bg-hover) 50%,
    var(--bg) 70%
  );
  background-size: 220% 100%;
  animation: videoSkeleton 1.4s ease-in-out infinite;
}

.msg-video-card__progress {
  width: min(64%, 20rem);
  height: var(--sp-1);
  overflow: hidden;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
}

.msg-video-card__progress-bar {
  display: block;
  height: 100%;
  border-radius: var(--radius-full);
  background: var(--accent);
  transition: width var(--dur-base) var(--ease-standard);
}

.msg-video-card__status {
  max-width: 32rem;
  margin: 0;
  padding: 0 var(--sp-4);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  text-align: center;
}

.msg-video-card__loading-copy,
.msg-video-card__loading-cancel {
  position: relative;
  z-index: 1;
}

.msg-video-card__fallback-actions {
  display: inline-flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--sp-2);
}

.msg-video-card__retry,
.msg-video-card__unload {
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

.msg-video-card__caption {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border);
}

.msg-video-card__name {
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-video-card__meta {
  flex-shrink: 0;
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.msg-video-card__spacer {
  flex: 1;
}

.msg-video-card__unload {
  flex-shrink: 0;
}

.msg-video-card__download {
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

.msg-video-card__retry:hover,
.msg-video-card__load:hover,
.msg-video-card__unload:hover,
.msg-video-card__download:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.msg-video-card__retry:focus-visible,
.msg-video-card__load:focus-visible,
.msg-video-card__unload:focus-visible,
.msg-video-card__download:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

@keyframes videoSkeleton {
  from { background-position: 180% 0; }
  to { background-position: -80% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .msg-video-card__retry,
  .msg-video-card__load,
  .msg-video-card__unload,
  .msg-video-card__download,
  .msg-video-card__progress-bar {
    transition: none;
  }

  .msg-video-card__skeleton {
    animation: none;
    background: var(--bg-hover);
  }
}
</style>
