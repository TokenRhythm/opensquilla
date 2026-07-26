<template>
  <article class="msg-audio-card" :data-state="state">
    <span class="msg-audio-card__icon" aria-hidden="true">
      <Icon name="music" :size="22" />
    </span>
    <span class="msg-audio-card__info">
      <span class="msg-audio-card__name">{{ artifactFileTitle(artifact) }}</span>
      <span class="msg-audio-card__meta">{{ artifactFileSubtitle(artifact) }}</span>
      <span v-if="state === 'error'" class="msg-audio-card__status" role="status">
        {{ t('chat.audioLoadFailed') }}
      </span>
      <span v-else-if="state === 'unsupported'" class="msg-audio-card__status" role="status">
        {{ t('chat.audioUnsupported') }}
      </span>
    </span>

    <audio
      v-if="state === 'ready' && objectUrl"
      ref="audioElement"
      class="msg-audio-card__player"
      :src="objectUrl"
      controls
      preload="metadata"
      @error="markUnsupported"
    />
    <button
      v-else-if="state !== 'unsupported'"
      type="button"
      class="msg-audio-card__action"
      :disabled="state === 'loading'"
      :aria-busy="state === 'loading'"
      :aria-label="primaryActionLabel"
      @click="loadAudio"
    >
      <span v-if="state === 'loading'" class="spinner msg-audio-card__spinner" aria-hidden="true" />
      <Icon v-else-if="state === 'error'" name="refresh" :size="14" />
      <Icon v-else name="volume" :size="14" />
      <span>{{ primaryActionText }}</span>
    </button>

    <button
      type="button"
      class="msg-audio-card__download"
      :class="{ 'msg-audio-card__download--labelled': state === 'unsupported' }"
      :aria-label="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
      @click="emit('download', artifact)"
    >
      <Icon name="download" :size="16" />
      <span v-if="state === 'unsupported'">{{ t('chat.download') }}</span>
    </button>
  </article>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ArtifactPayload } from '@/types/rpc'
import { fetchArtifactBlob } from '@/utils/chat/artifactAccess'
import { artifactFileSubtitle, artifactFileTitle } from '@/utils/chat/artifacts'

type AudioState = 'idle' | 'loading' | 'ready' | 'error' | 'unsupported'

const props = defineProps<{
  artifact: ArtifactPayload
  sessionKey?: string
  authToken?: string
}>()

const emit = defineEmits<{
  download: [artifact: ArtifactPayload]
}>()

const { t } = useI18n()
const state = ref<AudioState>('idle')
const objectUrl = ref('')
const audioElement = ref<HTMLAudioElement | null>(null)
let requestController: AbortController | null = null

const identity = computed(() => [
  props.artifact.id,
  props.artifact.key,
  props.artifact.download_url,
  props.artifact.name,
  props.artifact.mime,
  props.artifact.size,
].map(value => String(value || '')).join('\u0000'))
const primaryActionText = computed(() =>
  state.value === 'error' ? t('chat.retry') : t('chat.playAudio'))
const primaryActionLabel = computed(() =>
  `${primaryActionText.value} ${artifactFileTitle(props.artifact)}`)

function revokeObjectUrl() {
  const url = objectUrl.value
  objectUrl.value = ''
  if (!url) return
  try { URL.revokeObjectURL(url) } catch {}
}

function reset() {
  requestController?.abort()
  requestController = null
  try { audioElement.value?.pause() } catch {}
  revokeObjectUrl()
  state.value = 'idle'
}

function supportedByBrowser(blob: Blob): boolean {
  const mime = String(blob.type || props.artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
  if (!mime.startsWith('audio/')) return true
  try {
    const probe = document.createElement('audio')
    return typeof probe.canPlayType !== 'function' || probe.canPlayType(mime) !== ''
  } catch {
    return true
  }
}

async function loadAudio() {
  if (state.value === 'loading' || state.value === 'ready') return
  requestController?.abort()
  const controller = new AbortController()
  requestController = controller
  state.value = 'loading'
  try {
    const fetched = await fetchArtifactBlob(props.artifact, {
      baseOrigin: window.location.origin,
      sessionKey: props.sessionKey,
      authToken: props.authToken,
      signal: controller.signal,
      requireSameOrigin: true,
    })
    if (controller.signal.aborted || requestController !== controller) return
    requestController = null
    if (!fetched.ok) {
      state.value = 'error'
      return
    }
    if (!supportedByBrowser(fetched.blob)) {
      state.value = 'unsupported'
      return
    }
    objectUrl.value = URL.createObjectURL(fetched.blob)
    state.value = 'ready'
    await nextTick()
    const playback = audioElement.value?.play()
    if (playback && typeof playback.catch === 'function') void playback.catch(() => undefined)
  } catch (error) {
    if (controller.signal.aborted || (
      typeof DOMException !== 'undefined' && error instanceof DOMException && error.name === 'AbortError'
    )) return
    if (requestController === controller) requestController = null
    state.value = 'error'
  }
}

function markUnsupported() {
  try { audioElement.value?.pause() } catch {}
  revokeObjectUrl()
  state.value = 'unsupported'
}

watch(
  () => [identity.value, props.sessionKey || '', props.authToken || ''],
  (_next, previous) => { if (previous) reset() },
)

onUnmounted(reset)
</script>

<style scoped>
.msg-audio-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(9rem, auto) auto;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.msg-audio-card__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 3rem;
  height: 3rem;
  border-radius: var(--radius-md);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
}

.msg-audio-card__info {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.msg-audio-card__name {
  overflow: hidden;
  color: var(--text);
  font-size: 0.9375rem;
  font-weight: 500;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-audio-card__meta,
.msg-audio-card__status {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.msg-audio-card__status {
  color: var(--warn);
}

.msg-audio-card__player {
  width: min(25rem, 100%);
  height: 2.5rem;
}

.msg-audio-card__action,
.msg-audio-card__download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-1);
  height: var(--sp-8);
  padding: 0 var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.msg-audio-card__download--labelled {
  width: auto;
  padding: 0 var(--sp-3);
}

.msg-audio-card__download {
  width: var(--sp-8);
  padding: 0;
}

.msg-audio-card__action:hover:not(:disabled),
.msg-audio-card__download:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.msg-audio-card__action:focus-visible,
.msg-audio-card__download:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-audio-card__action:disabled {
  cursor: wait;
  opacity: 0.68;
}

.msg-audio-card__spinner {
  width: 0.875rem;
  height: 0.875rem;
}

@media (max-width: 640px) {
  .msg-audio-card {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }

  .msg-audio-card__player,
  .msg-audio-card__action {
    grid-column: 1 / -1;
    width: 100%;
  }

  .msg-audio-card__download {
    grid-column: 3;
    grid-row: 1;
  }
}
</style>
