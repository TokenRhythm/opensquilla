<template>
  <div class="workspace-file" data-testid="workspace-file-panel">
    <p v-if="loading" role="status">{{ t('workspaceReference.loading') }}</p>
    <p v-else-if="errorKey" role="alert">{{ t(errorKey) }}</p>
    <template v-else-if="snapshot">
      <div class="workspace-file__toolbar">
        <label>
          <span class="sr-only">{{ t('workspaceReference.search') }}</span>
          <input v-model="query" type="search" maxlength="512" @input="resetSearch" :placeholder="t('workspaceReference.searchPlaceholder')" @keydown.enter="findMatch">
        </label>
        <button type="button" class="workspace-file__tool" :disabled="!query.trim()" @click="findMatch">
          {{ t('workspaceReference.search') }}
        </button>
        <label class="workspace-file__jump">
          <span class="sr-only">{{ t('workspaceReference.jumpToLine') }}</span>
          <input v-model="jump" type="number" min="1" :max="snapshot.totalLines" :placeholder="t('workspaceReference.jumpToLine')" @keydown.enter="jumpToLine">
        </label>
        <button type="button" class="workspace-file__tool" :disabled="copying" @click="copyContents">
          {{ copying ? t('workspaceReference.loading') : copied ? t('workspaceReference.copied') : t('workspaceReference.copyContents') }}
        </button>
      </div>
      <p v-if="searching" class="workspace-file__status" role="status">{{ t('workspaceReference.loading') }}</p>
      <p v-else-if="noMatches" class="workspace-file__status" role="status">{{ t('workspaceReference.noMatches') }}</p>
      <p v-if="searchErrorKey" class="workspace-file__status" role="alert">{{ t(searchErrorKey) }}</p>
      <p v-if="copyErrorKey" class="workspace-file__status" role="alert">{{ t(copyErrorKey) }}</p>
      <div class="workspace-file__range" role="status">
        {{ t('workspaceReference.lines', { start: rangeStart, end: rangeEnd, total: snapshot.totalLines }) }}
      </div>
      <button v-if="canPrevious" class="workspace-file__more" type="button" @click="previousPage">{{ t('workspaceReference.previous') }}</button>
      <pre ref="source" class="workspace-file__source" tabindex="0" :aria-label="snapshot.relativePath"><code><span
        v-for="line in visibleLines" :key="line.number" class="workspace-file__line"
        :class="{ 'is-selected': Boolean(snapshot.reference) && line.number >= snapshot.startLine && line.number <= snapshot.endLine, 'is-match': line.match }"
        :data-line="line.number" :aria-current="line.number === focusLine ? 'location' : undefined"
      ><span class="workspace-file__number" aria-hidden="true">{{ line.number }}</span><span>{{ line.text || ' ' }}</span></span></code></pre>
      <button v-if="canNext" class="workspace-file__more" type="button" @click="nextPage">{{ t('workspaceReference.next') }}</button>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { WorkspaceSourceSnapshot } from '@/modules/workspaceReferences'

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
  snapshot?: WorkspaceFileViewSnapshot | null
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
const query = ref('')
const jump = ref('')
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
const visibleLines = computed(() => lines.value.slice(windowStart.value, windowEnd.value)
  .map((text, index) => ({
    text,
    number: (paged.value ? (props.snapshot?.startLine ?? 1) - 1 : 0) + windowStart.value + index + 1,
    match: matches.value.includes(windowStart.value + index + 1),
  })))
const rangeStart = computed(() => props.snapshot?.reference ? props.snapshot.startLine : visibleLines.value[0]?.number ?? 1)
const rangeEnd = computed(() => props.snapshot?.reference ? props.snapshot.endLine : visibleLines.value[visibleLines.value.length - 1]?.number ?? 1)
watch(() => props.viewId, () => {
  query.value = ''
  jump.value = ''
  searchedQuery.value = ''
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
.workspace-file { height: 100%; min-width: 0; overflow: auto; background: var(--bg-surface); color: var(--text); }
.workspace-file > p { padding: var(--sp-4); }
.workspace-file__toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border); background: var(--bg-surface); }
.workspace-file__toolbar label { min-width: 0; }
.workspace-file__toolbar input { min-width: 10rem; max-width: 18rem; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.35rem 0.5rem; background: var(--bg-hover); color: var(--text); font: inherit; font-size: var(--fs-sm); }
.workspace-file__toolbar input:focus-visible, .workspace-file__tool:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 2px; }
.workspace-file__jump input { width: 8.5rem; min-width: 0; }
.workspace-file__tool { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0.35rem 0.55rem; background: transparent; color: var(--text); font: inherit; font-size: var(--fs-sm); cursor: pointer; }
.workspace-file__tool:hover:not(:disabled) { background: var(--bg-hover); }
.workspace-file__tool:disabled { cursor: default; opacity: var(--state-disabled-opacity); }
.workspace-file__status { margin: 0; padding: var(--sp-2) var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
.workspace-file__range { padding: var(--sp-2) var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); border-bottom: 1px solid var(--border); }
.workspace-file__source { margin: 0; padding: var(--sp-3) 0; overflow: auto; font-size: var(--fs-sm); tab-size: 4; }
.workspace-file__line { display: flex; min-width: max-content; padding-right: var(--sp-4); line-height: 1.65; }
.workspace-file__line.is-selected { background: var(--bg-hover); box-shadow: inset 3px 0 var(--border-focus); }
.workspace-file__line.is-match { outline: 1px solid color-mix(in srgb, var(--accent) 45%, transparent); outline-offset: -1px; }
.workspace-file__number { display: inline-block; flex: 0 0 5ch; text-align: right; margin-right: var(--sp-4); color: var(--text-muted); user-select: none; }
.workspace-file__more { width: 100%; border: 0; padding: var(--sp-2); background: var(--bg-hover); color: var(--text); font: inherit; font-size: var(--fs-sm); cursor: pointer; }
.workspace-file__source:focus-visible, button:focus-visible { outline: 2px solid var(--border-focus); outline-offset: -2px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
</style>
