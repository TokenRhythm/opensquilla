<template>
  <aside
    v-if="shouldMount"
    v-show="shouldRender"
    id="workbench-panel"
    ref="hostRef"
    class="workbench-host"
    tabindex="-1"
    :class="`workbench-host--${layoutMode}`"
    :style="hostStyle"
    :role="layoutMode === 'mobile-dialog' ? 'dialog' : 'complementary'"
    :aria-modal="layoutMode === 'mobile-dialog' ? 'true' : undefined"
    :aria-label="ariaLabel"
    :aria-hidden="modalBlocked ? 'true' : undefined"
    :inert="modalBlocked ? true : undefined"
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
      @preview="previewWidth = $event"
      @commit="commitWidth"
      @reset="resetWidth"
      @cancel="previewWidth = null"
      @resize-end="previewWidth = null"
    />

    <header class="workbench-host__chrome">
      <div
        v-if="store.hasMultipleItems"
        class="workbench-host__tabs"
        role="tablist"
        :aria-label="openItemsLabel"
      >
        <div
          v-for="(item, index) in store.items"
          :key="item.id"
          class="workbench-host__tab-wrap"
          :class="{ 'is-active': item.id === store.activeItemId }"
          role="presentation"
        >
          <button
            :id="tabId(item.id)"
            class="workbench-host__tab"
            role="tab"
            type="button"
            :aria-selected="item.id === store.activeItemId"
            :aria-controls="panelId(item.id)"
            :tabindex="item.id === store.activeItemId ? 0 : -1"
            @click="store.activateItem(item.id)"
            @keydown="onTabKeydown($event, index)"
          >
            <span class="workbench-host__tab-title">{{ item.title }}</span>
          </button>
          <button
            class="workbench-host__tab-close"
            type="button"
            :aria-label="`${closeItemLabel}: ${item.title}`"
            :tabindex="item.id === store.activeItemId ? 0 : -1"
            @click="closeWorkbenchItem(item.id)"
          >
            <Icon name="x" :size="13" aria-hidden="true" />
          </button>
        </div>
        <button
          class="workbench-host__tabs-overflow"
          type="button"
          :aria-label="tabOverflowLabel"
          :aria-expanded="tabMenuOpen ? 'true' : 'false'"
          data-testid="workbench-tab-overflow"
          @click="toggleTabMenu"
        >
          <Icon name="chevronDown" :size="13" aria-hidden="true" />
        </button>
      </div>

      <div v-else class="workbench-host__single-title">
        <slot name="title" :item="store.activeItem">
          <span class="workbench-host__title">{{ store.activeItem?.title }}</span>
        </slot>
        <button
          class="workbench-host__single-close"
          type="button"
          :aria-label="`${closeItemLabel}: ${store.activeItem?.title ?? ''}`"
          data-testid="workbench-single-close"
          @click="store.activeItem && closeWorkbenchItem(store.activeItem.id)"
        >
          <Icon name="x" :size="13" aria-hidden="true" />
        </button>
      </div>

      <div class="workbench-host__actions">
        <slot name="actions" :item="store.activeItem" />
      </div>

      <!-- Listed outside the scrollable strip (it would be clipped there) and
           outside the tabs/single-title v-if/v-else pair (an element between
           the two would break the v-else pairing and render the single-title
           identity unconditionally). -->
      <div
        v-if="tabMenuOpen"
        ref="tabMenuRef"
        class="workbench-host__tab-menu"
        role="menu"
        data-workbench-tab-menu
        :aria-label="tabOverflowLabel"
      >
        <button
          v-for="item in store.items"
          :key="item.id"
          class="workbench-host__tab-menu-item"
          :class="{ 'is-active': item.id === store.activeItemId }"
          role="menuitem"
          type="button"
          @click="selectTabFromMenu(item.id)"
        >
          {{ item.title }}
        </button>
        <div class="workbench-host__tab-menu-divider" role="separator" />
        <button
          class="workbench-host__tab-menu-item"
          role="menuitem"
          type="button"
          @click="collapseFromMenu"
        >
          {{ collapseLabel }}
        </button>
      </div>
    </header>

    <section
      ref="surfaceRef"
      class="workbench-host__surface"
      :class="{
        'workbench-host__surface--native':
          store.activeItem?.hostKind === 'native-webcontents',
      }"
      data-testid="workbench-surface"
    >
      <template v-for="item in store.items" :key="item.id">
        <div
          v-if="
            item.retention === 'keep-alive'
              || (item.id === store.activeItemId && runtimeAvailable)
          "
          v-show="item.id === store.activeItemId"
          class="workbench-host__panel-layer"
          :id="panelId(item.id)"
          role="tabpanel"
          :aria-labelledby="store.hasMultipleItems ? tabId(item.id) : undefined"
          :aria-label="store.hasMultipleItems ? undefined : item.title"
          :data-workbench-item-id="item.id"
          :aria-hidden="item.id === store.activeItemId ? undefined : 'true'"
          :inert="item.id === store.activeItemId ? undefined : true"
        >
          <slot
            v-if="item.hostKind === 'dom'"
            name="panel"
            :item="item"
            :active="item.id === store.activeItemId"
            :layout-mode="layoutMode"
          >
            <div class="workbench-host__empty">{{ emptyLabel }}</div>
          </slot>
          <slot
            v-else
            name="native-surface"
            :item="item"
            :active="item.id === store.activeItemId"
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
    </section>
  </aside>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import {
  WORKBENCH_MIN_WIDTH,
  defaultWorkbenchWidthPreference,
  workbenchDynamicMax,
  workbenchEffectiveWidth,
  workbenchLayoutMode,
} from '@/workbench/layout'
import { useWorkbenchStore } from '@/workbench/store'
import type { NativeSurfaceRect, WorkbenchItem } from '@/workbench/types'
import WorkbenchResizer from './WorkbenchResizer.vue'

type WorkbenchResizerHandle = { cancel: () => boolean }

const props = withDefaults(defineProps<{
  enabled?: boolean
  routeActive?: boolean
  modalBlocked?: boolean
  availableWidth?: number
  coarseOnly?: boolean
  ariaLabel?: string
  emptyLabel?: string
  openItemsLabel?: string
  collapseLabel?: string
  closeItemLabel?: string
  resizeLabel?: string
  pixelsLabel?: string
  tabOverflowLabel?: string
  beforeCloseItem?: (item: WorkbenchItem) => boolean | Promise<boolean>
}>(), {
  enabled: true,
  routeActive: true,
  modalBlocked: false,
  availableWidth: undefined,
  coarseOnly: undefined,
  ariaLabel: 'Workbench',
  emptyLabel: 'No preview is available for this item.',
  openItemsLabel: 'Open workbench items',
  collapseLabel: 'Collapse workbench',
  closeItemLabel: 'Close tab',
  resizeLabel: 'Resize workbench',
  pixelsLabel: 'pixels',
  tabOverflowLabel: 'All tabs',
})

const emit = defineEmits<{
  collapsed: []
  emptied: []
  'layout-change': [mode: 'split' | 'overlay' | 'mobile-dialog']
  'surface-rect': [rect: NativeSurfaceRect]
}>()

const store = useWorkbenchStore()
const hostRef = ref<HTMLElement | null>(null)
const surfaceRef = ref<HTMLElement | null>(null)
const resizerRef = ref<WorkbenchResizerHandle | null>(null)
const viewportWidth = ref(typeof window === 'undefined' ? 0 : window.innerWidth)
const containerWidth = ref(0)
const containerRect = ref({ top: 0, right: viewportWidth.value, height: 0 })

// Tab overflow dropdown: the strip scrolls horizontally with its scrollbar
// hidden, so tabs pushed out of view are otherwise unreachable.
const tabMenuOpen = ref(false)
const tabMenuRef = ref<HTMLElement | null>(null)

function toggleTabMenu() {
  tabMenuOpen.value = !tabMenuOpen.value
  if (tabMenuOpen.value) {
    void nextTick(() => {
      tabMenuRef.value
        ?.querySelector('.is-active')
        ?.scrollIntoView({ block: 'nearest' })
    })
  }
}

function selectTabFromMenu(id: string) {
  tabMenuOpen.value = false
  store.activateItem(id)
}

function collapseFromMenu() {
  tabMenuOpen.value = false
  collapseWorkbench()
}

function onTabMenuMousedown(event: MouseEvent) {
  if (!tabMenuOpen.value) return
  const target = event.target as Element | null
  if (target?.closest('[data-workbench-tab-menu], .workbench-host__tabs-overflow')) return
  tabMenuOpen.value = false
}

function onTabMenuKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') tabMenuOpen.value = false
}
const detectedCoarseOnly = ref(false)
const previewWidth = ref<number | null>(null)
let coarseQuery: MediaQueryList | null = null
let surfaceObserver: ResizeObserver | null = null
let containerObserver: ResizeObserver | null = null
let surfaceMutationObserver: MutationObserver | null = null
let rectFrame = 0
let lastNativeItemId: string | null = null
const nativeSurfaceSlotSelector = '[data-workbench-native-surface-slot]'

const measuredAvailableWidth = computed(() => {
  const supplied = props.availableWidth
  if (typeof supplied === 'number' && Number.isFinite(supplied)) return supplied
  return containerWidth.value > 0 ? containerWidth.value : viewportWidth.value
})
const layoutMode = computed(() => workbenchLayoutMode({
  availableWidth: measuredAvailableWidth.value,
  coarseOnly: props.coarseOnly ?? detectedCoarseOnly.value,
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
  '--workbench-width': `${effectiveWidth.value}px`,
  '--workbench-container-top': `${containerRect.value.top}px`,
  '--workbench-container-end': `${Math.max(
    0,
    viewportWidth.value - containerRect.value.right,
  )}px`,
  '--workbench-container-height': `${containerRect.value.height}px`,
}))
const shouldRender = computed(() =>
  props.enabled && props.routeActive && store.expanded && store.activeItem !== null)
const shouldMount = computed(() =>
  props.enabled && store.activeItem !== null)
const runtimeAvailable = computed(() =>
  props.enabled
  && props.routeActive
  && shouldRender.value)
const mobileDialogOpen = computed(() =>
  shouldRender.value && layoutMode.value === 'mobile-dialog')

useDialogA11y(
  hostRef,
  mobileDialogOpen,
  collapseWorkbench,
  {
    // No initialFocus: the dialog always contains focusable controls (tab
    // strip buttons or the single-title close), so the composable's
    // first-focusable fallback applies and the focus trap stays consistent.
    occludesNativeSurface: false,
  },
)

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

async function closeWorkbenchItem(id: string) {
  const item = store.items.find(candidate => candidate.id === id)
  if (!item || props.beforeCloseItem && !await props.beforeCloseItem(item)) return
  if (!store.closeItem(id)) return
  if (!store.activeItem) {
    emit('emptied')
    return
  }
  void nextTick(() => {
    const focusTarget = hostRef.value?.querySelector<HTMLElement>(
      '[role="tab"][aria-selected="true"]',
    ) ?? hostRef.value
    focusTarget?.focus({ preventScroll: true })
  })
}

function onTabKeydown(event: KeyboardEvent, currentIndex: number) {
  const count = store.items.length
  if (count < 2) return
  let nextIndex: number | null = null
  if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + count) % count
  else if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % count
  else if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = count - 1
  if (nextIndex === null) return
  event.preventDefault()
  const item = store.items[nextIndex]
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
  containerRect.value = {
    top: rect.top,
    right: rect.right,
    height: rect.height || parent.clientHeight,
  }
}

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

function updateCoarseOnly(event: MediaQueryListEvent | MediaQueryList) {
  detectedCoarseOnly.value = event.matches
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
  const item = store.activeItem
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
  if (!element || !runtimeAvailable.value) {
    emit('surface-rect', hiddenRect(activeNativeId))
    return
  }
  const rect = element.getBoundingClientRect()
  emit('surface-rect', {
    itemId: activeNativeId,
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.max(0, Math.round(rect.width)),
    height: Math.max(0, Math.round(rect.height)),
    visible: !props.modalBlocked && rect.width > 0 && rect.height > 0,
  })
}

watch(layoutMode, mode => {
  resizerRef.value?.cancel()
  previewWidth.value = null
  emit('layout-change', mode)
  void nextTick(scheduleSurfaceRect)
}, { immediate: true })

watch(runtimeAvailable, available => {
  store.setHostAvailable(available)
  void nextTick(scheduleSurfaceRect)
}, { immediate: true })

watch(() => props.modalBlocked, () => void nextTick(scheduleSurfaceRect))
watch(() => store.activeItem?.id, () => void nextTick(scheduleSurfaceRect))
watch(() => store.activeItem?.hostKind, () => void nextTick(scheduleSurfaceRect))
watch(effectiveWidth, scheduleSurfaceRect)
watch([hostRef, surfaceRef], () => {
  reconnectObservers()
  scheduleSurfaceRect()
}, { flush: 'post' })

onMounted(() => {
  window.addEventListener('resize', updateViewportWidth)
  window.addEventListener('mousedown', onTabMenuMousedown)
  window.addEventListener('keydown', onTabMenuKeydown)
  window.addEventListener('scroll', measureContainer, true)
  coarseQuery = window.matchMedia?.('(pointer: coarse) and (hover: none)') ?? null
  if (coarseQuery) {
    updateCoarseOnly(coarseQuery)
    coarseQuery.addEventListener?.('change', updateCoarseOnly)
  }
  if (typeof ResizeObserver !== 'undefined') {
    surfaceObserver = new ResizeObserver(scheduleSurfaceRect)
    containerObserver = new ResizeObserver(() => {
      measureContainer()
      scheduleSurfaceRect()
    })
  }
  if (typeof MutationObserver !== 'undefined') {
    surfaceMutationObserver = new MutationObserver(onSurfaceMutation)
  }
  reconnectObservers()
  scheduleSurfaceRect()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewportWidth)
  window.removeEventListener('scroll', measureContainer, true)
  window.removeEventListener('mousedown', onTabMenuMousedown)
  window.removeEventListener('keydown', onTabMenuKeydown)
  coarseQuery?.removeEventListener?.('change', updateCoarseOnly)
  surfaceObserver?.disconnect()
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

/* Keep the split resizer's chat-side hit area outside the clipped workbench.
   The native browser surface starts at the workbench edge and can consume
   pointer events on its side of the handle, so the handle must remain
   reachable from the adjacent chat pane. */
.workbench-host--split {
  overflow: visible;
}

.workbench-host--overlay {
  position: fixed;
  z-index: 220;
  inset:
    var(--workbench-container-top)
    var(--workbench-container-end)
    auto
    auto;
  max-width: calc(100vw - var(--workbench-container-end) - 24px);
  height: var(--workbench-container-height);
}

.workbench-host--mobile-dialog {
  position: fixed;
  z-index: 500;
  inset: 0;
  width: 100%;
  height: 100dvh;
  border-inline-start: 0;
}

.workbench-host__chrome {
  position: relative;
  display: flex;
  min-height: 48px;
  align-items: center;
  gap: var(--sp-2);
  padding: 0 var(--sp-3);
  border-block-end: 1px solid var(--border);
}

.workbench-host__tabs {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 2px;
  overflow-x: auto;
  scrollbar-width: none;
}

/* Pinned to the strip's visible right edge: margin-left eats the spare
 * space when tabs don't fill the strip, sticky keeps it visible when the
 * strip scrolls horizontally. */
.workbench-host__tabs-overflow {
  position: sticky;
  right: 0;
  margin-left: auto;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 26px;
  border: 0;
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.workbench-host__tabs-overflow:hover,
.workbench-host__tabs-overflow:focus-visible {
  background: var(--bg-hover);
  color: var(--text);
}

.workbench-host__tab-menu {
  position: absolute;
  top: 100%;
  right: var(--sp-3);
  z-index: 1100;
  min-width: 180px;
  max-height: 280px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 4px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow-md);
}

.workbench-host__tab-menu-item {
  display: block;
  width: 100%;
  border: 0;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  text-align: start;
  padding: 6px 10px;
  border-radius: var(--radius-xs);
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.workbench-host__tab-menu-item:hover,
.workbench-host__tab-menu-item:focus-visible {
  background: var(--bg-hover);
}

.workbench-host__tab-menu-item.is-active {
  background: var(--bg-hover);
  font-weight: 600;
}

.workbench-host__tabs::-webkit-scrollbar {
  display: none;
}

.workbench-host__tab-wrap {
  display: flex;
  min-width: 120px;
  max-width: 220px;
  align-items: center;
  color: var(--text-dim);
}

.workbench-host__tab-wrap.is-active {
  color: var(--text);
}

.workbench-host__tab {
  min-width: 0;
  flex: 1;
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
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: var(--sp-2);
}

.workbench-host__single-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.workbench-host__single-close:hover,
.workbench-host__single-close:focus-visible {
  background: var(--bg-hover);
  color: var(--text);
}

.workbench-host__tab-menu-divider {
  height: 1px;
  margin: 4px 6px;
  background: var(--border);
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
