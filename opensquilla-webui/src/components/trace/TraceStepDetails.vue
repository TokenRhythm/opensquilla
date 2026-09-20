<template>
  <div class="trace-step-details">
    <div class="trace-detail-tabs" role="tablist" :aria-label="t(`${ns}.recordInfo`)">
      <button v-for="tab in tabs" :id="`${instanceId}-${tab}`" :key="tab" type="button" role="tab" :aria-selected="activeTab === tab" :aria-controls="`${instanceId}-panel`" :tabindex="activeTab === tab ? 0 : -1" :data-detail-tab="tab" @click="activeTab = tab" @keydown="moveTab($event, tab)">{{ t(`${ns}.${tab}`) }}</button>
    </div>
    <div :id="`${instanceId}-panel`" class="trace-detail-panel" role="tabpanel" :aria-labelledby="`${instanceId}-${activeTab}`">
      <template v-if="activeTab === 'overview'">
        <p class="trace-detail-summary">{{ t(`${ns}.${model.summaryKey}`) }}</p>
        <dl class="trace-detail-facts">
          <div><dt>{{ t(`${ns}.stage`) }}</dt><dd>{{ t(`usageLogs.logs.traceCategory.${model.category}`) }}</dd></div>
          <div><dt>{{ t(`${ns}.status`) }}</dt><dd :class="`trace-detail-facts__status--${row.status}`">{{ t(`usageLogs.logs.traceStatus.${row.status}`) }}</dd></div>
          <div v-for="fact in facts" :key="fact.key" :data-fact="fact.key"><dt>{{ factLabel(fact.key) }}</dt><dd>{{ factText(fact.value, fact.key) }}</dd></div>
        </dl>
        <div class="trace-detail-content">
          <TraceContentBlock v-for="block in overviewBlocks" :key="block.id" :block="block" />
          <p v-if="!overviewBlocks.length" class="trace-detail-empty">{{ t(`${ns}.${row.status === 'running' ? 'waitingOutput' : 'noOutput'}`) }}</p>
        </div>
      </template>
      <div v-else-if="activeTab === 'input'" class="trace-detail-content">
        <TraceContentBlock v-for="block in model.inputs" :key="block.id" :block="block" />
        <p v-if="!model.inputs.length" class="trace-detail-empty">{{ t(`${ns}.noInput`) }}</p>
      </div>
      <div v-else-if="activeTab === 'preview'" class="trace-detail-content">
        <TraceContentBlock v-for="block in previewBlocks" :key="block.id" :block="block" />
        <p v-if="!previewBlocks.length" class="trace-detail-empty">{{ t(`${ns}.${row.status === 'running' ? 'waitingOutput' : 'noOutput'}`) }}</p>
      </div>
      <div v-else class="trace-detail-content">
        <TraceContentBlock v-for="block in rawBlocks" :key="block.id" :block="block" />
      </div>
      <p v-if="row.kind === 'llm_progress'" class="trace-detail-notice" role="status">{{ t(`${ns}.partialNotice`) }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TraceSpan } from '@/types/traceView'
import { traceInspectorModel, type InspectorBlock } from '@/utils/traceInspector'
import TraceContentBlock from './TraceContentBlock.vue'

const props = defineProps<{ row: TraceSpan; input?: unknown; output?: unknown; preferredTab?: 'overview' | 'input' }>()
const { t, te } = useI18n()
const ns = 'usageLogs.logs.traceInspectorView'
const instanceId = useId()
const tabs = ['overview', 'input', 'preview', 'raw'] as const
type DetailTab = typeof tabs[number]
const activeTab = ref<DetailTab>(props.preferredTab || 'overview')
watch([() => props.row.id, () => props.preferredTab], () => { activeTab.value = props.preferredTab || 'overview' })
const model = computed(() => traceInspectorModel(props.row, props.input, props.output))
const facts = computed(() => {
  const values = new Map<string, unknown>(model.value.facts.map(fact => [fact.key, fact.value]))
  if (props.row.provider) values.set('provider', props.row.provider)
  if (props.row.model) values.set('model', props.row.model)
  return [...values].filter(([, value]) => value != null && value !== '').map(([key, value]) => ({ key, value }))
})
const previewBlocks = computed(() => [
  ...model.value.inputs.filter(block => props.row.phase === 'tool_execution' && (block.labelKey === 'blocks.content' || block.labelKey === 'blocks.changes')),
  ...model.value.outputs,
])
const overviewBlocks = computed(() => previewBlocks.value.length ? previewBlocks.value : model.value.inputs)
const rawBlocks = computed<InspectorBlock[]>(() => [
  { id: 'raw-input', label: '', labelKey: 'input', value: JSON.stringify(props.input ?? null, null, 2), format: 'code', language: 'json' },
  { id: 'raw-output', label: '', labelKey: 'blocks.output', value: JSON.stringify(props.output ?? null, null, 2), format: 'code', language: 'json' },
])
function factLabel(key: string): string {
  if (te(`${ns}.facts.${key}`)) return t(`${ns}.facts.${key}`)
  return te(`${ns}.${key}`) ? t(`${ns}.${key}`) : key
}
function factText(value: unknown, key?: string): string {
  if ((key === 'requested_mode' || key === 'effective_mode') && typeof value === 'string') {
    return t(`usageLogs.logs.traceRoutingModes.${value}`, value)
  }
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}
function moveTab(event: KeyboardEvent, tab: DetailTab) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const current = tabs.indexOf(tab)
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
  activeTab.value = tabs[index]
  document.getElementById(`${instanceId}-${activeTab.value}`)?.focus()
}
</script>

<style scoped>
.trace-step-details { min-width: 0; margin-top: 14px; }
.trace-detail-tabs { display: flex; gap: 16px; border-bottom: 1px solid var(--border); }
.trace-detail-tabs button { background: transparent; border: 0; border-bottom: 2px solid transparent; padding: 8px 0; color: var(--text-muted); cursor: pointer; font-size: 12px; white-space: nowrap; }
.trace-detail-tabs button[aria-selected='true'] { color: var(--accent); border-bottom-color: var(--accent); font-weight: 650; }
.trace-detail-tabs button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.trace-detail-panel { padding-top: 12px; }
.trace-detail-summary { color: var(--text-muted); font-size: 12px; line-height: 1.7; margin: 0 0 12px; }
.trace-detail-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 18px; margin: 0 0 16px; padding: 12px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-md); }
.trace-detail-facts > div { min-width: 0; }.trace-detail-facts dt { font-size: 10px; color: var(--text-dim); margin-bottom: 3px; }.trace-detail-facts dd { font-size: 12px; color: var(--text); margin: 0; overflow-wrap: anywhere; }
.trace-detail-facts > [data-fact='path'], .trace-detail-facts > [data-fact='command'] { grid-column: 1 / -1; }
.trace-detail-facts .trace-detail-facts__status--error { color: var(--danger); }.trace-detail-facts .trace-detail-facts__status--running { color: var(--accent); }
.trace-detail-content { display: grid; gap: 10px; min-width: 0; }
.trace-detail-empty { color: var(--text-dim); font-size: 12px; line-height: 1.6; padding: 14px 0; }
.trace-detail-notice { color: var(--text-muted); font-size: 11px; line-height: 1.6; margin: 12px 0 0; }
@media (max-width: 520px) { .trace-detail-tabs { gap: 12px; }.trace-detail-tabs button { font-size: 11px; } }
@media (max-width: 340px) { .trace-detail-facts { grid-template-columns: minmax(0, 1fr); } }
</style>
