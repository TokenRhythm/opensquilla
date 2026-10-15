<template>
  <div class="workspace-files" data-testid="workspace-files-panel">
    <aside class="workspace-files__tree" aria-label="Workspace files">
      <div class="workspace-files__tree-toolbar">
        <label class="workspace-files__filter">
          <span class="sr-only">{{ t('workbench.workspaceFiles.filter') }}</span>
          <input v-model="filter" type="search" :placeholder="t('workbench.workspaceFiles.filterPlaceholder')" />
        </label>
        <button type="button" class="workspace-files__refresh" :disabled="loading" @click="refresh">
          {{ t('workbench.workspaceFiles.refresh') }}
        </button>
      </div>
      <p v-if="treeError" class="workspace-files__message" role="alert">{{ treeError }}</p>
      <p v-else-if="loading && !flatNodes.length" class="workspace-files__message" role="status">{{ t('workbench.workspaceFiles.loading') }}</p>
      <div ref="treeScroll" class="workspace-files__tree-scroll" role="tree" @scroll="virtualizer.measure">
        <div class="workspace-files__tree-spacer" :style="{ height: `${virtualizer.getTotalSize()}px` }">
          <div
            v-for="row in virtualRows"
            :key="row.key"
            class="workspace-files__row"
            :class="{ 'is-selected': row.entry?.path === selected?.path, 'is-more': row.kind === 'more' }"
            :style="{ transform: `translateY(${row.start}px)` }"
            role="treeitem"
            :aria-level="row.depth + 1"
            :aria-expanded="row.entry?.type === 'directory' ? expanded.has(row.entry.path) : undefined"
            @click="row.kind === 'more' ? loadMore(row.directoryPath) : row.entry && activate(row.entry)"
          >
            <template v-if="row.kind === 'more'">
              <span class="workspace-files__indent" :style="{ width: `${row.depth * 16}px` }" />
              <span>{{ t('workbench.workspaceFiles.loadMore') }}</span>
            </template>
            <template v-else-if="row.entry">
              <span class="workspace-files__indent" :style="{ width: `${row.depth * 16}px` }" />
              <button
                v-if="row.entry.type === 'directory'"
                type="button"
                class="workspace-files__disclosure"
                :aria-label="expanded.has(row.entry.path) ? t('workbench.workspaceFiles.collapse') : t('workbench.workspaceFiles.expand')"
                @click.stop="toggle(row.entry)"
              >{{ expanded.has(row.entry.path) ? '▾' : '▸' }}</button>
              <span v-else class="workspace-files__disclosure" aria-hidden="true">·</span>
              <span class="workspace-files__name" :title="row.entry.path">{{ row.entry.name }}</span>
              <small v-if="row.entry.type === 'file'" class="workspace-files__size">{{ formatSize(row.entry.size) }}</small>
            </template>
          </div>
        </div>
      </div>
      <p v-if="truncated" class="workspace-files__hint">{{ t('workbench.workspaceFiles.truncated') }}</p>
    </aside>
    <section class="workspace-files__preview" aria-label="File preview">
      <div v-if="selected" class="workspace-files__preview-title" :title="selected.path">{{ selected.path }}</div>
      <WorkspaceFilePanel
        v-if="selected?.kind === 'text'"
        v-bind="previewState"
        @workbench-event="handlePreviewEvent"
      />
      <div v-else-if="selected" class="workspace-files__binary-preview">
        <img v-if="imageUrl" :src="imageUrl" :alt="selected.name" />
        <p v-else-if="previewState.loading" role="status">{{ t('workbench.workspaceFiles.loading') }}</p>
        <p v-else>{{ t('workspaceReference.unsupported') }} · {{ selected.mime || selected.kind }}</p>
      </div>
      <p v-else class="workspace-files__empty">{{ t('workbench.workspaceFiles.selectFile') }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useVirtualizer } from '@tanstack/vue-virtual'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import { WORKSPACE_FILES_KEY, type WorkspaceDirectoryEntry, type WorkspaceFile, type WorkspaceFilePage } from '@/modules/workspaceFiles'
import { copyTextWithFallback } from '@/utils/browser'

const props = defineProps<{ sessionKey: string }>()
const { t } = useI18n()
const files = inject(WORKSPACE_FILES_KEY, null)
const treeScroll = ref<HTMLElement | null>(null)
const filter = ref('')
const treeError = ref('')
const selected = ref<WorkspaceDirectoryEntry | null>(null)
const binding = ref('')
const expanded = reactive(new Set<string>())
interface DirectoryState {
  entries: WorkspaceDirectoryEntry[]
  nextCursor: string | null
  hasMore: boolean
  truncated: boolean
  loading: boolean
}
const directories = reactive(new Map<string, DirectoryState>())
const loading = computed(() => [...directories.values()].some(state => state.loading))
const previewState = reactive<{
  snapshot: WorkspaceFilePage & { paged: true; focusLine?: number } | null
  viewId: number
  loading: boolean
  errorKey: string
  searchStatus: 'idle' | 'searching' | 'found' | 'not-found'
  searchErrorKey: string
  searchQuery: string
  copying: boolean
  copied: boolean
  copyErrorKey: string
}>({ snapshot: null, viewId: 0, loading: false, errorKey: '', searchStatus: 'idle', searchErrorKey: '', searchQuery: '', copying: false, copied: false, copyErrorKey: '' })
const imageUrl = ref('')
let directoryRequest = new AbortController()
interface PreviewRequest {
  controller: AbortController
  sessionKey: string
  kind: 'load' | 'search'
}
let previewRequest: PreviewRequest | null = null
let copyRequest: AbortController | null = null
let copyTimer: ReturnType<typeof setTimeout> | null = null

function cancelPreview() {
  previewRequest?.controller.abort()
  previewRequest = null
}

function beginPreview(kind: PreviewRequest['kind']): PreviewRequest {
  cancelPreview()
  const request = { controller: new AbortController(), sessionKey: props.sessionKey, kind }
  previewRequest = request
  previewState.loading = false
  previewState.errorKey = ''
  previewState.searchStatus = 'idle'
  previewState.searchErrorKey = ''
  return request
}

function isCurrentPreview(request: PreviewRequest) {
  // A transport may still settle after abort; only the active view may consume it.
  return previewRequest === request && !request.controller.signal.aborted && props.sessionKey === request.sessionKey
}

function cancelCopy() {
  copyRequest?.abort()
  copyRequest = null
  if (copyTimer !== null) clearTimeout(copyTimer)
  copyTimer = null
  previewState.copying = false
  previewState.copied = false
  previewState.copyErrorKey = ''
}

function resetPreview() {
  cancelPreview()
  cancelCopy()
  releaseImage()
  previewState.viewId++
  previewState.snapshot = null
  previewState.loading = false
  previewState.errorKey = ''
  previewState.searchErrorKey = ''
  previewState.searchStatus = 'idle'
  previewState.searchQuery = ''
}

const flatNodes = computed(() => {
  const rows: Array<{ key: string; kind: 'entry' | 'more'; entry?: WorkspaceDirectoryEntry; depth: number; directoryPath: string }> = []
  const query = filter.value.trim().toLowerCase()
  const append = (path: string, depth: number) => {
    const state = directories.get(path)
    if (!state) return
    for (const entry of state.entries) {
      const matches = !query || entry.path.toLowerCase().includes(query)
      const isExpanded = entry.type === 'directory' && expanded.has(entry.path)
      if (!matches && !isExpanded) continue
      const rowIndex = rows.length
      rows.push({ key: `entry:${entry.path}`, kind: 'entry', entry, depth, directoryPath: path })
      if (isExpanded) append(entry.path, depth + 1)
      // Keep ancestors of visible matches or pagination, using only loaded directories.
      if (!matches && rows.length === rowIndex + 1) rows.pop()
    }
    if (state.hasMore) rows.push({ key: `more:${path}`, kind: 'more', depth, directoryPath: path })
  }
  append('', 0)
  return rows
})
const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => ({
  count: flatNodes.value.length,
  getScrollElement: () => treeScroll.value,
  estimateSize: () => 32,
  initialRect: { width: 320, height: 600 },
  overscan: 8,
  getItemKey: (index: number) => flatNodes.value[index]?.key || index,
})))
const virtualRows = computed(() => virtualizer.value.getVirtualItems().map(item => ({
  ...flatNodes.value[item.index]!, start: item.start,
})))
const truncated = computed(() => [...directories.values()].some(state => state.truncated))

function formatSize(size: number | null) {
  if (size === null) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function fileFor(entry: WorkspaceDirectoryEntry): WorkspaceFile {
  return {
    requestedPath: entry.path,
    path: entry.path,
    name: entry.name,
    mime: entry.mime,
    size: entry.size || 0,
    kind: entry.kind === 'directory' ? 'download' : entry.kind,
    workspaceBinding: binding.value,
    textPaging: entry.kind === 'text',
  }
}

function releaseImage() {
  if (imageUrl.value) URL.revokeObjectURL(imageUrl.value)
  imageUrl.value = ''
}

async function loadDirectory(path: string, append = false) {
  if (!files?.listDir || !props.sessionKey) {
    treeError.value = t('workbench.workspaceFiles.unavailable')
    return
  }
  const current = directories.get(path)
  if (current?.loading) return
  const request = directoryRequest
  const sessionKey = props.sessionKey
  const state: DirectoryState = current || reactive({ entries: [] as WorkspaceDirectoryEntry[], nextCursor: null, hasMore: false, truncated: false, loading: false })
  directories.set(path, state)
  state.loading = true
  if (!append) state.entries = []
  const isCurrent = () => directoryRequest === request && !request.signal.aborted
    && props.sessionKey === sessionKey && directories.get(path) === state
  try {
    const page = await files.listDir(sessionKey, path, append ? state.nextCursor || '0' : '0', 200, binding.value, request.signal)
    if (!isCurrent()) return
    binding.value = page.workspaceBinding
    state.entries = append ? [...state.entries, ...page.entries] : page.entries
    state.nextCursor = page.nextCursor
    state.hasMore = page.hasMore
    state.truncated = page.truncated
    treeError.value = ''
  } catch {
    if (!isCurrent()) return
    if (!append) directories.delete(path)
    treeError.value = t('workbench.workspaceFiles.unavailable')
  } finally {
    if (isCurrent()) state.loading = false
  }
}

function loadMore(path: string) { void loadDirectory(path, true) }

async function toggle(entry: WorkspaceDirectoryEntry) {
  if (entry.type !== 'directory') return
  if (expanded.has(entry.path)) { expanded.delete(entry.path); return }
  expanded.add(entry.path)
  if (!directories.has(entry.path)) await loadDirectory(entry.path)
}

async function activate(entry: WorkspaceDirectoryEntry) {
  if (entry.type === 'directory') { await toggle(entry); return }
  selected.value = entry
  resetPreview()
  const request = beginPreview('load')
  if (entry.kind === 'image' && files?.read) {
    previewState.loading = true
    try {
      const blob = await files.read(request.sessionKey, fileFor(entry), request.controller.signal)
      if (isCurrentPreview(request)) imageUrl.value = URL.createObjectURL(blob)
    } catch {
      if (isCurrentPreview(request)) previewState.errorKey = 'workbench.workspaceFiles.unavailable'
    } finally {
      if (isCurrentPreview(request)) previewState.loading = false
    }
    return
  }
  if (entry.kind !== 'text' || !files?.readPage) {
    previewState.errorKey = 'workspaceReference.unsupported'
    return
  }
  previewState.loading = true
  try {
    const page = await files.readPage(request.sessionKey, fileFor(entry), 1, 200, request.controller.signal)
    if (isCurrentPreview(request)) previewState.snapshot = { ...page, paged: true }
  } catch {
    if (isCurrentPreview(request)) previewState.errorKey = 'workbench.workspaceFiles.unavailable'
  } finally {
    if (isCurrentPreview(request)) previewState.loading = false
  }
}

async function handlePreviewEvent(event: { type: string; payload?: unknown }) {
  const entry = selected.value
  const value = event.payload && typeof event.payload === 'object' ? event.payload as Record<string, unknown> : {}
  if (!entry || entry.type !== 'file' || entry.kind !== 'text' || !files) return
  const file = fileFor(entry)
  if (event.type === 'workspace-file-page') {
    const start = Number(value.startLine)
    if (!Number.isSafeInteger(start) || start < 1 || !files.readPage) return
    const request = beginPreview('load')
    previewState.loading = true
    try {
      const page = await files.readPage(request.sessionKey, file, start, start + 199, request.controller.signal)
      if (isCurrentPreview(request)) previewState.snapshot = { ...page, paged: true, focusLine: Number(value.focusLine) || undefined }
    } catch {
      if (isCurrentPreview(request)) previewState.errorKey = 'workbench.workspaceFiles.unavailable'
    } finally {
      if (isCurrentPreview(request)) previewState.loading = false
    }
  } else if (event.type === 'workspace-file-search' && typeof value.query === 'string' && files.search && files.readPage) {
    const request = beginPreview('search')
    previewState.searchStatus = 'searching'
    previewState.searchQuery = value.query.trim()
    try {
      const result = await files.search(request.sessionKey, file, value.query, request.controller.signal)
      if (!isCurrentPreview(request)) return
      if (result.matchLine === null) { previewState.searchStatus = 'not-found'; return }
      const start = Math.floor((result.matchLine - 1) / 200) * 200 + 1
      const page = await files.readPage(request.sessionKey, file, start, start + 199, request.controller.signal)
      if (!isCurrentPreview(request)) return
      previewState.snapshot = { ...page, paged: true, focusLine: result.matchLine }
      previewState.searchStatus = 'found'
    } catch {
      if (isCurrentPreview(request)) {
        previewState.searchErrorKey = 'workbench.workspaceFiles.unavailable'
        previewState.searchStatus = 'idle'
      }
    }
  } else if (event.type === 'workspace-file-search-cancel') {
    if (previewRequest?.kind === 'search') cancelPreview()
    previewState.searchStatus = 'idle'; previewState.searchQuery = ''; previewState.searchErrorKey = ''
  } else if (event.type === 'workspace-file-copy' && files.read) {
    cancelCopy()
    const request = new AbortController()
    const sessionKey = props.sessionKey
    copyRequest = request
    const isCurrent = () => copyRequest === request && !request.signal.aborted && props.sessionKey === sessionKey
    previewState.copying = true; previewState.copyErrorKey = ''
    try {
      const blob = await files.read(sessionKey, file, request.signal)
      if (!isCurrent()) return
      const content = await blob.text()
      if (!isCurrent()) return
      await copyTextWithFallback(content)
      if (!isCurrent()) return
      previewState.copied = true
      copyTimer = setTimeout(() => { if (isCurrent()) previewState.copied = false }, 1600)
    } catch {
      if (isCurrent()) previewState.copyErrorKey = 'workspaceReference.copyFailed'
    } finally {
      if (isCurrent()) previewState.copying = false
    }
  }
}

async function refresh() {
  directoryRequest.abort()
  directoryRequest = new AbortController()
  resetPreview()
  selected.value = null
  expanded.clear()
  binding.value = ''
  directories.clear()
  treeError.value = ''
  await loadDirectory('')
}

watch(() => props.sessionKey, () => { void refresh() }, { flush: 'sync' })
onMounted(() => { void refresh(); void nextTick(() => virtualizer.value.measure()) })
onBeforeUnmount(() => { directoryRequest.abort(); resetPreview() })
</script>

<style scoped>
.workspace-files { display: grid; grid-template-columns: minmax(14rem, 28%) minmax(0, 1fr); height: 100%; min-width: 0; color: var(--text); background: var(--bg-surface); }
.workspace-files__tree { display: flex; min-height: 0; flex-direction: column; border-right: 1px solid var(--border); }
.workspace-files__tree-toolbar { display: flex; gap: var(--sp-2); padding: var(--sp-2); border-bottom: 1px solid var(--border); }
.workspace-files__filter { min-width: 0; flex: 1; }
.workspace-files__filter input { width: 100%; min-width: 0; box-sizing: border-box; padding: .35rem .5rem; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-hover); color: var(--text); font: inherit; font-size: var(--fs-sm); }
.workspace-files__refresh, .workspace-files__disclosure { border: 0; background: transparent; color: var(--text); font: inherit; cursor: pointer; }
.workspace-files__refresh { padding: .2rem .35rem; font-size: var(--fs-sm); }
.workspace-files__tree-scroll { min-height: 0; flex: 1; overflow: auto; }
.workspace-files__tree-spacer { position: relative; min-height: 100%; }
.workspace-files__row { position: absolute; inset-inline: 0; display: flex; align-items: center; min-width: max-content; height: 32px; padding-inline: var(--sp-2); box-sizing: border-box; gap: .25rem; cursor: pointer; }
.workspace-files__row:hover, .workspace-files__row.is-selected { background: var(--bg-hover); }
.workspace-files__row.is-more { color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-files__indent { flex: 0 0 auto; }
.workspace-files__disclosure { width: 1rem; padding: 0; text-align: center; color: var(--text-muted); }
.workspace-files__name { max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-files__size { margin-left: auto; padding-left: var(--sp-2); color: var(--text-muted); font-size: .72rem; }
.workspace-files__message, .workspace-files__hint, .workspace-files__empty { margin: 0; padding: var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-files__preview { display: flex; min-width: 0; min-height: 0; flex-direction: column; }
.workspace-files__preview-title { flex: 0 0 auto; padding: var(--sp-2) var(--sp-3); overflow: hidden; border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: var(--fs-sm); text-overflow: ellipsis; white-space: nowrap; }
.workspace-files__preview > .workspace-file { min-height: 0; flex: 1; }
.workspace-files__binary-preview { display: flex; min-height: 0; flex: 1; align-items: center; justify-content: center; overflow: auto; padding: var(--sp-4); }
.workspace-files__binary-preview img { max-width: 100%; max-height: 100%; object-fit: contain; }
.workspace-files__binary-preview p { color: var(--text-muted); font-size: var(--fs-sm); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 700px) { .workspace-files { grid-template-columns: minmax(11rem, 38%) minmax(0, 1fr); } .workspace-files__size { display: none; } }
</style>
