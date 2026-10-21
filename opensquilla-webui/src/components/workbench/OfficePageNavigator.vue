<template>
  <nav ref="root" class="office-page-nav" :class="{ 'is-collapsed': collapsed }" :aria-label="t('workbench.office.pageNavigator')">
    <button type="button" class="office-page-nav__toggle" :aria-label="t(collapsed ? 'workbench.office.openNavigator' : 'workbench.office.closeNavigator')"
      :title="t(collapsed ? 'workbench.office.openNavigator' : 'workbench.office.closeNavigator')" @click="toggle">
      <span v-if="!collapsed" class="office-page-nav__label" :title="t('workbench.office.pageNavigator')">{{ t('workbench.office.pageNavigator') }}</span>
      <span v-if="!collapsed" class="office-page-nav__count">{{ count ? activePage + 1 : 0 }} / {{ count }}</span>
      <Icon :name="collapsed ? 'chevronRight' : 'chevronLeft'" :size="16" aria-hidden="true" />
    </button>
    <div v-if="!collapsed" ref="list" class="office-page-nav__list" @scroll="onScroll" @keydown="onListKeydown">
      <div class="office-page-nav__spacer" :style="{ height: `${count * rowHeight}px` }">
        <button v-for="index in visibleIndices" :key="index" type="button" class="office-page-nav__page" :data-page="index"
          :class="{ 'is-active': index === activePage }" :style="{ top: `${index * rowHeight}px`, height: `${rowHeight - 8}px` }"
          :aria-label="pageLabel(index)" :aria-current="index === activePage ? 'page' : undefined" :disabled="!session"
          @click="navigate(index)">
          <span class="office-page-nav__thumbnail">
            <img v-if="thumbnails.get(index)?.url" :src="thumbnails.get(index)!.url" alt="" draggable="false" />
            <span v-else class="office-page-nav__placeholder" :aria-label="thumbnailLabel(index)" :title="thumbnailLabel(index)">—</span>
          </span>
          <span class="office-page-nav__number">{{ index + 1 }}</span>
        </button>
      </div>
    </div>
    <div v-else class="office-page-nav__rail-container">
      <div ref="rail" class="office-page-nav__rail" role="slider" :tabindex="session && count ? 0 : -1" aria-orientation="vertical"
        :aria-label="t('workbench.office.pageNavigator')" :aria-valuemin="1" :aria-valuemax="Math.max(1, count)"
        :aria-valuenow="activePage + 1" :aria-valuetext="pageLabel(activePage)" :aria-disabled="!session || !count"
        :aria-describedby="hoverIndex !== null ? tooltipId : undefined" :style="{ maxHeight: `${Math.max(28, markers.length * 14)}px` }"
        @pointermove="onRailMove" @pointerleave="hideHover" @click="onRailClick" @focus="showHover(activePage)" @blur="hideHover" @keydown="onRailKeydown">
        <span v-for="index in markers" :key="index" class="office-page-nav__line" :style="markerStyle(index)" aria-hidden="true" />
        <span v-if="count" class="office-page-nav__line is-active" :style="{ top: markerTop(activePage) }" aria-hidden="true" />
      </div>
    </div>
    <div v-if="collapsed && hoverIndex !== null" :id="tooltipId" role="tooltip" class="office-page-nav__hover" :style="{ top: `${hoverTop}px` }">
      <span>{{ pageLabel(hoverIndex) }}</span>
      <img v-if="thumbnails.get(hoverIndex)?.url" :src="thumbnails.get(hoverIndex)!.url" alt="" draggable="false" />
      <span v-else class="office-page-nav__placeholder" :aria-label="thumbnailLabel(hoverIndex)">—</span>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { OfficePreviewSession } from '@/workbench/officePreviewAdapter'

const props = defineProps<{ session: OfficePreviewSession | null; total: number; active: number; format: 'docx' | 'pptx'; collapsed: boolean }>()
const emit = defineEmits<{ navigate: [index: number]; close: []; expand: []; thumbnail: [blob: Blob] }>()
const { t } = useI18n()
const root = ref<HTMLElement | null>(null), list = ref<HTMLElement | null>(null), rail = ref<HTMLElement | null>(null)
const tooltipId = useId()
const MAX_ROWS = 32, MAX_MARKERS = 64, OVERSCAN = 2
const viewportHeight = ref(480), scrollTop = ref(0)
const count = computed(() => Number.isFinite(props.total) ? Math.max(0, Math.floor(props.total)) : 0)
const activePage = computed(() => clampPage(props.active))
// Fixed-size virtual rows keep both DOM and decoded thumbnail memory bounded.
const rowHeight = computed(() => Math.max(props.format === 'docx' ? 184 : 118, Math.ceil(viewportHeight.value / (MAX_ROWS - OVERSCAN * 2))))
const visibleIndices = computed(() => {
  if (props.collapsed) return []
  const first = Math.max(0, Math.min(count.value - 1, Math.floor(scrollTop.value / rowHeight.value)) - OVERSCAN)
  const length = Math.min(MAX_ROWS, Math.ceil(viewportHeight.value / rowHeight.value) + OVERSCAN * 2, count.value - first)
  return Array.from({ length: Math.max(0, length) }, (_, offset) => first + offset)
})
const markers = computed(() => {
  const length = Math.min(count.value, MAX_MARKERS)
  return Array.from({ length }, (_, index) => length === 1 ? 0 : Math.round(index * (count.value - 1) / (length - 1)))
})
const hoverIndex = ref<number | null>(null), requestedHover = ref<number | null>(null), hoverTop = ref(36)
const wanted = computed(() => props.collapsed ? requestedHover.value === null ? [] : [requestedHover.value] : visibleIndices.value)
type ThumbnailState = { status: 'loading' | 'ready' | 'error'; url?: string }
const thumbnails = shallowRef(new Map<number, ThumbnailState>())
let epoch = 0, pumping = false, destroyed = false
let request: { index: number; controller: AbortController } | null = null
let hoverTimer: ReturnType<typeof setTimeout> | null = null
let observer: ResizeObserver | null = null

function clampPage(index: number) { return Math.max(0, Math.min(count.value - 1, Number.isFinite(index) ? Math.floor(index) : 0)) }
function pageLabel(index: number) { return t(props.format === 'pptx' ? 'workbench.office.slideLabel' : 'workbench.office.pageLabel', { page: index + 1 }) }
function thumbnailLabel(index: number) { return t(thumbnails.value.get(index)?.status === 'error' ? 'workbench.office.thumbnailFailed' : 'workbench.office.thumbnailLoading') }
function markerTop(index: number) { return count.value <= 1 ? '50%' : `calc(8px + (100% - 16px) * ${index / (count.value - 1)})` }
function markerStyle(index: number) {
  const spacing = Math.max(1, (count.value - 1) / Math.max(1, markers.value.length - 1))
  const distance = hoverIndex.value === null ? Infinity : Math.abs(index - hoverIndex.value) / spacing
  const lens = distance < 3.5 ? (1 + Math.cos(Math.PI * distance / 3.5)) / 2 : 0
  return { top: markerTop(index), width: `${8 + 8 * lens}px`, opacity: .4 + .5 * lens }
}
function toggle() { if (props.collapsed) emit('expand'); else emit('close') }
function navigate(index: number) { if (props.session && count.value) emit('navigate', clampPage(index)) }
function updateThumbnail(index: number, state: ThumbnailState) { thumbnails.value = new Map(thumbnails.value).set(index, state) }
function revoke(state: ThumbnailState) { if (state.url) URL.revokeObjectURL(state.url) }

function reconcileThumbnails() {
  const desired = new Set(wanted.value)
  const next = new Map(thumbnails.value)
  for (const [index, state] of next) if (!desired.has(index)) { revoke(state); next.delete(index) }
  thumbnails.value = next
  if (request && !desired.has(request.index)) request.controller.abort()
  void pumpThumbnails()
}
async function pumpThumbnails() {
  if (pumping || destroyed) return
  pumping = true
  try {
    while (!destroyed && props.session) {
      const index = wanted.value.find(value => !thumbnails.value.has(value))
      if (index === undefined) break
      const session = props.session, generation = epoch, controller = new AbortController()
      request = { index, controller }
      updateThumbnail(index, { status: 'loading' })
      const current = () => !destroyed && !controller.signal.aborted && epoch === generation && props.session === session && wanted.value.includes(index)
      try {
        const thumbnail = await session.renderThumbnail(index, controller.signal)
        if (current()) {
          updateThumbnail(index, { status: 'ready', url: URL.createObjectURL(thumbnail.blob) })
          if (index === activePage.value) emit('thumbnail', thumbnail.blob)
        }
      } catch {
        if (current()) updateThumbnail(index, { status: 'error' })
      } finally {
        if (request?.controller === controller) request = null
      }
    }
  } finally { pumping = false }
}
function resetThumbnails() {
  epoch++
  request?.controller.abort()
  for (const state of thumbnails.value.values()) revoke(state)
  thumbnails.value = new Map()
}
function onScroll() { scrollTop.value = list.value?.scrollTop ?? 0 }
function revealActive() {
  const element = list.value
  if (!element) return
  const top = activePage.value * rowHeight.value, bottom = top + rowHeight.value
  if (top < element.scrollTop) element.scrollTop = top
  else if (bottom > element.scrollTop + viewportHeight.value) element.scrollTop = Math.max(0, bottom - viewportHeight.value)
  onScroll()
}
function observeViewport() {
  observer?.disconnect()
  if (list.value) {
    viewportHeight.value = list.value.clientHeight || 480
    observer?.observe(list.value)
    revealActive()
  }
}
function showHover(index: number) {
  if (!props.session || !count.value) return
  index = clampPage(index)
  if (hoverIndex.value === index) return
  hoverIndex.value = index
  requestedHover.value = null
  if (hoverTimer) clearTimeout(hoverTimer)
  const bounds = root.value?.getBoundingClientRect(), railBounds = rail.value?.getBoundingClientRect()
  if (bounds && railBounds) {
    const ratio = count.value <= 1 ? .5 : index / (count.value - 1)
    hoverTop.value = Math.max(0, Math.min(Math.max(0, bounds.height - 260), railBounds.top - bounds.top + 8 + ratio * Math.max(0, railBounds.height - 16)))
  }
  // Moving across a long document should not enqueue every crossed page.
  hoverTimer = setTimeout(() => { hoverTimer = null; requestedHover.value = index }, 120)
}
function hideHover() {
  if (hoverTimer) clearTimeout(hoverTimer)
  hoverTimer = null
  hoverIndex.value = requestedHover.value = null
}
function pageAtPointer(event: MouseEvent) {
  const bounds = rail.value?.getBoundingClientRect()
  if (!bounds || bounds.height <= 16) return activePage.value
  const ratio = Math.max(0, Math.min(1, (event.clientY - bounds.top - 8) / (bounds.height - 16)))
  return Math.round(ratio * (count.value - 1))
}
function onRailMove(event: PointerEvent) { showHover(pageAtPointer(event)) }
function onRailClick(event: MouseEvent) { const index = event.detail === 0 ? hoverIndex.value ?? activePage.value : pageAtPointer(event); navigate(index); showHover(index) }
function keyboardTarget(event: KeyboardEvent, from: number): number | null {
  switch (event.key) {
    case 'ArrowUp': case 'ArrowLeft': return clampPage(from - 1)
    case 'ArrowDown': case 'ArrowRight': return clampPage(from + 1)
    case 'PageUp': return clampPage(from - 10)
    case 'PageDown': return clampPage(from + 10)
    case 'Home': return 0
    case 'End': return Math.max(0, count.value - 1)
    default: return null
  }
}
function onRailKeydown(event: KeyboardEvent) {
  const index = keyboardTarget(event, hoverIndex.value ?? activePage.value)
  if (index === null) return // Escape remains owned by the request-change mode.
  event.preventDefault(); event.stopPropagation()
  navigate(index); showHover(index)
}
async function onListKeydown(event: KeyboardEvent) {
  const target = event.target instanceof HTMLElement ? event.target.closest<HTMLElement>('[data-page]') : null
  if (!target) return
  const index = keyboardTarget(event, Number(target.dataset.page))
  if (index === null || !props.session || !list.value) return
  event.preventDefault(); event.stopPropagation()
  navigate(index)
  list.value.scrollTop = Math.max(0, index * rowHeight.value - viewportHeight.value / 2)
  onScroll()
  await nextTick()
  list.value?.querySelector<HTMLButtonElement>(`[data-page="${index}"]`)?.focus({ preventScroll: true })
}

watch(() => props.session, () => { hideHover(); resetThumbnails(); reconcileThumbnails() })
watch(wanted, reconcileThumbnails, { immediate: true })
watch(() => [props.active, count.value], () => { if (hoverIndex.value !== null && hoverIndex.value >= count.value) hideHover(); revealActive() }, { flush: 'post' })
watch(() => props.collapsed, async () => { hideHover(); await nextTick(); observeViewport() }, { flush: 'post' })
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') observer = new ResizeObserver(entries => {
    const entry = entries.find(value => value.target === list.value)
    if (entry && entry.contentRect.height > 0) { viewportHeight.value = entry.contentRect.height; revealActive() }
  })
  observeViewport()
})
onBeforeUnmount(() => { destroyed = true; observer?.disconnect(); hideHover(); resetThumbnails() })
</script>

<style scoped>
.office-page-nav { box-sizing: border-box; position: relative; display: flex; flex-direction: column; flex: 0 0 160px; width: 160px; min-height: 0; border-right: 1px solid var(--border); background: var(--bg-surface); color: var(--text); z-index: 2; }
.office-page-nav.is-collapsed { flex-basis: 28px; width: 28px; background: transparent; }
.office-page-nav__toggle { display: flex; align-items: center; justify-content: space-between; gap: 4px; flex: none; width: 100%; min-height: 32px; padding: 4px 8px; border: 0; background: transparent; color: var(--text-dim); font-size: var(--fs-xs); cursor: pointer; }
.is-collapsed .office-page-nav__toggle { justify-content: center; padding: 4px; }
.office-page-nav__toggle:hover { color: var(--text); background: var(--bg-hover); }
.office-page-nav__label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.office-page-nav__count { margin-left: auto; font-variant-numeric: tabular-nums; white-space: nowrap; }
.office-page-nav__toggle:focus-visible, .office-page-nav__page:focus-visible, .office-page-nav__rail:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.office-page-nav__list { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain; scrollbar-width: thin; }
.office-page-nav__spacer { position: relative; width: 100%; }
.office-page-nav__page { box-sizing: border-box; position: absolute; left: 6px; width: calc(100% - 12px); display: flex; flex-direction: column; align-items: center; gap: 5px; padding: 6px; border: 1px solid transparent; border-radius: var(--radius-sm); background: transparent; color: var(--text-dim); cursor: pointer; }
.office-page-nav__page:hover { background: var(--bg-hover); }
.office-page-nav__page.is-active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface)); color: var(--accent); }
.office-page-nav__thumbnail { display: flex; flex: 1; align-items: center; justify-content: center; width: 100%; min-height: 0; }
.office-page-nav__thumbnail img { display: block; max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: var(--shadow-sm); }
.office-page-nav__number { flex: none; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; }
.office-page-nav__placeholder { display: block; padding: 12px; color: var(--text-dim); font-size: var(--fs-xs); }
.office-page-nav__rail-container { flex: 1; min-height: 0; display: flex; align-items: center; padding: 8px 0; }
.office-page-nav__rail { position: relative; width: 100%; height: 100%; min-height: 16px; cursor: pointer; touch-action: pan-y; }
.office-page-nav__line { position: absolute; left: 6px; width: 8px; height: 2px; border-radius: var(--radius-full); background: var(--text-dim); opacity: .5; pointer-events: none; transform: translateY(-50%); }
.office-page-nav__line.is-active { width: 16px; height: 3px; background: var(--accent); opacity: 1; }
.office-page-nav__hover { position: absolute; left: 32px; display: flex; flex-direction: column; gap: 8px; align-items: center; width: 160px; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-elevated); box-shadow: var(--shadow-lg); color: var(--text); font-size: var(--fs-xs); pointer-events: none; z-index: 3; }
.office-page-nav__hover img { display: block; max-width: 160px; max-height: 220px; object-fit: contain; }
</style>
