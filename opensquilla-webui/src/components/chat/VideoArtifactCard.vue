<template>
  <article class="msg-video-card" :data-state="state">
    <span class="msg-video-card__icon" aria-hidden="true">
      <Icon name="video" :size="22" />
    </span>
    <span class="msg-video-card__info">
      <span class="msg-video-card__name">{{ artifactFileTitle(artifact) }}</span>
      <span class="msg-video-card__meta">{{ artifactFileSubtitle(artifact) }}</span>
      <span v-if="state === 'error'" class="msg-video-card__status" role="status">
        {{ t('chat.videoLoadFailed') }}
      </span>
      <span v-else-if="state === 'unsupported'" class="msg-video-card__status" role="status">
        {{ t('chat.videoUnsupported') }}
      </span>
    </span>

    <button
      v-if="state !== 'ready' && state !== 'unsupported'"
      type="button"
      class="msg-video-card__action"
      :disabled="state === 'loading'"
      :aria-busy="state === 'loading'"
      :aria-label="primaryActionLabel"
      @click="loadVideo"
    >
      <span v-if="state === 'loading'" class="spinner msg-video-card__spinner" aria-hidden="true" />
      <Icon v-else-if="state === 'error'" name="refresh" :size="14" />
      <Icon v-else name="video" :size="14" />
      <span>{{ primaryActionText }}</span>
    </button>

    <button
      type="button"
      class="msg-video-card__download"
      :class="{ 'msg-video-card__download--labelled': state === 'unsupported' }"
      :aria-label="t('chat.downloadTitle', { title: artifactFileTitle(artifact) })"
      @click="emit('download', artifact)"
    >
      <Icon name="download" :size="16" />
      <span v-if="state === 'unsupported'">{{ t('chat.download') }}</span>
    </button>

    <video
      v-if="state === 'ready' && objectUrl"
      ref="videoElement"
      class="msg-video-card__player"
      :src="objectUrl"
      controls
      playsinline
      preload="metadata"
      @error="markUnsupported"
    />
  </article>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useInlineMediaArtifact } from '@/composables/chat/useInlineMediaArtifact'
import type { ArtifactPayload } from '@/types/rpc'
import { artifactFileSubtitle, artifactFileTitle } from '@/utils/chat/artifacts'

const props = defineProps<{
  artifact: ArtifactPayload
  sessionKey?: string
  authToken?: string
}>()

const emit = defineEmits<{
  download: [artifact: ArtifactPayload]
}>()

const { t } = useI18n()
const videoElement = ref<HTMLVideoElement | null>(null)
const {
  state,
  objectUrl,
  load: loadVideo,
  markUnsupported,
} = useInlineMediaArtifact({
  artifact: () => props.artifact,
  sessionKey: () => props.sessionKey,
  authToken: () => props.authToken,
  kind: 'video',
  element: videoElement,
})
const primaryActionText = computed(() =>
  state.value === 'error' ? t('chat.retry') : t('chat.playVideo'))
const primaryActionLabel = computed(() =>
  `${primaryActionText.value} ${artifactFileTitle(props.artifact)}`)
</script>

<style scoped>
.msg-video-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.msg-video-card__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 3rem;
  height: 3rem;
  border-radius: var(--radius-md);
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
}

.msg-video-card__info {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.msg-video-card__name {
  overflow: hidden;
  color: var(--text);
  font-size: 0.9375rem;
  font-weight: 500;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-video-card__meta,
.msg-video-card__status {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.msg-video-card__status {
  color: var(--warn);
}

.msg-video-card__player {
  grid-column: 1 / -1;
  width: 100%;
  max-height: min(60vh, 32rem);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--text) 92%, var(--bg));
}

.msg-video-card__action,
.msg-video-card__download {
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

.msg-video-card__download {
  width: var(--sp-8);
  padding: 0;
}

.msg-video-card__download--labelled {
  width: auto;
  padding: 0 var(--sp-3);
}

.msg-video-card__action:hover:not(:disabled),
.msg-video-card__download:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.msg-video-card__action:focus-visible,
.msg-video-card__download:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-video-card__action:disabled {
  cursor: wait;
  opacity: 0.68;
}

.msg-video-card__spinner {
  width: 0.875rem;
  height: 0.875rem;
}

@media (max-width: 640px) {
  .msg-video-card {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }

  .msg-video-card__action {
    grid-column: 1 / -1;
    width: 100%;
  }

  .msg-video-card__download {
    grid-column: 3;
    grid-row: 1;
  }
}
</style>
