<template>
  <Transition name="workbench-presence" :css="motionEnabled && !resizeActive" @after-leave="leavingWidth = null">
  <aside
    v-if="shouldMount"
    v-show="shouldRender"
    id="workbench-panel"
    ref="hostRef"
    class="workbench-host"
    :class="[
      `workbench-host--${layoutMode}`,
      { 'workbench-host--conversation': activeItem?.kind === 'assistant-chat',
        'workbench-host--native': activeItem?.hostKind === 'native-webcontents',
        'workbench-host--direct': !motionEnabled || resizeActive,
        'workbench-host--layout-motion': animateLayout && !resizeActive },
    ]"
    :style="hostStyle"
    role="complementary"
    :data-layout-mode="layoutMode"
    :aria-label="ariaLabel"
    :aria-hidden="modalBlocked || !shouldRender ? 'true' : undefined"
    :inert="modalBlocked || !shouldRender ? true : undefined"
    data-testid="workbench-host"
  >
    <WorkbenchResizer
      ref="resizerRef"
      :enabled="layoutMode === 'split' && !modalBlocked"
      :width="effectiveWidth"
      :min="WORKBENCH_MIN_WIDTH"
      :max="dynamicMaximumWidth"
      :reset-width="defaultWidth"
      :aria-label="resizeLabel"
      :unit-label="pixelsLabel"
      aria-controls="app-main workbench-panel"
      @resize-start="resizeActive = true"
      @preview="previewWidth = $event"
      @commit="commitWidth"
      @reset="resetWidth"
      @cancel="previewWidth = null"
      @resize-end="previewWidth = null; resizeActive = false"
    />

    <header
      class="workbench-host__chrome"
      :class="{ 'workbench-host__chrome--browser': activeItem?.kind === 'browser' }"
    >
      <button
        v-if="layoutMode === 'single-pane'"
        ref="returnButtonRef"
        class="workbench-host__return-button"
        type="button"
        :disabled="modalBlocked"
        :aria-label="returnLabel"
        data-testid="workbench-return-chat"
        @click="returnToChat"
      >
        <Icon name="chevronLeft" :size="17" aria-hidden="true" />
        <span>{{ returnLabel }}</span>
      </button>
      <div
        v-if="showTabStrip"
        ref="tabsRef"
        class="workbench-host__tabs"
        role="tablist"
        :aria-label="openItemsLabel"
      >
        <div
          v-for="(item, index) in store.visibleItems"
          :key="item.id"
          class="workbench-host__tab-wrap"
          :class="{ 'is-active': item.id === activeItem?.id }"
          role="presentation"
          @pointerenter="scheduleHover(item, $event)"
          @pointerleave="scheduleHideHover"
          @focusin="scheduleHover(item, $event)"
          @focusout="onTabFocusOut"
          @contextmenu="openTabMenu(item, $event)"
        >
          <button
            :id="tabId(item.id)"
            class="workbench-host__tab"
            role="tab"
            type="button"
            :aria-selected="item.id === activeItem?.id"
            :aria-controls="panelId(item.id)"
            :aria-describedby="hoveredItem?.id === item.id ? hoverCardId : undefined"
            :aria-busy="tabInfo?.(item).loading || undefined"
            :tabindex="item.id === activeItem?.id || (!activeItem && index === 0) ? 0 : -1"
            @click="activateTab(item.id)"
            @keydown="onTabKeydown($event, index)"
          >
            <Icon v-if="tabInfo?.(item).loading" name="refresh" :size="14" class="workbench-host__tab-spinner" aria-hidden="true" />
            <span class="workbench-host__tab-title">{{ item.title }}</span>
          </button>
          <button
            class="workbench-host__tab-close"
            type="button"
            :aria-label="`${closeItemLabel}: ${item.title}`"
            :tabindex="item.id === activeItem?.id ? 0 : -1"
            @click="closeWorkbenchItem(item.id)"
          >
            <Icon name="x" :size="13" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div v-else class="workbench-host__single-title">
        <slot name="title" :item="activeItem">
          <span class="workbench-host__title">{{ activeItem?.title }}</span>
        </slot>
      </div>

      <div class="workbench-host__actions">
        <slot name="actions" :item="activeItem" />
        <button
          v-if="measuredAvailableWidth >= WORKBENCH_SPLIT_MIN_WIDTH"
          ref="maximizeButtonRef"
          class="workbench-host__icon-button"
          type="button"
          :aria-label="store.maximized ? restoreLabel : maximizeLabel"
          :title="store.maximized ? restoreLabel : maximizeLabel"
          :aria-pressed="store.maximized"
          :disabled="modalBlocked"
          data-testid="workbench-maximize"
          @click="toggleMaximized"
        >
          <Icon :name="store.maximized ? 'collapse' : 'expand'" :size="17" aria-hidden="true" />
        </button>
        <slot name="collapse-control">
        <button
          v-if="layoutMode === 'split' && !allowEmpty"
          ref="closeButtonRef"
          class="workbench-host__icon-button"
          type="button"
          :aria-label="collapseLabel"
          :title="collapseLabel"
          @click="collapseWorkbench"
        >
          <Icon name="panel-right-close" :size="17" aria-hidden="true" />
        </button>
        </slot>
      </div>
    </header>
    <div v-if="showToolbar" class="workbench-host__toolbar">
      <slot name="toolbar" :item="activeItem" />
    </div>

    <section
      ref="surfaceRef"
      class="workbench-host__surface"
      :class="{
        'workbench-host__surface--native':
          activeItem?.hostKind === 'native-webcontents',
      }"
      data-testid="workbench-surface"
    >
      <slot v-if="!activeItem && allowEmpty" name="empty" />
      <template v-for="item in mountedItems" :key="item.id">
        <div
          v-if="
            item.retention === 'keep-alive'
              || (item.id === activeItem?.id && runtimeAvailable)
          "
          v-show="item.id === activeItem?.id"
          class="workbench-host__panel-layer"
          :id="panelId(item.id)"
          role="tabpanel"
          :aria-labelledby="showTabStrip ? tabId(item.id) : undefined"
          :aria-label="showTabStrip ? undefined : item.title"
          :data-workbench-item-id="item.id"
          :aria-hidden="item.id === activeItem?.id ? undefined : 'true'"
          :inert="item.id === activeItem?.id ? undefined : true"
        >
          <slot
            v-if="item.hostKind === 'dom'"
            name="panel"
            :item="item"
            :active="item.id === activeItem?.id"
            :layout-mode="layoutMode"
          >
            <div class="workbench-host__empty">{{ emptyLabel }}</div>
          </slot>
          <slot
            v-else
            name="native-surface"
            :item="item"
            :active="item.id === activeItem?.id"
            :layout-mode="layoutMode"
          >
            <div
              class="workbench-host__native-placeholder"
              data-workbench-native-surface-slot
              aria-hidden="true"
            />
          </slot>
        </div>
      </template>
      <img v-if="nativeSnapshotUrl && activeItem?.hostKind === 'native-webcontents'"
        class="workbench-host__native-backdrop" :src="nativeSnapshotUrl" :style="nativeBackdropStyle"
        alt="" aria-hidden="true" draggable="false" />
    </section>
  </aside>
  </Transition>
  <Teleport to="body">
    <div v-if="hoveredItem && shouldRender && !modalBlocked" :id="hoverCardId" ref="hoverCardRef"
      class="workbench-tab-preview" role="tooltip" :style="hoverStyle"
      @pointerenter="cancelHideHover" @pointerleave="scheduleHideHover">
      <strong>{{ hoveredItem.title }}</strong>
      <span v-if="tabInfo?.(hoveredItem).subtitle">{{ tabInfo?.(hoveredItem).subtitle }}</span>
      <img v-if="hoveredItem.id !== activeItem?.id && tabInfo?.(hoveredItem).thumbnailUrl"
        :src="tabInfo?.(hoveredItem).thumbnailUrl" alt="" draggable="false" />
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
  useId,
} from 'vue'
import Icon from '@/components/Icon.vue'
import { useDialogLayer } from '@/composables/useDialogA11y'
import {
  WORKBENCH_MIN_WIDTH,
  WORKBENCH_SPLIT_MIN_WIDTH,
  defaultWorkbenchWidthPreference,
  workbenchDynamicMax,
  workbenchEffectiveWidth,
  workbenchLayoutMode,
  type WorkbenchLayoutMode,
} from '@/workbench/layout'
import { useWorkbenchStore } from '@/workbench/store'
import type { NativeSurfaceRect, WorkbenchItem } from '@/workbench/types'
import WorkbenchResizer from './WorkbenchResizer.vue'

type WorkbenchResizerHandle = { cancel: () => boolean }

const props = withDefaults(defineProps<{
  enabled?: boolean
  allowEmpty?: boolean
  routeActive?: boolean
  modalBlocked?: boolean
  nativeBlocked?: boolean
  nativeSnapshotUrl?: string
  showToolbar?: boolean
  tabInfo?: (item: WorkbenchItem) => { subtitle?: string; thumbnailUrl?: string; loading?: boolean }
  availableWidth?: number
  layoutMode?: WorkbenchLayoutMode
  motionEnabled?: boolean
  animateLayout?: boolean
  returnLabel?: string
  ariaLabel?: string
  emptyLabel?: string
  openItemsLabel?: string
  collapseLabel?: string
  maximizeLabel?: string
  restoreLabel?: string
  closeItemLabel?: string
  resizeLabel?: string
  pixelsLabel?: string
  beforeCloseItem?: (item: WorkbenchItem) => boolean | Promise<boolean>
}>(), {
  enabled: true,
  allowEmpty: false,
  routeActive: true,
  modalBlocked: false,
  availableWidth: undefined,
  layoutMode: undefined,
  motionEnabled: false,
  animateLayout: false,
  returnLabel: 'Back to chat',
  ariaLabel: 'Workbench',
  emptyLabel: 'No preview is available for this item.',
  openItemsLabel: 'Open workbench items',
  collapseLabel: 'Collapse workbench',
  maximizeLabel: 'Maximize workbench',
  restoreLabel: 'Restore workbench size',
  closeItemLabel: 'Close tab',
  resizeLabel: 'Resize workbench',
  pixelsLabel: 'pixels',
})

const emit = defineEmits<{
  'return-chat': []
  collapsed: []
  emptied: []
  'focus-return': []
  'layout-change': [mode: WorkbenchLayoutMode]
  'surface-rect': [rect: NativeSurfaceRect]
  'tab-menu': [item: WorkbenchItem, event: MouseEvent | KeyboardEvent]
}>()

const store = useWorkbenchStore()
const hostRef = ref<HTMLElement | null>(null)
const tabsRef = ref<HTMLElement | null>(null)
const surfaceRef = ref<HTMLElement | null>(null)
const resizerRef = ref<WorkbenchResizerHandle | null>(null)
const returnButtonRef = ref<HTMLButtonElement | null>(null)
const closeButtonRef = ref<HTMLButtonElement | null>(null)
const maximizeButtonRef = ref<HTMLButtonElement | null>(null)
const viewportWidth = ref(typeof window === 'undefined' ? 0 : window.innerWidth)
const containerWidth = ref(0)
const previewWidth = ref<number | null>(null)
const resizeActive = ref(false)
const leavingWidth = ref<number | null>(null)
let surfaceObserver: ResizeObserver | null = null
let tabsObserver: ResizeObserver | null = null
let containerObserver: ResizeObserver | null = null
let surfaceMutationObserver: MutationObserver | null = null
let rectFrame = 0
let lastNativeItemId: string | null = null
const nativeSurfaceSlotSelector = '[data-workbench-native-surface-slot]'
const nativeBackdropStyle = ref<Record<string, string>>({ display: 'none' })
const showTabStrip = computed(() => store.visibleItems.length > 0)
const activeItem = computed(() => store.visibleItems.find(
  item => item.id === store.activeItemId,
) ?? null)
const hoverCardId = useId()
const hoveredId = ref<string | null>(null)
const hoveredItem = computed(() => store.visibleItems.find(item => item.id === hoveredId.value) ?? null)
const hoverCardRef = ref<HTMLElement | null>(null)
const hoverPoint = ref({ left: 8, top: 8 })
const hoverStyle = computed(() => ({ left: `${hoverPoint.value.left}px`, top: `${hoverPoint.value.top}px` }))
const hoverOpen = computed(() => Boolean(hoveredItem.value) && shouldRender.value && !props.modalBlocked)
let hoverTimer: ReturnType<typeof setTimeout> | undefined
let hideHoverTimer: ReturnType<typeof setTimeout> | undefined
function cancelHideHover() { clearTimeout(hideHoverTimer) }
function hideHover() {
  clearTimeout(hoverTimer)
  clearTimeout(hideHoverTimer)
  hoveredId.value = null
}
function scheduleHideHover() {
  clearTimeout(hoverTimer)
  cancelHideHover()
  hideHoverTimer = setTimeout(hideHover, 130)
}
function onTabFocusOut(event: FocusEvent) {
  if (event.currentTarget instanceof Element && event.relatedTarget instanceof Node
    && event.currentTarget.contains(event.relatedTarget)) return
  scheduleHideHover()
}
function scheduleHover(item: WorkbenchItem, event: Event) {
  if (event instanceof PointerEvent && event.pointerType === 'touch') return
  cancelHideHover()
  clearTimeout(hoverTimer)
  const element = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  hoverTimer = setTimeout(async () => {
    if (!element?.isConnected || props.modalBlocked) return
    hoveredId.value = item.id
    const rect = element.getBoundingClientRect()
    hoverPoint.value = { left: Math.max(8, Math.min(rect.left, window.innerWidth - 320 - 8)), top: rect.bottom + 8 }
    await nextTick()
    const bounds = hoverCardRef.value?.getBoundingClientRect()
    if (bounds) hoverPoint.value = {
      left: Math.max(8, Math.min(rect.left, window.innerWidth - bounds.width - 8)),
      top: Math.max(8, Math.min(rect.bottom + 8, window.innerHeight - bounds.height - 8)),
    }
  }, 400)
}
function activateTab(id: string) { hideHover(); store.activateItem(id) }
function openTabMenu(item: WorkbenchItem, event: MouseEvent | KeyboardEvent) {
  event.preventDefault()
  event.stopPropagation()
  hideHover()
  emit('tab-menu', item, event)
}
function onHoverEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && hoverOpen.value && hoverTopmost.value) {
    event.preventDefault()
    event.stopPropagation()
    hideHover()
  }
}
const mountedItems = computed(() => {
  const visibleIds = new Set(store.visibleItems.map(item => item.id))
  return store.items.filter(item => visibleIds.has(item.id)
    || ['browser', 'assistant-chat'].includes(item.kind) && item.retention === 'keep-alive')
})

const measuredAvailableWidth = computed(() => {
  const supplied = props.availableWidth
  if (typeof supplied === 'number' && Number.isFinite(supplied)) return supplied
  return containerWidth.value > 0 ? containerWidth.value : viewportWidth.value
})
const layoutMode = computed(() => props.layoutMode ?? workbenchLayoutMode({
  availableWidth: measuredAvailableWidth.value,
  maximized: store.maximized,
}))
const dynamicMaximumWidth = computed(() =>
  workbenchDynamicMax(measuredAvailableWidth.value))
const defaultWidth = computed(() => workbenchEffectiveWidth(
  defaultWorkbenchWidthPreference(),
  layoutMode.value,
  measuredAvailableWidth.value,
))
const effectiveWidth = computed(() => previewWidth.value ?? workbenchEffectiveWidth(
  store.widthPreference,
  layoutMode.value,
  measuredAvailableWidth.value,
))
const hostStyle = computed(() => ({
  '--workbench-width': `${leavingWidth.value ?? effectiveWidth.value}px`,
}))
// Automatic space saving must not run suspend/dispose lifecycle hooks. Native
// surface visibility is published separately from the retained runtime.
const runtimeAvailable = computed(() =>
  props.enabled && props.routeActive && store.expanded
  && (activeItem.value !== null || props.allowEmpty))
const shouldRender = computed(() => runtimeAvailable.value && layoutMode.value !== 'main-only')
const shouldMount = computed(() =>
  props.enabled && (mountedItems.value.length > 0 || props.allowEmpty && store.expanded))
const hoverTopmost = useDialogLayer(hoverOpen)

function commitWidth(width: number) {
  previewWidth.value = null
  store.setWidth(width)
}

function resetWidth() {
  previewWidth.value = null
  store.resetWidth()
}

function collapseWorkbench() {
  store.setExpanded(false)
  emit('collapsed')
}

function returnToChat() {
  if (!store.maximized && measuredAvailableWidth.value < WORKBENCH_SPLIT_MIN_WIDTH) {
    emit('return-chat')
    emit('focus-return')
    return
  }
  collapseWorkbench()
}

function toggleMaximized() {
  if (props.modalBlocked) return
  store.toggleMaximized()
  void nextTick(() => maximizeButtonRef.value?.focus({ preventScroll: true }))
}

async function closeWorkbenchItem(id: string) {
  hideHover()
  const item = store.items.find(candidate => candidate.id === id)
  if (!item || props.beforeCloseItem && !await props.beforeCloseItem(item)) return
  if (!store.closeItem(id)) return
  if (!store.activeItem) {
    if (!props.allowEmpty) emit('emptied')
    return
  }
  void nextTick(() => {
    const activeTab = hostRef.value?.querySelector<HTMLElement>(
      '[role="tab"][aria-selected="true"]',
    )
    const target = activeTab || closeButtonRef.value
    if (target) target.focus({ preventScroll: true })
    else emit('focus-return')
  })
}

async function closeOtherItems(id: string) {
  for (const item of [...store.visibleItems]) {
    if (item.id !== id) await closeWorkbenchItem(item.id)
  }
}

defineExpose({ closeItem: closeWorkbenchItem, closeOtherItems, hideHover })

function onTabKeydown(event: KeyboardEvent, currentIndex: number) {
  if (event.key === 'ContextMenu' || event.shiftKey && event.key === 'F10') {
    const item = store.visibleItems[currentIndex]
    if (item) openTabMenu(item, event)
    return
  }
  if (event.key === 'Escape') { hideHover(); return }
  const count = store.visibleItems.length
  if (count < 2) return
  let nextIndex: number | null = null
  if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + count) % count
  else if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % count
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = count - 1
  if (nextIndex === null) return
  event.preventDefault()
  const item = store.visibleItems[nextIndex]
  if (!item) return
  store.activateItem(item.id)
  void nextTick(() => {
    const tabs = hostRef.value?.querySelectorAll<HTMLElement>('[role="tab"]')
    tabs?.[nextIndex]?.focus()
  })
}

function tabId(itemId: string): string {
  return `workbench-tab-${itemId}`
}

function panelId(itemId: string): string {
  return `workbench-panel-${itemId}`
}

function updateViewportWidth() {
  viewportWidth.value = window.innerWidth
  measureContainer()
  scheduleSurfaceRect()
}

function measureContainer() {
  const parent = hostRef.value?.parentElement
  if (!parent) return
  const rect = parent.getBoundingClientRect()
  containerWidth.value = Math.max(0, rect.width || parent.clientWidth)

}

function revealActiveTab() {
  const strip = tabsRef.value
  const active = strip?.querySelector<HTMLElement>('.workbench-host__tab-wrap.is-active')
  if (!strip || !active || !shouldRender.value) return
  const viewport = strip.getBoundingClientRect()
  const tab = active.getBoundingClientRect()
  if (viewport.width <= 0) return
  // Scroll this strip only: scrollIntoView can also move the transcript/page.
  if (tab.right > viewport.right) strip.scrollLeft += tab.right - viewport.right
  else if (tab.left < viewport.left) strip.scrollLeft += tab.left - viewport.left
}

watch([tabsRef, () => activeItem.value?.id, shouldRender], () => {
  tabsObserver?.disconnect()
  if (tabsRef.value) tabsObserver?.observe(tabsRef.value)
  void nextTick(revealActiveTab)
}, { flush: 'post' })

function reconnectSurfaceObserver() {
  surfaceObserver?.disconnect()
  if (surfaceObserver) {
    if (hostRef.value) surfaceObserver.observe(hostRef.value)
    if (surfaceRef.value) surfaceObserver.observe(surfaceRef.value)
    surfaceRef.value
      ?.querySelectorAll<HTMLElement>(nativeSurfaceSlotSelector)
      .forEach(element => surfaceObserver?.observe(element))
  }
}

function reconnectObservers() {
  reconnectSurfaceObserver()
  containerObserver?.disconnect()
  const parent = hostRef.value?.parentElement
  if (containerObserver && parent) containerObserver.observe(parent)
  surfaceMutationObserver?.disconnect()
  if (surfaceMutationObserver && surfaceRef.value) {
    surfaceMutationObserver.observe(surfaceRef.value, {
      childList: true,
      subtree: true,
    })
  }
  measureContainer()
}

function containsNativeSurfaceSlot(nodes: NodeList): boolean {
  return Array.from(nodes).some((node) => {
    if (!(node instanceof Element)) return false
    return node.matches(nativeSurfaceSlotSelector)
      || node.querySelector(nativeSurfaceSlotSelector) !== null
  })
}

function onSurfaceMutation(records: MutationRecord[]) {
  // Native panels can add or remove their surface slot after async resource loading
  // without changing the Workbench border box, so ResizeObserver alone cannot detect it.
  const nativeSlotChanged = records.some(record =>
    containsNativeSurfaceSlot(record.addedNodes)
    || containsNativeSurfaceSlot(record.removedNodes))
  if (!nativeSlotChanged) return
  reconnectSurfaceObserver()
  scheduleSurfaceRect()
}

function scheduleSurfaceRect() {
  if (rectFrame) cancelAnimationFrame(rectFrame)
  rectFrame = requestAnimationFrame(() => {
    rectFrame = 0
    emitSurfaceRect()
  })
}

function hiddenRect(itemId: string): NativeSurfaceRect {
  return { itemId, x: 0, y: 0, width: 0, height: 0, visible: false }
}

function emitSurfaceRect() {
  const item = activeItem.value
  const activeNativeId = item?.hostKind === 'native-webcontents' ? item.id : null
  if (lastNativeItemId && lastNativeItemId !== activeNativeId) {
    emit('surface-rect', hiddenRect(lastNativeItemId))
  }
  lastNativeItemId = activeNativeId
  if (!activeNativeId) return
  const activeLayer = [...(surfaceRef.value?.querySelectorAll<HTMLElement>(
    '[data-workbench-item-id]',
  ) || [])].find(layer => layer.dataset.workbenchItemId === activeNativeId)
  const element = activeLayer?.querySelector<HTMLElement>(
    '[data-workbench-native-surface-slot]',
  )
  if (!element || !shouldRender.value) {
    nativeBackdropStyle.value = { display: 'none' }
    emit('surface-rect', hiddenRect(activeNativeId))
    return
  }
  const rect = element.getBoundingClientRect()
  const parentRect = surfaceRef.value?.getBoundingClientRect()
  nativeBackdropStyle.value = parentRect ? {
    left: `${rect.left - parentRect.left}px`, top: `${rect.top - parentRect.top}px`,
    width: `${rect.width}px`, height: `${rect.height}px`,
  } : { display: 'none' }
  emit('surface-rect', {
    itemId: activeNativeId,
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.max(0, Math.round(rect.width)),
    height: Math.max(0, Math.round(rect.height)),
    visible: !props.modalBlocked && !props.nativeBlocked && rect.width > 0 && rect.height > 0,
  })
}

function focusReturnUnlessOwned() {
  // App may have just moved the focused pane toggle into this chrome. Preserve
  // that explicit focus handoff instead of replacing it with the Back button.
  if (document.activeElement instanceof HTMLElement
    && hostRef.value?.contains(document.activeElement)) return
  returnButtonRef.value?.focus({ preventScroll: true })
}

watch(layoutMode, mode => {
  const focused = typeof document !== 'undefined' ? document.activeElement : null
  const focusWasInMain = focused instanceof Element && Boolean(focused.closest('.app-chat-pane'))
  const focusWasOnReturn = focused === returnButtonRef.value
  const focusWasInWorkbench = focused instanceof Element && Boolean(hostRef.value?.contains(focused))
  if (mode === 'main-only' && focusWasInWorkbench) {
    void nextTick(() => emit('focus-return'))
  } else if (mode === 'single-pane' && focusWasInMain) {
    void nextTick(focusReturnUnlessOwned)
  } else if (mode === 'split' && focusWasOnReturn) {
    void nextTick(() => hostRef.value?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')?.focus({ preventScroll: true }))
  }
  resizerRef.value?.cancel()
  previewWidth.value = null
  emit('layout-change', mode)
  void nextTick(scheduleSurfaceRect)
}, { immediate: true })

watch(runtimeAvailable, available => {
  store.setHostAvailable(available)
  void nextTick(scheduleSurfaceRect)
}, { immediate: true })

watch(() => props.modalBlocked, blocked => { if (blocked) hideHover(); void nextTick(scheduleSurfaceRect) })
watch(() => props.nativeBlocked, () => void nextTick(scheduleSurfaceRect))
watch(() => store.activeSessionId, hideHover)
watch(shouldRender, (visible, previous) => {
  if (visible) leavingWidth.value = null
  else {
    hideHover()
    // setExpanded(false) also clears maximized in the same transaction. Read
    // the last painted track before Vue patches it, not its new split target.
    if (previous && props.motionEnabled) {
      const renderedWidth = hostRef.value?.getBoundingClientRect().width
      const previousTarget = Number.parseFloat(hostRef.value?.style.getPropertyValue('--workbench-width') || '')
      leavingWidth.value = renderedWidth || (Number.isFinite(previousTarget) ? previousTarget : null)
    }
  }
})
watch(() => activeItem.value?.id, () => void nextTick(scheduleSurfaceRect))
watch(() => activeItem.value?.hostKind, () => void nextTick(scheduleSurfaceRect))
watch(effectiveWidth, scheduleSurfaceRect)
watch([hostRef, surfaceRef], () => {
  reconnectObservers()
  scheduleSurfaceRect()
}, { flush: 'post' })

watch(shouldRender, (visible, previous) => {
  if (visible && !previous && layoutMode.value === 'single-pane') {
    void nextTick(focusReturnUnlessOwned)
  }
})

onMounted(() => {
  document.addEventListener('keydown', onHoverEscape, true)
  window.addEventListener('resize', updateViewportWidth)
  window.addEventListener('scroll', measureContainer, true)
  if (typeof ResizeObserver !== 'undefined') {
    surfaceObserver = new ResizeObserver(scheduleSurfaceRect)
    containerObserver = new ResizeObserver(() => {
      measureContainer()
      scheduleSurfaceRect()
    })
    tabsObserver = new ResizeObserver(revealActiveTab)
    if (tabsRef.value) tabsObserver.observe(tabsRef.value)
  }
  if (typeof MutationObserver !== 'undefined') {
    surfaceMutationObserver = new MutationObserver(onSurfaceMutation)
  }
  reconnectObservers()
  scheduleSurfaceRect()
})

onBeforeUnmount(() => {
  hideHover()
  document.removeEventListener('keydown', onHoverEscape, true)
  window.removeEventListener('resize', updateViewportWidth)
  window.removeEventListener('scroll', measureContainer, true)
  surfaceObserver?.disconnect()
  tabsObserver?.disconnect()
  containerObserver?.disconnect()
  surfaceMutationObserver?.disconnect()
  if (rectFrame) cancelAnimationFrame(rectFrame)
  if (lastNativeItemId) emit('surface-rect', hiddenRect(lastNativeItemId))
  store.setHostAvailable(false)
})
</script>

<style scoped>
.workbench-host {
  position: relative;
  display: flex;
  flex: 0 0 var(--workbench-width);
  flex-direction: column;
  container-type: inline-size;
  width: var(--workbench-width);
  min-width: 0;
  height: 100%;
  overflow: hidden;
  border-inline-start: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
}

/* A conversation uses the same canvas as the routed chat, including its
   transparent tab chrome. Other tools retain their own workbench ground. */
.workbench-host--conversation {
  background: var(--bg-surface);
}

/* Keep the split resizer's chat-side hit area outside the clipped workbench.
   The native browser surface starts at the workbench edge and can consume
   pointer events on its side of the handle, so the handle must remain
   reachable from the adjacent chat pane. */
.workbench-host--split {
  overflow: visible;
}

.workbench-host--single-pane {
  border-inline-start: 0;
}

/* Opening and closing share one flow track with the chat. Keep DOM content
   at its destination width while revealing it, so icons/text never scale or
   wrap through tiny intermediate widths. Native content keeps real slot bounds. */
.workbench-host--layout-motion,
.workbench-presence-enter-active,
.workbench-presence-leave-active {
  flex-grow: 0;
  flex-basis: var(--workbench-width);
  width: var(--workbench-width);
  overflow: hidden;
  transition: width var(--dur-base) var(--ease-standard),
    flex-basis var(--dur-base) var(--ease-standard), opacity var(--dur-base) var(--ease-standard);
}
.workbench-host--layout-motion > .workbench-host__chrome,
.workbench-host--layout-motion > .workbench-host__toolbar,
.workbench-host--layout-motion:not(.workbench-host--native) > .workbench-host__surface,
.workbench-presence-enter-active > .workbench-host__chrome,
.workbench-presence-leave-active > .workbench-host__chrome,
.workbench-presence-enter-active > .workbench-host__toolbar,
.workbench-presence-leave-active > .workbench-host__toolbar,
.workbench-presence-enter-active:not(.workbench-host--native) > .workbench-host__surface,
.workbench-presence-leave-active:not(.workbench-host--native) > .workbench-host__surface {
  width: var(--workbench-width);
  flex-shrink: 0;
}
/* Content can keep its settled width during the reveal, but the pane's chrome
   stays anchored to its outer edge so its collapse control never slides offscreen. */
.workbench-host--layout-motion > .workbench-host__chrome,
.workbench-presence-enter-active > .workbench-host__chrome,
.workbench-presence-leave-active > .workbench-host__chrome {
  align-self: flex-end;
}
.workbench-presence-enter-from,
.workbench-presence-leave-to { flex-basis: 0; width: 0; opacity: 0; }
.workbench-presence-leave-active { pointer-events: none; }
.workbench-host--direct { transition: none; }

.workbench-host__return-button {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
  max-width: 45%;
  min-height: 30px;
  padding: 4px 6px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  cursor: pointer;
}
.workbench-host__return-button span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workbench-host__return-button:hover { background: var(--bg-hover); }
.workbench-host__return-button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (prefers-reduced-motion: reduce) {
  .workbench-host--layout-motion,
  .workbench-presence-enter-active,
  .workbench-presence-leave-active { transition: none; }
}

.workbench-host__chrome {
  display: flex;
  box-sizing: border-box;
  min-height: var(--workspace-header-height, 49px);
  align-items: center;
  gap: var(--sp-2);
  padding: 0 var(--sp-3);
  border-block-end: 1px solid var(--border);
}

.workbench-host__toolbar { display: flex; flex: 0 0 auto; flex-wrap: wrap; align-items: center;
  justify-content: flex-end; gap: var(--sp-1); min-height: 42px; padding: 4px var(--sp-3);
  border-block-end: 1px solid var(--border); box-sizing: border-box; }

.workbench-host__tabs {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 2px;
  overflow-x: auto;
  scrollbar-width: none;
}

.workbench-host__tabs::-webkit-scrollbar {
  display: none;
}

.workbench-host__tab-wrap {
  display: flex;
  min-width: 0;
  flex: 0 0 auto;
  width: max-content;
  max-width: min(200px, 100%);
  align-items: center;
  color: var(--text-dim);
}

.workbench-host__tab-wrap.is-active {
  color: var(--text);
}

.workbench-host__tab {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex: 1 1 auto;
  padding: var(--sp-2);
  border: 0;
  border-block-end: 2px solid transparent;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: start;
}

.workbench-host__tab-wrap.is-active .workbench-host__tab {
  border-block-end-color: var(--text-dim);
}

.workbench-host__tab:focus-visible,
.workbench-host__tab-close:focus-visible,
.workbench-host__icon-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.workbench-host__tab-title {
  min-width: 0;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workbench-host__tab-close,
.workbench-host__icon-button {
  display: inline-flex;
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.workbench-host__tab-close:hover,
.workbench-host__icon-button:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.workbench-host__single-title {
  min-width: 0;
  flex: 1;
}

.workbench-host__title {
  display: block;
  overflow: hidden;
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workbench-host__actions {
  display: flex;
  min-width: 0;
  max-width: 100%;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-1);
}

/* Keep tabs and actions on one aligned row. Rare actions live in the menu. */
.workbench-host__tab-close {
  opacity: 0;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: var(--radius-xs);
}
.workbench-host__tab-wrap:hover .workbench-host__tab-close,
.workbench-host__tab-wrap:focus-within .workbench-host__tab-close { opacity: 1; }
.workbench-host__tab-spinner { flex: 0 0 auto; animation: workbench-tab-spin 1s linear infinite; }
@keyframes workbench-tab-spin { to { transform: rotate(360deg); } }
.workbench-tab-preview { position: fixed; z-index: 10001; display: flex; flex-direction: column; gap: 7px;
  box-sizing: border-box; width: max-content; max-width: min(320px, calc(100vw - 16px)); padding: 12px;
  border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface);
  color: var(--text); box-shadow: var(--shadow-lg); font-size: var(--fs-sm); overflow-wrap: anywhere; }
.workbench-tab-preview strong { font-weight: 600; }
.workbench-tab-preview span { color: var(--text-dim); font-size: var(--fs-xs); }
.workbench-tab-preview img { display: block; width: 296px; max-width: 100%; max-height: 180px; object-fit: contain;
  background: var(--bg); border-radius: var(--radius-sm); }
@media (hover: none) { .workbench-host__tab-close { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .workbench-host__tab-spinner { animation: none; } }

.workbench-host__surface {
  position: relative;
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1;
  overflow: hidden;
}

.workbench-host__surface--native {
  overflow: hidden;
}

.workbench-host__panel-layer {
  position: absolute;
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  inset: 0;
  overflow: auto;
}

.workbench-host__empty {
  display: grid;
  min-height: 100%;
  place-items: center;
  padding: var(--sp-5);
  color: var(--text-dim);
  font-size: var(--fs-sm);
  text-align: center;
}

.workbench-host__native-backdrop { position: absolute; z-index: 1; display: block; object-fit: fill;
  pointer-events: none; background: var(--bg); }

.workbench-host__native-placeholder {
  width: 100%;
  height: 100%;
  background: var(--bg);
}

@media (prefers-reduced-motion: reduce) {
  .workbench-host {
    scroll-behavior: auto;
  }
}

@media (forced-colors: active) {
  .workbench-host {
    border-inline-start-color: CanvasText;
  }

  .workbench-host__chrome {
    border-block-end-color: CanvasText;
  }

  .workbench-host__tab-wrap.is-active .workbench-host__tab {
    border-block-end-color: Highlight;
  }
}
</style>
