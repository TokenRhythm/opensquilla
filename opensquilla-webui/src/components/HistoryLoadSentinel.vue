<template>
  <div
    v-if="visible"
    ref="sentinelRef"
    class="history-load-sentinel"
    :class="{ 'history-load-sentinel--idle': idle }"
    :role="loading || retryable || unavailable ? 'status' : undefined"
    :aria-live="loading || retryable || unavailable ? 'polite' : undefined"
    :aria-atomic="loading || retryable || unavailable ? 'true' : undefined"
    data-testid="history-load-sentinel"
  >
    <Transition name="history-load-feedback">
      <span
        v-if="loading"
        key="loading"
        class="history-load-sentinel__feedback history-load-sentinel__feedback--loading"
      >
        <span class="history-load-sentinel__spinner" aria-hidden="true" />
        <span>{{ t('chat.loadingEarlier') }}</span>
      </span>
      <button
        v-else-if="retryable"
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
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  scrollContainer: HTMLElement | null
  hasMore: boolean
  loading: boolean
  error: boolean
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
const retryable = computed(() => (
  props.error
  || (props.canonicalAvailable === false && props.canonicalComplete !== true)
))
const unavailable = computed(() => (
  !props.hasMore
  && props.canonicalAvailable === true
  && props.canonicalComplete === false
))
const visible = computed(() => props.hasMore || props.loading || retryable.value || unavailable.value)
const idle = computed(() => (
  props.hasMore && !props.loading && !props.blocked && !retryable.value
))

let observer: IntersectionObserver | null = null
let lastAutoCursor = ''

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
    || retryable.value
  ) return
  lastAutoCursor = key
  emit('loadEarlier')
}

function requestRetry() {
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
  ],
  (next, previous) => {
    if (
      next[4] !== previous?.[4]
      || next[5] !== previous?.[5]
      || next[6] !== previous?.[6]
      || (previous?.[7] === true && next[7] !== true)
    ) lastAutoCursor = ''
    void nextTick(attach)
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
  font-size: var(--fs-xs);
  justify-content: center;
  margin: 0 auto var(--sp-2);
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
