<template>
  <div class="workspace-files" :class="{ 'is-tree-hidden': !treeVisible }" data-testid="workspace-files-panel">
    <aside v-show="treeVisible" class="workspace-files__tree" :aria-label="t('workbench.newPanel.workspaceFiles')">
      <div class="workspace-files__tree-toolbar">
        <div class="workspace-files__filter">
          <span class="workspace-files__filter-icon" aria-hidden="true" v-html="getIconSvg('search', 14)" />
          <input ref="filterInput" v-model="filter" class="workspace-files__filter-input" type="search" :placeholder="t('workbench.workspaceFiles.filterPlaceholder')"
            :aria-label="t('workbench.workspaceFiles.filterPlaceholder')" :aria-describedby="filterHelpId" :title="t('workbench.workspaceFiles.filter')"
            autocomplete="off" spellcheck="false" @keydown="onFilterKeydown" />
          <button v-if="filter" type="button" class="workspace-files__filter-clear"
            :aria-label="t('workbench.workspaceFiles.clearFilter')" :title="t('workbench.workspaceFiles.clearFilter')"
            @pointerdown.prevent @click="clearFilter"><span aria-hidden="true" v-html="getIconSvg('x', 12)" /></button>
          <span :id="filterHelpId" class="sr-only">{{ t('workbench.workspaceFiles.filter') }}</span>
        </div>
        <button type="button" class="workspace-files__refresh" :disabled="loading" :aria-label="t('workbench.workspaceFiles.refresh')" :title="t('workbench.workspaceFiles.refresh')" @click="refresh">
          <span v-html="getIconSvg('refresh', 14)" />
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
            :class="{ 'is-selected': Boolean(row.entry && row.entry.path === selected?.path), 'is-more': row.kind === 'more' }"
            :style="{ transform: `translateY(${row.start}px)` }"
            role="treeitem"
            tabindex="0"
            :aria-selected="Boolean(row.entry && row.entry.path === selected?.path)"
            @keydown.enter.prevent="row.kind === 'more' ? loadMore(row.directoryPath) : row.entry && activate(row.entry)"
            @keydown.space.prevent="row.kind === 'more' ? loadMore(row.directoryPath) : row.entry && activate(row.entry)"
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
              ><span v-html="getIconSvg(expanded.has(row.entry.path) ? 'chevronDown' : 'chevronRight', 12)" /></button>
              <span v-else class="workspace-files__disclosure" aria-hidden="true" />
              <span class="workspace-files__icon" :class="{ 'is-folder': row.entry.type === 'directory' }" aria-hidden="true" v-html="getIconSvg(row.entry.type === 'directory' ? 'folder' : row.entry.kind === 'image' ? 'image' : row.entry.kind === 'text' ? 'fileCode' : 'fileText', 14)" />
              <span class="workspace-files__name" :title="row.entry.path">{{ row.entry.name }}</span>

            </template>
          </div>
        </div>
      </div>
      <p v-if="truncated" class="workspace-files__hint">{{ t('workbench.workspaceFiles.truncated') }}</p>
    </aside>
    <section class="workspace-files__preview" aria-label="File preview">
      <div class="workspace-files__preview-title">
        <button type="button" class="workspace-files__refresh" :aria-label="t('workbench.newPanel.workspaceFiles')" :title="t('workbench.newPanel.workspaceFiles')" :aria-expanded="treeVisible" @click="treeVisible = !treeVisible"><span v-html="getIconSvg('folder', 16)" /></button>
        <span v-if="selected" class="workspace-files__breadcrumb" :title="selected.path"><span class="workspace-files__parent">{{ selected.path.slice(0, -selected.name.length) }}</span><strong>{{ selected.name }}</strong></span>
        <span v-else class="workspace-files__breadcrumb">{{ t('workbench.newPanel.workspaceFiles') }}</span>
        <small v-if="selected" class="workspace-files__size">{{ formatSize(selected.size) }}</small>
        <WorkspaceResourceActions v-if="resolvedFile" :session-key="sessionKey" :file="resolvedFile" />
        <WorkspaceResourceActions v-else-if="root" :session-key="sessionKey" :file="null" :root="root" />
        <button v-if="selected && ['html', 'csv'].includes(previewKind)" type="button" class="workspace-files__mode" @click="toggleSource">{{ t(sourceMode ? 'workspaceReference.preview' : 'workspaceReference.source') }}</button>
      </div>
      <div v-if="previewState.errorKey === 'workbench.workspaceFiles.changed'" class="workspace-files__message" role="alert">
        {{ t(previewState.errorKey) }}
        <button type="button" class="workspace-files__mode" @click="selected && activate(selected, sourceMode)">{{ t('workbench.workspaceFiles.refresh') }}</button>
      </div>
      <WorkspaceFilePanel
        v-if="selected && (previewKind === 'source' || sourceMode)"
        v-bind="previewState"
        :mime="resolvedFile?.mime || selected.mime"
        @workbench-event="handlePreviewEvent"
      />
      <ResourcePreviewPanel v-else-if="selected && previewBlob" :key="previewState.viewId" :blob="previewBlob" :kind="previewKind" :name="selected.name" />
      <div v-else-if="selected" class="workspace-files__binary-preview">
        <p v-if="previewState.loading" role="status">{{ t('workbench.workspaceFiles.loading') }}</p>
        <p v-else-if="previewState.errorKey" role="alert">{{ t(previewState.errorKey) }}</p>
        <p v-else>{{ t('workbench.artifactPreview.unsupported') }} · {{ selected.mime || selected.kind }}</p>
      </div>
      <div v-else-if="rootError" class="workspace-files__empty" role="alert">
        <p>{{ t('workbench.workspaceFiles.rootUnavailable') }}</p>
        <button type="button" class="workspace-files__mode" @click="refresh">{{ t('workbench.workspaceFiles.retryRoot') }}</button>
      </div>
      <p v-else class="workspace-files__empty">{{ t('workbench.workspaceFiles.selectFile') }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useVirtualizer } from '@tanstack/vue-virtual'
import WorkspaceFilePanel from './WorkspaceFilePanel.vue'
import ResourcePreviewPanel from './ResourcePreviewPanel.vue'
import WorkspaceResourceActions from './WorkspaceResourceActions.vue'
import { resourcePreviewKind, resourcePreviewLimit } from '@/workbench/resourcePreview'
import { completeWorkspaceMarkdown, isWorkspaceMarkdown, WORKSPACE_MARKDOWN_PREVIEW_LIMIT } from '@/workbench/workspaceTextPreview'
import { WORKSPACE_FILES_KEY, WorkspaceFilesChangedError, WorkspaceTextLimitError, type WorkspaceDirectoryEntry, type WorkspaceFile, type WorkspaceFilePage, type WorkspaceRoot } from '@/modules/workspaceFiles'
import { getIconSvg } from '@/utils/icons'
import { copyTextWithFallback } from '@/utils/browser'

const props = defineProps<{ sessionKey: string }>()
const { t } = useI18n()
const files = inject(WORKSPACE_FILES_KEY, null)
const treeScroll = ref<HTMLElement | null>(null)
const filter = ref('')
const filterInput = ref<HTMLInputElement | null>(null)
const filterHelpId = useId()
const treeVisible = ref(true)
const loading = computed(() => [...directories.values()].some(state => state.loading))
const treeError = ref('')
const selected = ref<WorkspaceDirectoryEntry | null>(null)
const resolvedFile = shallowRef<WorkspaceFile | null>(null)
const root = shallowRef<WorkspaceRoot | null>(null)
const rootError = ref(false)
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
const previewState = reactive<{
  snapshot: WorkspaceFilePage & { paged?: boolean; focusLine?: number } | null
  viewId: number
  loading: boolean
  errorKey: string
  previewErrorKey: string
  searchStatus: 'idle' | 'searching' | 'found' | 'not-found'
  searchErrorKey: string
  searchQuery: string
  copying: boolean
  copied: boolean
  copyErrorKey: string
}>({ snapshot: null, viewId: 0, loading: false, errorKey: '', previewErrorKey: '', searchStatus: 'idle', searchErrorKey: '', searchQuery: '', copying: false, copied: false, copyErrorKey: '' })
const previewBlob = shallowRef<Blob | null>(null)
const sourceMode = ref(false)
const previewKind = computed(() => selected.value ? resourcePreviewKind(selected.value) : 'unsupported')
interface PreviewRequest {
  controller: AbortController
  sessionKey: string
  kind: 'load' | 'search'
}
let previewRequest: PreviewRequest | null = null
let directoryRequest = new AbortController()
let copyRequest: AbortController | null = null
let copyTimer: ReturnType<typeof setTimeout> | undefined
let rootAbort: AbortController | null = null

const flatNodes = computed(() => {
  const rows: Array<{ key: string; kind: 'entry' | 'more'; entry?: WorkspaceDirectoryEntry; depth: number; directoryPath: string }> = []
  const query = filter.value.trim().toLowerCase()
  const matchesLoaded = (entry: WorkspaceDirectoryEntry): boolean => entry.path.toLowerCase().includes(query)
    || (entry.type === 'directory' && Boolean(directories.get(entry.path)?.hasMore || directories.get(entry.path)?.entries.some(matchesLoaded)))
  const append = (path: string, depth: number) => {
    const state = directories.get(path)
    if (!state) return
    for (const entry of state.entries) {
      if (query && !matchesLoaded(entry)) continue
      rows.push({ key: `entry:${entry.path}`, kind: 'entry', entry, depth, directoryPath: path })
      if (entry.type === 'directory' && (expanded.has(entry.path) || Boolean(query))) append(entry.path, depth + 1)
    }
    if (state.hasMore) rows.push({ key: `more:${path}`, kind: 'more', depth, directoryPath: path })
  }
  append('', 0)
  return rows
})
const virtualizer = useVirtualizer<HTMLElement, HTMLElement>(computed(() => ({
  count: flatNodes.value.length,
  getScrollElement: () => treeScroll.value,
  estimateSize: () => 28,
  initialRect: { width: 320, height: 600 },
  overscan: 8,
  getItemKey: (index: number) => flatNodes.value[index]?.key || index,
})))
const virtualRows = computed(() => virtualizer.value.getVirtualItems().map(item => ({
  ...flatNodes.value[item.index]!, start: item.start,
})))
const truncated = computed(() => [...directories.values()].some(state => state.truncated))

function clearFilter() {
  filter.value = ''
  filterInput.value?.focus({ preventScroll: true })
}
function onFilterKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || !filter.value) return
  event.preventDefault()
  event.stopPropagation()
  clearFilter()
}

function formatSize(size: number | null) {
  if (size === null) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function fileFor(entry: WorkspaceDirectoryEntry): WorkspaceFile {
  if (resolvedFile.value?.path === entry.path) return resolvedFile.value
  return {
    requestedPath: entry.path,
    path: entry.path,
    name: entry.name,
    mime: entry.mime,
    size: entry.size || 0,
    kind: entry.kind === 'directory' ? 'download' : entry.kind,
    workspaceBinding: binding.value,
    textPaging: entry.kind === 'text',
    revision: entry.revision,
    resource: entry.resource,
  }
}

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
  return previewRequest === request && !request.controller.signal.aborted && props.sessionKey === request.sessionKey
}

function cancelCopy() {
  copyRequest?.abort()
  copyRequest = null
  clearTimeout(copyTimer)
  copyTimer = undefined
  previewState.copying = false
  previewState.copied = false
  previewState.copyErrorKey = ''
}

function resetPreview() {
  cancelPreview()
  cancelCopy()
  previewBlob.value = null
  previewState.viewId++
  previewState.snapshot = null
  previewState.loading = false
  previewState.errorKey = ''
  previewState.previewErrorKey = ''
  previewState.searchErrorKey = ''
  previewState.searchStatus = 'idle'
  previewState.searchQuery = ''
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

async function activate(entry: WorkspaceDirectoryEntry, asSource = false) {
  if (entry.type === 'directory') { await toggle(entry); return }
  selected.value = entry
  sourceMode.value = asSource
  resetPreview()
  resolvedFile.value = null
  if (!files) return
  const request = beginPreview('load')
  previewState.loading = true
  try {
    const matches = await files.resolve(request.sessionKey, [entry.path], request.controller.signal)
    if (!isCurrentPreview(request)) return
    const match = matches.find(file => file.path === entry.path && file.workspaceBinding === binding.value)
    if (!match) throw new Error('File unavailable')
    resolvedFile.value = match
  } catch (error) {
    if (isCurrentPreview(request)) { previewState.errorKey = fileError(error); previewState.loading = false }
    return
  }
  if (!asSource && entry.kind === 'text' && isWorkspaceMarkdown(entry)) {
    if ((entry.size || 0) <= WORKSPACE_MARKDOWN_PREVIEW_LIMIT) {
      try {
        const blob = await files.read(request.sessionKey, fileFor(entry), request.controller.signal)
        const snapshot = await completeWorkspaceMarkdown(entry.path, blob)
        if (!isCurrentPreview(request)) return
        previewState.snapshot = snapshot
        previewState.loading = false
        return
      } catch (error) {
        if (!isCurrentPreview(request)) return
        if (error instanceof WorkspaceFilesChangedError) {
          previewState.errorKey = fileError(error); previewState.loading = false; return
        }
        previewState.previewErrorKey = error instanceof Error && error.message === 'workbench.artifactPreview.tooLarge'
          ? error.message : 'workbench.artifactPreview.unsupported'
      }
    } else previewState.previewErrorKey = 'workbench.artifactPreview.tooLarge'
  }
  if (!asSource && previewKind.value !== 'source' && previewKind.value !== 'unsupported' && files?.read) {
    previewState.loading = true
    try {
      if ((entry.size || 0) > resourcePreviewLimit(previewKind.value)) throw new Error('workbench.artifactPreview.tooLarge')
      const blob = await files.read(request.sessionKey, fileFor(entry), request.controller.signal)
      if (blob.size > resourcePreviewLimit(previewKind.value)) throw new Error('workbench.artifactPreview.tooLarge')
      if (isCurrentPreview(request)) previewBlob.value = blob
    } catch (error) {
      if (!isCurrentPreview(request)) return
      if (entry.kind === 'text' && !(error instanceof WorkspaceFilesChangedError)) {
        sourceMode.value = true
        previewState.previewErrorKey = error instanceof Error && error.message === 'workbench.artifactPreview.tooLarge'
          ? error.message : 'workbench.artifactPreview.unsupported'
      } else previewState.errorKey = fileError(error)
    } finally {
      if (isCurrentPreview(request)) previewState.loading = false
    }
    if (!sourceMode.value) return
  }
  if (entry.kind !== 'text' || !files?.readPage) {
    previewState.loading = false
    previewState.errorKey = entry.kind === 'text' ? 'workspaceReference.unsupported' : 'workbench.artifactPreview.unsupported'
    return
  }
  previewState.loading = true
  try {
    const page = await files.readPage(request.sessionKey, fileFor(entry), 1, 200, request.controller.signal)
    if (isCurrentPreview(request)) previewState.snapshot = { ...page, paged: true }
  } catch (error) {
    if (isCurrentPreview(request)) previewState.errorKey = fileError(error)
  } finally {
    if (isCurrentPreview(request)) previewState.loading = false
  }
}

function fileError(error: unknown) {
  if (error instanceof Error && error.message === 'workbench.artifactPreview.tooLarge') return error.message
  if (error instanceof WorkspaceTextLimitError) return error.limit === 'line'
    ? 'workbench.workspaceFiles.textLineLimit' : 'workbench.workspaceFiles.textPageLimit'
  return error instanceof WorkspaceFilesChangedError ? 'workbench.workspaceFiles.changed' : 'workbench.workspaceFiles.unavailable'
}

function toggleSource() {
  if (selected.value) void activate(selected.value, !sourceMode.value)
}

async function handlePreviewEvent(event: { type: string; payload?: unknown }) {
  const entry = selected.value
  const value = event.payload && typeof event.payload === 'object' ? event.payload as Record<string, unknown> : {}
  if (!entry || entry.type !== 'file' || entry.kind !== 'text' || !files) return
  const file = fileFor(entry)
  const sessionKey = props.sessionKey
  if (event.type === 'workspace-file-page') {
    const start = Number(value.startLine)
    if (!Number.isSafeInteger(start) || start < 1 || !files.readPage) return
    const request = beginPreview('load')
    previewState.loading = true
    previewState.errorKey = ''
    previewState.searchStatus = 'idle'
    try {
      const page = await files.readPage(request.sessionKey, file, start, start + 199, request.controller.signal)
      if (isCurrentPreview(request)) previewState.snapshot = { ...page, paged: true, focusLine: Number(value.focusLine) || undefined }
    } catch (error) { if (isCurrentPreview(request)) previewState.errorKey = fileError(error) }
    finally { if (isCurrentPreview(request)) previewState.loading = false }
  } else if (event.type === 'workspace-file-search' && typeof value.query === 'string' && files.search && files.readPage) {
    const request = beginPreview('search')
    previewState.loading = false
    previewState.searchErrorKey = ''
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
    } catch (error) { if (isCurrentPreview(request)) { previewState.searchErrorKey = fileError(error); previewState.searchStatus = 'idle'; if (error instanceof WorkspaceFilesChangedError) previewState.errorKey = fileError(error) } }
  } else if (event.type === 'workspace-file-search-cancel') {
    if (previewRequest?.kind === 'search') cancelPreview()
    previewState.searchStatus = 'idle'; previewState.searchQuery = ''; previewState.searchErrorKey = ''
  } else if (event.type === 'workspace-file-copy' && files.read) {
    cancelCopy()
    const request = new AbortController()
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
      clearTimeout(copyTimer)
      copyTimer = setTimeout(() => { if (isCurrent()) previewState.copied = false }, 1600)
    } catch { if (isCurrent()) previewState.copyErrorKey = 'workspaceReference.copyFailed' }
    finally { if (isCurrent()) previewState.copying = false }
  }
}

async function refresh() {
  directoryRequest.abort()
  directoryRequest = new AbortController()
  treeError.value = ''
  resetPreview()
  rootAbort?.abort()
  rootAbort = new AbortController()
  selected.value = null
  resolvedFile.value = null
  root.value = null
  rootError.value = false
  expanded.clear()
  binding.value = ''
  directories.clear()
  const rootRequest = rootAbort
  if (files?.root) {
    try {
      const resolvedRoot = await files.root({ sessionKey: props.sessionKey }, rootRequest.signal)
      if (rootRequest.signal.aborted || rootRequest !== rootAbort) return
      root.value = resolvedRoot
    } catch {
      if (!rootRequest.signal.aborted && rootRequest === rootAbort) rootError.value = true
    }
  }
  if (!rootRequest.signal.aborted && rootRequest === rootAbort) await loadDirectory('')
}

watch(() => props.sessionKey, () => { void refresh() }, { flush: 'sync' })
onMounted(() => { void refresh(); void nextTick(() => virtualizer.value.measure()) })
onBeforeUnmount(() => { directoryRequest.abort(); rootAbort?.abort(); resetPreview() })
</script>

<style scoped>
.workspace-files { display: grid; grid-template-columns: minmax(170px, 26%) minmax(0, 1fr); height: 100%; min-width: 0; color: var(--text); background: var(--bg-surface); }
.workspace-files.is-tree-hidden { grid-template-columns: minmax(0, 1fr); }
.workspace-files__tree { min-width: 0; display: flex; min-height: 0; flex-direction: column; border-right: 1px solid var(--border); }
.workspace-files__tree-toolbar { display: flex; align-items: center; gap: var(--sp-1); padding: var(--sp-2); border-bottom: 1px solid var(--border); }
.workspace-files__filter { display: flex; align-items: center; gap: var(--sp-1); min-width: 0; flex: 1; height: 28px; box-sizing: border-box; padding-inline: 6px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-surface); }
.workspace-files__filter:focus-within { border-color: var(--border-focus); box-shadow: 0 0 0 2px color-mix(in srgb, var(--border-focus) 28%, transparent); }
.workspace-files__filter-input { display: block; flex: 1; width: 0; min-width: 0; height: 26px; min-height: 0; box-sizing: border-box; margin: 0; padding: 0; border: 0; border-radius: 0; outline: none; box-shadow: none; background: transparent; color: var(--text); font: inherit; font-size: 12px; }
.workspace-files__filter > input[type="search"]::-webkit-search-cancel-button { appearance: none; }
.workspace-files__filter-icon { display: inline-flex; flex: 0 0 14px; color: var(--text-muted); pointer-events: none; }
.workspace-files__filter-clear { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 20px; width: 20px; height: 20px; min-height: 0; margin-right: -3px; padding: 0; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.workspace-files__filter-clear span { display: flex; }
.workspace-files__filter-clear:hover { background: var(--bg-hover); color: var(--text); }
.workspace-files__filter-clear:focus-visible { outline: 2px solid var(--border-focus); outline-offset: -2px; }
.workspace-files__refresh, .workspace-files__disclosure { border: 0; background: transparent; color: var(--text); font: inherit; cursor: pointer; }
.workspace-files__refresh { display: flex; align-items: center; justify-content: center; flex: 0 0 28px; width: 28px; height: 28px; min-height: 0; padding: 0; border-radius: var(--radius-sm); }
.workspace-files__refresh:hover:not(:disabled) { background: var(--bg-hover); }
.workspace-files__refresh:disabled { opacity: .5; cursor: default; }
.workspace-files__refresh span, .workspace-files__disclosure span, .workspace-files__icon { display: flex; }
.workspace-files__icon { flex-shrink: 0; color: var(--text-muted); }
.workspace-files__icon.is-folder { color: var(--accent); }
.workspace-files__tree-scroll { min-height: 0; flex: 1; overflow: auto; }
.workspace-files__tree-spacer { position: relative; min-height: 100%; }
.workspace-files__row { position: absolute; inset-inline: 0; display: flex; align-items: center; min-width: 0; height: 28px; font-size: 12px; padding-inline: var(--sp-2); box-sizing: border-box; gap: .25rem; cursor: pointer; }
.workspace-files__row:hover, .workspace-files__row.is-selected { background: var(--bg-hover); }
.workspace-files__row.is-more { color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-files__indent { flex: 0 0 auto; }
.workspace-files__disclosure { display: inline-flex; align-items: center; justify-content: center; flex: 0 0 12px; width: 12px; min-height: 0; padding: 0; text-align: center; color: var(--text-muted); }
.workspace-files__name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-files__size { margin-left: auto; padding-left: var(--sp-2); color: var(--text-muted); font-size: .72rem; }
.workspace-files__message, .workspace-files__hint, .workspace-files__empty { margin: 0; padding: var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-files__preview { display: flex; min-width: 0; min-height: 0; flex-direction: column; }
.workspace-files__preview-title { display: flex; align-items: center; gap: 8px; height: 36px; box-sizing: border-box; flex: 0 0 auto; padding: var(--sp-2) var(--sp-3); overflow: hidden; border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: var(--fs-sm); text-overflow: ellipsis; white-space: nowrap; }
.workspace-files__breadcrumb { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.workspace-files__breadcrumb strong { font-weight: 500; color: var(--text); }
.workspace-files__mode { flex-shrink: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text); background: transparent; font-size: 12px; padding: 3px 7px; cursor: pointer; }
.workspace-files__parent { opacity: .8; }
.workspace-files__row:focus-visible, .workspace-files__refresh:focus-visible { outline: 2px solid var(--border-focus); outline-offset: -2px; }
.workspace-files__preview > .workspace-file { min-height: 0; flex: 1; }
.workspace-files__binary-preview { display: flex; min-height: 0; flex: 1; align-items: center; justify-content: center; overflow: auto; padding: var(--sp-4); }
.workspace-files__binary-preview img { max-width: 100%; max-height: 100%; object-fit: contain; }
.workspace-files__binary-preview p { color: var(--text-muted); font-size: var(--fs-sm); }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
@media (max-width: 700px) { .workspace-files { grid-template-columns: minmax(150px, 34%) minmax(0, 1fr); } .workspace-files__size { display: none; } }
</style>
