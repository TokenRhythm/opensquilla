<template>
  <!-- Terminal errors are plain conversation text, not a separate panel. -->
  <div v-if="message.displayRole === 'error'" class="msg-error" role="alert">
    <p v-if="errorText" class="msg-error__text">{{ errorText }}</p>
    <p v-if="hasPartialAnswer" class="msg-error__note">{{ t('chat.partialFailureNote') }}</p>
    <div v-if="diagnosticId || showModelCapacity || showResume || showRetry || timeIso" class="msg-error__actions">
      <RouterLink v-if="showModelCapacity" class="msg-error__action msg-error__capacity"
        :to="{ path: '/settings/modelStrategy', query: message.modelCapacity ? {
          capacityProvider: message.modelCapacity.provider, capacityModel: message.modelCapacity.model,
        } : {} }">{{ t('setup.capacity.title') }}</RouterLink>
      <button
        v-if="diagnosticId"
        type="button"
        class="msg-error__action msg-error__copy"
        @click="copyDiagnosticId"
      >{{ copied ? t('chat.copiedDiagnosticId') : t('chat.copyDiagnosticId') }}: {{ diagnosticId }}</button>
      <button
        v-if="showResume"
        type="button"
        class="msg-error__action msg-error__resume"
        :disabled="resolving"
        @click="onResume"
      >{{ t('chat.sandboxPausedResume') }}</button>
      <button
        v-if="showRetry"
        type="button"
        class="msg-error__action msg-error__resume"
        :disabled="retryResolving"
        @click="onRetry"
      >{{ t('chat.retry') }}</button>
      <time v-if="timeIso" class="msg-error__time" :datetime="timeIso" :title="timeFull">{{ timeAbs }}</time>
    </div>
    <span v-if="diagnosticId && copyFailed" class="msg-error__note" role="status">{{ t('chat.copyDiagnosticFailed') }}</span>
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
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { absoluteTime, fullTime, isoTime } from '@/utils/messageTime'
import { copyTextWithFallback } from '@/utils/browser'
import { diagnosticErrorId } from '@/utils/chat/providerFailure'
import { localizedChatErrorMessage } from '@/utils/chat/errors'
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
  hasPartialAnswer?: boolean
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
const showModelCapacity = computed(() => ['provider_request_too_large', 'provider_request_budget_exhausted'].includes(props.message.errorCode || ''))
const copied = ref(false)
const copyFailed = ref(false)
const diagnosticId = computed(() => props.message.turnId && props.message.turnId === props.message.turnOutcome?.turnId
  ? diagnosticErrorId(props.message.turnOutcome?.errorId)
  : undefined)
const errorText = computed(() => localizedChatErrorMessage(
  props.message.errorCode, props.message.text,
  props.message.turnOutcome?.replaySafe === true, props.message.turnOutcome?.failureKind,
  props.message.turnOutcome?.status,
))

async function copyDiagnosticId() {
  if (!diagnosticId.value) return
  try {
    await copyTextWithFallback(diagnosticId.value)
    copied.value = true
    copyFailed.value = false
  } catch {
    copyFailed.value = true
  }
}
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

.msg-error {
  padding: 0.375rem 1.5rem;
  font-size: 0.875rem;
  color: var(--text-muted);
  line-height: 1.5;
}

.msg-error__text,
.msg-error__note {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.msg-error__note,
.msg-error__actions {
  font-size: 0.8125rem;
  color: var(--text-dim);
}

.msg-error__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.25rem 0.75rem;
}

.msg-error__action {
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: start;
  text-decoration: underline;
  text-underline-offset: 0.15em;
  user-select: text;
  cursor: pointer;
}

.msg-error__action:disabled {
  opacity: 0.55;
  cursor: default;
}

.msg-error__time {
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  opacity: 0;
  transition: opacity var(--dur-fast);
}

.msg-error:hover .msg-error__time {
  opacity: 1;
}

@media (hover: none) {
  .msg-error__time {
    opacity: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .msg-error__time {
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
