<template>
  <section
    class="meta-ribbon"
    role="region"
    :data-run-id="run.runId"
    :data-collapsed="String(collapsed)"
    :aria-label="`MetaSkill ${run.metaSkillName} run progress: ${headerIndexValue} of ${run.total}`"
  >
    <div class="meta-ribbon-shell">
      <header class="meta-ribbon-head">
        <span class="meta-ribbon-icon" :class="overallStateValue" :aria-label="humanizeStepId(overallStateValue)">
          {{ stateIcon(overallStateValue) }}
        </span>
        <span class="meta-ribbon-title">{{ run.metaSkillName }}</span>
        <span class="meta-ribbon-counter">{{ counterTextValue }}</span>
        <button
          class="meta-ribbon-toggle"
          type="button"
          :aria-label="copy.toggleAria"
          :aria-controls="stepsId"
          :aria-expanded="!collapsed"
          @click="collapsed = !collapsed"
        >
          {{ collapsed ? copy.expand : copy.collapse }}
        </button>
      </header>
      <div class="meta-ribbon-main" aria-live="polite">
        <div class="meta-ribbon-current">{{ currentLabelValue }}</div>
        <div class="meta-ribbon-status">{{ statusTextValue }}</div>
      </div>
      <div
        class="meta-ribbon-track"
        role="progressbar"
        :aria-label="copy.progressAria(run.metaSkillName)"
        :aria-valuenow="progressPercentValue"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <div class="meta-ribbon-fill" :style="{ width: `${progressPercentValue}%` }" />
      </div>
      <ol :id="stepsId" class="meta-ribbon-chips">
        <li
          v-for="(step, i) in run.steps"
          :key="step.id || i"
          class="chip"
          :class="normalizeStateClass(step.state)"
          :data-step-id="step.id"
          tabindex="0"
          :aria-label="copy.stepAria(i + 1, run.total, step.label, normalizeStateClass(step.state))"
          @click="onChipClick(step.id)"
          @keydown.enter.prevent="onChipClick(step.id)"
          @keydown.space.prevent="onChipClick(step.id)"
        >
          {{ stepGlyph(step) }} {{ step.label }}
        </li>
      </ol>
      <div v-show="showActions" class="meta-ribbon-actions">
        <template v-if="showActions">
          <span class="meta-ribbon-fail-summary">{{ failSummaryValue.summary }}</span>
          <button
            v-for="(btn, i) in failSummaryValue.buttons"
            :key="`${btn.action}-${i}`"
            type="button"
            :data-action="btn.action"
            :data-step-id="btn.stepId || undefined"
            :disabled="actionDisabled(btn.action)"
            @click="onActionClick(btn.action, btn.stepId)"
          >
            {{ btn.label }}
          </button>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  counterText,
  currentLabel,
  failSummary,
  headerIndex,
  humanizeStepId,
  normalizeStateClass,
  overallState,
  progressPercent,
  ribbonCopy,
  shouldShowActions,
  statusText,
  stateIcon,
  stepGlyph,
  type MetaRibbonState,
} from '@/utils/chat/metaRibbon'

const props = defineProps<{
  run: MetaRibbonState
  turnActionsDisabled?: boolean
}>()

const emit = defineEmits<{
  action: [payload: { action: string; stepId: string | null; runId: string }]
  'chip-select': [payload: { stepId: string; runId: string }]
}>()

// Collapse is the only local UI state; everything else derives from the prop.
const collapsed = ref(false)

const copy = computed(() => ribbonCopy(props.run.language))
const headerIndexValue = computed(() => headerIndex(props.run))
const overallStateValue = computed(() => overallState(props.run))
const counterTextValue = computed(() => counterText(props.run, copy.value))
const currentLabelValue = computed(() => currentLabel(props.run, copy.value))
const statusTextValue = computed(() => statusText(props.run, copy.value))
const progressPercentValue = computed(() => progressPercent(props.run))
const showActions = computed(() => shouldShowActions(props.run))
const failSummaryValue = computed(() => failSummary(props.run, copy.value))
const stepsId = computed(() => `meta-ribbon-steps-${props.run.runId || 'current'}`)

function onChipClick(stepId: string) {
  if (!stepId) return
  emit('chip-select', { stepId, runId: props.run.runId })
}

function onActionClick(action: string, stepId: string | null) {
  if (actionDisabled(action)) return
  emit('action', { action, stepId, runId: props.run.runId })
}

function actionDisabled(action: string): boolean {
  return props.turnActionsDisabled === true && action !== 'show-detail'
}
</script>

<style scoped>
.meta-ribbon {
  width: calc(100% - 32px);
  max-width: min(760px, 100%);
  margin: 10px auto;
  color: var(--text);
  font-size: var(--fs-sm, 0.875rem);
  flex-shrink: 0;
}

.meta-ribbon-shell {
  position: relative;
  overflow: hidden;
  padding: 10px 12px 11px;
  border: 1px solid color-mix(in srgb, var(--border) 82%, var(--accent) 18%);
  border-radius: var(--radius-md);
  background:
    linear-gradient(180deg,
      color-mix(in srgb, var(--bg-surface) 96%, var(--accent) 4%),
      var(--bg-surface));
  box-shadow: var(--shadow-sm);
}

.meta-ribbon-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.meta-ribbon-icon {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border: 1px solid color-mix(in srgb, var(--text-dim) 24%, transparent);
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--bg-base, var(--bg)) 76%, var(--text-dim) 8%);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
}

.meta-ribbon-icon.running {
  border-color: color-mix(in srgb, var(--accent) 44%, transparent);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  color: var(--accent);
  /* Shared opacity-only rhythm — no size throb, same beat as the work-card dot. */
  animation: live-pulse var(--dur-pulse) var(--ease-standard) infinite;
}

.meta-ribbon-icon.succeeded {
  border-color: color-mix(in srgb, var(--success, var(--ok)) 42%, transparent);
  background: color-mix(in srgb, var(--success, var(--ok)) 10%, var(--bg-surface));
  color: var(--success, var(--ok));
}

.meta-ribbon-icon.failed {
  border-color: color-mix(in srgb, var(--danger) 42%, transparent);
  background: color-mix(in srgb, var(--danger) 9%, var(--bg-surface));
  color: var(--danger);
}

.meta-ribbon-toggle {
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  padding: 2px 7px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs, 0.75rem);
}

.meta-ribbon-toggle:hover,
.meta-ribbon-toggle:focus-visible {
  border-color: var(--border);
  background: color-mix(in srgb, var(--bg-base, var(--bg)) 86%, var(--accent) 14%);
  color: var(--text);
  outline: none;
}

.meta-ribbon-title {
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-ribbon-counter {
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  font-size: var(--fs-xs, 0.75rem);
}

.meta-ribbon-main {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 0;
  margin-top: 7px;
}

.meta-ribbon-current {
  min-width: 0;
  color: var(--text);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-ribbon-status {
  min-width: 0;
  color: var(--text-muted);
  font-size: var(--fs-xs, 0.75rem);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-ribbon-track {
  height: 2px;
  margin-top: 9px;
  overflow: hidden;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--border) 64%, transparent);
}

.meta-ribbon-fill {
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg,
    var(--accent),
    color-mix(in srgb, var(--accent) 64%, var(--success, var(--ok)) 36%));
  transition: width var(--dur-base) var(--ease-out);
}

.meta-ribbon-chips {
  display: flex;
  gap: 6px;
  margin: 10px 0 0;
  padding: 0;
  overflow-x: auto;
  list-style: none;
  scroll-behavior: smooth;
}

.meta-ribbon-chips .chip {
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--bg-base, var(--bg)) 92%, var(--text-muted) 8%);
  color: var(--text-muted);
  cursor: pointer;
  font-size: var(--fs-xs, 0.75rem);
  line-height: 1.45;
  white-space: nowrap;
  transition: background var(--dur-fast), border-color var(--dur-fast), color var(--dur-fast);
}

.meta-ribbon-chips .chip.pending { opacity: 0.64; }
.meta-ribbon-chips .chip.running {
  border-color: color-mix(in srgb, var(--accent) 34%, transparent);
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-surface));
  color: var(--accent);
  font-weight: 650;
}
.meta-ribbon-chips .chip.succeeded {
  border-color: color-mix(in srgb, var(--success, var(--ok)) 28%, transparent);
  background: color-mix(in srgb, var(--success, var(--ok)) 12%, var(--bg-surface));
  color: var(--success, var(--ok));
}
.meta-ribbon-chips .chip.failed {
  border-color: color-mix(in srgb, var(--danger) 32%, transparent);
  background: color-mix(in srgb, var(--danger) 10%, var(--bg-surface));
  color: var(--danger);
  font-weight: 650;
}
.meta-ribbon-chips .chip.skipped {
  opacity: 0.44;
  text-decoration: line-through;
}
.meta-ribbon-chips .chip.substituted {
  border-color: color-mix(in srgb, var(--warn) 28%, transparent);
  background: color-mix(in srgb, var(--warn) 12%, var(--bg-surface));
  color: var(--warn);
}
.meta-ribbon-chips .chip.paused,
.meta-ribbon-chips .chip.cancelled {
  opacity: 0.62;
  border-style: dashed;
}

.meta-ribbon-chips .chip.running::after {
  content: "";
  display: inline-block;
  width: 5px;
  height: 5px;
  margin-left: 6px;
  border-radius: var(--radius-full);
  background: currentColor;
  animation: live-pulse var(--dur-pulse) var(--ease-standard) infinite;
}

.meta-ribbon[data-collapsed="true"] .meta-ribbon-chips,
.meta-ribbon[data-collapsed="true"] .meta-ribbon-actions {
  display: none;
}

.meta-ribbon-actions {
  margin-top: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.meta-ribbon-fail-summary {
  color: var(--danger);
  font-size: var(--fs-xs, 0.75rem);
}

.meta-ribbon-actions button {
  padding: 5px 10px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border);
  background: var(--bg-surface);
  color: var(--text);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs, 0.75rem);
  transition: background var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.meta-ribbon-actions button:hover,
.meta-ribbon-actions button:focus-visible {
  border-color: color-mix(in srgb, var(--accent) 48%, var(--border));
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  outline: none;
}

@media (max-width: 640px) {
  .meta-ribbon {
    width: calc(100% - 16px);
    margin: 8px auto;
  }

  .meta-ribbon-head {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }

  .meta-ribbon-toggle {
    grid-column: 1 / -1;
    justify-self: end;
  }

  .meta-ribbon-main {
    align-items: stretch;
    flex-direction: column;
    gap: 3px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .meta-ribbon-icon.running,
  .meta-ribbon-chips .chip.running::after {
    animation: none;
  }

  .meta-ribbon-fill,
  .meta-ribbon-chips,
  .meta-ribbon-chips .chip,
  .meta-ribbon-actions button {
    scroll-behavior: auto;
    transition: none;
  }
}
</style>
