<template>
  <section class="chat-trace-panel" :aria-label="t('chat.traceView.trajectory')">
    <header class="chat-trace-panel__toolbar">
      <span :class="['chat-trace-panel__state', { 'chat-trace-panel__state--live': live }]">{{ t(live ? 'chat.traceView.live' : 'chat.traceView.snapshot') }}</span>
      <span v-if="projection" class="chat-trace-panel__state" :title="activeTraceId || ''">{{ t(`usageLogs.logs.traceStatus.${displayStatus}`, displayStatus) }}</span>
      <label v-if="traces.length > 1" class="chat-trace-panel__attempts">
        {{ t('chat.traceView.attempts') }}
        <select :value="activeTraceId || ''" @change="selectAttempt">
          <option v-for="(trace, index) in traces" :key="trace.trace_id" :value="trace.trace_id">{{ t('chat.traceView.attempt', { number: index + 1 }) }} · {{ t(`usageLogs.logs.traceStatus.${trace.status}`, trace.status) }}</option>
        </select>
      </label>
      <button type="button" :disabled="loading || !hasTurn" @click="refresh">{{ t('chat.traceView.refresh') }}</button>
    </header>
    <p v-if="error" class="chat-trace-panel__error" role="alert">{{ t('chat.traceView.loadFailed') }} <span>{{ error }}</span></p>
    <TraceTimeline
      v-if="projection"
      :key="activeTraceId || undefined"
      :projection="projection"
      compact
      :details="details?.rows"
      :details-available="details?.available"
      :details-reason="details?.reason"
      :clock-origin="details?.clockOrigin"
      :allow-full-payload="details?.available === true && !payloadRestricted"
      :full-payload="payload && selectedRow ? { ...payload, rowId: selectedRow.id } : undefined"
      :full-payload-loading="payloadLoading"
      :full-payload-error="payloadError"
      :full-payload-unavailable="payloadUnavailable"
      :full-payload-restricted="payloadRestricted"
      @select="selectRow"
      @load-payload="loadPayload"
    />
    <p v-else class="chat-trace-panel__empty" role="status">{{ emptyText }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useChatTrace } from '@/composables/trace/useChatTrace'
import { isTraceTerminal, traceTerminalStatus } from '@/utils/traceProjection'
import TraceTimeline from './TraceTimeline.vue'

const props = defineProps<{ sessionKey: string; turnId: string; running: boolean }>()
const { t } = useI18n()
const { traces, activeTraceId, projection, details, live, loading, error, rawEnabled, hasTurn, refresh, selectTrace, selectedRow, selectRow, payload, payloadLoading, payloadError, payloadUnavailable, payloadRestricted, loadPayload } = useChatTrace({ sessionKey: () => props.sessionKey, turnId: () => props.turnId, running: () => props.running })
const displayStatus = computed(() => {
  const rows = details.value?.rows.length ? details.value.rows : projection.value?.spans || []
  const result = [...rows].reverse().find(isTraceTerminal)
  return result ? traceTerminalStatus(result) : projection.value?.status || 'unknown'
})
const emptyText = computed(() => {
  if (!hasTurn.value) return t('chat.traceView.waitingForTurn')
  if (loading.value) return t('chat.traceView.loading')
  if (rawEnabled.value === false) return t('usageLogs.logs.traceDetailDisabled')
  return t(live.value ? 'chat.traceView.waitingForTrace' : 'chat.traceView.noTrace')
})
function selectAttempt(event: Event) { selectTrace((event.target as HTMLSelectElement).value) }
</script>

<style scoped>
.chat-trace-panel { color: var(--text); min-width: 0; padding: 8px 12px 16px; }
.chat-trace-panel__toolbar { align-items: center; display: flex; flex-wrap: wrap; gap: 10px; min-height: 28px; }
.chat-trace-panel__state { color: var(--text-muted); font-size: 11px; }.chat-trace-panel__state--live { color: var(--accent); }
.chat-trace-panel__attempts { align-items: center; color: var(--text-muted); display: flex; font-size: 11px; gap: 6px; }
.chat-trace-panel__toolbar button, .chat-trace-panel__attempts select { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text); font-size: 11px; padding: 4px 8px; }
.chat-trace-panel__toolbar button { cursor: pointer; margin-left: auto; }.chat-trace-panel__toolbar button:disabled { cursor: default; opacity: .5; }
.chat-trace-panel__empty { color: var(--text-muted); font-size: 12px; padding: 28px 8px; text-align: center; }
.chat-trace-panel__error { color: var(--danger); font-size: 11px; line-height: 1.5; }.chat-trace-panel__error span { overflow-wrap: anywhere; }
.chat-trace-panel :deep(.trace-timeline) { margin: 8px 0 0; }
</style>
