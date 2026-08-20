<template>
  <Transition name="conversation-minimap-shell">
    <nav
      v-if="visible"
      ref="rootRef"
      class="conversation-minimap"
      :aria-label="navigatorLabel"
      data-testid="conversation-minimap"
    >
      <ul
        ref="listRef"
        class="conversation-minimap__list"
        @scroll="closeHoverPreview"
        @pointermove="onListPointerMove"
        @pointerleave="closeHoverPreview"
      >
        <li v-for="(turn, index) in turns" :key="turn.key" class="conversation-minimap__item">
          <button
            :ref="el => setMarkerRef(el, index)"
            type="button"
            class="conversation-minimap__marker"
            :class="{ 'is-active': index === activeIndex }"
            :style="markerStyle(index)"
            :tabindex="index === (focusedIndex ?? activeIndex) ? 0 : -1"
            :aria-current="index === activeIndex ? 'location' : undefined"
            :aria-controls="turn.controlId"
            :aria-label="t(historyHasMore ? 'chat.historyTurnLoadedLabel' : 'chat.historyTurnLabel', {
              number: index + 1,
              total: turns.length,
              preview: turn.preview,
            })"
            :aria-describedby="previewIndex === index ? tooltipId : undefined"
            data-testid="conversation-minimap-marker"
            @click="navigateTo(index, $event.detail === 0)"
            @mouseenter="showHoverPreview(index, $event.currentTarget)"
            @focus="showFocusPreview(index, $event.currentTarget)"
            @blur="focusedIndex = null"
            @keydown="onMarkerKeydown(index, $event)"
          >
            <span class="conversation-minimap__line" aria-hidden="true" />
          </button>
        </li>
      </ul>

      <Transition name="conversation-minimap-preview">
        <div
          v-if="previewTurn"
          class="conversation-minimap__preview-positioner"
          :style="{ '--conversation-minimap-preview-y': `${previewTop}px` }"
        >
          <div
            :id="tooltipId"
            ref="tooltipRef"
            class="conversation-minimap__preview"
            role="tooltip"
          >
            <div class="conversation-minimap__preview-meta">
              <span>{{ t(historyHasMore ? 'chat.historyTurnLoadedCount' : 'chat.historyTurnCount', { number: (previewIndex ?? 0) + 1, total: turns.length }) }}</span>
              <time v-if="previewTurn.time">{{ previewTurn.time }}</time>
            </div>
            <p>{{ previewTurn.preview }}</p>
            <span v-if="previewTurn.attachmentCount" class="conversation-minimap__attachments">
              {{ t('chat.historyAttachmentCount', { count: previewTurn.attachmentCount }) }}
            </span>
          </div>
        </div>
      </Transition>
    </nav>
  </Transition>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  type ComponentPublicInstance,
} from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import { chatMessageKey } from '@/utils/chat/messageIdentity'

const MIN_TURNS = 8
const ENTER_SCROLL_RANGE_RATIO = 1.5
const EXIT_SCROLL_RANGE_RATIO = 1
// Width uses a Schmitt-trigger style hysteresis so a live sidebar resize does
// not mount/unmount the rail on every sub-pixel wobble around one boundary.
// A fresh rail must reach the enter threshold; once visible it is allowed to
// remain until the conversation pane crosses the lower exit threshold.
// The rail's 30px interactive lens starts 14px from the shell edge; its focus
// ring reaches another 4px. At the 1104px floor, the shared 980px conversation
// column still leaves a stable gap after accounting for the thread's scrollbar
// gutter.
// Enter a little later than that floor so live sidebar resizing cannot flicker
// the rail at the exact collision boundary.
const ENTER_INLINE_SIZE = 1120
const EXIT_INLINE_SIZE = 1104
const ARRIVAL_HIGHLIGHT_MS = 650
const ARRIVAL_TOLERANCE_PX = 4
const MAX_PREVIEW_LENGTH = 220
const LENS_SIGMA = 1.15
const MIN_LINE_SCALE_X = 4 / 15
const MIN_LINE_SCALE_Y = 0.5
const MIN_LINE_OPACITY = 0.45
const ESTIMATED_PREVIEW_HEIGHT = 112
const PREVIEW_EDGE_GAP = 8
const COARSE_POINTER_QUERY = '(hover: none) and (pointer: coarse)'
const FINE_INPUT_QUERY = '(any-hover: hover), (any-pointer: fine)'

interface ConversationTurn {
  key: string
  sourceIndex: number
  controlId: string
  preview: string
  time: string
  attachmentCount: number
}

const props = defineProps<{
  messages: ChatRenderedMessage[]
  scrollContainer: HTMLElement | null
  stripTimePrefix: (text: string) => string
  sessionKey?: string
  historyHasMore?: boolean
  /** Mount a logical history row before DOM-dependent focus/highlight work. */
  ensureMessageVisible?: (sourceIndex: number) => Promise<HTMLElement | null>
  /** Release a row pinned only for navigation once the destination settles. */
  releaseEnsuredMessage?: (sourceIndex?: number) => void
  /** Absolute offset in the scroll container for an unmounted logical row. */
  messageOffset?: (sourceIndex: number) => number | null
}>()
const emit = defineEmits<{
  navigate: [index: number]
  navigateEnd: []
}>()

const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
const tooltipRef = ref<HTMLElement | null>(null)
const markerRefs = ref<HTMLButtonElement[]>([])
const activeIndex = ref(0)
const hoveredIndex = ref<number | null>(null)
const focusedIndex = ref<number | null>(null)
const pointerLensIndex = ref<number | null>(null)
const previewTop = ref(0)
const hasLongHistory = ref(false)
const isWideEnough = ref(false)
const supportsRailInput = ref(true)
const anchorOffsets = ref<number[]>([])
const tooltipId = `conversation-minimap-tooltip-${Math.random().toString(36).slice(2, 9)}`

let shellResizeObserver: ResizeObserver | null = null
let threadResizeObserver: ResizeObserver | null = null
let mutationObserver: MutationObserver | null = null
let coarsePointerMedia: MediaQueryList | null = null
let fineInputMedia: MediaQueryList | null = null
let measureFrame = 0
let overflowFrame = 0
let activeFrame = 0
let pointerFrame = 0
let pendingPointerClientY = 0
let observersActive = false
let navigationPending = false
let navigationContainer: HTMLElement | null = null
let navigationEndTimer = 0
let navigationTarget: HTMLElement | null = null
let navigationTargetTop = 0
let navigationTargetSourceIndex: number | null = null
let navigationGeneration = 0
let arrivalElement: HTMLElement | null = null
let arrivalTimer = 0
let lastAnchorElement: HTMLElement | null = null

const turns = computed<ConversationTurn[]>(() => {
  // Phones and narrow panes use the native conversation flow. Avoid rebuilding
  // summaries for every streaming update when the rail cannot be rendered.
  if (!isWideEnough.value) return []
  const userMessages = props.messages
    .map((message, sourceIndex) => ({ message, sourceIndex }))
    .filter(({ message }) => message.displayRole === 'user')

  return userMessages.map(({ message, sourceIndex }) => {
    const plainText = props.stripTimePrefix(message.text || '').replace(/\s+/g, ' ').trim()
    const preview = plainText
      ? truncatePreview(plainText)
      : t('chat.historyAttachmentOnly')
    return {
      key: chatMessageKey(message, sourceIndex),
      sourceIndex,
      controlId: `chat-turn-${sourceIndex}`,
      preview,
      time: message.timeStr || '',
      attachmentCount: message.attachments?.length || 0,
    }
  })
})

const visible = computed(() => isWideEnough.value && turns.value.length >= MIN_TURNS && hasLongHistory.value)
const navigatorLabel = computed(() => t(
  props.historyHasMore ? 'chat.historyNavigatorLoadedLabel' : 'chat.historyNavigatorLabel',
  { count: turns.value.length },
))
const previewIndex = computed(() => hoveredIndex.value ?? focusedIndex.value)
const previewTurn = computed(() => {
  const index = previewIndex.value
  return index === null ? null : turns.value[index] || null
})
const visualFocusIndex = computed<number | null>(() => (
  pointerLensIndex.value
  ?? focusedIndex.value
  ?? hoveredIndex.value
))

function truncatePreview(text: string): string {
  if (text.length <= MAX_PREVIEW_LENGTH) return text
  return `${text.slice(0, MAX_PREVIEW_LENGTH - 1).trimEnd()}…`
}

function setMarkerRef(el: Element | ComponentPublicInstance | null, index: number) {
  if (el instanceof HTMLButtonElement) markerRefs.value[index] = el
}

function markerStyle(index: number): Record<string, string> {
  const isActive = index === activeIndex.value
  const focusIndex = visualFocusIndex.value
  let scaleX = MIN_LINE_SCALE_X
  if (focusIndex !== null) {
    const distance = Math.abs(index - focusIndex)
    const influence = Math.exp(-(distance * distance) / (2 * LENS_SIGMA * LENS_SIGMA))
    scaleX += (1 - MIN_LINE_SCALE_X) * influence
  }

  return {
    '--conversation-minimap-line-scale-x': scaleX.toFixed(4),
    '--conversation-minimap-line-scale-y': (isActive ? 1 : MIN_LINE_SCALE_Y).toFixed(4),
    '--conversation-minimap-line-opacity': (isActive ? 1 : MIN_LINE_OPACITY).toFixed(4),
  }
}

function anchorElements(): Map<string, HTMLElement> {
  const container = props.scrollContainer
  const anchors = new Map<string, HTMLElement>()
  if (!container) return anchors
  container.querySelectorAll<HTMLElement>('[data-chat-turn-key]').forEach(element => {
    const key = element.dataset.chatTurnKey
    if (key) anchors.set(key, element)
  })
  return anchors
}

function updateOverflowState() {
  const container = props.scrollContainer
  if (!container || !isWideEnough.value || turns.value.length < MIN_TURNS) {
    hasLongHistory.value = false
    return
  }
  const scrollRange = Math.max(0, container.scrollHeight - container.clientHeight)
  const threshold = hasLongHistory.value
    ? EXIT_SCROLL_RANGE_RATIO
    : ENTER_SCROLL_RANGE_RATIO
  hasLongHistory.value = container.clientHeight > 0
    && scrollRange >= container.clientHeight * threshold
  updateActiveTurn()
}

function measureLayout() {
  const container = props.scrollContainer
  if (!container || !isWideEnough.value) {
    hasLongHistory.value = false
    anchorOffsets.value = []
    return
  }

  updateOverflowState()

  const containerRect = container.getBoundingClientRect()
  const anchors = anchorElements()
  lastAnchorElement = anchors.get(turns.value[turns.value.length - 1]?.key || '') || null
  anchorOffsets.value = turns.value.map(turn => {
    const anchor = anchors.get(turn.key)
    if (anchor) {
      return anchor.getBoundingClientRect().top - containerRect.top + container.scrollTop
    }
    const logicalOffset = props.messageOffset?.(turn.sourceIndex)
    return logicalOffset !== null
      && logicalOffset !== undefined
      && Number.isFinite(logicalOffset)
      ? logicalOffset
      : Number.POSITIVE_INFINITY
  })
  updateActiveTurn()
}

function updateActiveTurn() {
  // Keep the selected destination visually stable while the scroll viewport
  // crosses intermediate prompts. Reconcile to the reading line on arrival.
  if (navigationPending) return
  const container = props.scrollContainer
  const offsets = anchorOffsets.value
  if (!container || offsets.length === 0) {
    activeIndex.value = 0
    return
  }

  const bottomGap = container.scrollHeight - container.scrollTop - container.clientHeight
  let nextIndex = 0
  if (bottomGap <= 2) {
    nextIndex = offsets.length - 1
  } else {
    const readingLine = container.scrollTop + Math.min(180, container.clientHeight * 0.3)
    let low = 0
    let high = offsets.length - 1
    while (low <= high) {
      const mid = Math.floor((low + high) / 2)
      if (offsets[mid] <= readingLine) {
        nextIndex = mid
        low = mid + 1
      } else {
        high = mid - 1
      }
    }
  }

  if (nextIndex !== activeIndex.value) {
    activeIndex.value = nextIndex
    void nextTick(keepActiveMarkerVisible)
  }
}

function keepActiveMarkerVisible() {
  const list = listRef.value
  const marker = markerRefs.value[activeIndex.value]
  if (!list || !marker || list.matches(':hover')) return
  const markerTop = marker.offsetTop
  const markerBottom = markerTop + marker.offsetHeight
  if (markerTop < list.scrollTop) list.scrollTop = markerTop
  else if (markerBottom > list.scrollTop + list.clientHeight) {
    list.scrollTop = markerBottom - list.clientHeight
  }
}

function scheduleMeasure() {
  if (!observersActive) return
  if (measureFrame) return
  measureFrame = requestFrame(() => {
    measureFrame = 0
    measureLayout()
  })
}

function scheduleOverflowUpdate() {
  if (!observersActive) return
  if (overflowFrame) return
  overflowFrame = requestFrame(() => {
    overflowFrame = 0
    updateOverflowState()
  })
}

function scheduleActiveUpdate() {
  if (!observersActive) return
  if (activeFrame) return
  activeFrame = requestFrame(() => {
    activeFrame = 0
    updateActiveTurn()
  })
}

function requestFrame(callback: FrameRequestCallback): number {
  if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
    return window.requestAnimationFrame(callback)
  }
  return window.setTimeout(() => callback(performance.now()), 0)
}

function cancelFrame(frame: number) {
  if (!frame || typeof window === 'undefined') return
  if (typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame)
  else window.clearTimeout(frame)
}

function elementNeedsAnchorRemeasure(element: Element): boolean {
  if (!lastAnchorElement || element === lastAnchorElement) return true
  return Boolean(element.compareDocumentPosition(lastAnchorElement) & Node.DOCUMENT_POSITION_FOLLOWING)
}

function onThreadResize(entries: ResizeObserverEntry[]) {
  const container = props.scrollContainer
  if (!container) return
  if (entries.some(entry => entry.target === container || elementNeedsAnchorRemeasure(entry.target))) {
    scheduleMeasure()
  } else {
    // The streaming tail grows after the last prompt. Its height changes the
    // overflow state but cannot move any cached prompt anchor.
    scheduleOverflowUpdate()
  }
}

function observeDirectChildren(container: HTMLElement) {
  Array.from(container.children).forEach(child => threadResizeObserver?.observe(child))
}

function activateThreadObservers() {
  if (observersActive) return
  const container = props.scrollContainer
  if (!container) return
  observersActive = true
  container.addEventListener('scroll', scheduleActiveUpdate, { passive: true })

  if (typeof ResizeObserver !== 'undefined') {
    threadResizeObserver = new ResizeObserver(onThreadResize)
    threadResizeObserver.observe(container)
    observeDirectChildren(container)
  }
  if (typeof MutationObserver !== 'undefined') {
    mutationObserver = new MutationObserver(records => {
      for (const record of records) {
        record.removedNodes.forEach(node => {
          if (node instanceof Element) threadResizeObserver?.unobserve(node)
        })
        record.addedNodes.forEach(node => {
          if (node instanceof Element) threadResizeObserver?.observe(node)
        })
      }
      scheduleMeasure()
    })
    // Only top-level thread structure can add or remove prompt anchors. Nested
    // live-token mutations are covered by the direct child's ResizeObserver.
    mutationObserver.observe(container, { childList: true })
  }
  scheduleMeasure()
}

function deactivateThreadObservers() {
  const container = props.scrollContainer
  container?.removeEventListener('scroll', scheduleActiveUpdate)
  threadResizeObserver?.disconnect()
  mutationObserver?.disconnect()
  threadResizeObserver = null
  mutationObserver = null
  observersActive = false
  cancelFrame(measureFrame)
  cancelFrame(overflowFrame)
  cancelFrame(activeFrame)
  cancelFrame(pointerFrame)
  measureFrame = 0
  overflowFrame = 0
  activeFrame = 0
  pointerFrame = 0
  hasLongHistory.value = false
  anchorOffsets.value = []
  lastAnchorElement = null
  hoveredIndex.value = null
  focusedIndex.value = null
  pointerLensIndex.value = null
}

function shellElement(container: HTMLElement): HTMLElement {
  const parent = container.parentElement
  return parent?.classList.contains('chat-thread-shell') ? parent : container
}

function syncWidthEligibility() {
  const container = props.scrollContainer
  if (!container) return
  const shell = shellElement(container)
  const inlineSize = shell.clientWidth || shell.getBoundingClientRect().width
  const widthThreshold = isWideEnough.value ? EXIT_INLINE_SIZE : ENTER_INLINE_SIZE
  const nextWideEnough = supportsRailInput.value
    && (isWideEnough.value ? inlineSize > widthThreshold : inlineSize >= widthThreshold)
  if (nextWideEnough === isWideEnough.value) {
    if (nextWideEnough) scheduleOverflowUpdate()
    return
  }
  isWideEnough.value = nextWideEnough
  if (nextWideEnough) activateThreadObservers()
  else deactivateThreadObservers()
}

function syncInputEligibility() {
  const isCoarseOnly = Boolean(coarsePointerMedia?.matches && !fineInputMedia?.matches)
  supportsRailInput.value = !isCoarseOnly
  syncWidthEligibility()
}

function attachInputEligibility() {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
  coarsePointerMedia = window.matchMedia(COARSE_POINTER_QUERY)
  fineInputMedia = window.matchMedia(FINE_INPUT_QUERY)
  coarsePointerMedia.addEventListener?.('change', syncInputEligibility)
  fineInputMedia.addEventListener?.('change', syncInputEligibility)
  syncInputEligibility()
}

function detachInputEligibility() {
  coarsePointerMedia?.removeEventListener?.('change', syncInputEligibility)
  fineInputMedia?.removeEventListener?.('change', syncInputEligibility)
  coarsePointerMedia = null
  fineInputMedia = null
}

function attachContainer(next: HTMLElement | null, previous: HTMLElement | null) {
  previous?.removeEventListener('scroll', scheduleActiveUpdate)
  deactivateThreadObservers()
  shellResizeObserver?.disconnect()
  shellResizeObserver = null
  isWideEnough.value = false
  if (!next) return

  if (typeof ResizeObserver !== 'undefined') {
    shellResizeObserver = new ResizeObserver(syncWidthEligibility)
    shellResizeObserver.observe(shellElement(next))
  }
  void nextTick(syncWidthEligibility)
}

function clearNavigationEnd() {
  if (navigationContainer) navigationContainer.removeEventListener('scrollend', finishNavigation)
  navigationContainer = null
  if (navigationEndTimer) window.clearTimeout(navigationEndTimer)
  navigationEndTimer = 0
}

function clearArrivalHighlight() {
  if (arrivalTimer) window.clearTimeout(arrivalTimer)
  arrivalTimer = 0
  arrivalElement?.classList.remove('is-history-target')
  arrivalElement = null
}

function showArrivalHighlight(target: HTMLElement) {
  clearArrivalHighlight()
  arrivalElement = target
  target.classList.add('is-history-target')
  arrivalTimer = window.setTimeout(clearArrivalHighlight, ARRIVAL_HIGHLIGHT_MS)
}

function settleNavigation(showArrival: boolean) {
  if (!navigationPending) return
  const target = navigationTarget
  const container = navigationContainer
  const sourceIndex = navigationTargetSourceIndex
  const arrived = showArrival
    && Boolean(container)
    && Math.abs((container?.scrollTop || 0) - navigationTargetTop) <= ARRIVAL_TOLERANCE_PX
  navigationPending = false
  navigationTarget = null
  navigationTargetTop = 0
  navigationTargetSourceIndex = null
  clearNavigationEnd()
  if (target && arrived) showArrivalHighlight(target)
  if (sourceIndex !== null) props.releaseEnsuredMessage?.(sourceIndex)
  scheduleActiveUpdate()
  emit('navigateEnd')
}

function finishNavigation() {
  settleNavigation(true)
}

function cancelNavigation() {
  const container = navigationContainer
  const shouldCancelSmoothScroll = navigationPending && Boolean(container)
  navigationGeneration += 1
  settleNavigation(false)
  if (shouldCancelSmoothScroll && container) {
    container.scrollTo({ top: container.scrollTop, behavior: 'auto' })
  }
}

defineExpose({ cancelNavigation })

function armNavigationEnd(container: HTMLElement, smooth: boolean) {
  navigationPending = true
  navigationContainer = container
  container.addEventListener('scrollend', finishNavigation, { once: true })
  // scrollend is not universal yet. The safety net is deliberately longer
  // than a long native smooth scroll so slower engines cannot settle early.
  navigationEndTimer = window.setTimeout(finishNavigation, smooth ? 2000 : 180)
}

async function navigateTo(index: number, focusTarget = false) {
  const container = props.scrollContainer
  const turn = turns.value[index]
  if (!container || !turn) return
  cancelNavigation()
  const generation = ++navigationGeneration
  if (focusTarget) {
    focusedIndex.value = index
  } else {
    // Pointer activation focuses the button before click. Clear both preview
    // sources so the floating card does not linger over the destination; the
    // DOM focus itself stays put for keyboard continuity.
    closeHoverPreview()
    focusedIndex.value = null
  }
  // Open the lifecycle before awaiting a virtualized row. Session changes,
  // unmounts, or reader input during that await must still emit navigateEnd
  // and invalidate the eventual continuation.
  navigationPending = true
  emit('navigate', index)
  activeIndex.value = index
  let anchor = anchorElements().get(turn.key) || null
  if (!anchor) {
    try {
      anchor = await props.ensureMessageVisible?.(turn.sourceIndex) || null
    } catch {
      if (generation === navigationGeneration) settleNavigation(false)
      return
    }
  }
  if (generation !== navigationGeneration) {
    props.releaseEnsuredMessage?.(turn.sourceIndex)
    return
  }
  const containerRect = container.getBoundingClientRect()
  const anchorTop = anchor
    ? anchor.getBoundingClientRect().top - containerRect.top + container.scrollTop
    : props.messageOffset?.(turn.sourceIndex)
  if (anchorTop === null || anchorTop === undefined || !Number.isFinite(anchorTop)) {
    props.releaseEnsuredMessage?.(turn.sourceIndex)
    settleNavigation(false)
    return
  }
  const targetTop = Math.min(
    Math.max(0, container.scrollHeight - container.clientHeight),
    Math.max(0, anchorTop - 16),
  )
  const distance = Math.abs(targetTop - container.scrollTop)
  const reduceMotion = typeof window !== 'undefined'
    && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  // Keep one spatially continuous motion model at every distance. The old
  // long-jump branch teleported the thread and then flashed the destination,
  // which read as a rendering glitch rather than navigation. Native smooth
  // scrolling stays interruptible by wheel/touch input and avoids per-frame
  // Vue work; reduced-motion remains an immediate jump.
  const smooth = !reduceMotion
  if (focusTarget) anchor?.focus({ preventScroll: true })
  if (distance <= ARRIVAL_TOLERANCE_PX) {
    if (anchor) showArrivalHighlight(anchor)
    props.releaseEnsuredMessage?.(turn.sourceIndex)
    settleNavigation(false)
    return
  }
  navigationTarget = anchor
  navigationTargetTop = targetTop
  navigationTargetSourceIndex = turn.sourceIndex
  armNavigationEnd(container, smooth)
  container.scrollTo({ top: targetTop, behavior: smooth ? 'smooth' : 'auto' })
}

function showHoverPreview(index: number, target: EventTarget | null) {
  hoveredIndex.value = index
  pointerLensIndex.value ??= index
  positionPreview(target)
}

function closeHoverPreview() {
  cancelFrame(pointerFrame)
  pointerFrame = 0
  hoveredIndex.value = null
  pointerLensIndex.value = null
}

function onListPointerMove(event: PointerEvent) {
  pendingPointerClientY = event.clientY
  if (pointerFrame) return
  pointerFrame = requestFrame(() => {
    pointerFrame = 0
    const firstMarker = markerRefs.value[0]
    const lastMarker = markerRefs.value[turns.value.length - 1]
    if (!firstMarker || !lastMarker || turns.value.length === 0) return

    const firstRect = firstMarker.getBoundingClientRect()
    const firstCenter = firstRect.top + firstRect.height / 2
    let nextLensIndex = 0
    if (turns.value.length > 1) {
      const lastRect = lastMarker.getBoundingClientRect()
      const lastCenter = lastRect.top + lastRect.height / 2
      const pitch = (lastCenter - firstCenter) / (turns.value.length - 1)
      if (Math.abs(pitch) >= 1) {
        nextLensIndex = (pendingPointerClientY - firstCenter) / pitch
      } else {
        nextLensIndex = hoveredIndex.value ?? focusedIndex.value ?? activeIndex.value
      }
    }
    pointerLensIndex.value = Math.min(turns.value.length - 1, Math.max(0, nextLensIndex))
    const nextHoveredIndex = Math.round(pointerLensIndex.value)
    if (nextHoveredIndex !== hoveredIndex.value) {
      hoveredIndex.value = nextHoveredIndex
      positionPreview(markerRefs.value[nextHoveredIndex] || null)
    }
  })
}

function showFocusPreview(index: number, target: EventTarget | null) {
  focusedIndex.value = index
  positionPreview(target)
}

function positionPreview(target: EventTarget | null) {
  if (!(target instanceof HTMLElement) || !rootRef.value) return
  const rootRect = rootRef.value.getBoundingClientRect()
  const targetRect = target.getBoundingClientRect()
  const targetCenter = targetRect.top - rootRect.top + targetRect.height / 2
  const positionForHeight = (height: number) => {
    const maxTop = Math.max(PREVIEW_EDGE_GAP, rootRef.value!.clientHeight - height - PREVIEW_EDGE_GAP)
    return Math.min(maxTop, Math.max(PREVIEW_EDGE_GAP, targetCenter - height / 2))
  }
  previewTop.value = positionForHeight(tooltipRef.value?.offsetHeight || ESTIMATED_PREVIEW_HEIGHT)
  void nextTick(() => {
    previewTop.value = positionForHeight(tooltipRef.value?.offsetHeight || ESTIMATED_PREVIEW_HEIGHT)
  })
}

function onMarkerKeydown(index: number, event: KeyboardEvent) {
  let nextIndex: number | null = null
  if (event.key === 'ArrowDown') nextIndex = Math.min(turns.value.length - 1, index + 1)
  else if (event.key === 'ArrowUp') nextIndex = Math.max(0, index - 1)
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = turns.value.length - 1
  else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    navigateTo(index, true)
    return
  } else if (event.key === 'Escape') {
    hoveredIndex.value = null
    focusedIndex.value = null
    pointerLensIndex.value = null
    return
  }

  if (nextIndex === null) return
  event.preventDefault()
  const marker = markerRefs.value[nextIndex]
  marker?.focus()
  if (marker) showFocusPreview(nextIndex, marker)
}

watch(() => props.scrollContainer, attachContainer)
watch(() => props.sessionKey, () => {
  cancelNavigation()
  clearArrivalHighlight()
  hasLongHistory.value = false
  activeIndex.value = 0
  hoveredIndex.value = null
  focusedIndex.value = null
  pointerLensIndex.value = null
  markerRefs.value = []
  if (observersActive) void nextTick(scheduleMeasure)
})
watch(turns, (nextTurns, previousTurns) => {
  if (nextTurns.length < MIN_TURNS) hasLongHistory.value = false
  const remap = (index: number | null): number | null => {
    if (index === null) return null
    const key = previousTurns[index]?.key
    if (!key) return null
    const nextIndex = nextTurns.findIndex(turn => turn.key === key)
    return nextIndex >= 0 ? nextIndex : null
  }
  hoveredIndex.value = remap(hoveredIndex.value)
  focusedIndex.value = remap(focusedIndex.value)
  activeIndex.value = remap(activeIndex.value) ?? 0
  pointerLensIndex.value = null
  markerRefs.value = []
  void nextTick(() => {
    if (observersActive) {
      observeDirectChildren(props.scrollContainer!)
      scheduleMeasure()
    }
  })
}, { deep: false })

onMounted(() => {
  attachInputEligibility()
  attachContainer(props.scrollContainer, null)
})

onBeforeUnmount(() => {
  cancelNavigation()
  clearArrivalHighlight()
  deactivateThreadObservers()
  detachInputEligibility()
  shellResizeObserver?.disconnect()
  shellResizeObserver = null
})
</script>

<style scoped>
.conversation-minimap {
  position: absolute;
  /* The resizer owns the first 10px of the main pane. Start 4px after that hit
     area so the two pointer targets never overlap at the shared left edge. */
  inset: 0.75rem auto 0.75rem 0.875rem;
  z-index: 18;
  display: flex;
  align-items: center;
  width: 2rem;
  pointer-events: none;
}

.conversation-minimap__list {
  display: flex;
  flex-direction: column;
  width: 2rem;
  height: auto;
  max-height: 100%;
  margin: 0;
  padding: 0.25rem 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
  list-style: none;
  pointer-events: auto;
}

.conversation-minimap__list::-webkit-scrollbar {
  display: none;
}

.conversation-minimap__item {
  display: block;
  flex: 0 0 1rem;
  width: 100%;
  margin: 0;
  padding: 0;
}

.conversation-minimap__marker {
  display: flex;
  align-items: center;
  width: 100%;
  height: 1rem;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  transition: color var(--dur-base) var(--ease-standard);
}

.conversation-minimap__line {
  display: block;
  width: 1.875rem;
  height: 0.1875rem;
  border-radius: var(--radius-full);
  background: currentColor;
  opacity: var(--conversation-minimap-line-opacity, 0.45);
  transform:
    scaleX(var(--conversation-minimap-line-scale-x, 0.2667))
    scaleY(var(--conversation-minimap-line-scale-y, 0.5));
  transform-origin: left center;
  transition:
    transform var(--dur-base) var(--ease-out),
    opacity var(--dur-base) var(--ease-standard),
    box-shadow var(--dur-fast) var(--ease-standard);
  will-change: transform, opacity;
}

.conversation-minimap__marker.is-active {
  color: var(--text-muted);
}

.conversation-minimap__marker:hover,
.conversation-minimap__marker:focus-visible {
  color: var(--text);
}

.conversation-minimap__marker:focus-visible {
  outline: none;
}

.conversation-minimap__marker:focus-visible .conversation-minimap__line {
  box-shadow: 0 0 0 2px var(--bg-surface), 0 0 0 4px var(--accent);
}

.conversation-minimap__preview-positioner {
  position: absolute;
  top: 0;
  left: 2.375rem;
  width: min(20rem, calc(100vw - 5rem));
  pointer-events: none;
  transform: translate3d(0, var(--conversation-minimap-preview-y, 0), 0);
  transition: transform var(--dur-base) var(--ease-out);
  will-change: transform, opacity;
}

.conversation-minimap__preview {
  padding: 0.75rem 0.875rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  color: var(--text);
  box-shadow: var(--shadow-lg);
  transform-origin: left center;
}

.conversation-minimap-shell-enter-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.conversation-minimap-shell-leave-active {
  transition:
    opacity var(--dur-fast) var(--ease-in),
    transform var(--dur-fast) var(--ease-in);
}

.conversation-minimap-shell-enter-from,
.conversation-minimap-shell-leave-to {
  opacity: 0;
  transform: translateX(-0.25rem);
}

.conversation-minimap-preview-enter-active,
.conversation-minimap-preview-leave-active {
  transition: opacity var(--dur-fast) var(--ease-out);
}

.conversation-minimap-preview-enter-active .conversation-minimap__preview,
.conversation-minimap-preview-leave-active .conversation-minimap__preview {
  transition: transform var(--dur-base) var(--ease-out);
}

.conversation-minimap-preview-enter-from,
.conversation-minimap-preview-leave-to {
  opacity: 0;
}

.conversation-minimap-preview-enter-from .conversation-minimap__preview,
.conversation-minimap-preview-leave-to .conversation-minimap__preview {
  transform: translateX(-0.375rem) scale(0.985);
}

.conversation-minimap__preview-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.375rem;
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}

.conversation-minimap__preview p {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
  line-height: 1.5;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.conversation-minimap__attachments {
  display: block;
  margin-top: 0.5rem;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

/* This is the hard safety floor only. JavaScript owns the 1120px enter / 1104px
   exit hysteresis; using the enter value here would defeat that hysteresis. */
@container chat-thread-shell (max-width: 1104px) {
  .conversation-minimap {
    display: none;
  }
}

@media (hover: none) and (pointer: coarse) and (any-hover: none) and (any-pointer: coarse) {
  .conversation-minimap {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .conversation-minimap__marker,
  .conversation-minimap__line,
  .conversation-minimap__preview-positioner,
  .conversation-minimap__preview,
  .conversation-minimap-shell-enter-active,
  .conversation-minimap-shell-leave-active,
  .conversation-minimap-preview-enter-active,
  .conversation-minimap-preview-leave-active {
    transition: none;
  }
}

@media (forced-colors: active) {
  .conversation-minimap__marker {
    color: ButtonText;
  }

  .conversation-minimap__marker.is-active,
  .conversation-minimap__marker:focus-visible {
    color: Highlight;
  }
}
</style>
