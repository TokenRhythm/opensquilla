<template>
  <section
    ref="rootElement"
    class="plan-run"
    :class="`plan-run--${run.status}`"
    :data-plan-run-id="run.runId"
    role="region"
    :aria-label="t('chat.planRun.title')"
  >
    <template v-if="hasInspectableSteps">
      <div class="plan-run__control">
        <div
          class="plan-run__disclosure"
          :class="{ 'plan-run__disclosure--open': open }"
          @pointerenter="onPointerEnter"
          @pointerleave="onPointerLeave"
          @focusin="onFocusIn"
          @focusout="onFocusOut"
          @keydown.esc.stop.prevent="onEscape"
        >
          <button
            ref="summaryButton"
            type="button"
            class="plan-run__summary"
            :aria-label="regionLabel"
            :aria-expanded="open"
            :aria-controls="stepsId"
            @pointerdown="onPointerDown"
            @pointerup="onPointerUp"
            @pointercancel="onPointerCancel"
            @keydown="onSummaryKeydown"
            @click="onSummaryClick"
          >
            <span class="plan-run__marker-slot">
              <Transition name="plan-run-marker" mode="out-in">
                <ExecutionTodoMarker
                  :key="summaryMarkerKey"
                  :status="summaryMarkerStatus"
                />
              </Transition>
            </span>
            <span class="plan-run__summary-label">
              {{ deliveryReady
                ? t('chat.planRun.finishing', {
                    completed: completedCount,
                    total: run.steps.length,
                  })
                : isRunning
                ? t('chat.planRun.progress', {
                    current: summaryOrdinal,
                    total: run.steps.length,
                  })
                : runStatusLabel }}
            </span>
            <span
              v-if="!isRunning"
              class="plan-run__summary-count"
              aria-hidden="true"
            >
              <span aria-hidden="true">·</span>
              <span>
                {{ summaryOrdinal }}/{{ run.steps.length }}
              </span>
            </span>
            <Icon
              class="plan-run__chevron"
              :class="{ 'plan-run__chevron--open': open }"
              name="chevronRight"
              :size="14"
              aria-hidden="true"
            />
          </button>

          <Transition name="plan-run-popover">
            <div
              v-if="hasSteps && open"
              :id="stepsId"
              class="plan-run__popover"
            >
              <TransitionGroup
                tag="ol"
                name="plan-run-step"
                class="plan-run__steps"
                :aria-label="t('chat.planRun.title')"
              >
                <li
                  v-for="step in run.steps"
                  :key="step.stepId"
                  class="plan-run__step"
                  :class="`plan-run__step--${step.status}`"
                  :data-step-id="step.stepId"
                  :aria-current="isCurrent(step.stepId) ? 'step' : undefined"
                >
                  <span class="plan-run__marker-slot">
                    <Transition name="plan-run-marker" mode="out-in">
                      <ExecutionTodoMarker
                        :key="`${step.stepId}:${step.status}`"
                        :status="step.status"
                      />
                    </Transition>
                  </span>
                  <span class="plan-run__step-copy">
                    <span class="plan-run__step-title">{{ step.title }}</span>
                    <span
                      v-if="safeRunReason(step.reason)"
                      class="plan-run__step-reason"
                    >{{ safeRunReason(step.reason) }}</span>
                  </span>
                  <span class="plan-run__step-state">
                    {{ stepStatusLabel(step.status) }}
                  </span>
                </li>
              </TransitionGroup>
              <div v-if="statusReason" class="plan-run__popover-reason">
                {{ statusReason }}
              </div>
              <div v-if="canCancel" class="plan-run__actions">
                <button
                  type="button"
                  class="plan-run__end"
                  :disabled="cancelBusy || disabled"
                  @click.stop="onEndPlan"
                >
                  {{ cancelBusy
                    ? t('chat.planRun.endingPlan')
                    : t('chat.planRun.endPlan') }}
                </button>
              </div>
            </div>
          </Transition>
        </div>
        <span
          class="plan-run__sr-only"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >{{ liveStatusLabel }}</span>
      </div>
    </template>

    <div
      v-else-if="isRunning"
      class="plan-run__summary plan-run__summary--static"
      role="status"
    >
      <ExecutionTodoMarker status="in_progress" />
      <span class="plan-run__title">{{ t('chat.planRun.running') }}</span>
    </div>

    <div v-else class="plan-run__static" role="status">
      <ExecutionTodoMarker :status="staticTodoStatus" />
      <span class="plan-run__static-copy">
        <span class="plan-run__static-head">
          <span class="plan-run__title">{{ runStatusLabel }}</span>
          <span v-if="staticProgressText" class="plan-run__static-status">
            {{ staticProgressText }}
          </span>
        </span>
        <span v-if="statusReason" class="plan-run__reason">{{ statusReason }}</span>
      </span>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ExecutionTodoMarker from '@/components/chat/ExecutionTodoMarker.vue'
import type {
  PlanRunSnapshot,
  PlanRunStatus,
  PlanRunStepStatus,
} from '@/types/plans'

const props = withDefaults(defineProps<{
  run: PlanRunSnapshot
  defaultOpen?: boolean
  cancelBusy?: boolean
  disabled?: boolean
}>(), {
  defaultOpen: false,
  cancelBusy: false,
  disabled: false,
})

const emit = defineEmits<{
  cancel: []
  focusReturn: []
}>()

const { t } = useI18n()
const rootElement = ref<HTMLElement | null>(null)
const summaryButton = ref<HTMLButtonElement | null>(null)
const pointerWithin = ref(false)
const focusWithin = ref(false)
const activationOpen = ref<boolean | null>(props.defaultOpen ? true : null)
const dismissed = ref(false)
const open = computed(() => !dismissed.value && (
  pointerWithin.value
  || activationOpen.value === true
  || (focusWithin.value && activationOpen.value !== false)
))
let lastPointerType = ''
let suppressNextFocusOpen = false
const stepsId = `plan-run-steps-${useId()}`
const isRunning = computed(() => props.run.status === 'running')
const hasSteps = computed(() => props.run.steps.length > 0)
const inspectableStatuses = new Set<PlanRunStatus>([
  'queued',
  'running',
  'paused',
  'blocked',
])
const hasInspectableSteps = computed(() =>
  hasSteps.value && inspectableStatuses.has(props.run.status),
)
const canCancel = computed(() =>
  props.run.status === 'paused'
  || props.run.status === 'blocked',
)
const RUN_REASON_MAX_CHARS = 160

function safeRunReason(value: string | undefined): string {
  if (!value) return ''
  const compact = [...value]
    .map((character) => {
      const code = character.codePointAt(0) ?? 0
      return code < 32 || (code >= 127 && code <= 159) ? ' ' : character
    })
    .join('')
    .replace(/\s+/g, ' ')
    .trim()
  const characters = [...compact]
  if (characters.length <= RUN_REASON_MAX_CHARS) return compact
  return `${characters.slice(0, RUN_REASON_MAX_CHARS - 1).join('')}…`
}

const statusReason = computed(() => {
  if (props.run.status === 'paused' || props.run.status === 'blocked') {
    return safeRunReason(props.run.pauseReason || props.run.terminalReason)
  }
  if (props.run.status === 'cancelled' || props.run.status === 'superseded') {
    return safeRunReason(props.run.terminalReason || props.run.pauseReason)
  }
  return ''
})

watch(
  () => props.run.runId,
  () => {
    pointerWithin.value = false
    focusWithin.value = false
    activationOpen.value = props.defaultOpen ? true : null
    dismissed.value = false
    lastPointerType = ''
    suppressNextFocusOpen = false
  },
)

const terminalRunStatuses = new Set<PlanRunStatus>([
  'completed',
  'cancelled',
  'superseded',
])

watch(
  () => props.run.status,
  (status, previousStatus) => {
    if (
      !terminalRunStatuses.has(status)
      || terminalRunStatuses.has(previousStatus)
      || typeof document === 'undefined'
    ) return
    const activeElement = document.activeElement
    if (activeElement && rootElement.value?.contains(activeElement)) {
      emit('focusReturn')
    }
  },
  { flush: 'pre' },
)

function isTouchLikePointer(pointerType: string): boolean {
  return pointerType === 'touch' || pointerType === 'pen'
}

function onPointerDown(event: PointerEvent): void {
  lastPointerType = event.pointerType
  suppressNextFocusOpen = Boolean(event.pointerType)
  if (event.pointerType === 'mouse') {
    activationOpen.value = null
    dismissed.value = false
  }
}

function onPointerUp(): void {
  suppressNextFocusOpen = false
}

function onPointerCancel(): void {
  lastPointerType = ''
  suppressNextFocusOpen = false
}

function onPointerEnter(event: PointerEvent): void {
  if (event.pointerType === 'mouse' || event.pointerType === '') {
    activationOpen.value = null
    dismissed.value = false
    lastPointerType = 'mouse'
    pointerWithin.value = true
  }
}

function onPointerLeave(event: PointerEvent): void {
  if (event.pointerType === 'mouse' || event.pointerType === '') {
    pointerWithin.value = false
  }
}

function onFocusIn(): void {
  if (suppressNextFocusOpen) {
    suppressNextFocusOpen = false
    return
  }
  if (!focusWithin.value) {
    activationOpen.value = null
    dismissed.value = false
  }
  focusWithin.value = true
}

function onFocusOut(event: FocusEvent): void {
  const disclosure = event.currentTarget as HTMLElement | null
  const nextTarget = event.relatedTarget
  if (
    disclosure
    && nextTarget instanceof Node
    && disclosure.contains(nextTarget)
  ) {
    return
  }
  focusWithin.value = false
  activationOpen.value = null
  dismissed.value = false
}

function toggleActivation(): void {
  const shouldOpen = !open.value
  activationOpen.value = shouldOpen
  dismissed.value = !shouldOpen
}

function onSummaryKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' && event.key !== ' ') return
  pointerWithin.value = false
  lastPointerType = ''
  suppressNextFocusOpen = false
}

function onSummaryClick(event: MouseEvent): void {
  const eventPointerType = 'pointerType' in event
    ? String((event as PointerEvent).pointerType || '')
    : ''
  const pointerType = eventPointerType || lastPointerType
  const syntheticActivation = event.detail === 0
  if (!syntheticActivation && !isTouchLikePointer(pointerType)) return
  pointerWithin.value = false
  toggleActivation()
  lastPointerType = ''
  suppressNextFocusOpen = false
}

function onEscape(): void {
  pointerWithin.value = false
  activationOpen.value = false
  dismissed.value = true
  summaryButton.value?.focus({ preventScroll: true })
}

function onEndPlan(): void {
  emit('cancel')
  emit('focusReturn')
}

const currentOrdinal = computed(() => {
  if (!props.run.steps.length) return 0
  const explicit = props.run.steps.findIndex(step => step.stepId === props.run.currentStepId)
  if (explicit >= 0) return explicit + 1
  const inProgress = props.run.steps.findIndex(step => step.status === 'in_progress')
  if (inProgress >= 0) return inProgress + 1
  const completed = props.run.steps.filter(step =>
    step.status === 'completed' || step.status === 'skipped',
  ).length
  return Math.min(completed + 1, props.run.steps.length)
})
const completedCount = computed(() => props.run.steps.filter(step =>
  step.status === 'completed' || step.status === 'skipped',
).length)
const deliveryReady = computed(() =>
  isRunning.value
  && !props.run.currentStepId
  && hasSteps.value
  && completedCount.value === props.run.steps.length,
)
const currentStep = computed(() => props.run.steps[currentOrdinal.value - 1])
const currentStepStatus = computed<PlanRunStepStatus>(() =>
  currentStep.value?.status || 'in_progress',
)
const summaryMarkerStatus = computed<PlanRunStepStatus>(() => {
  if (deliveryReady.value) return 'in_progress'
  if (props.run.status === 'running') return currentStepStatus.value
  if (props.run.status === 'blocked') return 'blocked'
  return 'pending'
})
const summaryOrdinal = computed(() =>
  props.run.status === 'queued' ? completedCount.value : currentOrdinal.value,
)
const summaryTitle = computed(() =>
  deliveryReady.value
    ? String(t('chat.planRun.finishing', {
        completed: completedCount.value,
        total: props.run.steps.length,
      }))
    : currentStep.value?.title || runStatusLabel.value,
)
const summaryMarkerKey = computed(() =>
  `${props.run.status}:${props.run.currentStepId || currentOrdinal.value}:${summaryMarkerStatus.value}`,
)

const runStatusLabel = computed(() => String(t(`chat.planRun.status.${props.run.status}`)))
const staticProgressText = computed(() => hasSteps.value
  ? `${completedCount.value}/${props.run.steps.length}`
  : '')

const regionLabel = computed(() => {
  if (hasInspectableSteps.value) {
    const status = deliveryReady.value
      ? String(t('chat.planRun.regionFinishing', {
          total: props.run.steps.length,
        }))
      : isRunning.value
      ? String(t('chat.planRun.regionRunning', {
          current: summaryOrdinal.value,
          total: props.run.steps.length,
        }))
      : String(t('chat.planRun.regionStatus', { status: runStatusLabel.value }))
    return `${status}: ${summaryTitle.value}`
  }
  return String(t('chat.planRun.regionStatus', { status: runStatusLabel.value }))
})
const liveStatusLabel = computed(() =>
  isRunning.value ? summaryTitle.value : runStatusLabel.value,
)

const staticTodoStatus = computed<PlanRunStepStatus>(() => {
  const statuses: Record<Exclude<PlanRunStatus, 'running'>, PlanRunStepStatus> = {
    queued: 'pending',
    paused: 'pending',
    blocked: 'blocked',
    completed: 'completed',
    cancelled: 'skipped',
    superseded: 'skipped',
  }
  return props.run.status === 'running' ? 'in_progress' : statuses[props.run.status]
})

function isCurrent(stepId: string): boolean {
  if (deliveryReady.value) return false
  if (props.run.currentStepId) return props.run.currentStepId === stepId
  return props.run.steps[currentOrdinal.value - 1]?.stepId === stepId
}

function stepStatusLabel(status: PlanRunStepStatus): string {
  return String(t(`chat.planRun.stepStatus.${status}`))
}

</script>

<style scoped>
.plan-run {
  position: relative;
  width: min(440px, calc(100vw - 24px));
  max-width: 100%;
  flex-shrink: 0;
  color: var(--text);
}

.plan-run__control {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  width: 100%;
  max-width: calc(100vw - 24px);
  align-items: center;
}

.plan-run__disclosure {
  position: relative;
  grid-column: 2;
  width: max-content;
  min-width: 0;
  max-width: 100%;
}

.plan-run__disclosure--open::after {
  position: absolute;
  bottom: 100%;
  left: 50%;
  width: min(420px, calc(100vw - 24px));
  height: 10px;
  transform: translateX(-50%);
  content: '';
}

.plan-run__summary,
.plan-run__static {
  display: flex;
  width: max-content;
  max-width: 100%;
  align-items: center;
  min-width: 0;
  min-height: 36px;
  gap: 7px;
  padding: 6px 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-sm);
  color: inherit;
  font: inherit;
  text-align: left;
}

.plan-run__summary {
  position: relative;
  width: 100%;
  flex: none;
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease-standard),
    background var(--dur-fast) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard);
}

.plan-run__static {
  margin-inline: auto;
}

.plan-run__summary::before {
  position: absolute;
  inset: -4px 0;
  content: '';
}

.plan-run__summary--static {
  cursor: default;
}

.plan-run__summary:hover {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: color-mix(in srgb, var(--bg-elevated) 96%, var(--accent) 4%);
}

.plan-run__summary[aria-expanded="true"] {
  border-color: color-mix(in srgb, var(--accent) 22%, var(--border));
  background: var(--bg-elevated);
}

.plan-run__summary--static:hover {
  border-color: var(--border);
  background: var(--bg-elevated);
}

.plan-run__summary:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.plan-run__marker-slot {
  display: grid;
  width: 18px;
  height: 18px;
  flex: none;
  place-items: center;
}

.plan-run__sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.plan-run__title {
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plan-run__summary-label,
.plan-run__summary-count {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  font-weight: 500;
  line-height: 1;
  white-space: nowrap;
}

.plan-run__summary-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.plan-run__static-status {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.plan-run__summary-count {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-variant-numeric: tabular-nums;
}

.plan-run__static-copy {
  display: grid;
  min-width: 0;
  flex: 1 1 auto;
  gap: 2px;
}

.plan-run__static-head {
  display: flex;
  min-width: 0;
  align-items: baseline;
  gap: var(--sp-2);
}

.plan-run__static-head .plan-run__title {
  flex: 0 1 auto;
}

.plan-run__reason {
  max-width: 260px;
  overflow: hidden;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plan-run__chevron {
  flex: none;
  color: var(--text-muted);
  transition: transform var(--dur-fast) var(--ease-standard);
}

.plan-run__chevron--open {
  transform: rotate(90deg);
}

.plan-run__popover {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 50%;
  width: min(420px, calc(100vw - 24px));
  max-height: min(50vh, 360px);
  overflow-y: auto;
  transform: translateX(-50%);
  transform-origin: bottom center;
  z-index: 1;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-lg);
  scrollbar-gutter: stable;
}

.plan-run__steps {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 4px 12px;
  list-style: none;
}

.plan-run__step {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  min-height: 38px;
  gap: 9px;
  padding: 8px 2px;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: var(--fs-sm);
  transition:
    background var(--dur-fast) var(--ease-standard),
    color var(--dur-fast) var(--ease-standard);
}

.plan-run__step:last-child {
  border-bottom: 0;
}

.plan-run__step--in_progress {
  color: var(--text);
}

.plan-run__step--completed,
.plan-run__step--skipped {
  color: var(--text-muted);
}

.plan-run__step--blocked {
  color: var(--text);
}

.plan-run__step-title {
  min-width: 0;
  display: -webkit-box;
  overflow: hidden;
  font-weight: 500;
  line-height: 1.4;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow-wrap: anywhere;
}

.plan-run__step--in_progress .plan-run__step-title,
.plan-run__step--blocked .plan-run__step-title {
  font-weight: 650;
}

.plan-run__step-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.plan-run__step-state {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.25;
  white-space: nowrap;
}

.plan-run__step--in_progress .plan-run__step-state {
  color: color-mix(in srgb, var(--accent) 72%, var(--text-muted));
}

.plan-run__step--blocked .plan-run__step-state {
  color: var(--danger);
}

.plan-run__step-reason {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 400;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.plan-run__popover-reason {
  padding: var(--sp-2) var(--sp-3);
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
}

.plan-run__actions {
  display: flex;
  justify-content: flex-end;
  padding: 6px 10px 8px;
  border-top: 1px solid var(--border);
}

.plan-run__end {
  min-height: 30px;
  padding: 0 7px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs);
}

.plan-run__end:hover:not(:disabled),
.plan-run__end:focus-visible {
  background: var(--bg-hover);
  color: var(--danger);
  outline: none;
}

.plan-run__end:focus-visible {
  box-shadow: var(--focus-ring);
}

.plan-run__end:disabled {
  opacity: var(--state-disabled-opacity);
  cursor: not-allowed;
}

.plan-run-popover-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.plan-run-popover-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.plan-run-popover-enter-from,
.plan-run-popover-leave-to {
  opacity: 0;
  transform: translate(-50%, 4px);
}

.plan-run-marker-enter-active,
.plan-run-marker-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-standard),
    transform var(--dur-fast) var(--ease-standard);
}

.plan-run-marker-enter-from {
  opacity: 0;
  transform: translateY(3px) scale(0.84);
}

.plan-run-marker-leave-to {
  opacity: 0;
  transform: translateY(-3px) scale(0.84);
}

.plan-run-step-enter-active,
.plan-run-step-leave-active,
.plan-run-step-move {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.plan-run-step-enter-from,
.plan-run-step-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

@media (max-width: 640px) {
  .plan-run {
    width: calc(100vw - 16px);
    max-width: calc(100vw - 16px);
  }

  .plan-run__summary,
  .plan-run__static,
  .plan-run__end {
    min-height: 44px;
  }

  .plan-run__popover {
    width: calc(100vw - 16px);
  }

  .plan-run__step {
    min-height: 40px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .plan-run__summary,
  .plan-run__chevron,
  .plan-run__step,
  .plan-run-popover-enter-active,
  .plan-run-popover-leave-active,
  .plan-run-marker-enter-active,
  .plan-run-marker-leave-active,
  .plan-run-step-enter-active,
  .plan-run-step-leave-active,
  .plan-run-step-move {
    transition: none;
  }
}
</style>
