<template>
  <div
    ref="statusRef"
    class="chat-session-recovery-status"
    :class="`chat-session-recovery-status--${state}`"
    :role="isFailure ? 'alert' : 'status'"
    :aria-live="isFailure ? 'assertive' : 'polite'"
    aria-atomic="true"
    :data-recovery-state="state"
    data-testid="chat-session-recovery-status"
  >
    <span
      v-if="isBusy"
      class="chat-session-recovery-status__spinner"
      aria-hidden="true"
    />
    <Icon
      v-else
      class="chat-session-recovery-status__icon"
      name="info"
      :size="16"
      aria-hidden="true"
    />
    <span class="chat-session-recovery-status__copy">
      <strong>{{ title }}</strong>
      <span v-if="description">{{ description }}</span>
    </span>
    <button
      v-if="isFailure"
      type="button"
      class="chat-session-recovery-status__retry btn btn--ghost"
      data-testid="chat-session-recovery-retry"
      @click="requestRetry"
    >
      {{ action }}
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import Icon from '@/components/Icon.vue'
import type { ChatSessionRecoveryState } from '@/utils/chat/sessionLoadState'

const props = defineProps<{
  state: ChatSessionRecoveryState
}>()

const emit = defineEmits<{
  retry: []
}>()

const { t } = useI18n()
const statusRef = ref<HTMLElement | null>(null)
const isFailure = computed(() => (
  props.state === 'history-error' || props.state === 'live-degraded'
))
const isBusy = computed(() => !isFailure.value)
const title = computed(() => {
  switch (props.state) {
    case 'history-loading':
      return t('chat.loadingSession')
    case 'history-retrying':
      return t('chat.retryingSession')
    case 'history-error':
      return t('chat.loadSessionFailed')
    case 'live-connecting':
      return t('chat.liveConnecting')
    case 'live-degraded':
      return t('chat.liveUnavailable')
  }
})
const description = computed(() => {
  switch (props.state) {
    case 'history-loading':
      return t('chat.loadingSessionDescription')
    case 'history-retrying':
      return t('chat.retryingSessionDescription')
    case 'history-error':
      return t('chat.loadSessionDescription')
    case 'live-connecting':
      return ''
    case 'live-degraded':
      return t('chat.liveUnavailableDescription')
  }
})
const action = computed(() => (
  props.state === 'live-degraded'
    ? t('chat.reconnectLive')
    : t('chat.reloadSession')
))

function requestRetry() {
  emit('retry')
  void nextTick(() => {
    const thread = statusRef.value?.closest('.chat-thread') as HTMLElement | null
    thread?.focus({ preventScroll: true })
  })
}
</script>

<style scoped>
.chat-session-recovery-status {
  align-items: center;
  align-self: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow-xs);
  box-sizing: border-box;
  color: var(--text-muted);
  display: flex;
  flex: 0 0 auto;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  margin: var(--sp-2) auto;
  max-width: min(calc(100% - 32px), 680px);
  min-height: 34px;
  padding: var(--sp-1) var(--sp-3);
}

.chat-session-recovery-status--history-error,
.chat-session-recovery-status--live-degraded {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--warn) 35%, var(--border));
}

.chat-session-recovery-status__copy {
  align-items: baseline;
  display: flex;
  flex: 1;
  flex-wrap: wrap;
  gap: var(--sp-1) var(--sp-2);
  min-width: 0;
}

.chat-session-recovery-status__copy strong {
  color: var(--text);
  font-weight: 600;
}

.chat-session-recovery-status__spinner {
  animation: chat-session-recovery-spin 0.8s linear infinite;
  border: 2px solid var(--border-strong);
  border-radius: 50%;
  border-top-color: var(--text-muted);
  flex: 0 0 auto;
  height: 14px;
  width: 14px;
}

.chat-session-recovery-status__icon {
  color: var(--warn);
  flex: 0 0 auto;
}

.chat-session-recovery-status__retry {
  flex: 0 0 auto;
  min-height: 26px;
  padding: 0 var(--sp-2);
}

@keyframes chat-session-recovery-spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 600px) {
  .chat-session-recovery-status {
    align-items: flex-start;
    border-radius: var(--radius-lg);
    flex-wrap: wrap;
  }

  .chat-session-recovery-status__retry {
    margin-left: 22px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat-session-recovery-status__spinner {
    animation: none;
  }
}
</style>
