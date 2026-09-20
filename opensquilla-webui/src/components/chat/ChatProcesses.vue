<template>
  <section v-if="processes.length || error" class="chat-processes" data-testid="background-processes" :aria-label="t('processes.title')">
    <div class="process-header">
      <button class="process-summary" :aria-expanded="expanded" @click="expanded = !expanded">
        <Icon :name="expanded ? 'chevronDown' : 'chevronRight'" :size="14" />
        <span>{{ t('processes.title') }}</span>
        <span>{{ runningCount ? t('processes.runningCount', { count: runningCount }) : processes.length }}</span>
      </button>
      <button class="process-action" :disabled="loading || !gateway.isAvailable" :title="t('processes.refresh')"
        :aria-label="t('processes.refresh')" @click="refresh"><Icon name="refresh" :size="14" /></button>
    </div>
    <p v-if="error" class="process-error" role="status">{{ t('processes.loadError') }}</p>
    <div v-if="expanded" class="process-list">
      <div v-for="process in processes" :key="process.executionId" class="process-row" :data-status="process.status" :data-testid="`background-process-${process.executionId}`">
        <code class="process-command" :title="process.command">{{ process.command }}</code>
        <span class="process-status">{{ statusLabel(process) }}</span>
        <button class="process-action" :disabled="!gateway.isAvailable" :title="t('processes.logs')"
          :aria-label="t('processes.logs')" @click="inspect(process.executionId)"><Icon name="logs" :size="14" /></button>
        <button v-if="process.status === 'running'" class="process-action" :disabled="!!stopping || !gateway.isAvailable || !processAccess.canStop"
          :title="t('processes.stop')" :aria-label="t('processes.stop')" @click="stop(process.executionId)"><Icon name="stop" :size="14" /></button>
      </div>
      <p v-if="stopError" class="process-error" role="status">{{ t('processes.stopError') }}</p>
      <div v-if="selectedId" class="process-output">
        <div class="process-header">
          <span>{{ t('processes.logs') }}</span>
          <div class="process-output-actions">
            <button class="process-action" :disabled="logLoading || !gateway.isAvailable" :title="t('processes.refresh')"
              :aria-label="t('processes.refresh')" @click="inspect(selectedId)"><Icon name="refresh" :size="14" /></button>
            <button class="process-action" :title="t('processes.close')" :aria-label="t('processes.close')" @click="closeLog"><Icon name="x" :size="14" /></button>
          </div>
        </div>
        <p v-if="logLoading" role="status">{{ t('processes.loading') }}</p>
        <p v-else-if="logError" class="process-error" role="status">{{ t('processes.logError') }}</p>
        <template v-else-if="log">
          <pre>{{ log.output || t('processes.emptyLog') }}</pre>
          <p v-if="log.truncated">{{ t('processes.truncated') }}</p>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, toRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useSessionProcesses } from '@/composables/chat/useSessionProcesses'
import type { ConversationEventHub } from '@/modules/conversationEventHub'
import type { ConversationEvent } from '@/modules/conversationEvents'
import type { GatewayAccess } from '@/modules/gatewayAccess'
import type { SessionProcess, SessionProcesses } from '@/modules/sessionProcesses'

const props = defineProps<{ sessionKey: string; gateway: GatewayAccess;
  processAccess: SessionProcesses; events: ConversationEventHub<ConversationEvent> }>()
const { t } = useI18n()
const expanded = ref(false)
const { processes, runningCount, loading, error, selectedId, log, logLoading, logError,
  stopping, stopError, refresh, inspect, closeLog, stop } = useSessionProcesses({
  sessionKey: toRef(props, 'sessionKey'), gateway: props.gateway, processes: props.processAccess, events: props.events,
})
function statusLabel(process: SessionProcess) {
  if (process.status === 'done') return t(process.returncode === 0 ? 'processes.done' : 'processes.failed', { code: process.returncode ?? '?' })
  return t(`processes.${process.status}`)
}
</script>

<style scoped>
.chat-processes { width: min(calc(100% - 3rem), calc(var(--composer-col, 820px) - 1rem)); margin: 0 auto 8px; font-size: 12px; color: var(--text-muted); }
.process-header, .process-summary, .process-output-actions { display: flex; align-items: center; gap: 8px; }
.process-header { justify-content: space-between; min-height: 30px; }
.process-summary, .process-action { border: 0; background: transparent; color: inherit; cursor: pointer; }
.process-summary { min-width: 0; padding: 4px 0; text-align: start; }
.process-action { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; flex-shrink: 0; border-radius: var(--radius-sm); }
.process-action:not(:disabled):hover { color: var(--text); background: var(--bg-hover); }
.process-action:disabled { opacity: 0.5; cursor: default; }
.process-list { max-height: 280px; overflow: auto; }
.process-row { display: grid; grid-template-columns: minmax(0, 1fr) auto 28px 28px; align-items: center; gap: 6px; min-height: 34px; border-top: 1px solid var(--border); }
.process-command { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.process-status { white-space: nowrap; }
.process-error { color: var(--danger); }
.process-output { border-top: 1px solid var(--border); padding: 6px 0; }
.process-output pre { max-height: 160px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; margin: 4px 0; color: var(--text); }
.process-output p { margin: 4px 0; }
@media (max-width: 640px) { .chat-processes { width: calc(100% - 2rem); } }
</style>
