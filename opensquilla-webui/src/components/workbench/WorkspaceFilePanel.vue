<template>
  <div class="workspace-file" data-testid="workspace-file-panel">
    <div v-if="file && sessionKey" class="workspace-file__actions">
      <WorkspaceResourceActions :session-key="sessionKey" :file="file" />
    </div>
    <p v-if="loading" role="status">{{ t('workspaceReference.loading') }}</p>
    <p v-else-if="errorKey" role="alert">{{ t(errorKey === 'workbench.artifactPreview.tooLarge' ? 'workbench.artifactPreview.tooLargeDetail' : errorKey) }}</p>
    <template v-else-if="!snapshot && previewBlob && previewKind">
      <p v-if="previewErrorKey" class="workspace-file__status" role="status">{{ t(previewErrorKey) }}</p>
      <ResourcePreviewPanel :blob="previewBlob" :kind="previewKind" :name="file?.name || ''" />
    </template>
    <template v-else-if="snapshot">
      <div class="workspace-file__toolbar">
        <div v-if="canPreview" class="workspace-file__modes">
          <button type="button" :class="{ 'is-active': mode === 'preview' }" :aria-pressed="mode === 'preview'" @click="mode = 'preview'">{{ t('workspaceReference.preview') }}</button>
          <button type="button" :class="{ 'is-active': mode === 'source' }" :aria-pressed="mode === 'source'" @click="mode = 'source'">{{ t('workspaceReference.source') }}</button>
        </div>
        <span v-else class="workspace-file__language">{{ snapshot.relativePath.split('.').pop()?.toUpperCase() }}</span>
        <span class="workspace-file__spacer" />
        <button type="button" class="workspace-file__tool" :aria-label="t('workspaceReference.search')" :title="t('workspaceReference.search')" :aria-expanded="toolsOpen" @click="toolsOpen = !toolsOpen; mode = 'source'"><span v-html="getIconSvg('search', 15)" /></button>
        <button type="button" class="workspace-file__tool" :disabled="copying" :aria-label="t('workspaceReference.copyContents')" :title="copied ? t('workspaceReference.copied') : t('workspaceReference.copyContents')" @click="copyContents"><span v-html="getIconSvg(copied ? 'check' : 'copy', 15)" /></button>
      </div>
      <div v-show="toolsOpen" class="workspace-file__find">
        <label>
          <span class="sr-only">{{ t('workspaceReference.search') }}</span>
          <input v-model="query" type="search" maxlength="512" @input="resetSearch" :placeholder="t('workspaceReference.searchPlaceholder')" @keydown.enter="findMatch">
        </label>
        <button type="button" class="workspace-file__tool" :disabled="!query.trim()" :aria-label="t('workspaceReference.search')" @click="findMatch"><span v-html="getIconSvg('chevronRight', 15)" /></button>
        <label class="workspace-file__jump">
          <span class="sr-only">{{ t('workspaceReference.jumpToLine') }}</span>
          <input v-model="jump" type="number" min="1" :max="snapshot.totalLines" :placeholder="t('workspaceReference.jumpToLine')" @keydown.enter="jumpToLine">
        </label>
      </div>
      <p v-if="searching" class="workspace-file__status" role="status">{{ t('workspaceReference.loading') }}</p>
      <p v-else-if="noMatches" class="workspace-file__status" role="status">{{ t('workspaceReference.noMatches') }}</p>
      <p v-if="searchErrorKey" class="workspace-file__status" role="alert">{{ t(searchErrorKey) }}</p>
      <p v-if="copyErrorKey" class="workspace-file__status" role="alert">{{ t(copyErrorKey) }}</p>
      <p v-if="previewErrorKey" class="workspace-file__status" role="status">{{ t(previewErrorKey === 'workbench.artifactPreview.tooLarge' ? 'workbench.artifactPreview.tooLargeDetail' : previewErrorKey) }}</p>
      <ResourcePreviewPanel v-if="mode === 'preview' && canPreviewResource && previewBlob"
        :blob="previewBlob" :kind="previewKind!" :name="snapshot.relativePath" />
      <div v-else-if="mode === 'preview' && canPreviewMarkdown" ref="markdownPreview" class="workspace-file__markdown artifact-preview__markdown chat-markdown" v-html="markdownHtml" />
      <pre v-else ref="source" class="workspace-file__source" tabindex="0" :aria-label="snapshot.relativePath"><code><span
        v-for="line in visibleLines" :key="line.number" class="workspace-file__line"
        :class="{ 'is-selected': Boolean(snapshot.reference) && line.number >= snapshot.startLine && line.number <= snapshot.endLine, 'is-match': line.match }"
        :data-line="line.number" :aria-current="line.number === focusLine ? 'location' : undefined"
      ><span class="workspace-file__number" aria-hidden="true">{{ line.number }}</span><span class="workspace-file__text" v-html="line.html || ' '" /></span></code></pre>
      <div v-if="mode !== 'preview' || !canPreview" class="workspace-file__footer">
        <span role="status">{{ t('workspaceReference.lines', { start: rangeStart, end: rangeEnd, total: snapshot.totalLines }) }}</span>
        <span class="workspace-file__spacer" />
        <button v-if="canPrevious" class="workspace-file__more workspace-file__tool" type="button" :aria-label="t('workspaceReference.previous')" :title="t('workspaceReference.previous')" @click="previousPage"><span v-html="getIconSvg('chevronLeft', 15)" /></button>
        <button v-if="canNext" class="workspace-file__more workspace-file__tool" type="button" :aria-label="t('workspaceReference.next')" :title="t('workspaceReference.next')" @click="nextPage"><span v-html="getIconSvg('chevronRight', 15)" /></button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { getIconSvg } from '@/utils/icons'
import { renderMarkdownCore } from '@/utils/markdown/renderCore'
import { useMarkdownEnhancements } from '@/composables/chat/useMarkdownEnhancements'
import { highlightWorkspaceLines } from './workspaceSourceHighlight'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'
import type { ResourcePreviewKind } from '@/workbench/resourcePreview'
import type { WorkspaceFile } from '@/modules/workspaceFiles'
import { isWorkspaceMarkdown, WORKSPACE_MARKDOWN_PREVIEW_LIMIT } from '@/workbench/workspaceTextPreview'
import ResourcePreviewPanel from './ResourcePreviewPanel.vue'
import WorkspaceResourceActions from './WorkspaceResourceActions.vue'

interface WorkspaceFileViewSnapshot {
  relativePath: string
  content: string
  totalLines: number
  startLine: number
  endLine: number
  reference?: WorkspaceSourceSnapshot['reference']
  paged?: boolean
  focusLine?: number
}

const props = defineProps<{
  file?: WorkspaceFile | null
  mime?: string
  sessionKey?: string
  snapshot?: WorkspaceFileViewSnapshot | null
  previewBlob?: Blob | null
  previewKind?: ResourcePreviewKind
  previewErrorKey?: string
  loading?: boolean
  errorKey?: string
  viewId?: number
  copying?: boolean
  copied?: boolean
  copyErrorKey?: string
  searchStatus?: 'idle' | 'searching' | 'found' | 'not-found'
  searchQuery?: string
  searchErrorKey?: string
}>()
const emit = defineEmits<{ 'workbench-event': [event: { type: string; payload?: unknown }] }>()
const { t } = useI18n()
const source = ref<HTMLElement | null>(null)
const markdownPreview = ref<HTMLElement | null>(null)
const query = ref('')
const jump = ref('')
const toolsOpen = ref(false)
const mode = ref<'preview' | 'source'>('preview')
const canPreviewMarkdown = computed(() => Boolean(props.snapshot && !props.snapshot.reference
  && isWorkspaceMarkdown({ name: props.snapshot.relativePath, mime: props.file?.mime || props.mime || '' }) && props.snapshot.startLine === 1
  && props.snapshot.endLine >= props.snapshot.totalLines && props.snapshot.content.length <= WORKSPACE_MARKDOWN_PREVIEW_LIMIT))
const canPreviewResource = computed(() => Boolean(props.previewBlob && props.previewKind
  && !['source', 'unsupported'].includes(props.previewKind) && !props.snapshot?.reference))
const canPreview = computed(() => canPreviewMarkdown.value || canPreviewResource.value)
const markdownHtml = computed(() => canPreviewMarkdown.value && mode.value === 'preview'
  ? renderMarkdownCore(props.snapshot!.content, { images: 'placeholder' }) : '')
useMarkdownEnhancements(markdownPreview, { version: () => markdownHtml.value })
const searchedQuery = ref('')
const focusLine = ref(1)
const lines = computed(() => {
  if (!props.snapshot) return []
  // Match Python splitlines(), with a visible first line for an empty file.
  const values = props.snapshot.content.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/)
  if (values.length > 1 && values[values.length - 1] === '') values.pop()
  return values
})
const windowStart = ref(0)
const windowEnd = computed(() => Math.min(lines.value.length, windowStart.value + 200))
const paged = computed(() => props.snapshot?.paged === true)
const canPrevious = computed(() => paged.value
  ? (props.snapshot?.startLine ?? 1) > 1
  : windowStart.value > 0)
const canNext = computed(() => paged.value
  ? (props.snapshot?.endLine ?? 0) < (props.snapshot?.totalLines ?? 0)
  : windowEnd.value < lines.value.length)
const matches = computed(() => {
  const needle = query.value.trim().toLowerCase()
  if (!needle) return []
  return lines.value.flatMap((text, index) => text.toLowerCase().includes(needle) ? [index + 1] : [])
})
const searching = computed(() => props.searchStatus === 'searching' && props.searchQuery === query.value.trim())
const noMatches = computed(() => !!query.value.trim() && (paged.value
  ? props.searchStatus === 'not-found' && props.searchQuery === query.value.trim()
  : searchedQuery.value === query.value.trim() && !matches.value.length))
const highlightedLines = computed(() => highlightWorkspaceLines(props.snapshot?.relativePath || '', lines.value.slice(windowStart.value, windowEnd.value)))
const visibleLines = computed(() => lines.value.slice(windowStart.value, windowEnd.value)
  .map((text, index) => ({
    text,
    html: highlightedLines.value[index],
    number: (paged.value ? (props.snapshot?.startLine ?? 1) - 1 : 0) + windowStart.value + index + 1,
    match: matches.value.includes(windowStart.value + index + 1),
  })))
const rangeStart = computed(() => props.snapshot?.reference ? props.snapshot.startLine : visibleLines.value[0]?.number ?? 1)
const rangeEnd = computed(() => props.snapshot?.reference ? props.snapshot.endLine : visibleLines.value[visibleLines.value.length - 1]?.number ?? 1)
watch(() => props.viewId, () => {
  query.value = ''
  jump.value = ''
  searchedQuery.value = ''
  mode.value = 'preview'
  toolsOpen.value = false
})
watch(() => props.snapshot, async snapshot => {
  if (!snapshot) return
  focusLine.value = snapshot.focusLine ?? snapshot.startLine
  windowStart.value = snapshot.paged ? 0 : Math.max(0, focusLine.value - 41)
  await nextTick()
  if (props.snapshot === snapshot) scrollToLine(focusLine.value)
}, { immediate: true })

function scrollToLine(line: number) {
  source.value?.querySelector(`[data-line="${line}"]`)?.scrollIntoView?.({ block: 'center' })
}

function previousPage() {
  if (paged.value) {
    emit('workbench-event', { type: 'workspace-file-page', payload: {
      startLine: Math.max(1, (props.snapshot?.startLine ?? 1) - 200),
    } })
  } else {
    windowStart.value = Math.max(0, windowStart.value - 200)
    focusLine.value = windowStart.value + 1
    void nextTick(() => scrollToLine(focusLine.value))
  }
}

function nextPage() {
  if (paged.value) {
    emit('workbench-event', { type: 'workspace-file-page', payload: {
      startLine: (props.snapshot?.endLine ?? 0) + 1,
    } })
  } else {
    windowStart.value = windowEnd.value
    focusLine.value = windowStart.value + 1
    void nextTick(() => scrollToLine(focusLine.value))
  }
}

function resetSearch() {
  searchedQuery.value = ''
  emit('workbench-event', { type: 'workspace-file-search-cancel' })
}

function findMatch() {
  if (!query.value.trim()) return
  if (paged.value) {
    emit('workbench-event', { type: 'workspace-file-search', payload: { query: query.value } })
    return
  }
  searchedQuery.value = query.value.trim()
  const line = matches.value[0]
  if (line) {
    focusLine.value = line
    windowStart.value = Math.max(0, line - 41)
    void nextTick(() => scrollToLine(line))
  }
}

function jumpToLine() {
  const requested = Math.max(1, Number.parseInt(jump.value, 10) || 1)
  const line = Math.min(props.snapshot?.totalLines ?? 1, requested)
  if (paged.value && (line < (props.snapshot?.startLine ?? 1) || line > (props.snapshot?.endLine ?? 0))) {
    emit('workbench-event', { type: 'workspace-file-page', payload: {
      startLine: Math.floor((line - 1) / 200) * 200 + 1,
      focusLine: line,
    } })
    return
  }
  focusLine.value = line
  if (!paged.value) windowStart.value = Math.max(0, line - 41)
  void nextTick(() => scrollToLine(line))
}

function copyContents() {
  emit('workbench-event', { type: 'workspace-file-copy' })
}
</script>

<style scoped>
.workspace-file { display: flex; flex-direction: column; height: 100%; min-width: 0; min-height: 0; overflow: hidden; background: var(--bg-surface); color: var(--text); }
.workspace-file > p { padding: 12px; }
.workspace-file__actions { display: flex; justify-content: flex-end; padding: 4px 12px; border-bottom: 1px solid var(--border); }
.workspace-file__toolbar, .workspace-file__find, .workspace-file__footer { display: flex; flex: 0 0 auto; align-items: center; gap: 6px; padding: 4px 12px; border-bottom: 1px solid var(--border); }
.workspace-file__toolbar { min-height: 32px; }
.workspace-file__spacer { flex: 1; }
.workspace-file__language { color: var(--text-muted); font-size: 11px; letter-spacing: .06em; }
.workspace-file__modes { display: flex; gap: 2px; }
.workspace-file__modes button, .workspace-file__tool { display: inline-flex; align-items: center; justify-content: center; min-height: 28px; border: 0; border-radius: var(--radius-sm); padding: 5px; background: transparent; color: var(--text-muted); font: inherit; font-size: 12px; font-weight: 400; cursor: pointer; }
.workspace-file__modes button { padding-inline: 9px; }
.workspace-file__modes .is-active { background: var(--bg-hover); color: var(--text); }
.workspace-file__tool span { display: flex; }
.workspace-file__tool:hover:not(:disabled), .workspace-file__modes button:hover { background: var(--bg-hover); color: var(--text); }
.workspace-file__tool:disabled { cursor: default; opacity: var(--state-disabled-opacity); }
.workspace-file__find { flex-wrap: wrap; }
.workspace-file__find label { flex: 1; min-width: 90px; }
.workspace-file__find input { box-sizing: border-box; width: 100%; height: 30px; min-height: 0; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 4px 8px; background: var(--bg-surface); color: var(--text); font: inherit; font-size: 12px; }
.workspace-file__find .workspace-file__jump { flex: 0 1 120px; }
.workspace-file__status { margin: 0; padding: 6px 12px; color: var(--text-muted); font-size: 12px; }
.workspace-file__source { flex: 1; min-height: 0; margin: 0; border: 0; border-radius: 0; padding: 12px 0; overflow: auto; background: transparent; color: var(--text); font: 400 13px/1.65 var(--font-mono); tab-size: 4; }
.workspace-file__source code { display: block; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; }
.workspace-file__line { display: flex; min-width: max-content; padding-right: 16px; }
.workspace-file__line.is-selected { background: var(--bg-hover); box-shadow: inset 3px 0 var(--border-focus); }
.workspace-file__line.is-match { outline: 1px solid color-mix(in srgb, var(--accent) 45%, transparent); outline-offset: -1px; }
.workspace-file__number { position: sticky; left: 0; flex: 0 0 5ch; padding-right: 16px; text-align: right; background: var(--bg-surface); color: var(--text-muted); user-select: none; }
.workspace-file__footer { min-height: 26px; border-top: 1px solid var(--border); border-bottom: 0; color: var(--text-muted); font-size: 11px; }
.workspace-file__markdown { flex: 1; overflow: auto; padding: 24px clamp(16px, 4%, 48px); font-size: 14px; line-height: 1.75; }
.workspace-file__markdown :deep(ul), .workspace-file__markdown :deep(ol) { margin: .75rem 0; padding-inline-start: 1.5rem; }
.workspace-file__markdown :deep(li) { margin: .375rem 0; }
/* Shared Markdown enhancements own code headers/copy controls; only the base
   block layout is local because the main conversation's pre rules are scoped. */
.workspace-file__markdown :deep(pre) { margin: .75rem 0; padding: .75rem .875rem .875rem; overflow-x: auto; border: 1px solid var(--code-block-border); border-radius: var(--radius-control); background: var(--code-block-bg); font: 400 13px/1.65 var(--font-mono); }
.workspace-file__markdown :deep(pre > code) { display: block; padding: 0; border: 0; background: transparent; color: var(--text); font: inherit; }
.workspace-file__text :deep(.hljs-comment), .workspace-file__text :deep(.hljs-quote) { color: var(--syntax-comment); }
.workspace-file__text :deep(.hljs-keyword), .workspace-file__text :deep(.hljs-selector-tag) { color: var(--syntax-keyword); }
.workspace-file__text :deep(.hljs-string), .workspace-file__text :deep(.hljs-attr) { color: var(--syntax-string); }
.workspace-file__text :deep(.hljs-number), .workspace-file__text :deep(.hljs-literal) { color: var(--syntax-literal); }
.workspace-file__text :deep(.hljs-title), .workspace-file__text :deep(.hljs-built_in) { color: var(--syntax-title); }
.workspace-file__text :deep(.hljs-variable), .workspace-file__text :deep(.hljs-type) { color: var(--syntax-attr); }
button:focus-visible, input:focus-visible, .workspace-file__source:focus-visible { outline: 2px solid var(--border-focus); outline-offset: -2px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
