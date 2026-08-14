<template>
  <div
    class="chat-compaction-event"
    :class="{
      'chat-compaction-event--running': maintenance?.state === 'running',
      'chat-compaction-event--failed': maintenance?.state === 'failed',
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
    <span class="chat-compaction-event__marker" aria-hidden="true" />
    <span class="chat-compaction-event__title">{{ t(labelCode) }}</span>
    <span v-if="maintenance?.detail" class="chat-compaction-event__detail">
      {{ maintenance.detail }}
    </span>
    <span
      v-else-if="maintenance?.durability === 'request_scoped'"
      class="chat-compaction-event__detail"
    >
      {{ t('chat.compact.requestScoped') }}
    </span>
    <time v-if="message.timeStr" class="chat-compaction-event__detail">
      {{ message.timeStr }}
    </time>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { compactionSkippedLabelCode } from '@/utils/chat/compactionStatus'

const props = defineProps<{
  message: ChatRenderedMessage
}>()

const { t } = useI18n()
const maintenance = computed(() => props.message.maintenance)
const labelCode = computed(() => {
  if (maintenance.value?.state === 'running') return 'chat.compact.compacting'
  if (maintenance.value?.state === 'failed') return 'chat.compact.failed'
  if (maintenance.value?.state === 'skipped') {
    return compactionSkippedLabelCode(maintenance.value.reason)
  }
  if (maintenance.value?.state === 'stale' || maintenance.value?.state === 'cancelled') {
    return 'chat.compact.cancelled'
  }
  if (maintenance.value?.historyArchived) {
    if (maintenance.value.canonicalComplete === true) return 'chat.compact.historyPreserved'
    if (maintenance.value.canonicalComplete === false) return 'chat.compact.historyIncomplete'
    return 'chat.compact.historySummarized'
  }
  return 'chat.compact.compacted'
})
const liveRole = computed(() => {
  if (props.message.restoredFromHistory) return undefined
  return maintenance.value?.state === 'failed' ? 'alert' : 'status'
})
const liveMode = computed(() => {
  if (!liveRole.value) return undefined
  return maintenance.value?.state === 'failed' ? 'assertive' : 'polite'
})
</script>

<style scoped>
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

@media (prefers-reduced-motion: reduce) {
  .chat-compaction-event--running .chat-compaction-event__marker {
    animation: none;
  }
}
</style>
