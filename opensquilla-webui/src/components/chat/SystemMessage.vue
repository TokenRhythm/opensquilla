<template>
  <!-- Keep the reason and its one safe action in the conversation flow. -->
  <div v-if="message.displayRole === 'error'" class="msg-error" :role="errorRole">
    <span v-if="errorText" class="msg-error__text">{{ errorText }}</span>
    <span v-if="hasPartialAnswer" class="msg-error__note">{{ t('chat.partialFailureNote') }}</span>
    <span v-if="settingsTarget || showResume || showRetry" class="msg-error__actions">
      <RouterLink v-if="settingsTarget" class="msg-error__action"
        :class="{ 'msg-error__capacity': isCapacityError }"
        :to="settingsTarget">{{ actionLabel }}</RouterLink>
      <button
        v-else-if="showResume"
        type="button"
        class="msg-error__action msg-error__resume"
        :disabled="resolving"
        @click="onResume"
      >{{ t('chat.errorAction.resumeSandbox') }}</button>
      <button
        v-else-if="showRetry"
        type="button"
        class="msg-error__action msg-error__resume"
        :disabled="retryResolving"
        @click="onRetry"
      >{{ t('chat.errorAction.retryUsageReplay') }}</button>
    </span>
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
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { absoluteTime, fullTime, isoTime } from '@/utils/messageTime'
import { chatErrorPresentation } from '@/utils/chat/chatErrorPresentation'
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
  resumeAvailable?: boolean
  hasPartialAnswer?: boolean
}>()

// Owner-driven recovery: a run paused by the sandbox denial ledger surfaces as a
// terminal error carrying this code. Offer a Resume action that the parent wires
// to the sandbox.resume RPC. Resume is idempotent, but we disable the button
// after one click to avoid duplicate confirmations.
const emit = defineEmits<{
  resume: [message: ChatRenderedMessage]
  retry: [message: ChatRenderedMessage, settle: (accepted: boolean) => void]
}>()
const resolving = ref(false)
const retryResolving = ref(false)
watch(
  () => [props.resumeAvailable, props.message.turnId, props.message.turnOutcome?.turnId] as const,
  ([available]) => {
    if (available) resolving.value = false
  },
)
const errorCode = computed(() => props.message.errorCode || props.message.turnOutcome?.errorClass)
const presentation = computed(() => chatErrorPresentation({
  code: errorCode.value,
  failureKind: props.message.turnOutcome?.failureKind,
  terminalStatus: props.message.turnOutcome?.status,
  reason: props.message.turnOutcome?.reason,
  cancellationSource: props.message.turnOutcome?.cancellationSource,
  outcomeKind: props.message.turnOutcome?.kind,
  replaySafe: hasStrictUsageBarrierReplayProof(props.message),
}))
const errorText = computed(() => localizedChatErrorMessage(
  errorCode.value,
  '',
  hasStrictUsageBarrierReplayProof(props.message),
  props.message.turnOutcome?.failureKind,
  props.message.turnOutcome?.status,
  {
    reason: props.message.turnOutcome?.reason,
    cancellationSource: props.message.turnOutcome?.cancellationSource,
    outcomeKind: props.message.turnOutcome?.kind,
  },
))
const errorRole = computed(() => [
  'chat.errorMessage.stopped', 'chat.errorMessage.interrupted',
  'chat.errorMessage.approvalRequired', 'chat.errorMessage.needsConfirmation',
  'chat.errorMessage.runLimit',
].includes(presentation.value.messageKey) ? 'status' : 'alert')
const isCapacityError = computed(() => presentation.value.messageKey === 'chat.errorMessage.contextLimit')
const settingsTarget = computed(() => {
  const action = presentation.value.action
  if (action === 'open-provider-settings') return { path: '/settings/provider' }
  if (action !== 'open-model-settings' && action !== 'choose-model') return undefined
  const capacity = isCapacityError.value ? props.message.modelCapacity : undefined
  return capacity
    ? { path: '/settings/modelStrategy', query: { capacityProvider: capacity.provider, capacityModel: capacity.model } }
    : { path: '/settings/modelStrategy' }
})
const actionLabel = computed(() => {
  const action = presentation.value.action
  if (action === 'open-provider-settings') return t('chat.errorAction.openProviderSettings')
  if (action === 'choose-model') return t('chat.errorAction.chooseModel')
  return t('chat.errorAction.openModelSettings')
})
const showResume = computed(
  () =>
    props.message.displayRole === 'error' &&
    presentation.value.action === 'resume-sandbox' &&
    props.resumeAvailable === true,
)
const isUsageBarrier = computed(
  () => props.message.displayRole === 'error'
    && isUsageAccountingBarrierMessage(props.message),
)
const showRetry = computed(
  () =>
    isUsageBarrier.value
    && presentation.value.action === 'retry-usage-replay'
    && hasStrictUsageBarrierReplayProof(props.message)
    && props.retryAvailable === true,
)

function onResume() {
  if (resolving.value || !showResume.value) return
  resolving.value = true
  emit('resume', props.message)
}

function onRetry() {
  if (retryResolving.value || !showRetry.value) return
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
  font-size: 0.8125rem;
  color: var(--text-muted);
  line-height: 1.5;
  text-align: center;
  overflow-wrap: anywhere;
}

.msg-error__text,
.msg-error__note {
  margin: 0;
  white-space: normal;
}

.msg-error__note,
.msg-error__actions {
  margin-inline-start: 0.5rem;
}

.msg-error__action {
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: inherit;
  text-decoration: underline;
  text-underline-offset: 0.15em;
  user-select: text;
  cursor: pointer;
}

.msg-error__action:disabled {
  opacity: 0.55;
  cursor: default;
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
