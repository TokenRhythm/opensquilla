<template>
  <section ref="panel" class="office-preview" :class="{ 'office-preview--paged': paged }" tabindex="-1" :aria-busy="loading" @keydown.esc="exitSelectionMode">
    <div class="office-preview__toolbar">
      <button v-if="paged" type="button" class="btn btn--ghost office-preview__navigator-toggle"
        :aria-label="t(navigatorExpanded ? 'workbench.office.closeNavigator' : 'workbench.office.openNavigator')"
        :title="t(navigatorExpanded ? 'workbench.office.closeNavigator' : 'workbench.office.openNavigator')"
        :aria-expanded="navigatorExpanded" :aria-controls="navigatorId" :disabled="!session || !total"
        @click="navigatorPreference = !navigatorExpanded">
        <Icon :name="navigatorExpanded ? 'sidebar-visible' : 'sidebar-hidden'" :size="18" aria-hidden="true" />
      </button>
      <button
        v-if="annotationEnabled && selectionMode === undefined"
        type="button"
        class="btn office-preview__request"
        :class="{ 'is-active': choosing }"
        :disabled="!session || loading || failure || suspended"
        :aria-pressed="choosing"
        :aria-controls="guideId"
        :title="choosing ? `${t('workbench.office.exitRequestMode')} (Esc)` : t('workbench.office.requestChange')"
        @pointerdown.prevent
        @click="toggleSelectionMode"
      >
        <Icon name="pencil" :size="15" aria-hidden="true" />
        {{ t('workbench.office.requestChange') }}
      </button>
      <div class="office-preview__navigation">
      <button type="button" class="btn btn--ghost office-preview__step" :disabled="!session || page <= 0" :aria-label="t('workbench.office.previous')" :title="t('workbench.office.previous')" @click="navigate('previous')"><Icon name="chevronLeft" :size="16" aria-hidden="true" /></button>
      <span class="office-preview__position" aria-live="polite">{{ total ? `${page + 1} / ${total}` : '—' }}</span>
      <button type="button" class="btn btn--ghost office-preview__step" :disabled="!session || page + 1 >= total" :aria-label="t('workbench.office.next')" :title="t('workbench.office.next')" @click="navigate('next')"><Icon name="chevronRight" :size="16" aria-hidden="true" /></button>
      <span class="office-preview__separator" aria-hidden="true" />
      <button type="button" class="btn btn--ghost" :disabled="!session" @click="navigate('zoomOut')" :aria-label="t('workbench.office.zoomOut')">−</button>
      <select v-if="paged" class="office-preview__zoom" :value="viewMode" :disabled="!session" :aria-label="t('workbench.office.zoomMode')" @change="changeFit">
        <option value="page">{{ t('workbench.office.fitPage') }}</option>
        <option value="width">{{ t('workbench.office.fitWidth') }}</option>
        <option v-if="viewMode === 'manual'" value="manual">{{ Math.round(scale * 100) }}%</option>
      </select>
      <button v-else type="button" class="btn btn--ghost" :disabled="!session" @click="navigate('fitWidth')">{{ t('workbench.office.fitWidth') }}</button>
      <button type="button" class="btn btn--ghost" :disabled="!session" @click="navigate('zoomIn')" :aria-label="t('workbench.office.zoomIn')">+</button>
      </div>
    </div>
    <div v-if="choosing && annotationEnabled" :id="guideId" class="office-preview__guide">
      <div role="status" class="office-preview__context">
        <template v-if="selection">
          <strong class="office-preview__selection-label">{{ t('workbench.office.selected') }}</strong>
          <span class="office-preview__selection-text">{{ selection.text?.slice(0, 100) || selection.range || selection.name }}</span>
        </template>
        <span v-else>{{ selectionHint }}</span>
      </div>
      <kbd aria-hidden="true">Esc</kbd>
    </div>
    <p v-if="loading" role="status">{{ t('chat.loadingPreview') }}</p>
    <div v-if="failure" class="office-preview__error" role="alert">
      <p>{{ t('workbench.artifactPreview.failedDetail') }}</p>
      <button type="button" class="btn btn--ghost" @click="start">{{ t('chat.retry') }}</button>
    </div>
    <div class="office-preview__body">
      <OfficePageNavigator v-if="paged && session && total" :id="navigatorId"
        :session="session" :total="total" :active="page" :format="format as 'docx' | 'pptx'"
        :collapsed="!navigatorExpanded" @thumbnail="emit('thumbnail', $event)" @navigate="goTo" @close="navigatorPreference = false" @expand="navigatorPreference = true" />
      <div ref="viewport" class="office-preview__viewport" :class="{ 'office-preview__viewport--paged': paged }" @scroll.passive="saveReading">
        <div class="office-preview__reading" :class="{ 'office-preview__reading--slides': format === 'pptx' }">
          <div ref="container" class="office-preview__canvas" />
          <section v-if="format === 'pptx' && notes" class="office-preview__notes" :style="notesStyle" :aria-label="t('workbench.office.speakerNotes')">
            <h3>{{ t('workbench.office.speakerNotes') }}</h3>
            <p>{{ notes }}</p>
          </section>
        </div>
      </div>
    </div>
    <p v-if="selectionMode === undefined" class="office-preview__hint">{{ t(choosing ? 'workbench.office.modeHint' : annotationEnabled ? 'workbench.office.previewHint' : 'workbench.office.readOnlyHint') }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import OfficePageNavigator from './OfficePageNavigator.vue'
import type { OfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import type { OfficeSelection } from '@/types/officeSelection'
import { createOfficePreviewSession, type OfficePreviewSession, type OfficeSelectionCommit,
  type OfficeSelectionAnchor, type OfficeLocateResult } from '@/workbench/officePreviewAdapter'
import { resourceReadingState, type ResourceReadingState } from './resourceReadingState'

const props = withDefaults(defineProps<{ data: ArrayBuffer; format: OfficePreviewFormat; annotationEnabled?: boolean; suspended?: boolean; readingState?: ResourceReadingState;
  revisionId?: string; selectionMode?: boolean; selectionLocked?: boolean; selectionToLocate?: { selection: OfficeSelection; revisionId?: string; stale?: boolean; requestId: string | number } }>(), {
  annotationEnabled: false, suspended: false, selectionMode: undefined, selectionLocked: false,
})
const emit = defineEmits<{ 'selection-request': [selection: OfficeSelection]; 'selection-commit': [value: OfficeSelectionCommit];
  'anchor-change': [anchor: OfficeSelectionAnchor | null]; 'locate-result': [result: OfficeLocateResult & { requestId: string | number }];
  'selection-mode-change': [enabled: boolean];
  loading: []; ready: []; error: [error: Error]; thumbnail: [blob: Blob]; 'reading-state': [state: ResourceReadingState, data: ArrayBuffer] }>()
const { t } = useI18n()
const panel = ref<HTMLElement | null>(null)
const container = ref<HTMLElement | null>(null)
const viewport = ref<HTMLElement | null>(null)
const session = shallowRef<OfficePreviewSession | null>(null)
const selection = shallowRef<OfficeSelection | null>(null)
const choosing = ref(false)
const guideId = useId()
const navigatorId = useId()
const paged = computed(() => props.format !== 'xlsx')
const panelWidth = ref(0)
const navigatorPreference = ref<boolean | null>(null)
const navigatorExpanded = computed(() => navigatorPreference.value ?? panelWidth.value >= 800)
const viewMode = ref<'page' | 'width' | 'manual'>(props.format === 'pptx' ? 'page' : 'width')
const scale = ref(1), renderedWidth = ref(0)
const notes = computed(() => session.value?.getNotes(page.value) || '')
const notesStyle = computed(() => renderedWidth.value ? { width: `${renderedWidth.value}px`, maxWidth: '100%' } : undefined)
const selectionHint = computed(() => t({
  docx: 'workbench.office.selectDocx',
  pptx: 'workbench.office.selectPptx',
  xlsx: 'workbench.office.selectXlsx',
}[props.format]))
const loading = ref(false), failure = ref(false), page = ref(0), total = ref(0)
let controller: AbortController | null = null
let generation = 0
let readingData: ArrayBuffer | null = null
let observer: ResizeObserver | null = null
function saveReading() {
  if (!session.value || !readingData || readingData !== props.data || loading.value || failure.value) return
  emit('reading-state', { page: page.value, mode: viewMode.value, scale: scale.value,
    top: viewport.value?.scrollTop ?? 0, left: viewport.value?.scrollLeft ?? 0, time: 0 }, readingData)
}
function exitSelectionMode(event?: Event) {
  // An open editor owns Escape/cancellation. A toolbar-focused Escape must not
  // destroy its target while the draft card remains open.
  if (props.selectionLocked && event?.type === 'keydown') return
  if (choosing.value && event?.type === 'keydown') {
    event.preventDefault()
    event.stopPropagation()
  }
  choosing.value = false
  selection.value = null
  session.value?.setSelectionMode(false)
  emit('selection-mode-change', false)
}
function toggleSelectionMode() {
  if (!props.annotationEnabled || !session.value || loading.value || failure.value || props.suspended) return
  if (choosing.value) { exitSelectionMode(); return }
  selection.value = null
  choosing.value = true
  session.value.setSelectionMode(true)
  emit('selection-mode-change', true)
  // Keep Escape scoped to this preview even when the pointer button preserves
  // the document's DOM selection instead of taking focus itself.
  panel.value?.focus({ preventScroll: true })
}
function requestChange(value: OfficeSelection) {
  if (!choosing.value || !props.annotationEnabled || !session.value || loading.value || failure.value || props.suspended) return
  emit('selection-request', value)
}
function commitSelection(value: OfficeSelectionCommit) {
  if (!choosing.value || !props.annotationEnabled || !session.value || loading.value || failure.value || props.suspended) return
  emit('selection-commit', value)
}
function clearTransientSelection() {
  selection.value = null
  if (session.value?.clearTransientSelection) session.value.clearTransientSelection()
  else session.value?.clearSelection()
}
function reselect() {
  if (!props.annotationEnabled || !session.value || loading.value || props.suspended) return
  choosing.value = true
  selection.value = null
  if (session.value.reselect) session.value.reselect()
  else { session.value.clearSelection(); session.value.setSelectionMode(true) }
  panel.value?.focus({ preventScroll: true })
}
async function locateRequestedSelection() {
  const request = props.selectionToLocate, current = session.value, run = generation
  if (!request || !current || loading.value || props.suspended) return
  if (!request.stale && props.annotationEnabled) {
    choosing.value = !props.selectionLocked
    current.setSelectionMode(!props.selectionLocked, props.selectionLocked)
  }
  const result = await current.locateSelection?.(request.selection, request) ?? { status: 'unavailable' as const, anchor: null }
  if (run !== generation || current !== session.value || request.requestId !== props.selectionToLocate?.requestId) return
  emit('locate-result', { ...result, requestId: request.requestId })
}
function stop() {
  generation += 1
  controller?.abort()
  controller = null
  session.value?.destroy()
  session.value = null
  readingData = null
  selection.value = null
  choosing.value = false
  loading.value = false
  renderedWidth.value = 0
}
async function start() {
  stop()
  if (props.suspended || !container.value) return
  const run = generation
  controller = new AbortController()
  const signal = controller.signal
  const data = props.data
  loading.value = true
  emit('loading')
  failure.value = false
  page.value = 0
  total.value = 0
  try {
    const created = await createOfficePreviewSession({ container: container.value, viewportElement: viewport.value || undefined,
      viewportBottomInset: props.format === 'pptx' ? 112 : 0,
      data, format: props.format, revisionId: props.revisionId, signal,
      onView: state => {
        if (run !== generation) return
        viewMode.value = state.mode; scale.value = state.scale; renderedWidth.value = state.width
      },
      selectionLabels: { text: t('workbench.office.hoverText'), element: t('workbench.office.hoverElement'), range: t('workbench.office.hoverRange') },
      onSelection: value => {
        if (run !== generation || (!choosing.value && !props.selectionLocked) || !props.annotationEnabled) return
        selection.value = value
      },
      onSelectionCommit: value => {
        if (run !== generation) return
        requestChange(value)
      },
      onSelectionCommitDetail: value => { if (run === generation) commitSelection(value) },
      onAnchorChange: anchor => { if (run === generation) emit('anchor-change', anchor) },
      onLocation: (index, count) => {
        if (run !== generation) return
        if (page.value !== index && !props.selectionLocked) selection.value = null
        page.value = index; total.value = count
      },
      onError: error => {
        if (run !== generation) return
        failure.value = true; exitSelectionMode()
        emit('error', error)
      },
    })
    if (run !== generation) { created.destroy(); return }
    session.value = created
    readingData = data
    const saved = resourceReadingState(props.readingState)
    if (saved) {
      await created.goTo(Math.min(saved.page, Math.max(0, total.value - 1)))
      if (run !== generation) return
      if (saved.mode === 'manual') await created.setScale?.(saved.scale)
      else await (saved.mode === 'page' ? created.fitPage() : created.fitWidth())
      if (run !== generation) return
      if (viewport.value) { viewport.value.scrollTop = saved.top; viewport.value.scrollLeft = saved.left }
    }
    if (!failure.value) emit('ready')
  } catch (error) {
    if (run === generation && !signal.aborted) {
      failure.value = true
      emit('error', error instanceof Error ? error : new Error(String(error)))
    }
  } finally { if (run === generation) {
    loading.value = false
    if (props.selectionLocked) session.value?.setSelectionMode(false, true)
    else if (props.selectionMode && !choosing.value) toggleSelectionMode()
    saveReading(); void locateRequestedSelection()
  } }
}
async function navigate(action: 'previous' | 'next' | 'zoomIn' | 'zoomOut' | 'fitWidth' | 'fitPage' | 'resize') {
  const run = generation
  if ((action === 'previous' || action === 'next') && !props.selectionLocked) { selection.value = null; session.value?.clearSelection() }
  try {
    await session.value?.[action]()
    if (run === generation && (action === 'previous' || action === 'next')) viewport.value?.scrollTo({ top: 0, left: 0 })
    if (run === generation) saveReading()
  } catch (error) {
    if (run !== generation) return
    failure.value = true
    exitSelectionMode()
    emit('error', error instanceof Error ? error : new Error(String(error)))
  }
}
function changeFit(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value === 'page' || value === 'width') void navigate(value === 'page' ? 'fitPage' : 'fitWidth')
}
async function goTo(index: number) {
  if (!session.value || index === page.value) return
  const run = generation
  if (!props.selectionLocked) { selection.value = null; session.value.clearSelection() }
  try {
    await session.value.goTo(index)
    if (run === generation) viewport.value?.scrollTo({ top: 0, left: 0 })
    if (run === generation) saveReading()
  } catch (error) {
    if (run !== generation) return
    failure.value = true; exitSelectionMode()
    emit('error', error instanceof Error ? error : new Error(String(error)))
  }
}
watch(() => [props.data, props.format, props.suspended] as const, () => { void start() })
watch(() => props.selectionToLocate?.requestId, () => { void locateRequestedSelection() })
watch(() => [props.selectionMode, props.selectionLocked] as const, async ([value, locked]) => {
  if (locked && props.annotationEnabled) {
    choosing.value = false
    session.value?.setSelectionMode(false, true)
  } else if (value) {
    // Unlocking starts a fresh gesture even if selection mode stayed true.
    choosing.value = false
    toggleSelectionMode()
  } else if (value === false) exitSelectionMode()
  // Hiding the selection guide moves the canvas without necessarily resizing
  // it. Resolve geometry after Vue has placed the preview at its new position.
  await nextTick()
  await session.value?.refreshAnchor?.()
})
watch(() => props.annotationEnabled, enabled => { if (!enabled) exitSelectionMode() })
onMounted(() => {
  void start()
  if (typeof ResizeObserver !== 'undefined' && viewport.value) {
    let width = 0, height = 0
    observer = new ResizeObserver(entries => {
      for (const entry of entries) {
        const rect = entry.contentRect
        if (entry.target === panel.value) { panelWidth.value = rect.width; continue }
        // Selecting content must not resize its text layer when the guide appears.
        const watchHeight = props.format === 'xlsx' || (viewMode.value === 'page' && !choosing.value)
        const geometryChanged = rect.width !== width || rect.height !== height
        const changed = rect.width !== width || (watchHeight && rect.height !== height)
        width = rect.width; height = rect.height
        if (changed && session.value && !props.suspended) void navigate('resize')
        else if (geometryChanged && session.value && !props.suspended) void session.value.refreshAnchor?.()
      }
    })
    observer.observe(viewport.value)
    if (panel.value) observer.observe(panel.value)
  }
})
onBeforeUnmount(() => { saveReading(); observer?.disconnect(); stop() })
defineExpose({ clearTransientSelection, reselect, setSelectionMode: (enabled: boolean) => {
  if (enabled && !choosing.value) toggleSelectionMode()
  else if (!enabled) exitSelectionMode()
} })
</script>

<style scoped>
.office-preview { display: flex; flex-direction: column; position: relative; width: 100%; height: 100%; min-height: 240px; overflow: hidden; }
.office-preview:focus { outline: none; }
.office-preview__toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 10px 12px; flex: none; border-bottom: 1px solid var(--border); }
.office-preview__navigation { display: flex; gap: 2px; align-items: center; flex-wrap: wrap; margin-left: auto; }
.office-preview__navigator-toggle, .office-preview__step { display: inline-flex; justify-content: center; align-items: center; min-width: 30px; padding: 6px; }
.office-preview__position { min-width: 44px; text-align: center; font-size: var(--fs-xs); font-variant-numeric: tabular-nums; color: var(--text-dim); }
.office-preview__separator { height: 16px; width: 1px; background: var(--border); margin: 0 6px; }
.office-preview__zoom { max-width: 144px; min-height: 32px; padding: 4px 6px; border: 1px solid transparent; border-radius: var(--radius-sm); background: var(--bg-surface); color: var(--text); font: inherit; font-size: var(--fs-xs); cursor: pointer; }
.office-preview__zoom:hover { border-color: var(--border); }
.office-preview__zoom:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.office-preview__request { display: inline-flex; align-items: center; gap: 6px; min-height: 34px; padding: 0 12px; flex: none; white-space: nowrap; font-weight: 600; color: var(--accent); border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border)); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.office-preview__request:hover:not(:disabled) { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 15%, transparent); }
.office-preview__request.is-active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 25%, var(--bg-surface)); box-shadow: inset 0 0 0 1px var(--accent); }
.office-preview__request:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.office-preview__request:disabled { opacity: .5; cursor: not-allowed; }
.office-preview__guide { display: flex; align-items: center; gap: 10px; flex: none; min-height: 48px; padding: 8px 12px; font-size: var(--fs-xs); color: var(--text); background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface)); border-bottom: 1px solid var(--border); }
.office-preview__context { display: flex; flex: 1; align-items: center; gap: 10px; min-width: 0; }
.office-preview__guide kbd { flex: none; color: var(--text-dim); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 2px 4px; }
.office-preview__body { display: flex; flex: 1; min-height: 0; min-width: 0; position: relative; }
.office-preview--paged .office-preview__body { background: color-mix(in srgb, var(--bg-surface) 80%, var(--bg)); }
.office-preview__viewport { flex: 1; min-width: 0; min-height: 0; overflow: auto; position: relative; overscroll-behavior: contain; }
.office-preview__viewport--paged { padding: 24px; }
.office-preview__reading { height: 100%; min-width: 0; }
.office-preview--paged .office-preview__reading { height: auto; min-height: 100%; }
.office-preview__reading--slides { display: flex; flex-direction: column; justify-content: safe center; }
.office-preview__canvas { width: 100%; height: 100%; min-height: 0; min-width: 0; position: relative; }
.office-preview--paged .office-preview__canvas { height: auto; flex: none; display: flex; align-items: flex-start; justify-content: safe center; }
/* Move the complete vendor page wrapper, including its selectable text layer.
   Canvas-only transforms would make rendering and annotation hit targets diverge. */
.office-preview--paged .office-preview__canvas :deep(> div) { flex: none; }
.office-preview--paged .office-preview__canvas :deep(canvas) { display: block; box-shadow: 0 6px 24px color-mix(in srgb, var(--text) 12%, transparent); }
.office-preview__notes { box-sizing: border-box; margin: 24px auto 8px; padding: 0 4px; flex: none; color: var(--text); }
.office-preview__notes h3 { margin: 0 0 8px; font-size: var(--fs-xs); font-weight: 500; color: var(--text-dim); }
.office-preview__notes p { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--fs-sm); line-height: 1.65; user-select: text; }
.office-preview__selection-label { color: var(--accent); flex: none; font-weight: 600; }
.office-preview__selection-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.office-preview__hint { font-size: var(--fs-xs); color: var(--text-dim); padding: 8px 12px; margin: 0; }
.office-preview__error { padding: 12px; }
</style>
