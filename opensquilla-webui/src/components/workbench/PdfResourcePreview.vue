<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { PDFDocumentLoadingTask, PDFDocumentProxy, PDFPageProxy, RenderTask } from 'pdfjs-dist'
import Icon from '@/components/Icon.vue'
import { createPdfLoadingTask, pdfCanvasSize, PDF_MIN_SCALE, PDF_MAX_SCALE } from '@/workbench/pdfPreview'
import { resourceReadingState, type ResourceReadingState } from './resourceReadingState'

const props = defineProps<{ data: ArrayBuffer; name?: string; readingState?: ResourceReadingState }>()
const emit = defineEmits<{ 'reading-state': [state: ResourceReadingState, data: ArrayBuffer] }>()
const { t } = useI18n()
const viewport = ref<HTMLElement | null>(null), canvasHost = ref<HTMLElement | null>(null)
const loading = ref(false), rendering = ref(false), failed = ref(false)
const page = ref(1), total = ref(0), scale = ref(1), mode = ref<'width' | 'page' | 'manual'>('width')
const ready = computed(() => total.value > 0 && !loading.value && !failed.value)
let generation = 0, renderGeneration = 0
let controller: AbortController | null = null, task: PDFDocumentLoadingTask | null = null
let document: PDFDocumentProxy | null = null, currentPage: PDFPageProxy | null = null
let renderingTask: RenderTask | null = null, observer: ResizeObserver | null = null
let resizeFrame: number | null = null
let readingData: ArrayBuffer | null = null
function saveReading() {
  if (!document || !readingData || readingData !== props.data || loading.value || failed.value) return
  emit('reading-state', { page: page.value - 1, mode: mode.value, scale: scale.value,
    top: viewport.value?.scrollTop ?? 0, left: viewport.value?.scrollLeft ?? 0, time: 0 }, readingData)
}

function cancelRender() {
  renderGeneration++
  renderingTask?.cancel(); renderingTask = null
  currentPage?.cleanup(); currentPage = null
  // Each render gets its own canvas: a cancelling task can never paint into a
  // replacement page, even before its cancellation promise settles.
  canvasHost.value?.replaceChildren()
  rendering.value = false
}
function stop() {
  generation++
  cancelRender()
  controller?.abort(); controller = null
  const previous = task
  task = null; document = null; readingData = null
  void previous?.destroy().catch(() => {})
  total.value = 0; loading.value = false
}
async function start() {
  stop()
  const run = generation
  controller = new AbortController()
  const signal = controller.signal
  failed.value = false; loading.value = true; page.value = 1; mode.value = 'width'
  const saved = resourceReadingState(props.readingState)
  const data = props.data
  try {
    const created = await createPdfLoadingTask(data, signal)
    if (run !== generation) { void created.promise.catch(() => {}); void created.destroy().catch(() => {}); return }
    task = created
    const loaded = await created.promise
    if (run !== generation) return
    document = loaded; readingData = data; total.value = loaded.numPages; loading.value = false
    if (saved) {
      page.value = Math.min(total.value, saved.page + 1)
      mode.value = saved.mode
      scale.value = Math.min(PDF_MAX_SCALE, Math.max(PDF_MIN_SCALE, saved.scale))
    }
    await renderPage()
    if (run === generation && viewport.value && saved) {
      viewport.value.scrollTop = saved.top; viewport.value.scrollLeft = saved.left
    }
    if (run === generation) saveReading()
  } catch {
    if (run === generation && !signal.aborted) {
      failed.value = true; total.value = 0
      const invalid = task; task = null; document = null; readingData = null
      void invalid?.destroy().catch(() => {})
    }
  } finally { if (run === generation) loading.value = false }
}
async function renderPage() {
  cancelRender()
  const activeDocument = document, run = generation, render = renderGeneration
  if (!activeDocument || !canvasHost.value || !viewport.value) return
  rendering.value = true; failed.value = false
  let requestedPage: PDFPageProxy | null = null
  try {
    requestedPage = await activeDocument.getPage(page.value)
    if (run !== generation || render !== renderGeneration) { requestedPage.cleanup(); return }
    currentPage = requestedPage
    const original = requestedPage.getViewport({ scale: 1 })
    const availableWidth = Math.max(1, viewport.value.clientWidth - 32)
    const availableHeight = Math.max(1, viewport.value.clientHeight - 32)
    if (mode.value !== 'manual') {
      const fit = mode.value === 'width' ? availableWidth / original.width
        : Math.min(availableWidth / original.width, availableHeight / original.height)
      scale.value = Math.min(PDF_MAX_SCALE, Math.max(PDF_MIN_SCALE, fit))
    }
    const view = requestedPage.getViewport({ scale: scale.value })
    const size = pdfCanvasSize(view.width, view.height, window.devicePixelRatio)
    const canvas = window.document.createElement('canvas')
    canvas.width = size.width; canvas.height = size.height
    canvas.style.width = `${view.width}px`; canvas.style.height = `${view.height}px`
    canvas.setAttribute('role', 'img')
    canvas.setAttribute('aria-label', `${props.name || 'PDF'} — ${t('workbench.office.pageLabel', { page: page.value })}`)
    canvasHost.value.replaceChildren(canvas)
    const context = canvas.getContext('2d', { alpha: false })
    if (!context) throw new Error('PDF canvas unavailable')
    const activeRender = requestedPage.render({ canvas, canvasContext: context, viewport: view,
      transform: [size.density, 0, 0, size.density, 0, 0] })
    renderingTask = activeRender
    await activeRender.promise
  } catch (error) {
    if (run === generation && render === renderGeneration && (error as Error)?.name !== 'RenderingCancelledException') {
      failed.value = true; canvasHost.value?.replaceChildren()
    }
  } finally {
    if (run === generation && render === renderGeneration) {
      renderingTask = null; rendering.value = false
    } else requestedPage?.cleanup()
  }
}
function goTo(value: number) {
  if (!ready.value || !Number.isFinite(value)) return
  const next = Math.min(total.value, Math.max(1, Math.trunc(value)))
  if (next === page.value) return
  page.value = next
  viewport.value?.scrollTo({ top: 0, left: 0 })
  void renderPage()
  saveReading()
}
function changePage(event: Event) {
  const input = event.target as HTMLInputElement
  goTo(Number(input.value)); input.value = String(page.value)
}
function zoom(delta: number) {
  if (!ready.value) return
  mode.value = 'manual'
  scale.value = Math.min(PDF_MAX_SCALE, Math.max(PDF_MIN_SCALE, Math.round((scale.value + delta) * 100) / 100))
  void renderPage()
  saveReading()
}
function changeFit(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (value !== 'width' && value !== 'page') return
  mode.value = value; void renderPage(); saveReading()
}
watch(() => props.data, () => { void start() })
onMounted(() => {
  void start()
  if (typeof ResizeObserver !== 'undefined' && viewport.value) {
    let width = 0, height = 0
    observer = new ResizeObserver(entries => {
      const rect = entries[0]?.contentRect
      if (!rect || rect.width === width && rect.height === height) return
      width = rect.width; height = rect.height
      if (!document || mode.value === 'manual' || failed.value || resizeFrame !== null) return
      resizeFrame = requestAnimationFrame(() => { resizeFrame = null; void renderPage() })
    })
    observer.observe(viewport.value)
  }
})
onBeforeUnmount(() => {
  saveReading()
  observer?.disconnect()
  if (resizeFrame !== null) cancelAnimationFrame(resizeFrame)
  stop()
})
</script>

<template>
  <section class="pdf-preview" :aria-busy="loading || rendering">
    <div class="pdf-preview__toolbar">
      <button type="button" class="btn btn--ghost" :disabled="!ready || page <= 1" :aria-label="t('workbench.office.previous')" @click="goTo(page - 1)"><Icon name="chevronLeft" :size="16" /></button>
      <input class="pdf-preview__page" type="number" min="1" :max="total || 1" :value="page" :disabled="!ready" :aria-label="t('workbench.office.pageLabel', { page })" @change="changePage" @keydown.enter="changePage" />
      <span class="pdf-preview__total" aria-live="polite">/ {{ total || '—' }}</span>
      <button type="button" class="btn btn--ghost" :disabled="!ready || page >= total" :aria-label="t('workbench.office.next')" @click="goTo(page + 1)"><Icon name="chevronRight" :size="16" /></button>
      <span class="pdf-preview__separator" />
      <button type="button" class="btn btn--ghost" :disabled="!ready || scale <= PDF_MIN_SCALE" :aria-label="t('workbench.office.zoomOut')" @click="zoom(-0.25)">−</button>
      <select class="pdf-preview__fit" :value="mode" :disabled="!ready" :aria-label="t('workbench.office.zoomMode')" @change="changeFit">
        <option value="width">{{ t('workbench.office.fitWidth') }}</option>
        <option value="page">{{ t('workbench.office.fitPage') }}</option>
        <option v-if="mode === 'manual'" value="manual">{{ Math.round(scale * 100) }}%</option>
      </select>
      <button type="button" class="btn btn--ghost" :disabled="!ready || scale >= PDF_MAX_SCALE" :aria-label="t('workbench.office.zoomIn')" @click="zoom(0.25)">+</button>
    </div>
    <p v-if="loading || rendering" class="pdf-preview__status" role="status">{{ t('chat.loadingPreview') }}</p>
    <div v-if="failed" class="pdf-preview__error" role="alert">
      <p>{{ t('workbench.artifactPreview.failedDetail') }}</p>
      <button type="button" class="btn btn--ghost" @click="start">{{ t('chat.retry') }}</button>
    </div>
    <div v-show="!failed" ref="viewport" class="pdf-preview__viewport" @scroll.passive="saveReading"><div ref="canvasHost" class="pdf-preview__canvas" /></div>
  </section>
</template>

<style scoped>
.pdf-preview { display: flex; flex-direction: column; width: 100%; height: 100%; min-height: 240px; min-width: 0; overflow: hidden; position: relative; }
.pdf-preview__toolbar { display: flex; align-items: center; justify-content: center; gap: 2px; flex-wrap: wrap; flex: none; padding: 8px; border-bottom: 1px solid var(--border); }
.pdf-preview__toolbar button { display: inline-flex; align-items: center; justify-content: center; min-width: 28px; padding: 5px; }
.pdf-preview__page, .pdf-preview__fit { height: 28px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg); color: var(--text); font: inherit; font-size: var(--fs-xs); }
.pdf-preview__page { width: 48px; padding: 2px 4px; text-align: center; }
.pdf-preview__fit { max-width: 128px; padding: 2px 4px; }
.pdf-preview__total { min-width: 30px; font-size: var(--fs-xs); color: var(--text-dim); }
.pdf-preview__separator { width: 1px; height: 16px; background: var(--border); margin: 0 6px; }
.pdf-preview__viewport { flex: 1; min-height: 0; min-width: 0; overflow: auto; overscroll-behavior: contain; padding: 16px; background: var(--bg-surface); }
.pdf-preview__canvas { display: flex; justify-content: safe center; width: max-content; min-width: 100%; }
.pdf-preview__canvas :deep(canvas) { display: block; flex: none; box-shadow: var(--shadow-md); }
.pdf-preview__status { position: absolute; top: 48px; left: 12px; margin: 0; padding: 4px 8px; background: var(--bg); border-radius: var(--radius-sm); font-size: var(--fs-xs); color: var(--text-dim); }
.pdf-preview__error { padding: 16px; font-size: var(--fs-sm); color: var(--text-dim); }
</style>
