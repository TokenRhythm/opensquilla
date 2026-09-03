<template>
  <!-- Neutral long-running notice shown only after the high soft threshold. -->
  <div class="stall-notice" role="status" aria-live="polite" data-testid="chat-stall-notice">
    <span class="stall-notice__dot" aria-hidden="true" />
    <div class="stall-notice__text">
      <span class="stall-notice__title">{{ t('chat.stall.title') }}</span>
      <span class="stall-notice__detail">{{ t('chat.stall.detail', { seconds }) }}</span>
    </div>
    <div class="stall-notice__actions">
      <button
        class="btn btn--ghost stall-notice__wait"
        type="button"
        data-testid="chat-stall-wait"
        @click="$emit('wait')"
      >
        {{ t('chat.stall.keepWaiting') }}
      </button>
      <button
        v-if="canInterrupt"
        class="btn stall-notice__interrupt"
        type="button"
        data-testid="chat-stall-interrupt"
        @click="$emit('interrupt')"
      >
        {{ t('chat.stall.interrupt') }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

withDefaults(defineProps<{
  /** Seconds of content silence, live-updated by the watchdog. */
  seconds: number
  /** Whether this session policy permits the active turn to be interrupted. */
  canInterrupt?: boolean
}>(), {
  canInterrupt: true,
})

defineEmits<{
  /** "Keep waiting" — suppress this silence episode until real progress. */
  wait: []
  /** "Interrupt turn" — same stop path as the composer stop button. */
  interrupt: []
}>()
</script>

<style scoped>
.stall-notice {
  width: var(--chat-col, min(calc(100% - 48px), 980px));
  margin: var(--sp-2) auto;
  padding: var(--sp-3) var(--sp-4);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-3);
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--warn) 40%, var(--border));
  border-radius: var(--radius-lg);
  /* Direct child of the .chat-thread flex column (see .approval-card): keep
     the banner from collapsing when the thread scrolls. */
  flex-shrink: 0;
  animation: stall-notice-enter var(--dur-enter) var(--ease-out) both;
}

.stall-notice__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--warn);
  flex-shrink: 0;
  animation: stall-notice-pulse 1.6s var(--ease-standard) infinite;
}

.stall-notice__text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.stall-notice__title {
  color: var(--warn);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.stall-notice__detail {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.stall-notice__actions {
  display: flex;
  gap: var(--sp-2);
  flex-shrink: 0;
}

.stall-notice__wait {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
  color: var(--warn);
}

.stall-notice__wait:hover:not(:disabled) {
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-surface));
}

.stall-notice__interrupt {
  border-color: color-mix(in srgb, var(--danger) 45%, var(--border));
  color: var(--danger);
}

.stall-notice__interrupt:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-surface));
}

@keyframes stall-notice-enter {
  from {
    opacity: 0;
    transform: translateY(7px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes stall-notice-pulse {
  0%, 100% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--warn) 45%, transparent); }
  55% { box-shadow: 0 0 0 5px transparent; }
}

@media (max-width: 768px) {
  .stall-notice__actions {
    width: 100%;
  }

  .stall-notice__actions .btn {
    flex: 1;
    justify-content: center;
  }
}

@media (prefers-reduced-motion: reduce) {
  .stall-notice,
  .stall-notice__dot {
    animation: none;
  }
}
</style>
