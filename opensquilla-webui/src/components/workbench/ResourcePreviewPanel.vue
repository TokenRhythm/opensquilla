<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useVirtualizer } from '@tanstack/vue-virtual'
import { parseCsvPreview, type CsvPreview } from '@/workbench/csvPreview'
import { buildOfflineArtifactHtml, isOfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import type { ResourcePreviewKind } from '@/workbench/resourcePreview'
import { resourceReadingState, type ResourceReadingState } from './resourceReadingState'

const OfficePreviewPanel = defineAsyncComponent(() => import('./OfficePreviewPanel.vue'))
const PdfResourcePreview = defineAsyncComponent(() => import('./PdfResourcePreview.vue'))
const props = defineProps<{ blob: Blob; kind: ResourcePreviewKind; name: string; readingState?: ResourceReadingState }>()
const emit = defineEmits<{ 'reading-state': [state: ResourceReadingState, blob: Blob] }>()
const { t } = useI18n()
const data = shallowRef<ArrayBuffer | null>(null)
const url = ref(''), html = ref(''), error = ref(false), loading = ref(false)
const rows = shallowRef<string[][]>([]), truncated = ref(false)
const scroll = ref<HTMLElement | null>(null)
const media = ref<HTMLMediaElement | null>(null)
const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => ({
  count: rows.value.length, getScrollElement: () => scroll.value, estimateSize: () => 30,
  overscan: 10, initialRect: { width: 600, height: 600 },
})))
const columns = ref(1)
let generation = 0
let readingBlob: Blob | null = null
const readingSources = new WeakMap<ArrayBuffer, Blob>()
let cancelParsing: (() => void) | null = null
function saveReading() {
  if ((!media.value && !scroll.value) || readingBlob !== props.blob) return
  const state = resourceReadingState(props.readingState) || resourceReadingState({})!
  if (media.value && Number.isFinite(media.value.currentTime)) state.time = media.value.currentTime
  if (scroll.value) { state.top = scroll.value.scrollTop; state.left = scroll.value.scrollLeft }
  emit('reading-state', state, props.blob)
}
function saveDocumentReading(state: ResourceReadingState, source: ArrayBuffer) {
  const blob = readingSources.get(source)
  if (blob === props.blob) emit('reading-state', state, blob)
}
function restoreMedia() {
  const element = media.value, state = resourceReadingState(props.readingState)
  if (!element || !state || !Number.isFinite(element.duration)) return
  // Returning to a session restores position, never playback authorization.
  element.currentTime = Math.min(state.time, Math.max(0, element.duration))
}
function parseTable(source: string): Promise<CsvPreview> {
  const delimiter = props.name.toLowerCase().endsWith('.tsv') ? '\t' : ''
  if (typeof Worker === 'undefined') return Promise.resolve(parseCsvPreview(source, delimiter))
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../../workbench/csvPreviewWorker.ts', import.meta.url), { type: 'module' })
    let settled = false
    const finish = () => {
      if (settled) return false
      settled = true
      clearTimeout(timeout)
      worker.onmessage = null; worker.onerror = null
      worker.terminate()
      if (cancelParsing === cancel) cancelParsing = null
      return true
    }
    const timeout = setTimeout(() => { finish(); reject(new Error('Table preview timed out')) }, 15000)
    const cancel = () => { if (finish()) reject(new Error('Preview cancelled')) }
    cancelParsing = cancel
    worker.onmessage = (event: MessageEvent<{ result?: CsvPreview }>) => {
      if (!finish()) return
      if (event.data.result) resolve(event.data.result); else reject(new Error('Invalid table'))
    }
    worker.onerror = () => { finish(); reject(new Error('Table preview failed')) }
    worker.postMessage({ source, delimiter })
  })
}
function release() {
  generation++
  readingBlob = null
  cancelParsing?.()
  if (media.value) {
    media.value.pause()
    media.value.removeAttribute('src')
    media.value.load()
  }
  if (url.value) URL.revokeObjectURL(url.value)
  url.value = ''; data.value = null; html.value = ''; rows.value = []; error.value = false
}
async function loadPreview() {
  release()
  const current = generation
  const blob = props.blob
  loading.value = true
  try {
    if (isOfficePreviewFormat(props.kind) || props.kind === 'pdf') {
      const bytes = await props.blob.arrayBuffer()
      if (current === generation) { readingSources.set(bytes, blob); data.value = bytes }
    } else if (props.kind === 'html') {
      const source = await props.blob.text()
      if (current === generation) html.value = buildOfflineArtifactHtml(source)
    } else if (props.kind === 'csv') {
      const source = await props.blob.text()
      if (current !== generation) return
      const parsed = await parseTable(source)
      if (current !== generation) return
      rows.value = parsed.rows; columns.value = parsed.columns; truncated.value = parsed.truncated
    } else {
      if (current === generation) url.value = URL.createObjectURL(props.blob)
    }
  } catch { if (current === generation) error.value = true }
  finally {
    if (current === generation) {
      readingBlob = blob
      loading.value = false
      await nextTick()
      const saved = resourceReadingState(props.readingState)
      if (current === generation && scroll.value && saved) {
        scroll.value.scrollTop = saved.top; scroll.value.scrollLeft = saved.left
      }
    }
  }
}
watch(() => [props.blob, props.kind, props.name] as const, loadPreview, { immediate: true })
onBeforeUnmount(() => { saveReading(); release() })
function onMediaError(event: Event) {
  if (!url.value || (event.target as HTMLImageElement | HTMLMediaElement | null)?.src !== url.value) return
  // Dispose before the error view removes the media ref. Retry then creates a
  // fresh URL/element, and late decoder events cannot fail that new attempt.
  release()
  loading.value = false
  error.value = true
}
</script>

<template>
  <section class="resource-preview" :data-preview-kind="kind">
    <div v-if="error" class="resource-preview__error" role="alert">
      <p>{{ t('workbench.artifactPreview.failedDetail') }}</p>
      <button type="button" class="btn btn--ghost" @click="loadPreview">{{ t('chat.retry') }}</button>
    </div>
    <p v-else-if="loading" role="status">{{ t('chat.loadingPreview') }}</p>
    <OfficePreviewPanel v-else-if="isOfficePreviewFormat(kind) && data" :data="data" :format="kind" :reading-state="readingState" @reading-state="saveDocumentReading" />
    <PdfResourcePreview v-else-if="kind === 'pdf' && data" :data="data" :name="name" :reading-state="readingState" @reading-state="saveDocumentReading" />
    <img v-else-if="kind === 'image' && url" :key="url" :src="url" :alt="name" decoding="async" @error="onMediaError" />
    <iframe v-else-if="kind === 'html' && html" class="html-preview-frame" :srcdoc="html" :title="name" sandbox="allow-scripts" referrerpolicy="no-referrer" />
    <audio v-else-if="kind === 'audio' && url" :key="url" ref="media" :src="url" controls preload="metadata" @error="onMediaError" @loadedmetadata="restoreMedia" @timeupdate="saveReading" @seeked="saveReading" />
    <video v-else-if="kind === 'video' && url" :key="url" ref="media" :src="url" controls preload="metadata" @error="onMediaError" @loadedmetadata="restoreMedia" @timeupdate="saveReading" @seeked="saveReading" />
    <template v-else-if="kind === 'csv'">
      <p v-if="truncated" class="resource-preview__notice" role="status">{{ t('workbench.workspaceFiles.tableTruncated') }}</p>
      <div ref="scroll" class="resource-preview__table" role="table" :aria-label="name" :aria-rowcount="rows.length" :aria-colcount="columns" @scroll.passive="saveReading">
        <div :style="{ height: `${virtualizer.getTotalSize()}px`, position: 'relative', minWidth: `${columns * 140 + 48}px` }">
          <div v-for="row in virtualizer.getVirtualItems()" :key="String(row.key)" class="resource-preview__row" role="row" :aria-rowindex="row.index + 1" :style="{ transform: `translateY(${row.start}px)` }">
            <span class="resource-preview__number" role="rowheader">{{ row.index + 1 }}</span>
            <span v-for="(cell, index) in rows[row.index]" :key="index" role="cell" :title="cell">{{ cell }}</span>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped src="@/styles/html-preview-frame.css"></style>
<style scoped>
.resource-preview { display: flex; flex: 1; flex-direction: column; min-height: 0; min-width: 0; overflow: auto; background: var(--bg-surface); }
.resource-preview > iframe { border: 0; width: 100%; flex: 1; min-height: 300px; }
.resource-preview > img, .resource-preview > video { max-width: 100%; max-height: 100%; object-fit: contain; margin: auto; }
.resource-preview > audio { max-width: 100%; margin: auto; }
.resource-preview > p { padding: var(--sp-3); color: var(--text-muted); }
.resource-preview__error { padding: var(--sp-3); color: var(--text-muted); }
.resource-preview__error p { margin: 0 0 var(--sp-2); }
.resource-preview__table { flex: 1; min-height: 0; overflow: auto; font-size: 12px; }
.resource-preview__row { position: absolute; display: flex; height: 30px; min-width: 100%; border-bottom: 1px solid var(--border); }
.resource-preview__row > span { width: 140px; flex-shrink: 0; padding: 6px 8px; box-sizing: border-box; white-space: nowrap; text-overflow: ellipsis; overflow: hidden; border-right: 1px solid var(--border); }
.resource-preview__row > .resource-preview__number { width: 48px; color: var(--text-muted); background: var(--bg-hover); text-align: right; }
.resource-preview__notice { margin: 0; font-size: var(--fs-sm); }
</style>
