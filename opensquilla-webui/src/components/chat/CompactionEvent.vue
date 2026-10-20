<template>
  <div
    class="chat-compaction-event"
    :class="{
      'chat-compaction-event--running': maintenance?.state === 'running' && maintenance.providerRecovery?.phase !== 'retry_wait',
      'chat-compaction-event--failed': isFailure,
    }"
    data-testid="compaction-event"
    :data-compaction-id="maintenance?.compactionId"
    :data-status="maintenance?.state"
    :data-source="maintenance?.source"
    :data-durability="maintenance?.durability"
    data-placement="transcript"
    :role="liveRole"
    :aria-live="liveMode"
    :aria-atomic="liveRole ? 'true' : undefined"
  >
    <CompactionRecoveryMarker v-if="temporaryRecovery" />
    <span v-else class="chat-compaction-event__marker" aria-hidden="true" />
    <div class="chat-compaction-event__body">
      <span class="chat-compaction-event__title">{{ t(labelCode) }}</span>
      <span v-if="recoveryLabel" :aria-hidden="maintenance?.providerRecovery?.phase === 'retry_wait' ? 'true' : undefined"> · {{ recoveryLabel }}</span>
      <span v-if="recoveryAnnouncement" class="sr-only">{{ recoveryAnnouncement }}</span>
      <span v-if="detailLabelCode" class="chat-compaction-event__detail">
        {{ t(detailLabelCode) }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import CompactionRecoveryMarker from './CompactionRecoveryMarker.vue'
import { providerActivityLabel } from '@/utils/chat/assistantActivity'
import { chatErrorPresentation } from '@/utils/chat/chatErrorPresentation'
import { useProviderRecoveryClock } from '@/composables/chat/useProviderRecoveryClock'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import {
  compactionCompletedLabelCode,
  compactionFailurePresentation,
  compactionSkippedLabelCode,
} from '@/utils/chat/compactionStatus'

const props = defineProps<{
  message: ChatRenderedMessage
}>()

const { t } = useI18n()
const maintenance = computed(() => props.message.maintenance)
const temporaryRecovery = computed(() => maintenance.value?.state === 'completed' && maintenance.value.durability === 'request_scoped')
// A failed automatic attempt may still be recovered by the owning turn.
// Keep its receipt factual; the turn's SystemMessage owns the terminal error.
const isFailure = computed(() => maintenance.value?.state === 'failed' && maintenance.value.source === 'manual')
const now = useProviderRecoveryClock(() => !props.message.restoredFromHistory && maintenance.value?.state === 'running' && maintenance.value.providerRecovery?.phase === 'retry_wait')
function recoveryText(announce = false): string {
  const value = maintenance.value
  if (!value?.providerRecovery || value.state !== 'running') return ''
  const label = providerActivityLabel({ key: value.compactionId, at: 0, isCurrent: !props.message.restoredFromHistory, providerRecovery: value.providerRecovery, label: { code: 'chat.compact.compacting', params: {} } }, now.value, announce)
  return String(t(label.code, label.params))
}
const recoveryLabel = computed(() => recoveryText())
const recoveryAnnouncement = computed(() => maintenance.value?.providerRecovery?.phase === 'retry_wait' ? recoveryText(true) : '')
const failure = computed(() => compactionFailurePresentation(maintenance.value?.reason))
const labelCode = computed(() => {
  if (temporaryRecovery.value) return 'chat.compact.compacted'
  if (maintenance.value?.state === 'running') return 'chat.compact.compacting'
  if (maintenance.value?.state === 'failed') return isFailure.value ? failure.value.title : 'chat.compact.incomplete'
  if (maintenance.value?.state === 'skipped') {
    return compactionSkippedLabelCode(maintenance.value.reason)
  }
  if (maintenance.value?.state === 'stale' || maintenance.value?.state === 'cancelled') {
    return 'chat.compact.cancelled'
  }
  return compactionCompletedLabelCode(maintenance.value?.durability)
})
const detailLabelCode = computed(() => {
  if (temporaryRecovery.value) return undefined
  if (isFailure.value && maintenance.value?.failureKind) return chatErrorPresentation({ failureKind: maintenance.value.failureKind }).messageKey
  if (isFailure.value) {
    return failure.value.detail
  }
  if (maintenance.value?.historyArchived && maintenance.value.canonicalComplete === false) {
    return 'chat.compact.historyIncomplete'
  }
  return ''
})
const liveRole = computed(() => {
  if (props.message.restoredFromHistory) return undefined
  return isFailure.value ? 'alert' : 'status'
})
const liveMode = computed(() => {
  if (!liveRole.value) return undefined
  return isFailure.value ? 'assertive' : 'polite'
})
</script>

<style scoped>
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
/* This event is rendered below ChatMessageList, so it must own its styles.
   ChatView's scoped stylesheet cannot reach through that component boundary. */
.chat-compaction-event {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  width: var(--chat-col);
  max-width: calc(100% - 48px);
  min-height: 1.75rem;
  margin: 0.125rem auto 0.625rem;
  padding: 0.25rem 0.125rem;
  color: color-mix(in srgb, var(--text) 58%, transparent);
  font-size: var(--fs-xs);
  line-height: 1.45;
}

.chat-compaction-event__marker {
  width: 0.5rem;
  height: 0.5rem;
  flex: 0 0 auto;
  margin: 0 0.1875rem;
  border: 1px solid currentColor;
  border-radius: var(--radius-full);
}

.chat-compaction-event--running .chat-compaction-event__marker {
  border-color: var(--accent);
  border-right-color: transparent;
  animation: compactionEventSpin 0.9s linear infinite;
}

@keyframes compactionEventSpin {
  to { transform: rotate(360deg); }
}

.chat-compaction-event--failed {
  color: var(--danger);
}

.chat-compaction-event__detail {
  margin-left: auto;
  color: color-mix(in srgb, var(--text) 46%, transparent);
  font-size: 0.75rem;
}

.chat-compaction-event__body {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.25rem;
  min-width: 0;
}

.chat-compaction-event__body .chat-compaction-event__detail {
  margin-left: 0;
}

@media (prefers-reduced-motion: reduce) {
  .chat-compaction-event--running .chat-compaction-event__marker {
    animation: none;
  }
}
</style>
