<template>
  <div
    ref="stateRef"
    class="chat-session-load-state"
    :class="`chat-session-load-state--${visualState}`"
    :role="visualState === 'error' ? 'alert' : undefined"
    :aria-atomic="visualState === 'error' ? 'true' : undefined"
    :aria-busy="visualState === 'error' ? undefined : 'true'"
    :data-visual-state="visualState"
    data-testid="chat-session-load-state"
  >
    <div
      class="chat-session-load-state__panel"
      :class="`chat-session-load-state__panel--${visualState}`"
    >
      <template v-if="visualState === 'loading'">
        <span class="chat-session-load-state__spinner" aria-hidden="true" />
        <div class="chat-session-load-state__copy">
          <p class="chat-session-load-state__title">{{ t('chat.loadingSession') }}</p>
          <p class="chat-session-load-state__description">
            {{ t('chat.loadingSessionDescription') }}
          </p>
        </div>
      </template>

      <template v-else-if="visualState === 'retrying'">
        <span class="chat-session-load-state__spinner" aria-hidden="true" />
        <div class="chat-session-load-state__copy">
          <p class="chat-session-load-state__title">{{ t('chat.retryingSession') }}</p>
          <p class="chat-session-load-state__description">
            {{ t('chat.retryingSessionDescription') }}
          </p>
        </div>
        <button
          type="button"
          class="chat-session-load-state__retry btn btn--primary"
          data-testid="chat-session-load-retrying"
          disabled
        >
          {{ t('chat.retryingSessionAction') }}
        </button>
      </template>

      <template v-else>
        <span class="chat-session-load-state__icon" aria-hidden="true">
          <Icon name="info" :size="24" />
        </span>
        <div class="chat-session-load-state__copy">
          <p class="chat-session-load-state__title">{{ t('chat.loadSessionFailed') }}</p>
          <p class="chat-session-load-state__description">
            {{ t('chat.loadSessionDescription') }}
          </p>
        </div>
        <button
          type="button"
          class="chat-session-load-state__retry btn btn--primary"
          data-testid="chat-session-load-retry"
          @click="requestRetry"
        >
          {{ t('chat.reloadSession') }}
        </button>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import Icon from '@/components/Icon.vue'

const props = defineProps<{
  state: 'loading' | 'error'
}>()

const emit = defineEmits<{
  retry: []
}>()

const { t } = useI18n()
const stateRef = ref<HTMLElement | null>(null)
const retrying = ref(false)
const visualState = computed<'loading' | 'error' | 'retrying'>(() => (
  retrying.value ? 'retrying' : props.state
))

watch(() => props.state, (state, previous) => {
  if (state === 'error' && previous === 'loading') retrying.value = false
})

function requestRetry() {
  if (retrying.value) return
  const thread = stateRef.value?.closest('.chat-thread') as HTMLElement | null
  retrying.value = true
  emit('retry')
  void nextTick(() => thread?.focus({ preventScroll: true }))
}
</script>

<style scoped>
.chat-session-load-state {
  align-items: center;
  align-self: center;
  box-sizing: border-box;
  display: flex;
  flex: 1 0 min(42vh, 280px);
  justify-content: center;
  min-height: 180px;
  padding: var(--sp-6);
  text-align: center;
  width: min(var(--chat-col), 468px);
}

.chat-session-load-state__panel {
  align-items: center;
  background: color-mix(in srgb, var(--bg-elevated) 80%, var(--bg-surface));
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-xs);
  box-sizing: border-box;
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  justify-content: center;
  min-height: 210px;
  padding: var(--sp-6);
  width: 100%;
}

.chat-session-load-state__panel--error {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 25%, var(--border));
}

.chat-session-load-state__copy {
  max-width: 340px;
}

.chat-session-load-state__title,
.chat-session-load-state__description {
  margin: 0;
}

.chat-session-load-state__title {
  color: var(--text);
  font-size: var(--fs-md);
  font-weight: 600;
  line-height: 1.4;
}

.chat-session-load-state__description {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.5;
  margin-top: var(--sp-2);
}

.chat-session-load-state__icon {
  align-items: center;
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  border-radius: var(--radius-pill);
  color: var(--danger);
  display: inline-flex;
  height: 48px;
  justify-content: center;
  width: 48px;
}

.chat-session-load-state__spinner {
  animation: chat-session-load-spin 0.8s linear infinite;
  border: 2px solid var(--border-strong);
  border-radius: 50%;
  border-top-color: var(--accent);
  height: 32px;
  width: 32px;
}

.chat-session-load-state__retry {
  margin-top: var(--sp-1);
  min-width: 112px;
}

@keyframes chat-session-load-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat-session-load-state__spinner {
    animation: none;
    border-top-color: var(--border-strong);
  }
}

@media (max-width: 480px) {
  .chat-session-load-state {
    padding: var(--sp-4);
  }

  .chat-session-load-state__panel {
    min-height: 190px;
    padding: var(--sp-5) var(--sp-4);
  }
}
</style>
