<template>
  <section class="trace-timeline" :data-status="displayStatus" :aria-label="t('usageLogs.logs.traceTitle')">
    <header v-if="!compact" class="trace-timeline__header">
      <div class="trace-timeline__heading">
        <div class="trace-timeline__eyebrow">{{ t('usageLogs.logs.traceTitle') }}</div>
        <h2 class="trace-timeline__title">{{ modeLabel }}</h2>
        <p class="trace-timeline__meta">
          <span>{{ projection.traceId }}</span>
          <span v-if="projection.runId">{{ t('usageLogs.logs.traceRun', { id: projection.runId }) }}</span>
          <span v-if="projection.currentSeq != null">#{{ projection.currentSeq }}</span>
        </p>
      </div>
      <span :class="['trace-timeline__status', `trace-timeline__status--${displayStatus}`]">
        {{ statusLabel(displayStatus) }}
      </span>
    </header>

    <div v-if="!compact" class="trace-timeline__overview" :aria-label="t('usageLogs.logs.traceSummary')">
      <span v-if="projection.requestedMode">{{ t('usageLogs.logs.traceRequested', { mode: projection.requestedMode }) }}</span>
      <span v-if="projection.effectiveMode">{{ t('usageLogs.logs.traceEffective', { mode: projection.effectiveMode }) }}</span>
      <span>{{ t('usageLogs.logs.traceSpans', { count: projection.spans.length }) }}</span>
      <span>{{ projection.complete ? t('usageLogs.logs.traceComplete') : t('usageLogs.logs.traceLive') }}</span>
    </div>

    <p v-if="!detailRows.length && (detailsAvailable || detailsReason)" class="trace-details__empty" role="status">
      {{ detailsReason === 'access_denied' ? t('chat.traceView.detailsRestricted') : detailsReason ? t('usageLogs.logs.traceDetailDisabled') : t('usageLogs.logs.traceNoDetails') }}
    </p>

    <section v-if="displayRows.length" class="trace-visual" :aria-label="t('usageLogs.logs.traceTimeline')">
      <div class="trace-visual__toolbar">
        <div>
          <strong>{{ t('usageLogs.logs.traceTimeline') }}</strong>
          <span v-if="!compact" class="trace-visual__hint">{{ t('usageLogs.logs.traceDetailHint') }}</span>
        </div>
        <span class="trace-visual__count">{{ t(resultItems.length ? 'usageLogs.logs.traceOutcome.count' : 'usageLogs.logs.traceSemantic.count', { steps: operationItems.length, results: resultItems.length, records: displayRows.length }) }}</span>
      </div>
      <p class="trace-visual__sequence-note">{{ t('usageLogs.logs.traceSequenceHint') }}</p>
      <div class="trace-visual__legend-items" :aria-label="t('usageLogs.logs.traceCategories')">
        <span v-for="category in visibleCategories" :key="category" class="trace-visual__legend-item">
          <i :class="['trace-category', `trace-category--${category}`, { 'trace-category--boundary': boundaryCategories.has(category) }]" aria-hidden="true"></i>
          {{ categoryLabel(category) }}
        </span>
      </div>
      <p v-if="projection.requestedMode || projection.effectiveMode" class="trace-visual__routing">
        <span v-if="projection.requestedMode">{{ t('usageLogs.logs.traceRequested', { mode: routingModeLabel(projection.requestedMode) }) }}</span>
        <span v-if="projection.effectiveMode">{{ t('usageLogs.logs.traceEffective', { mode: routingModeLabel(projection.effectiveMode) }) }}</span>
      </p>
      <details v-if="compact" class="trace-visual__help">
        <summary>{{ t('chat.traceView.timingHelp') }}</summary>
        <p class="trace-visual__legend">{{ t('usageLogs.logs.traceTimingLegend') }}</p>
        <p class="trace-visual__origin">{{ t(clockOrigin === 'turn_runner_start' ? 'usageLogs.logs.traceClockRun' : 'usageLogs.logs.traceClockLog') }}</p>
      </details>
      <template v-else>
        <p class="trace-visual__legend">{{ t('usageLogs.logs.traceTimingLegend') }}</p>
        <p class="trace-visual__origin">{{ t(clockOrigin === 'turn_runner_start' ? 'usageLogs.logs.traceClockRun' : 'usageLogs.logs.traceClockLog') }}</p>
      </template>
      <div class="trace-sequence-overview" role="region" :aria-label="t('usageLogs.logs.traceSequenceAxis')">
        <div class="trace-sequence-grid">
          <div class="trace-ruler" aria-hidden="true">
            <span v-for="item in rulerSteps" :key="item.id" :style="{ left: `${(item.index + 0.5) / sequenceItems.length * 100}%` }">{{ operationNumber(item.id) }}</span>
          </div>
          <div class="trace-swimlane">
            <div class="trace-boundary-guides" aria-hidden="true">
              <i v-for="item in boundaryItems" :key="item.row.id" :style="{ left: `${(item.index + 0.5) / sequenceItems.length * 100}%` }"></i>
            </div>
            <div class="trace-control-track" :aria-label="t('usageLogs.logs.traceOutcome.control')">
              <button
                v-for="item in overlayItems"
                :key="item.id"
                type="button"
                :class="['trace-event', 'trace-event--overlay', `trace-event--${item.category}`, { 'trace-event--terminal': item.category === 'result', 'trace-event--boundary': item.category !== 'result' && item.boundary && item.row.durationMs == null, 'trace-event--measured-control': item.category !== 'result' && item.row.durationMs != null, 'trace-event--selected': selectedStep?.id === item.id, 'trace-event--running': stepStatus(item) === 'running', 'trace-event--error': stepStatus(item) === 'error', 'trace-event--cancelled': stepStatus(item) === 'cancelled' }]"
                :style="overlayStyle(item.index)"
                :title="timelineTitle(item)"
                :aria-label="item.category === 'result' ? timelineTitle(item) : `${operationNumber(item.id)}. ${timelineTitle(item)}`"
                :data-event-id="item.id"
                :data-category="item.category"
                :data-boundary="item.boundary || undefined"
                :data-terminal="item.category === 'result' || undefined"
                :data-time-start="recordedRange(item.row).start"
                :data-time-end="recordedRange(item.row).end"
                @click="selectStep(item.id)"
              ></button>
            </div>
            <div v-for="lane in lanes" :key="lane.key" class="trace-lane" :data-lane="lane.key">
              <div class="trace-lane__label"><span :class="['trace-lane__dot', `trace-lane__dot--${lane.key}`]"></span>{{ lane.label }}</div>
              <div class="trace-lane__track">
                <button
                  v-for="item in itemsByLane(lane.key)"
                  :key="item.row.id"
                  type="button"
                  :class="['trace-event', 'trace-event--sequence', `trace-event--${item.category}`, { 'trace-event--boundary': item.boundary, 'trace-event--selected': selectedStep?.id === item.id, 'trace-event--running': stepStatus(item) === 'running', 'trace-event--error': stepStatus(item) === 'error' }]"
                  :style="eventStyle(item.index)"
                  :title="timelineTitle(item)"
                  :aria-label="`${operationNumber(item.id)}. ${timelineTitle(item)}`"
                  :data-event-id="item.id"
                  :data-category="item.category"
                  :data-boundary="item.boundary || undefined"
                  :data-step="operationNumber(item.id)"
                  :data-time-start="recordedRange(item.row).start"
                  :data-time-end="recordedRange(item.row).end"
                  @click="selectStep(item.id)"
                >
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div v-if="selectedStep" class="trace-visual__selection" aria-live="polite">
        <strong>{{ stepTitle(selectedStep) }}</strong>
        <span>{{ statusLabel(stepStatus(selectedStep)) }}</span>
        <span v-if="selectedStep.category !== 'result' && selectedStep.row.durationMs != null">{{ formatDuration(selectedStep.row.durationMs) }}</span>
      </div>
    </section>

    <div v-if="displayRows.length" class="trace-workspace">
    <section class="trace-ledger" :aria-label="t('usageLogs.logs.traceDetails')">
      <div class="trace-ledger__header">
        <strong>{{ t('usageLogs.logs.traceDetails') }}</strong>
        <span>{{ t('usageLogs.logs.traceSelectStep') }}</span>
      </div>
      <div class="trace-ledger__body">
        <template v-for="step in sequenceItems" :key="step.id">
        <button v-if="step.category === 'result'" type="button"
          :class="['trace-result-row', `trace-result-row--${stepStatus(step)}`, { 'trace-result-row--selected': selectedStep?.id === step.id }]"
          :aria-pressed="selectedStep?.id === step.id" :data-result-id="step.id"
          :title="t('usageLogs.logs.traceOutcome.hint')" @click="selectStep(step.id)">
          <span class="trace-result-row__marker" aria-hidden="true"></span>
          <strong>{{ t('usageLogs.logs.traceOutcome.title') }}</strong>
          <span>{{ outcomeLabel(step.row) }}</span>
          <small>{{ t('usageLogs.logs.traceRecordedAt') }} {{ formatOffset(recordedRange(step.row).start) }}</small>
        </button>
        <button v-else
          type="button"
          :class="['trace-ledger-row', { 'trace-ledger-row--selected': selectedStep?.id === step.id }]"
          :aria-pressed="selectedStep?.id === step.id"
          :data-step-id="step.id"
          :title="t('usageLogs.logs.traceSequence', { display: operationNumber(step.id), seq: step.row.seq ?? '·' })"
          @click="selectStep(step.id)"
        >
          <span class="trace-ledger-row__seq">{{ operationNumber(step.id) }}</span>
          <span :class="['trace-ledger-row__lane', `trace-ledger-row__lane--${step.category}`]">{{ categoryLabel(step.category) }}</span>
          <span class="trace-ledger-row__title">{{ stepTitle(step) }}</span>
          <span class="trace-ledger-row__summary">{{ stepSummary(step) }}</span>
          <span v-if="step.row.model" class="trace-ledger-row__model">{{ step.row.model }}</span>
          <span v-if="step.row.durationMs != null" class="trace-ledger-row__duration">{{ formatDuration(step.row.durationMs) }}</span>
          <span :class="['trace-ledger-row__state', `trace-ledger-row__state--${stepStatus(step)}`]">{{ statusLabel(stepStatus(step)) }}</span>
        </button>
        </template>
      </div>
    </section>

    <section v-if="selectedDetail" ref="inspectorRef" class="trace-inspector" :aria-label="`${t('usageLogs.logs.traceDetails')}: ${detailTitle(selectedDetail)}`">
      <header class="trace-inspector__header">
        <div>
          <div class="trace-inspector__eyebrow">{{ laneLabel(selectedDetail) }} · {{ selectedDetail.kind }}</div>
          <h3>{{ selectedStep && (selectedDetail.id === selectedStep.row.id || selectedStep.label === 'preparation') ? stepTitle(selectedStep) : detailTitle(selectedDetail) }}</h3>
          <p v-if="selectedDetail.provider || selectedDetail.model" class="trace-inspector__meta">{{ selectedDetail.provider }} {{ selectedDetail.model }}</p>
        </div>
        <div class="trace-inspector__actions">
          <button v-if="allowFullPayload && detailRows.some(row => row.id === selectedDetail?.id)" type="button" class="trace-inspector__raw" :disabled="fullPayloadLoading" @click="emit('loadPayload')">{{ t(fullPayloadLoading ? 'chat.traceView.loadingPayload' : 'usageLogs.logs.traceInspectorView.loadFull') }}</button>
        </div>
      </header>
      <p v-if="isTraceTerminal(selectedDetail)" class="trace-inspector__semantic-note">{{ t('usageLogs.logs.traceOutcome.summaryHint') }}</p>
      <p v-if="selectedStep?.label" class="trace-inspector__semantic-note" :data-step-note="selectedStep.label">{{ stepSummary(selectedStep) }}</p>
      <div v-if="selectedStep?.change" class="trace-inspector__comparison">
        <button v-if="selectedStep.change.beforeId" type="button" data-compare="before" @click="selectOriginal(selectedStep.change.beforeId)">{{ t('usageLogs.logs.traceSemantic.before') }}</button>
        <button type="button" data-compare="after" @click="selectOriginal(selectedStep.change.afterId)">{{ t('usageLogs.logs.traceSemantic.after') }}</button>
        <button v-if="selectedStep.change.afterInputId" type="button" data-compare="model-input" @click="selectOriginal(selectedStep.change.afterInputId, 'input')">{{ t('usageLogs.logs.traceSemantic.modelInput') }}</button>
      </div>
      <p v-if="selectedStep?.label === 'toolResultTransformed' && !selectedStep.change?.afterInputId" class="trace-inspector__semantic-note">{{ t('usageLogs.logs.traceSemantic.pendingModelInput') }}</p>
      <details v-if="selectedOriginals.length > 1 || selectedStep?.label === 'preparation'" class="trace-inspector__records">
        <summary>{{ t('usageLogs.logs.traceSemantic.records', { count: selectedOriginals.length }) }}</summary>
        <div class="trace-inspector__record-list">
          <button v-for="record in selectedOriginals" :key="record.id" type="button" :class="{ 'is-selected': selectedId === record.id }" :aria-pressed="selectedId === record.id" :data-record-id="record.id" @click="selectOriginal(record.id)">
            <span>{{ detailTitle(record) }}</span><span>{{ formatOffset(record.recordedAt ?? record.startedAt) }}</span>
          </button>
        </div>
      </details>
      <button v-if="selectedStep && selectedDetail.id !== selectedStep.row.id && selectedStep.label !== 'preparation'" type="button" class="trace-inspector__return" @click="selectStep(selectedStep.id)">{{ t('usageLogs.logs.traceSemantic.primary') }}</button>
      <dl class="trace-inspector__timing">
        <template v-for="field in timingFields(selectedDetail)" :key="field.label"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></template>
      </dl>
      <TraceStepDetails :key="selectedDetail.id" :row="selectedDetail" :input="inspectorInput" :output="inspectorOutput" :preferred-tab="selectedTab" />
      <div v-if="selectedDetail.inputTruncated || selectedDetail.outputTruncated" class="trace-inspector__notice">{{ t('usageLogs.logs.traceDetailTruncated') }}</div>
      <div v-if="fullPayloadError || fullPayloadUnavailable" class="trace-inspector__notice" role="status">{{ t(fullPayloadError ? 'chat.traceView.payloadFailed' : 'chat.traceView.payloadUnavailable') }}</div>
      <div v-if="fullPayloadRestricted" class="trace-inspector__notice" role="status">{{ t('chat.traceView.detailsRestricted') }}</div>
    </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { TraceProjection, TraceSpan } from '@/types/traceView'
import { isTraceTerminal, projectTraceSteps, traceEventCategory, traceTerminalStatus, type TraceDisplayCategory, type TraceSemanticStep } from '@/utils/traceProjection'
import TraceStepDetails from './TraceStepDetails.vue'

const props = defineProps<{ projection: TraceProjection; details?: TraceSpan[]; detailsAvailable?: boolean; detailsReason?: string; clockOrigin?: 'turn_runner_start' | 'logger_start'; compact?: boolean; allowFullPayload?: boolean; fullPayload?: { rowId: string; input?: unknown; output?: unknown }; fullPayloadLoading?: boolean; fullPayloadError?: boolean; fullPayloadUnavailable?: boolean; fullPayloadRestricted?: boolean }>()
const emit = defineEmits<{ select: [row: TraceSpan]; loadPayload: [] }>()
const { t } = useI18n()
const selectedId = ref<string | null>(props.details?.[0]?.id || null)
const selectedStepId = ref<string | null>(null)
const selectedTab = ref<'overview' | 'input'>('overview')
const inspectorRef = ref<HTMLElement | null>(null)
const projection = computed(() => props.projection)
const detailRows = computed(() => props.details || [])
const displayRows = computed(() => detailRows.value.length ? detailRows.value : projection.value.spans)
const sequenceItems = computed(() => projectTraceSteps(displayRows.value))
const operationItems = computed(() => sequenceItems.value.filter(step => step.category !== 'result'))
const resultItems = computed(() => sequenceItems.value.filter(step => step.category === 'result'))
const displayStatus = computed(() => {
  const result = resultItems.value[resultItems.value.length - 1]
  return result ? traceTerminalStatus(result.row) : projection.value.status
})
const overlayItems = computed(() => sequenceItems.value.filter(step => step.lane === 'control' || step.lane === 'result'))
const operationNumbers = computed(() => new Map(operationItems.value.map((step, index) => [step.id, index + 1])))
const selectedStep = computed(() => sequenceItems.value.find(step => step.id === selectedStepId.value)
  || sequenceItems.value.find(step => selectedId.value != null && step.rawIds.includes(selectedId.value))
  || sequenceItems.value[0])
const selectedOriginals = computed(() => {
  const ids = new Set(selectedStep.value?.rawIds || [])
  return displayRows.value.filter(row => ids.has(row.id))
})
const selectedDetail = computed(() => {
  const row = displayRows.value.find(row => row.id === selectedId.value) || selectedStep.value?.row
  if (row && isTraceTerminal(row)) return { ...row, status: traceTerminalStatus(row) }
  if (row && selectedStep.value?.row.id === row.id && selectedStep.value.status) {
    return { ...row, status: selectedStep.value.status }
  }
  return row
})
const inspectorInput = computed(() => props.fullPayload?.rowId === selectedDetail.value?.id && props.fullPayload.input !== undefined ? props.fullPayload.input : selectedDetail.value?.input)
const inspectorOutput = computed(() => props.fullPayload?.rowId === selectedDetail.value?.id && props.fullPayload.output !== undefined ? props.fullPayload.output : selectedDetail.value?.output)
const modeLabel = computed(() => {
  if (projection.value.requestedMode && projection.value.effectiveMode && projection.value.requestedMode !== projection.value.effectiveMode) return `${projection.value.requestedMode} → ${projection.value.effectiveMode}`
  return projection.value.effectiveMode || projection.value.requestedMode || t('usageLogs.logs.traceTimeline')
})
const lanes = computed(() => [
  { key: 'intake', label: t('usageLogs.logs.traceLaneInputContext') },
  { key: 'model', label: t('usageLogs.logs.traceLaneModel') },
  { key: 'tool', label: t('usageLogs.logs.traceLaneTool') },
])
const boundaryItems = computed(() => sequenceItems.value.filter(item => item.boundary && item.category !== 'result'))
const boundaryCategories = computed(() => new Set(boundaryItems.value.map(item => item.category)))
const visibleCategories = computed(() => [...new Set(sequenceItems.value.map(item => item.category))])
const rulerSteps = computed(() => {
  const last = operationItems.value.length - 1
  return last < 0 ? [] : [...new Set([0, 0.25, 0.5, 0.75, 1].map(fraction => Math.round(last * fraction)))].map(index => operationItems.value[index])
})
const timeOrigin = computed(() => {
  const starts = displayRows.value.map(row => recordedRange(row).start).filter((value): value is number => value != null && Number.isFinite(value))
  if (displayRows.value.some(row => row.elapsedMs != null || row.startedElapsedMs != null)) starts.push(0)
  return starts.length ? Math.min(...starts) : 0
})
watch(() => props.projection.traceId, () => {
  selectedStepId.value = null
  selectedId.value = displayRows.value[0]?.id || null
})
watch(sequenceItems, steps => {
  const step = steps.find(item => item.id === selectedStepId.value)
    || steps.find(item => selectedId.value != null && item.rawIds.includes(selectedId.value))
    || steps[0]
  selectedStepId.value = step?.id || null
  if (!displayRows.value.some(row => row.id === selectedId.value)) {
    selectedId.value = step?.rawIds.includes(step.row.id) ? step.row.id : step?.rawIds[0] || null
  }
}, { immediate: true })
watch(selectedDetail, row => { if (row) emit('select', row) }, { immediate: true })
function statusLabel(status: string): string { return t(`usageLogs.logs.traceStatus.${status}`, status) }
function formatDuration(value: number): string { return `${Math.round(value * 1000) / 1000}ms` }
function laneFor(row: TraceSpan): TraceDisplayCategory { return traceEventCategory(row) }
function categoryLabel(category: TraceDisplayCategory): string { return t(`usageLogs.logs.traceCategory.${category}`) }
function laneLabel(row: TraceSpan): string { return categoryLabel(laneFor(row)) }
function routingModeLabel(mode: string): string { return t(`usageLogs.logs.traceRoutingModes.${mode}`, mode) }
function itemsByLane(lane: string) { return sequenceItems.value.filter(item => item.lane === lane) }
function operationNumber(id: string): number | undefined { return operationNumbers.value.get(id) }
function eventStyle(index: number): Record<string, string> {
  const count = Math.max(1, sequenceItems.value.length)
  return { left: `${(index + 0.06) / count * 100}%`, width: `${0.88 / count * 100}%`, top: '4px' }
}
function overlayStyle(index: number): Record<string, string> {
  const { left, width } = eventStyle(index)
  return { left, width }
}
function recordedRange(row: TraceSpan): { start?: number; end?: number } {
  if (isTraceTerminal(row)) {
    const point = row.recordedAt ?? row.endedElapsedMs ?? row.elapsedMs ?? row.endedAt ?? row.startedAt
    return { start: point, end: point }
  }
  const point = row.recordedAt ?? row.elapsedMs ?? row.startedAt
  if (row.status === 'running' && (row.kind.startsWith('llm_') || row.kind === 'tool_request')) return { start: row.startedAt ?? point }
  const measured = row.durationMs != null && row.startedAt != null
  const start = row.attrs?.grouped || measured ? row.startedAt : point
  const end = row.attrs?.grouped ? row.endedAt : measured ? row.endedAt ?? row.startedAt! + row.durationMs! : point
  return { start, end }
}
function selectStep(id: string) {
  const step = sequenceItems.value.find(item => item.id === id)
  if (!step) return
  selectedStepId.value = step.id
  selectedId.value = step.rawIds.includes(step.row.id) ? step.row.id : step.rawIds[0] || null
  selectedTab.value = 'overview'
  void nextTick(() => {
    if (inspectorRef.value) inspectorRef.value.scrollTop = 0
    inspectorRef.value?.scrollIntoView?.({ block: 'start' })
  })
}
function selectOriginal(id: string, tab: 'overview' | 'input' = 'overview') {
  if (displayRows.value.some(row => row.id === id)) {
    selectedId.value = id
    selectedTab.value = tab
  }
}
function stepStatus(step: TraceSemanticStep): string { return isTraceTerminal(step.row) ? traceTerminalStatus(step.row) : step.status || step.row.status }
function outcomeLabel(row: TraceSpan): string {
  const status = traceTerminalStatus(row)
  const key = status === 'success' ? 'completed' : status === 'error' ? 'failed' : status === 'cancelled' ? 'cancelled' : 'recorded'
  return t(`usageLogs.logs.traceOutcome.${key}`)
}
function stepTitle(step: TraceSemanticStep): string {
  return step.label ? t(`usageLogs.logs.traceSemantic.${step.label}`) : detailTitle(step.row)
}
function stepSummary(step: TraceSemanticStep): string {
  if (step.change?.uncertain) return t('usageLogs.logs.traceSemantic.uncertain')
  if (step.change?.removedMessages != null && step.change.removedMessages > 0) return t('usageLogs.logs.traceSemantic.removed', { count: step.change.removedMessages })
  if (step.change) return t('usageLogs.logs.traceSemantic.changed')
  if (step.label === 'preparation') return t('usageLogs.logs.traceSemantic.preparing')
  return compactSummary(step.row)
}
function formatOffset(value?: number): string { return value == null ? '—' : `${Math.round((value - timeOrigin.value) * 1000) / 1000}ms` }
function timingFields(row: TraceSpan): Array<{ label: string; value: string }> {
  const fields: Array<{ label: string; value: string }> = []
  if (isTraceTerminal(row)) return [
    { label: t('usageLogs.logs.traceRecordedAt'), value: formatOffset(recordedRange(row).start) },
    { label: t('usageLogs.logs.traceTimingType'), value: t('usageLogs.logs.traceOutcome.terminal') },
  ]
  if (row.attrs?.grouped) {
    fields.push({ label: t('usageLogs.logs.traceSampleRange'), value: `${formatOffset(row.startedAt)} – ${formatOffset(row.endedAt)}` })
  } else {
    fields.push({ label: t('usageLogs.logs.traceRecordedAt'), value: formatOffset(row.recordedAt ?? row.elapsedMs ?? row.startedAt) })
    if (row.durationMs != null) {
      fields.push({ label: t('usageLogs.logs.traceStartedAt'), value: formatOffset(row.startedAt) })
      fields.push({ label: t('usageLogs.logs.traceEndedAt'), value: formatOffset(row.endedAt ?? (row.startedAt != null ? row.startedAt + row.durationMs : undefined)) })
      fields.push({ label: t('usageLogs.logs.traceMeasuredDuration'), value: formatDuration(row.durationMs) })
    } else if (row.status === 'running') {
      fields.push({ label: t('usageLogs.logs.traceStartedAt'), value: formatOffset(row.startedAt) })
      fields.push({ label: t('usageLogs.logs.traceTimingType'), value: statusLabel('running') })
    } else {
      fields.push({ label: t('usageLogs.logs.traceTimingType'), value: t('usageLogs.logs.traceInstantCheckpoint') })
    }
  }
  return fields
}
function timelineTitle(step: TraceSemanticStep): string {
  return [stepTitle(step), ...timingFields(step.row).map(field => `${field.label}: ${field.value}`)].join('\n')
}
function detailTitle(row: TraceSpan): string {
  if (isTraceTerminal(row)) return `${t('usageLogs.logs.traceOutcome.title')} · ${outcomeLabel(row)}`
  if (row.attrs?.grouped && row.phase === 'context') {
    const count = Number(row.attrs.context_count) || 0
    return t('usageLogs.logs.traceCheckpointGroup', { count })
  }
  const category = traceEventCategory(row)
  if (['routing', 'retry', 'fallback', 'approval', 'maintenance', 'subagent'].includes(category)) {
    return `${categoryLabel(category)} · ${row.summary || row.title || row.kind}`
  }
  if (category === 'context') {
    return `${categoryLabel('context')} · ${row.summary || row.kind}`
  }
  if (row.toolName) return row.toolName
  if (row.kind.startsWith('llm_')) {
    return `${categoryLabel('model')} · ${row.kind}`
  }
  return row.title || row.kind
}
function compactSummary(row: TraceSpan): string { if (row.summary) return row.summary; if (row.toolName) return row.toolName; if (row.input !== undefined) return structuredText(row.input).split('\n')[0].slice(0, 100); if (row.output !== undefined) return structuredText(row.output).split('\n')[0].slice(0, 100); return row.kind }
function detailJson(value: unknown): string { try { return JSON.stringify(value, null, 2) } catch { return String(value) } }
function structuredText(value: unknown): string {
  if (value == null) return 'null'
  if (typeof value === 'string') return value.length > 800 ? `${value.slice(0, 800)}…` : value
  if (Array.isArray(value)) return value.map((item, index) => { const record = item && typeof item === 'object' ? item as Record<string, unknown> : null; if (record?.role || record?.content) return `${record.role || `#${index + 1}`}: ${structuredText(record.content ?? record.text ?? record)}`; return `${index + 1}. ${structuredText(item)}` }).join('\n')
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${key}: ${typeof item === 'object' ? detailJson(item).slice(0, 300) : String(item)}`).join('\n')
  return String(value)
}
</script>

<style scoped>
.trace-inspector__actions { display: flex; flex-wrap: wrap; gap: 6px; }
.trace-inspector__semantic-note { color: var(--text-muted); font-size: 11px; line-height: 1.65; margin: 10px 0; }
.trace-inspector__comparison { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.trace-inspector__comparison button, .trace-inspector__return { color: var(--text); background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-sm); cursor: pointer; font: inherit; font-size: 11px; padding: 5px 8px; }
.trace-inspector__records { border: 1px solid var(--border); border-radius: var(--radius-sm); margin-top: 12px; font-size: 11px; color: var(--text-muted); }
.trace-inspector__records summary { cursor: pointer; padding: 9px; }
.trace-inspector__record-list { display: grid; gap: 3px; max-height: 200px; overflow: auto; padding: 0 6px 6px; }
.trace-inspector__record-list button { align-items: baseline; display: flex; justify-content: space-between; gap: 8px; min-width: 0; border: 1px solid transparent; border-radius: var(--radius-xs); background: transparent; color: inherit; cursor: pointer; font: inherit; padding: 6px; text-align: left; }
.trace-inspector__record-list button span:first-child { overflow-wrap: anywhere; }.trace-inspector__record-list button span:last-child { flex-shrink: 0; font-family: var(--font-mono); }
.trace-inspector__record-list button.is-selected { background: var(--bg-surface); border-color: var(--border); color: var(--text); }
.trace-inspector__return { margin-top: 8px; }
.trace-inspector__comparison button:focus-visible, .trace-inspector__record-list button:focus-visible, .trace-inspector__return:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.trace-timeline { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg); color: var(--text); margin: var(--sp-4, 16px) 0; overflow: hidden; }
.trace-timeline__header { align-items: flex-start; display: flex; gap: 16px; justify-content: space-between; padding: 16px 18px 10px; }
.trace-timeline__eyebrow, .trace-inspector__eyebrow { color: var(--text-dim); font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
.trace-timeline__title { font-size: 16px; font-weight: 650; margin: 3px 0 4px; }
.trace-timeline__meta, .trace-inspector__meta { color: var(--text-dim); display: flex; flex-wrap: wrap; font-family: var(--font-mono, ui-monospace); font-size: 10px; gap: 8px; margin: 0; }
.trace-timeline__status { border: 1px solid currentColor; border-radius: 999px; font-size: 10px; padding: 3px 8px; text-transform: uppercase; }
.trace-timeline__status--accent-secondary { color: var(--accent-secondary); }.trace-timeline__status--error { color: var(--danger); }.trace-timeline__status--running { color: var(--accent); }.trace-timeline__status--cancelled { color: var(--warn); }
.trace-timeline__overview { border-bottom: 1px solid var(--border); border-top: 1px solid var(--border); color: var(--text-muted); display: flex; flex-wrap: wrap; font-size: 11px; gap: 14px; padding: 8px 18px; }
.trace-visual { border-bottom: 1px solid var(--border); padding: 12px 18px 14px; }.trace-visual__toolbar, .trace-ledger__header { align-items: baseline; display: flex; justify-content: space-between; gap: 10px; }.trace-visual__hint, .trace-ledger__header span { color: var(--text-dim); font-size: 10px; margin-left: 8px; }.trace-visual__count { color: var(--text-dim); font-size: 10px; }
.trace-ruler { height: 19px; margin: 12px 0 0 84px; position: relative; }.trace-ruler::before { border-top: 1px solid var(--border); content: ''; left: 0; position: absolute; right: 0; top: 6px; }.trace-ruler span { color: var(--text-dim); font: 9px var(--font-mono, ui-monospace); position: absolute; transform: translateX(-50%); top: 9px; }
.trace-lane { display: grid; grid-template-columns: 84px minmax(0, 1fr); min-height: 30px; }.trace-lane__label { align-items: center; color: var(--text-muted); display: flex; font-size: 10px; gap: 6px; }.trace-lane__dot { border-radius: var(--radius-xs); height: 8px; width: 8px; }.trace-lane__dot--intake, .trace-event--intake { background: var(--accent-secondary); }.trace-lane__dot--context, .trace-event--context { background: var(--info); }.trace-lane__dot--model, .trace-event--model { background: var(--accent); }.trace-lane__dot--tool, .trace-event--tool { background: var(--warn); }.trace-lane__dot--output, .trace-event--output { background: color-mix(in srgb, var(--accent) 65%, var(--info)); }.trace-lane__track { background: repeating-linear-gradient(90deg, transparent 0, transparent calc(25% - 1px), color-mix(in srgb, var(--border) 55%, transparent) 25%); border-bottom: 1px solid color-mix(in srgb, var(--border) 62%, transparent); min-height: 30px; position: relative; }
.trace-event { --event-color: var(--accent-secondary); border: 0; box-sizing: border-box; color: var(--accent-foreground); cursor: pointer; height: 18px; min-width: 0; padding: 0; position: absolute; }
.trace-event--intake { --event-color: var(--accent-secondary); }.trace-event--context { --event-color: var(--info); }.trace-event--model { --event-color: var(--accent); }.trace-event--tool { --event-color: var(--warn); }.trace-event--output { --event-color: color-mix(in srgb, var(--accent) 65%, var(--info)); }
.trace-event--sequence { background: var(--event-color); border-radius: var(--radius-xs); height: 18px; overflow: hidden; text-align: center; }
.trace-event--running { border: 1px dashed var(--accent-foreground); }
.trace-event--selected, .trace-event:hover { box-shadow: inset 0 0 0 1px var(--accent-foreground); z-index: 2; }
.trace-sequence-overview, .trace-sequence-grid { min-width: 0; width: 100%; }
.trace-sequence-grid .trace-ruler { margin-left: 88px; }
.trace-sequence-grid .trace-lane { grid-template-columns: 88px minmax(0, 1fr); min-height: 26px; }
.trace-sequence-grid .trace-lane__track { min-width: 0; min-height: 26px; }
.trace-swimlane { position: relative; }
.trace-lane__label { min-width: 0; padding-right: 5px; }
.trace-lane__dot { flex-shrink: 0; }
.trace-control-track { position: absolute; inset: 0 0 0 88px; pointer-events: none; z-index: 3; }
.trace-event--overlay { top: 0; height: 100%; background: transparent; pointer-events: auto; }
.trace-event--overlay::before { content: ''; position: absolute; top: 0; bottom: 0; left: 50%; border-left: 1px dashed var(--event-color); }
.trace-event--overlay::after { content: ''; position: absolute; top: calc(50% - 5px); left: 50%; width: min(10px, 70%); height: 10px; box-sizing: border-box; transform: translateX(-50%) rotate(45deg); background: var(--event-color); border: 1px solid var(--bg-surface); border-radius: var(--radius-xs); }
.trace-event--overlay.trace-event--measured-control::before { width: min(12px, 75%); transform: translateX(-50%); border: 1px solid var(--event-color); border-radius: var(--radius-xs); background: color-mix(in srgb, var(--event-color) 12%, var(--bg-surface)); }
.trace-event--overlay.trace-event--measured-control::after { transform: translateX(-50%); }
.trace-event--overlay.trace-event--terminal::before { border-left-style: solid; }
.trace-event--overlay.trace-event--terminal::after { transform: translateX(-50%); border-radius: 50%; background: var(--bg-surface); border: 2px solid var(--event-color); }
.trace-event--overlay.trace-event--selected, .trace-event--overlay:hover { box-shadow: none; }
.trace-event--overlay.trace-event--selected::after, .trace-event--overlay:hover::after { outline: 2px solid var(--event-color); outline-offset: 2px; }
.trace-event--result, .trace-category--result { --event-color: var(--accent-secondary); }
.trace-event--overlay.trace-event--error { --event-color: var(--danger); outline: none; }
.trace-event--overlay.trace-event--cancelled { --event-color: var(--warn); }
.trace-category--result { border-radius: 50%; background: transparent; border: 2px solid var(--event-color); box-sizing: border-box; }
.trace-result-row { width: 100%; min-width: 0; display: grid; grid-template-columns: 12px minmax(0, 1fr) auto; align-items: center; gap: 5px 8px; padding: 10px 8px; margin: 10px 0 2px; border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-muted); background: var(--bg-surface); text-align: left; font: inherit; font-size: 11px; cursor: pointer; }
.trace-result-row strong { color: var(--text); font-weight: 600; }
.trace-result-row small { grid-column: 2; font: 10px var(--font-mono, ui-monospace); }
.trace-result-row__marker { width: 8px; height: 8px; border: 2px solid var(--accent-secondary); border-radius: 50%; box-sizing: border-box; }
.trace-result-row--error .trace-result-row__marker { border-color: var(--danger); }
.trace-result-row--cancelled .trace-result-row__marker { border-color: var(--warn); }
.trace-result-row--selected, .trace-result-row:hover { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 7%, var(--bg-surface)); }
.trace-result-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.trace-boundary-guides { position: absolute; inset: 0 0 0 88px; pointer-events: none; }
.trace-boundary-guides i { position: absolute; top: 0; bottom: 0; border-left: 1px dashed var(--text-dim); opacity: .45; }
.trace-visual__legend-items { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 9px; }
.trace-visual__legend-item { display: inline-flex; align-items: center; gap: 5px; color: var(--text-muted); font-size: 10px; }
.trace-category { width: 8px; height: 8px; flex: 0 0 auto; border-radius: var(--radius-xs); background: var(--event-color); }
.trace-category--boundary { transform: rotate(45deg); border-radius: var(--radius-xs); }
.trace-visual__routing { display: flex; flex-wrap: wrap; gap: 5px 14px; color: var(--text-muted); font-size: 10px; margin: 8px 0 0; }
.trace-category--input, .trace-event--input { --event-color: var(--accent-secondary); }
.trace-category--context { --event-color: var(--info); }
.trace-category--model { --event-color: var(--accent); }
.trace-category--tool { --event-color: var(--warn); }
.trace-category--output { --event-color: color-mix(in srgb, var(--accent) 65%, var(--info)); }
.trace-category--routing, .trace-event--routing { --event-color: var(--info); }
.trace-category--retry, .trace-event--retry, .trace-category--fallback, .trace-event--fallback, .trace-category--approval, .trace-event--approval { --event-color: var(--warn); }
.trace-category--maintenance, .trace-event--maintenance, .trace-category--subagent, .trace-event--subagent { --event-color: color-mix(in srgb, var(--info) 65%, var(--accent)); }
.trace-category--unknown, .trace-event--unknown { --event-color: var(--text-dim); }
.trace-event--boundary { background: transparent; overflow: visible; }
.trace-event--boundary::after { content: ''; position: absolute; top: 4px; left: 50%; width: min(10px, 60%); height: 10px; transform: translateX(-50%) rotate(45deg); background: var(--event-color); border: 1px solid var(--bg-surface); box-sizing: border-box; }
.trace-event--boundary.trace-event--selected, .trace-event--boundary:hover { box-shadow: none; }
.trace-event--boundary.trace-event--selected::after, .trace-event--boundary:hover::after { outline: 2px solid var(--event-color); outline-offset: 2px; }
.trace-event--error { outline: 1px solid var(--danger); outline-offset: 1px; }
.trace-event:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
.trace-ledger-row__lane--input { background: var(--accent-secondary); }
.trace-ledger-row__lane--routing { background: var(--info); }
.trace-ledger-row__lane--retry, .trace-ledger-row__lane--fallback, .trace-ledger-row__lane--approval { background: var(--warn); }
.trace-ledger-row__lane--maintenance, .trace-ledger-row__lane--subagent { background: color-mix(in srgb, var(--info) 65%, var(--accent)); }
.trace-ledger-row__lane--unknown { background: var(--text-muted); }
.trace-visual__selection { display: flex; flex-wrap: wrap; gap: 6px 10px; min-height: 24px; align-items: baseline; padding-top: 8px; font-size: 10px; color: var(--text-muted); }
.trace-visual__selection strong { color: var(--text); overflow-wrap: anywhere; }
.trace-visual__sequence-note { color: var(--text-muted); font-size: 10px; line-height: 1.5; margin: 5px 0; }
.trace-visual__help { color: var(--text-muted); font-size: 10px; margin-top: 4px; }.trace-visual__help summary { cursor: pointer; }
.trace-visual__legend, .trace-visual__origin { color: var(--text-muted); font-size: 10px; line-height: 1.6; margin: 5px 0 0; }.trace-visual__origin { color: var(--text-dim); }
.trace-inspector__timing { align-items: baseline; display: flex; flex-wrap: wrap; font-size: 10px; gap: 4px 8px; margin: 10px 0 0; }.trace-inspector__timing dt { color: var(--text-dim); }.trace-inspector__timing dd { font-family: var(--font-mono, ui-monospace); margin: 0 10px 0 0; }
.trace-inspector__checkpoints { color: var(--text-muted); font-size: 10px; margin-top: 10px; }.trace-inspector__checkpoint-list { display: flex; flex-wrap: wrap; gap: 6px; }.trace-inspector__checkpoint-list button { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer; font-size: 10px; padding: 4px 6px; }.trace-inspector__checkpoint-list button.is-selected { border-color: var(--info); color: var(--text); }
.trace-ledger { border-bottom: 1px solid var(--border); padding: 12px 18px 8px; }.trace-ledger__body { margin-top: 8px; }.trace-ledger-row { align-items: center; background: transparent; border: 0; border-top: 1px solid color-mix(in srgb, var(--border) 68%, transparent); color: inherit; cursor: pointer; display: grid; font: inherit; gap: 8px; grid-template-columns: 25px 60px minmax(120px, 1.1fr) minmax(140px, 2fr) minmax(100px, 1fr) 48px 56px; min-height: 33px; padding: 5px 4px; text-align: left; width: 100%; }.trace-ledger-row:hover, .trace-ledger-row--selected { background: color-mix(in srgb, var(--accent) 8%, transparent); }.trace-ledger-row__seq { color: var(--text-dim); font: 10px var(--font-mono, ui-monospace); text-align: right; }.trace-ledger-row__lane { border-radius: var(--radius-xs); color: var(--accent-foreground); font-size: 9px; justify-self: start; padding: 2px 5px; }.trace-ledger-row__lane--intake { background: var(--accent-secondary); }.trace-ledger-row__lane--context { background: var(--info); }.trace-ledger-row__lane--model { background: var(--accent); }.trace-ledger-row__lane--tool { background: var(--warn); }.trace-ledger-row__lane--output { background: color-mix(in srgb, var(--accent) 65%, var(--info)); }.trace-ledger-row__title { font-size: 11px; font-weight: 650; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.trace-ledger-row__title small, .trace-ledger-row__summary, .trace-ledger-row__model, .trace-ledger-row__duration { color: var(--text-dim); font-size: 10px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.trace-ledger-row__state { font-size: 9px; text-align: right; }.trace-ledger-row__state--error { color: var(--danger); }.trace-ledger-row__state--success { color: var(--accent-secondary); }
.trace-inspector { background: color-mix(in srgb, var(--bg-elevated) 52%, var(--bg-surface)); border-bottom: 1px solid var(--border); padding: 14px 18px 16px; }.trace-inspector__header { align-items: flex-start; display: flex; gap: 12px; justify-content: space-between; }.trace-inspector h3 { font-size: 14px; margin: 3px 0 2px; }.trace-inspector__raw { background: transparent; border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-muted); cursor: pointer; font-size: 10px; padding: 4px 8px; }.trace-inspector__columns { display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); margin-top: 12px; }.trace-inspector__block > span { color: var(--text-dim); display: block; font-size: 10px; font-weight: 650; margin-bottom: 4px; }.trace-inspector__content, .trace-inspector pre { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-muted); font: 10px/1.45 var(--font-mono, ui-monospace); margin: 0; max-height: 180px; min-height: 30px; overflow: auto; padding: 8px; white-space: pre-wrap; word-break: break-word; }.trace-inspector__summary, .trace-inspector__notice { color: var(--text-muted); font-size: 10px; margin-top: 8px; }.trace-inspector__notice { color: var(--warn); }
.trace-details__empty { color: var(--text-muted); font-size: 11px; padding: 14px 18px; }.trace-timeline__phases { list-style: none; margin: 0; padding: 4px 0; }.trace-phase + .trace-phase { border-top: 1px solid color-mix(in srgb, var(--border) 72%, transparent); }.trace-phase__head { align-items: center; background: transparent; border: 0; color: inherit; cursor: pointer; display: flex; font: inherit; gap: 9px; padding: 9px 18px; text-align: left; width: 100%; }.trace-phase__head:hover { background: color-mix(in srgb, var(--accent) 7%, transparent); }.trace-phase__marker, .trace-span__dot { border-radius: 50%; display: inline-block; flex: 0 0 auto; height: 7px; width: 7px; }.trace-phase__marker--accent-secondary, .trace-span__dot--accent-secondary, .trace-phase__marker--success, .trace-span__dot--success { background: var(--accent-secondary); }.trace-phase__marker--error, .trace-span__dot--error { background: var(--danger); }.trace-phase__marker--running, .trace-span__dot--running { background: var(--accent); }.trace-phase__marker--unknown, .trace-span__dot--unknown, .trace-phase__marker--queued, .trace-span__dot--queued { background: var(--text-dim); }.trace-phase__label { font-size: 12px; font-weight: 650; }.trace-phase__count, .trace-phase__duration, .trace-span__duration { color: var(--text-dim); font-size: 10px; }.trace-phase__count { background: var(--bg-elevated); border-radius: 999px; min-width: 18px; padding: 2px 5px; text-align: center; }.trace-phase__duration { margin-left: auto; }.trace-phase__chevron { color: var(--text-dim); font-size: 16px; line-height: 1; width: 12px; }.trace-phase__body { padding: 0 18px 10px 38px; }.trace-span { display: flex; min-height: 30px; position: relative; }.trace-span__rail { border-left: 1px solid var(--border); margin: 0 12px 0 2px; width: 1px; }.trace-span:last-child .trace-span__rail { border-color: transparent; }.trace-span__content { flex: 1; min-width: 0; padding: 4px 0; }.trace-span__row { align-items: center; display: flex; flex-wrap: wrap; gap: 7px; min-height: 20px; }.trace-span__title { font-size: 11px; font-weight: 600; }.trace-span__role, .trace-span__state { color: var(--text-dim); font-size: 10px; }.trace-span__state { margin-left: auto; }.trace-span__state--error { color: var(--danger); }.trace-span__summary { color: var(--text-muted); font-size: 11px; margin: 2px 0 0 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 760px) { .trace-ledger-row { grid-template-columns: 18px 42px minmax(0, 1fr) 48px 42px; }.trace-ledger-row__summary, .trace-ledger-row__model { display: none; }.trace-inspector__columns { grid-template-columns: 1fr; } }
.trace-timeline { container-type: inline-size; }
.trace-workspace { min-width: 0; display: flex; flex-direction: column; }
.trace-workspace .trace-ledger { padding: 12px; order: 2; }
.trace-workspace .trace-ledger__header span { display: none; }
.trace-workspace .trace-ledger__body { max-height: 210px; overflow: auto; }
.trace-workspace .trace-ledger-row { grid-template-columns: 18px 44px minmax(0, 1fr) auto; gap: 7px; }
.trace-workspace .trace-ledger-row__summary, .trace-workspace .trace-ledger-row__model { display: none; }
.trace-workspace .trace-ledger-row__state { grid-column: 4; }
.trace-workspace .trace-ledger-row__duration { grid-column: 3; font-size: 10px; }
.trace-workspace .trace-ledger-row__title { font-size: 12px; }
.trace-workspace .trace-inspector { min-width: 0; padding: 14px; border-bottom: 0; order: 1; }
.trace-workspace .trace-inspector__header { flex-wrap: wrap; }
@container (min-width: 740px) {
  .trace-workspace { display: grid; grid-template-columns: 245px minmax(0, 1fr); align-items: start; }
  .trace-workspace .trace-ledger { border-bottom: 0; border-right: 1px solid var(--border); order: 0; }
  .trace-workspace .trace-ledger__body { max-height: 620px; }
  .trace-workspace .trace-inspector { max-height: 670px; overflow: auto; }
}
</style>
