<template>
  <div
    v-if="visible"
    ref="sentinelRef"
    class="history-load-sentinel"
    :class="{ 'history-load-sentinel--idle': idle }"
    :role="busy || hasFailure || unavailable ? 'status' : undefined"
    :aria-live="busy || hasFailure || unavailable ? 'polite' : undefined"
    :aria-atomic="busy || hasFailure || unavailable ? 'true' : undefined"
    data-testid="history-load-sentinel"
  >
    <Transition name="history-load-feedback">
      <span
        v-if="busy"
        key="loading"
        class="history-load-sentinel__feedback history-load-sentinel__feedback--loading"
      >
        <span class="history-load-sentinel__spinner" aria-hidden="true" />
        <span>{{ t('chat.loadingEarlier') }}</span>
      </span>
      <div
        v-else-if="failure"
        key="failure"
        class="history-load-sentinel__feedback history-load-sentinel__failure"
        role="alert"
        data-testid="history-load-failure"
      >
        <span>{{ failureLabel }}</span>
        <button
          v-if="failure.action !== 'none'"
          type="button"
          class="history-load-sentinel__action"
          data-testid="history-load-retry"
          @click="requestRetry"
        >
          {{ failure.action === 'reload-latest' ? t('chat.earlierHistoryFailure.reloadLatest') : t('chat.retry') }}
        </button>
      </div>
      <button
        v-else-if="hasFailure"
        key="retry"
        type="button"
        class="history-load-sentinel__feedback history-load-sentinel__retry"
        data-testid="history-load-retry"
        @click="requestRetry"
      >
        {{ t('chat.loadEarlierFailedRetry') }}
      </button>
      <span
        v-else-if="unavailable"
        key="unavailable"
        class="history-load-sentinel__feedback"
      >
        {{ t('chat.legacyHistoryUnavailable') }}
      </span>
    </Transition>
    <span
      v-if="metadataLimited"
      class="history-load-sentinel__metadata"
      role="status"
      data-testid="history-metadata-limited"
    >
      {{ t('chat.earlierHistoryFailure.metadataLimited') }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { HistoryLoadFailure } from '@/utils/chat/historyLoadFailure'

const props = defineProps<{
  scrollContainer: HTMLElement | null
  hasMore: boolean
  loading: boolean
  error: boolean
  failure?: HistoryLoadFailure | null
  metadataLimited?: boolean
  canonicalAvailable: boolean | null
  canonicalComplete: boolean | null
  cursor: string | number | null
  sessionKey?: string
  blocked?: boolean
}>()

const emit = defineEmits<{
  loadEarlier: []
  retry: []
}>()

const { t } = useI18n()
const sentinelRef = ref<HTMLElement | null>(null)
const hasFailure = computed(() => (
  Boolean(props.failure) || props.error
  || (props.canonicalAvailable === false && props.canonicalComplete !== true)
))
const busy = computed(() => (
  props.loading || Boolean(props.blocked && hasFailure.value)
))
const unavailable = computed(() => (
  !props.hasMore
  && props.canonicalAvailable === true
  && props.canonicalComplete === false
))
const visible = computed(() => props.hasMore || busy.value || hasFailure.value || unavailable.value || props.metadataLimited)
const idle = computed(() => (
  props.hasMore && !props.loading && !props.blocked && !hasFailure.value
))
const failureLabel = computed(() => {
  switch (props.failure?.kind) {
    case 'too-large': return t('chat.earlierHistoryFailure.tooLarge')
    case 'timeout': return t('chat.earlierHistoryFailure.timeout')
    case 'busy': return t('chat.earlierHistoryFailure.busy')
    case 'unavailable': return t('chat.earlierHistoryFailure.unavailable')
    case 'cursor': return t('chat.earlierHistoryFailure.cursor')
    default: return t('chat.earlierHistoryFailure.failed')
  }
})

let observer: IntersectionObserver | null = null
let lastAutoCursor = ''
const retryRequested = ref(false)

function cursorKey(): string {
  return props.cursor == null ? '' : String(props.cursor)
}

function disconnect() {
  observer?.disconnect()
  observer = null
}

function requestEarlier() {
  const key = cursorKey()
  if (
    !key
    || key === lastAutoCursor
    || !props.hasMore
    || props.loading
    || props.blocked
    || hasFailure.value
  ) return
  lastAutoCursor = key
  emit('loadEarlier')
}

function requestRetry() {
  if (retryRequested.value || props.blocked || props.loading || props.failure?.action === 'none') return
  retryRequested.value = true
  emit('retry')
  void nextTick(() => props.scrollContainer?.focus({ preventScroll: true }))
}

function attach() {
  disconnect()
  if (
    typeof IntersectionObserver === 'undefined'
    || !props.scrollContainer
    || !sentinelRef.value
    || !idle.value
  ) return
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) requestEarlier()
  }, {
    root: props.scrollContainer,
    rootMargin: '320px 0px 0px 0px',
    threshold: 0,
  })
  observer.observe(sentinelRef.value)
}

watch(
  () => [
    props.scrollContainer,
    props.hasMore,
    props.loading,
    props.error,
    props.canonicalAvailable,
    props.cursor,
    props.sessionKey,
    props.blocked,
    props.failure,
    props.metadataLimited,
  ],
  (next, previous) => {
    if (next[6] !== previous?.[6]) retryRequested.value = false
    if (
      next[4] !== previous?.[4]
      || next[5] !== previous?.[5]
      || next[6] !== previous?.[6]
      || (previous?.[7] === true && next[7] !== true)
    ) lastAutoCursor = ''
    void nextTick(attach)
  },
)

watch(
  () => Boolean(props.blocked || props.loading),
  (inFlight, wasInFlight) => {
    if (!inFlight && wasInFlight) retryRequested.value = false
  },
)

onMounted(() => { void nextTick(attach) })
onBeforeUnmount(disconnect)
</script>

<style scoped>
.history-load-sentinel {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-xs);
  justify-content: center;
  margin: 0 auto var(--sp-2);
  gap: var(--sp-2);
  min-height: 32px;
  width: var(--chat-col, min(calc(100% - 48px), 980px));
}

.history-load-sentinel--idle {
  pointer-events: none;
}

.history-load-sentinel__feedback {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow-xs);
  display: inline-flex;
  gap: var(--sp-2);
  justify-content: center;
  min-height: 28px;
  padding: var(--sp-1) var(--sp-3);
  white-space: nowrap;
}

.history-load-sentinel__spinner {
  animation: history-load-spin 0.8s linear infinite;
  border: 2px solid var(--border-strong);
  border-radius: 50%;
  border-top-color: var(--text-muted);
  height: 16px;
  width: 16px;
}

.history-load-sentinel__retry {
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
}

.history-load-sentinel__retry:hover {
  color: var(--text);
}

.history-load-sentinel__failure {
  flex-wrap: wrap;
  max-width: 100%;
  text-align: center;
  white-space: normal;
}

.history-load-sentinel__action {
  background: transparent;
  border: 0;
  color: var(--text);
  cursor: pointer;
  font: inherit;
  padding: 0;
  text-decoration: underline;
}

.history-load-sentinel__metadata {
  text-align: center;
}

@keyframes history-load-spin {
  to { transform: rotate(360deg); }
}

.history-load-feedback-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.history-load-sentinel__feedback--loading.history-load-feedback-enter-active {
  transition-delay: var(--dur-fast);
}

.history-load-feedback-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.history-load-feedback-enter-from,
.history-load-feedback-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (prefers-reduced-motion: reduce) {
  .history-load-sentinel__spinner {
    animation-duration: 1.6s; /* motion-allow: gentler loading cadence */
  }

  .history-load-feedback-enter-active,
  .history-load-feedback-leave-active {
    transition: none;
  }

  .history-load-feedback-enter-from,
  .history-load-feedback-leave-to {
    transform: none;
  }
}
</style>
