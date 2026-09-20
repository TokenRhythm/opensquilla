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
    <div class="chat-compaction-event__body">
      <span class="chat-compaction-event__title">{{ t(labelCode) }}</span>
      <span v-if="detailLabelCode" class="chat-compaction-event__detail">
        {{ t(detailLabelCode) }}
      </span>
      <span v-else-if="maintenance?.state === 'failed' && maintenance.detail" class="chat-compaction-event__detail">
        {{ maintenance.detail }}
      </span>
      <button
        v-if="maintenance?.state === 'failed' && maintenance.compactionId"
        type="button"
        class="chat-compaction-event__diagnostic"
        :title="maintenance.compactionId"
        @click="copyDiagnosticId"
      >{{ copied ? t('chat.copiedDiagnosticId') : t('chat.copyDiagnosticId') }}</button>
      <span v-if="copyFailed" class="chat-compaction-event__detail" role="status">
        {{ t('chat.copyDiagnosticFailed') }} {{ maintenance?.compactionId }}
      </span>
    </div>
    <time v-if="message.timeStr" class="chat-compaction-event__detail">
      {{ message.timeStr }}
    </time>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { copyTextWithFallback } from '@/utils/browser'
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
const copied = ref(false)
const copyFailed = ref(false)
const failure = computed(() => compactionFailurePresentation(maintenance.value?.reason))
async function copyDiagnosticId() {
  if (!maintenance.value?.compactionId) return
  try {
    await copyTextWithFallback(maintenance.value.compactionId)
    copied.value = true
    copyFailed.value = false
  } catch {
    copyFailed.value = true
  }
}
const labelCode = computed(() => {
  if (maintenance.value?.state === 'running') return 'chat.compact.compacting'
  if (maintenance.value?.state === 'failed') return failure.value.title
  if (maintenance.value?.state === 'skipped') {
    return compactionSkippedLabelCode(maintenance.value.reason)
  }
  if (maintenance.value?.state === 'stale' || maintenance.value?.state === 'cancelled') {
    return 'chat.compact.cancelled'
  }
  return compactionCompletedLabelCode(maintenance.value?.durability)
})
const detailLabelCode = computed(() => {
  if (maintenance.value?.state === 'failed' && maintenance.value.source === 'manual') {
    return failure.value.detail
  }
  if (maintenance.value?.historyArchived && maintenance.value.canonicalComplete === false) {
    return 'chat.compact.historyIncomplete'
  }
  return ''
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

.chat-compaction-event__diagnostic {
  padding: 0;
  border: 0;
  background: transparent;
  color: color-mix(in srgb, var(--text) 58%, transparent);
  font: inherit;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 0.15em;
}

@media (prefers-reduced-motion: reduce) {
  .chat-compaction-event--running .chat-compaction-event__marker {
    animation: none;
  }
}
</style>
