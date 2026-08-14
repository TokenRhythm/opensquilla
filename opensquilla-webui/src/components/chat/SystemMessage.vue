<template>
  <!-- Error role: distinct left-aligned card so a failed turn is unmissable. -->
  <div v-if="message.displayRole === 'error'" class="msg-error-wrap">
    <div class="msg-error-card" role="alert">
      <span class="msg-error-card__icon" aria-hidden="true">
        <Icon name="info" :size="16" />
      </span>
      <div class="msg-error-card__body">
        <span class="msg-error-card__heading">{{ errorHeading }}</span>
        <span v-if="message.text" class="msg-error-card__text">{{ message.text }}</span>
        <button
          v-if="showResume"
          type="button"
          class="msg-error-card__resume"
          :disabled="resolving"
          @click="onResume"
        >{{ t('chat.sandboxPausedResume') }}</button>
        <button
          v-if="showRetry"
          type="button"
          class="msg-error-card__resume"
          :disabled="retryResolving"
          @click="onRetry"
        >{{ t('chat.retry') }}</button>
      </div>
      <time v-if="timeIso" class="msg-error-card__time" :datetime="timeIso" :title="timeFull">{{ timeAbs }}</time>
    </div>
  </div>

  <!-- All other system roles: centered pill (unchanged). -->
  <div v-else class="msg-system-wrap">
    <div class="msg-system" :class="message.displayRole">
      <span class="msg-system-label">{{ message.roleLabel }}</span>
      <template v-if="message.displayRole === 'subagent'">
        <details class="chat-subagent-disclosure">
          <summary class="chat-subagent-disclosure-summary">{{ subagentSummary(message.text) }}</summary>
          <pre class="chat-subagent-disclosure-body">{{ subagentBody(message.text) }}</pre>
        </details>
      </template>
      <template v-else-if="message.text">
        <span class="msg-system__text">{{ message.text }}</span>
      </template>
      <time v-if="timeIso" class="msg-system-time" :datetime="timeIso" :title="timeFull">{{ timeAbs }}</time>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ChatRenderedMessage } from '@/types/chat'
import { absoluteTime, fullTime, isoTime } from '@/utils/messageTime'
import {
  hasStrictUsageBarrierReplayProof,
  isUsageAccountingBarrierMessage,
} from '@/utils/chat/usageAccountingFailure'

const { t } = useI18n()

const props = defineProps<{
  message: ChatRenderedMessage
  subagentSummary: (text: string) => string
  subagentBody: (text: string) => string
  retryAvailable?: boolean
}>()

// Owner-driven recovery: a run paused by the sandbox denial ledger surfaces as a
// terminal error carrying this code. Offer a Resume action that the parent wires
// to the sandbox.resume RPC. Resume is idempotent, but we disable the button
// after one click to avoid duplicate confirmations.
const emit = defineEmits<{
  resume: []
  retry: [message: ChatRenderedMessage, settle: (accepted: boolean) => void]
}>()
const resolving = ref(false)
const retryResolving = ref(false)
const showResume = computed(
  () =>
    props.message.displayRole === 'error' &&
    props.message.errorCode === 'sandbox_threshold_exceeded',
)
const isUsageBarrier = computed(
  () => props.message.displayRole === 'error'
    && isUsageAccountingBarrierMessage(props.message),
)
const showRetry = computed(
  () =>
    isUsageBarrier.value
    && hasStrictUsageBarrierReplayProof(props.message)
    && props.retryAvailable === true,
)
const errorHeading = computed(() =>
  isUsageBarrier.value ? t('chat.usageAccountingBlockedTitle') : t('chat.turnFailed'),
)

function onResume() {
  if (resolving.value) return
  resolving.value = true
  emit('resume')
}

function onRetry() {
  if (retryResolving.value) return
  emit('retry', props.message, (accepted) => {
    retryResolving.value = accepted
  })
}

const timeIso = computed(() => isoTime(props.message.ts))
const timeAbs = computed(() => absoluteTime(props.message.ts))
const timeFull = computed(() => fullTime(props.message.ts))
</script>

<style scoped>
.msg-system-wrap {
  display: flex;
  justify-content: center;
  padding: 0.375rem 2rem;
}

.msg-system {
  font-size: 0.8125rem;
  color: var(--text-dim);
  padding: 0.25rem 0.625rem;
  border-radius: var(--radius-md);
  max-width: 70%;
  text-align: center;
}

.msg-system.error {
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-surface));
  color: var(--danger);
}

.msg-system-label {
  font-weight: 600;
  margin-right: 0.375rem;
}

.msg-system__text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* Quiet, hover-revealed timestamp on the centered status pill. */
.msg-system-time {
  margin-left: 0.375rem;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

.msg-system-wrap:hover .msg-system-time {
  opacity: 1;
}

@media (hover: none) {
  .msg-system-time {
    opacity: 1;
  }
}

/* ── Error card (role: error) ──────────────────────────────────────────
   Left-aligned, danger-tinted card so a failed turn is unmissable.
   Distinct from the centered .msg-system pill used by all other roles. */
.msg-error-wrap {
  padding: 0.375rem 1rem;
}

.msg-error-card {
  display: flex;
  align-items: flex-start;
  gap: 0.625rem;
  padding: 0.75rem 1rem;
  border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  border-left: 3px solid var(--danger);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  animation: errorCardIn var(--dur-base) var(--ease-out) both;
}

.msg-error-card__icon {
  flex-shrink: 0;
  color: var(--danger);
  margin-top: 0.0625rem;
}

.msg-error-card__body {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
  flex: 1;
}

.msg-error-card__heading {
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--danger);
  line-height: 1.3;
}

.msg-error-card__text {
  font-size: 0.8125rem;
  color: var(--text-muted);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.msg-error-card__resume {
  align-self: flex-start;
  margin-top: 0.25rem;
  padding: 0.3125rem 0.75rem;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 12%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--dur-fast);
}

.msg-error-card__resume:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 20%, var(--bg-surface));
}

.msg-error-card__resume:disabled {
  opacity: 0.55;
  cursor: default;
}

.msg-error-card__time {
  flex-shrink: 0;
  align-self: center;
  font-size: var(--fs-xs);
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

.msg-error-wrap:hover .msg-error-card__time {
  opacity: 1;
}

@media (hover: none) {
  .msg-error-card__time {
    opacity: 1;
  }
}

@keyframes errorCardIn {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .msg-error-card {
    animation: none;
  }

  .msg-error-card__time {
    transition: none;
  }
}

.chat-subagent-disclosure {
  margin: 0;
}

.chat-subagent-disclosure-summary {
  font-weight: 500;
  cursor: pointer;
  padding: 0.25rem 0;
}

.chat-subagent-disclosure-body {
  padding: 0.5rem;
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  font-size: 0.8125rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
}
</style>
