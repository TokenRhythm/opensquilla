<template>
  <div
    v-if="enabled"
    ref="handleRef"
    class="workbench-resizer"
    :class="{ 'is-dragging': drag.active }"
    role="separator"
    tabindex="0"
    :aria-label="ariaLabel"
    aria-orientation="vertical"
    :aria-controls="ariaControls"
    :aria-valuemin="minimumWidth"
    :aria-valuemax="maximumWidth"
    :aria-valuenow="displayWidth"
    :aria-valuetext="`${ariaLabel}, ${displayWidth} ${unitLabel}`"
    data-testid="workbench-resizer"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="onPointerUp"
    @pointercancel="onPointerCancel"
    @lostpointercapture="onLostPointerCapture"
    @dblclick="resetToDefault"
    @keydown="onKeydown"
    @blur="onBlur"
  >
    <span class="workbench-resizer__status" role="status" aria-live="polite">
      {{ announcement }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  WORKBENCH_DEFAULT_WIDTH,
  WORKBENCH_MIN_WIDTH,
} from '@/workbench/layout'

const KEYBOARD_STEP = 8
const KEYBOARD_LARGE_STEP = 32
const DRAG_DEADZONE = 4
const GLOBAL_RESIZING_CLASS = 'is-workbench-resizing'

const props = withDefaults(defineProps<{
  enabled?: boolean
  width: number
  min?: number
  max: number
  resetWidth?: number
  ariaLabel?: string
  ariaControls?: string
  unitLabel?: string
}>(), {
  enabled: true,
  min: WORKBENCH_MIN_WIDTH,
  resetWidth: WORKBENCH_DEFAULT_WIDTH,
  ariaLabel: 'Resize workbench',
  ariaControls: 'app-main workbench-panel',
  unitLabel: 'pixels',
})

const emit = defineEmits<{
  'resize-start': [width: number]
  preview: [width: number]
  commit: [width: number]
  reset: [width: number]
  cancel: [width: number]
  'resize-end': [width: number]
}>()

const handleRef = ref<HTMLElement | null>(null)
const previewWidth = ref<number | null>(null)
const announcement = ref('')
const drag = reactive({
  active: false,
  moved: false,
  pointerId: -1,
  startX: 0,
  latestClientX: 0,
  startWidth: WORKBENCH_DEFAULT_WIDTH,
})
let pointerFrame = 0

const minimumWidth = computed(() =>
  Math.max(WORKBENCH_MIN_WIDTH, normalizeInteger(props.min, WORKBENCH_MIN_WIDTH)))
const maximumWidth = computed(() =>
  Math.max(minimumWidth.value, normalizeInteger(props.max, minimumWidth.value)))
const currentWidth = computed(() => clampWidth(props.width))
const displayWidth = computed(() => previewWidth.value ?? currentWidth.value)

function normalizeInteger(value: number, fallback: number): number {
  return Number.isFinite(value) ? Math.round(value) : fallback
}

function clampWidth(value: number): number {
  return Math.min(
    maximumWidth.value,
    Math.max(minimumWidth.value, normalizeInteger(value, minimumWidth.value)),
  )
}

function requestFrame(callback: FrameRequestCallback): number {
  if (typeof window.requestAnimationFrame === 'function') {
    return window.requestAnimationFrame(callback)
  }
  return window.setTimeout(() => callback(performance.now()), 0)
}

function cancelFrame(frame: number) {
  if (!frame) return
  if (typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame)
  else window.clearTimeout(frame)
}

function isSupportedPointer(event: PointerEvent): boolean {
  return event.isPrimary !== false
    && event.button === 0
    && (event.pointerType === 'mouse' || event.pointerType === 'pen')
}

function capturePointer(target: HTMLElement, pointerId: number) {
  try {
    target.setPointerCapture?.(pointerId)
  } catch {
    // The pointer may have ended before the browser grants capture.
  }
}

function releasePointer(target: HTMLElement | null, pointerId: number) {
  if (!target || pointerId < 0) return
  try {
    if (!target.hasPointerCapture || target.hasPointerCapture(pointerId)) {
      target.releasePointerCapture?.(pointerId)
    }
  } catch {
    // Losing capture is already an accepted terminal state.
  }
}

function setGlobalResizing(active: boolean) {
  document.documentElement.classList.toggle(GLOBAL_RESIZING_CLASS, active)
}

function onPointerDown(event: PointerEvent) {
  if (!props.enabled || drag.active || !isSupportedPointer(event)) return
  const target = event.currentTarget
  if (!(target instanceof HTMLElement)) return
  event.preventDefault()
  drag.active = true
  drag.moved = false
  drag.pointerId = event.pointerId
  drag.startX = event.clientX
  drag.latestClientX = event.clientX
  drag.startWidth = currentWidth.value
  previewWidth.value = currentWidth.value
  announcement.value = ''
  setGlobalResizing(true)
  target.focus({ preventScroll: true })
  capturePointer(target, event.pointerId)
  emit('resize-start', drag.startWidth)
}

function onPointerMove(event: PointerEvent) {
  if (!drag.active || event.pointerId !== drag.pointerId) return
  drag.latestClientX = event.clientX
  if (pointerFrame) return
  pointerFrame = requestFrame(() => {
    pointerFrame = 0
    applyPointerPosition(drag.latestClientX)
  })
}

function applyPointerPosition(clientX: number) {
  if (!drag.active) return
  // The workbench is on the right: moving the divider left grows the pane.
  const delta = drag.startX - clientX
  if (!drag.moved && Math.abs(delta) < DRAG_DEADZONE) return
  drag.moved = true
  const next = clampWidth(drag.startWidth + delta)
  if (next === previewWidth.value) return
  previewWidth.value = next
  announcement.value = `${next} ${props.unitLabel}`
  emit('preview', next)
}

function flushPointerPosition(clientX: number) {
  cancelFrame(pointerFrame)
  pointerFrame = 0
  applyPointerPosition(clientX)
}

function finishGesture() {
  cancelFrame(pointerFrame)
  pointerFrame = 0
  drag.active = false
  drag.moved = false
  drag.pointerId = -1
  previewWidth.value = null
  announcement.value = ''
  setGlobalResizing(false)
}

function rollback(releaseCapture = true): boolean {
  if (!drag.active) return false
  const startWidth = drag.startWidth
  const pointerId = drag.pointerId
  finishGesture()
  if (releaseCapture) releasePointer(handleRef.value, pointerId)
  emit('preview', startWidth)
  emit('cancel', startWidth)
  emit('resize-end', startWidth)
  return true
}

function onPointerUp(event: PointerEvent) {
  if (!drag.active || event.pointerId !== drag.pointerId) return
  flushPointerPosition(event.clientX)
  const finalWidth = previewWidth.value ?? drag.startWidth
  const shouldCommit = drag.moved && finalWidth !== drag.startWidth
  const startWidth = drag.startWidth
  const pointerId = drag.pointerId
  finishGesture()
  releasePointer(handleRef.value, pointerId)
  if (shouldCommit) emit('commit', finalWidth)
  else emit('cancel', startWidth)
  emit('resize-end', shouldCommit ? finalWidth : startWidth)
}

function onPointerCancel(event: PointerEvent) {
  if (drag.active && event.pointerId === drag.pointerId) rollback()
}

function onLostPointerCapture(event: PointerEvent) {
  if (drag.active && event.pointerId === drag.pointerId) rollback(false)
}

function onBlur() {
  if (drag.active) rollback()
}

function onWindowBlur() {
  if (drag.active) rollback()
}

function commitDiscreteWidth(nextWidth: number) {
  if (!props.enabled || drag.active) return
  const startWidth = currentWidth.value
  const next = clampWidth(nextWidth)
  if (next === startWidth) return
  emit('resize-start', startWidth)
  emit('preview', next)
  emit('commit', next)
  emit('resize-end', next)
}

function resetToDefault(event: MouseEvent) {
  if (event.button !== 0 || !props.enabled || drag.active) return
  event.preventDefault()
  const startWidth = currentWidth.value
  const next = clampWidth(props.resetWidth)
  emit('resize-start', startWidth)
  emit('preview', next)
  emit('reset', next)
  emit('resize-end', next)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && drag.active) {
    event.preventDefault()
    rollback()
    return
  }
  const step = event.shiftKey ? KEYBOARD_LARGE_STEP : KEYBOARD_STEP
  let next: number | null = null
  if (event.key === 'ArrowLeft') next = displayWidth.value + step
  else if (event.key === 'ArrowRight') next = displayWidth.value - step
  else if (event.key === 'Home') next = minimumWidth.value
  else if (event.key === 'End') next = maximumWidth.value
  if (next === null) return
  event.preventDefault()
  commitDiscreteWidth(next)
}

watch(() => props.enabled, enabled => {
  if (!enabled) rollback()
})

watch([() => props.min, () => props.max], () => {
  if (drag.active) rollback()
})

watch(() => props.width, nextWidth => {
  if (!drag.active) return
  const expected = previewWidth.value ?? drag.startWidth
  if (clampWidth(nextWidth) !== expected) rollback()
})

onMounted(() => window.addEventListener('blur', onWindowBlur))

onBeforeUnmount(() => {
  window.removeEventListener('blur', onWindowBlur)
  rollback()
  setGlobalResizing(false)
})

defineExpose({ cancel: rollback })
</script>

<style scoped>
.workbench-resizer {
  position: absolute;
  z-index: 2;
  inset-block: 0;
  inset-inline-start: -8px;
  width: 16px;
  padding: 0;
  border: 0;
  background: transparent;
  cursor: col-resize;
  touch-action: auto;
}

.workbench-resizer::before {
  position: absolute;
  inset-block: 0;
  inset-inline-start: 8px;
  width: 2px;
  background: var(--border-strong);
  content: '';
  transition:
    width var(--dur-fast) var(--ease-out),
    background-color var(--dur-fast) var(--ease-out);
}

.workbench-resizer:hover::before,
.workbench-resizer:focus-visible::before,
.workbench-resizer.is-dragging::before {
  width: 2px;
  background: var(--accent);
}

.workbench-resizer:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -3px;
}

.workbench-resizer__status {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  border: 0;
  white-space: nowrap;
}

:global(html.is-workbench-resizing),
:global(html.is-workbench-resizing *) {
  cursor: col-resize !important;
  user-select: none !important;
}

@media (prefers-reduced-motion: reduce) {
  .workbench-resizer::before {
    transition: none;
  }
}

@media (forced-colors: active) {
  .workbench-resizer::before {
    background: CanvasText;
  }

  .workbench-resizer:hover::before,
  .workbench-resizer:focus-visible::before,
  .workbench-resizer.is-dragging::before {
    width: 3px;
    background: Highlight;
  }
}
</style>
