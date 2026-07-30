<template>
  <div class="lg-stage control-stage">
    <header class="control-stage__header">
      <div class="control-stage__title-block">
        <nav class="lg-breadcrumb" :aria-label="t('usageLogs.logs.breadcrumbLabel')">
          <RouterLink class="lg-breadcrumb__link" to="/overview">
            {{ t('nav.overview') }}
          </RouterLink>
          <span class="lg-breadcrumb__separator" aria-hidden="true">/</span>
          <span aria-current="page">{{ t('nav.logs') }}</span>
        </nav>
        <h1 class="control-stage__title">{{ t('usageLogs.logs.title') }}</h1>
        <p class="control-stage__subtitle">{{ t('usageLogs.logs.subtitle') }}</p>
        <p v-if="status" class="lg-status-line">
          <span
            class="lg-status-line__seg"
            :class="{ 'lg-status-line__seg--warn': !fileLogEnabled }"
            :title="fileLogTitleText"
            :aria-label="`${fileLogLabel}. ${fileLogTitleText}`"
          >{{ fileLogLabel }}</span>
          <span class="lg-status-line__sep" aria-hidden="true">·</span>
          <span
            class="lg-status-line__seg"
            :class="{ 'lg-status-line__seg--warn': rawLogEnabled }"
            :title="rawTitleText"
            :aria-label="`${rawLabel}. ${rawTitleText}`"
          >{{ rawLabel }}</span>
        </p>
      </div>
      <div class="control-stage__actions">
        <span
          v-if="!status"
          class="control-pill control-pill--warn"
          :aria-label="t('usageLogs.logs.statusUnavailableAria')"
          :title="t('usageLogs.logs.statusUnavailableTitle')"
        >{{ t('usageLogs.logs.statusUnavailable') }}</span>
        <span
          v-else-if="rawLogEnabled"
          class="control-pill control-pill--warn"
          role="status"
          :aria-label="`${t('usageLogs.logs.rawRecordingPill')}. ${rawTitleText}`"
          :title="rawTitleText"
        ><span class="dot" aria-hidden="true"></span>{{ t('usageLogs.logs.rawRecordingPill') }}</span>
        <span
          v-else-if="!fileLogEnabled"
          class="control-pill control-pill--warn"
          :aria-label="`${t('usageLogs.logs.fileLogOff')}. ${fileLogTitleText}`"
          :title="fileLogTitleText"
        >{{ t('usageLogs.logs.fileLogOff') }}</span>
        <SupportDiagnosticsMenu />
      </div>
    </header>

    <!-- One toolbar row carries both filter and count: each level chip shows
         its live count, so the former four near-empty count tiles are gone.
         The in-view/loaded figure moves to the stream footer readout. -->
    <section class="lg-toolbar">
      <div class="lg-levels">
        <span class="lg-toolbar__label">{{ t('usageLogs.logs.levels') }}</span>
        <div class="lg-levels__row">
          <button
            v-for="level in LEVELS"
            :key="level"
            :class="['lg-level-btn', `lg-level-btn--${level.toLowerCase()}`, activeLevels.has(level) ? 'is-active' : '']"
            :aria-pressed="activeLevels.has(level) ? 'true' : 'false'"
            @click="toggleLevel(level)"
          >
            <span class="lg-level-btn__dot"></span>
            <span class="lg-level-btn__label">{{ level }}</span>
            <span class="lg-level-btn__count">{{ levelChipCount(level) }}</span>
          </button>
        </div>
      </div>
      <div class="lg-search-wrap">
        <span class="lg-search-icon"><Icon name="search" :size="16" /></span>
        <input
          v-model="searchText"
          class="lg-search-input"
          type="search"
          :placeholder="t('usageLogs.logs.filterPlaceholder')"
          autocomplete="off"
        />
      </div>
      <label class="lg-toggle">
        <ControlSwitch v-model:checked="autoFollow" :aria-label="t('usageLogs.logs.autoFollow')" />
        <span class="lg-toggle__label">{{ t('usageLogs.logs.autoFollow') }}</span>
      </label>
    </section>

    <section class="lg-stream">
      <div
        v-if="loadState === 'error' && allLines.length > 0"
        class="lg-stream__error"
        role="alert"
      >
        <span>{{ t('usageLogs.logs.loadFailed') }}</span>
        <button type="button" class="btn btn--ghost btn--sm" @click="retryLoad">
          {{ t('usageLogs.logs.retry') }}
        </button>
      </div>
      <div ref="displayRef" class="lg-display" @scroll="onScroll">
        <div
          v-if="loadState === 'loading' && allLines.length === 0"
          class="lg-display__placeholder"
          role="status"
        >
          <span class="lg-spinner"></span>
          {{ t('usageLogs.logs.loading') }}
        </div>
        <div
          v-else-if="loadState === 'error' && allLines.length === 0"
          class="lg-display__placeholder"
          role="alert"
        >
          <span class="lg-display__placeholder-icon lg-display__placeholder-icon--error">
            <Icon name="logs" :size="24" />
          </span>
          <span>{{ t('usageLogs.logs.loadFailed') }}</span>
          <button type="button" class="btn btn--primary" @click="retryLoad">
            {{ t('usageLogs.logs.retry') }}
          </button>
        </div>
        <div v-else-if="allLines.length === 0" class="lg-display__placeholder">
          <span class="lg-display__placeholder-icon"><Icon name="logs" :size="24" /></span>
          {{ t('usageLogs.logs.empty') }}
        </div>
        <div v-else-if="filteredLines.length === 0" class="lg-display__placeholder">
          <span class="lg-display__placeholder-icon"><Icon name="logs" :size="24" /></span>
          {{ t('usageLogs.logs.noMatch') }}
        </div>
        <div
          v-else
          class="lg-window"
          :style="{ paddingTop: topPad + 'px', paddingBottom: bottomPad + 'px' }"
        >
          <div
            v-for="{ item: line, index: idx } in windowedLines"
            :key="idx"
            :class="['lg-line', `lg-line--${(line.level || 'info').toLowerCase()}`, idx % 2 === 1 ? 'lg-line--alt' : '', runTraceEnabled ? 'lg-line--interactive' : '']"
            :role="runTraceEnabled ? 'button' : undefined"
            :tabindex="runTraceEnabled ? 0 : undefined"
            @click="openDetail(line)"
            @keydown="onLineKeydown($event, line)"
          >
            <span v-if="line.ts" class="lg-line__ts">{{ String(line.ts).slice(0, 23) }}</span>
            <span v-else class="lg-line__ts lg-line__ts--empty"></span>
            <span :class="['lg-line__lvl', `lg-line__lvl--${(line.level || 'info').toLowerCase()}`]">{{ line.level }}</span>
            <span class="lg-line__msg">
              <template
                v-for="(part, partIndex) in highlightParts(line.message)"
                :key="`${idx}-${partIndex}`"
              >
                <mark v-if="part.match" class="lg-line__match">{{ part.text }}</mark>
                <template v-else>{{ part.text }}</template>
              </template>
            </span>
          </div>
        </div>
      </div>
      <footer class="lg-stream__foot" aria-live="polite">
        {{ t('usageLogs.logs.ofLoadedFoot', { visible: visibleCount.toLocaleString(), total: totalCount.toLocaleString() }) }}
      </footer>
    </section>

    <Transition name="lg-detail">
    <div
      v-if="runTraceEnabled && selectedLine"
      class="lg-detail-overlay"
      @click.self="closeDetail"
    >
      <aside
        ref="detailRef"
        class="lg-detail"
        role="dialog"
        aria-modal="true"
        :aria-label="t('usageLogs.logs.lineDetail')"
      >
        <header class="lg-detail__head">
          <span class="lg-detail__title">{{ t('usageLogs.logs.lineDetail') }}</span>
          <button
            ref="detailCloseBtn"
            type="button"
            class="btn btn--icon btn--ghost"
            :aria-label="t('common.close')"
            :title="t('common.close')"
            @click="closeDetail"
          >
            <Icon name="x" :size="16" />
          </button>
        </header>
        <div class="lg-detail__body">
          <RunTrace
            v-if="lineSteps.length"
            :steps="lineSteps"
            :summary="lineSummary"
            :state-scope="detailStateScope"
            :is-tool-group-open="rt.isToolGroupOpen"
            :is-tool-item-open="rt.isToolItemOpen"
            @toggle-group="rt.toggleGroup"
            @toggle-item="rt.toggleItem"
            @show-result="onShowResult"
          />
          <pre v-else class="lg-detail__raw">{{ selectedLine.raw || selectedLine.message }}</pre>
        </div>
      </aside>
    </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, onActivated, onDeactivated, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'
import { useRpcStore } from '@/stores/rpc'
import { useFixedWindow } from '@/composables/useFixedWindow'
import Icon from '@/components/Icon.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'
import RunTrace from '@/components/run/RunTrace.vue'
import SupportDiagnosticsMenu from '@/components/SupportDiagnosticsMenu.vue'
import { useRunTrace } from '@/composables/run/useRunTrace'
import { nodeStepsFromHistoryMessage } from '@/components/run/runTrace'
import type { NodeStep, RunTraceSummary } from '@/types/runTrace'
import type { ChatHistoryMessage } from '@/types/rpc'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LogLine {
  level: string
  message: string
  ts?: string | number | null
  raw?: string
}

interface LogTailResponse {
  lines?: LogEntry[]
  entries?: LogEntry[]
  cursor?: number
}

interface LogEntry {
  level?: string
  lvl?: string
  message?: string
  msg?: string
  timestamp?: string | number
  ts?: string | number
  raw?: string
  [key: string]: unknown
}

interface LogStatus {
  gateway_file_log?: {
    enabled?: boolean
    path?: string
  }
  raw_turn_call_log?: {
    enabled?: boolean
    source?: string
    directory?: {
      path?: string
    }
  }
  diagnostics_enabled?: {
    effective?: boolean
    detail?: string
  }
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const LEVELS = ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR'] as const
const DEFAULT_LEVELS = new Set<string>(['DEBUG', 'INFO', 'WARN', 'ERROR'])

// Desktop .lg-line height: 12px mono at 1.6 line-height plus 2px vertical
// padding each side. At <=480px the row reflows to a taller column layout, so
// windowing is disabled there and the plain list renders (see windowingEnabled).
const ROW_H = 24
const WINDOW_MIN_WIDTH = '(min-width: 481px)'

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const { t } = useI18n()
const rpc = useRpcStore()
const allLines = ref<LogLine[]>([])
const cursor = ref(0)
const searchText = ref('')
const debouncedSearch = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
const autoFollow = ref(true)
const status = ref<LogStatus | null>(null)
const activeLevels = ref<Set<string>>(new Set(DEFAULT_LEVELS))
const displayRef = ref<HTMLElement | null>(null)
const loadState = ref<'loading' | 'ready' | 'error'>('loading')

// Opt-in run-trace detail drawer. Default-OFF so the stream DOM is unchanged;
// only flipping this localStorage flag makes log lines interactive.
const runTraceEnabled = ref(localStorage.getItem('opensquilla.logs.runTrace') === '1')
const selectedLine = ref<LogLine | null>(null)
const detailStateScope = ref('')
const detailRef = ref<HTMLElement | null>(null)
const detailCloseBtn = ref<HTMLButtonElement | null>(null)
const rt = useRunTrace()
let detailScopeSequence = 0
let detailInvokerEl: HTMLElement | null = null

let pollInterval: ReturnType<typeof setInterval> | null = null
let pollInFlight = false
let pollErrorShown = false
let isActive = false
let initialized = false
let initialLoadInFlight = false

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const totalCount = computed(() => allLines.value.length)
// One pass over the buffer instead of one full scan per level per change.
const levelCounts = computed(() => {
  const counts: Record<string, number> = { TRACE: 0, DEBUG: 0, INFO: 0, WARN: 0, ERROR: 0 }
  for (const l of allLines.value) {
    if (l.level in counts) counts[l.level]++
  }
  return counts
})

// Live count rendered inside each level-filter chip (the former count tiles).
function levelChipCount(level: string): string {
  return (levelCounts.value[level] ?? 0).toLocaleString()
}

const filteredLines = computed(() => {
  const term = debouncedSearch.value.toLowerCase()
  return allLines.value.filter(line => {
    if (!activeLevels.value.has(line.level)) return false
    if (term && !line.message.toLowerCase().includes(term)) return false
    return true
  })
})

const visibleCount = computed(() => filteredLines.value.length)

// The stream buffer caps at 2000 lines but the viewport only ever shows ~50, so
// mount the on-screen slice instead of the whole filtered set. Disabled on
// narrow viewports where the row reflows to a variable-height column layout.
const windowingEnabled = ref(true)
let windowMedia: MediaQueryList | null = null
const { visible: windowVisible, topPad: windowTopPad, bottomPad: windowBottomPad, onScroll, measure, scrollToEnd } =
  useFixedWindow<LogLine>(filteredLines, ROW_H, displayRef, 12)

const windowedLines = computed(() =>
  windowingEnabled.value
    ? windowVisible.value
    : filteredLines.value.map((item, index) => ({ item, index })))
const topPad = computed(() => (windowingEnabled.value ? windowTopPad.value : 0))
const bottomPad = computed(() => (windowingEnabled.value ? windowBottomPad.value : 0))

const fileLogEnabled = computed(() => status.value?.gateway_file_log?.enabled ?? false)
const filePath = computed(() => status.value?.gateway_file_log?.path || 'debug.log')

const rawLogEnabled = computed(() => status.value?.raw_turn_call_log?.enabled ?? false)
const rawSource = computed(() => status.value?.raw_turn_call_log?.source || 'off')
const rawPath = computed(() => status.value?.raw_turn_call_log?.directory?.path || '~/.opensquilla/logs')

const fileLogLabel = computed(() =>
  fileLogEnabled.value ? t('usageLogs.logs.fileLogOn') : t('usageLogs.logs.fileLogOff'))

const fileLogTitleText = computed(() =>
  fileLogEnabled.value
    ? t('usageLogs.logs.fileLogTitle', { path: filePath.value })
    : t('usageLogs.logs.fileLogOffTitle', { path: filePath.value }))

const rawLabel = computed(() =>
  rawLogEnabled.value ? t('usageLogs.logs.rawOn') : t('usageLogs.logs.rawOff'))

const rawTitleText = computed(() =>
  t('usageLogs.logs.rawTitle', { source: rawSource.value, path: rawPath.value }))

// A run-bearing line carries structured tool_calls in its raw JSON payload; the
// drawer renders those as a trace, falling back to the raw text otherwise.
const selectedTrace = computed<ChatHistoryMessage | null>(() => {
  const raw = selectedLine.value?.raw
  if (!raw || typeof raw !== 'string') return null
  const trimmed = raw.trim()
  if (!trimmed.startsWith('{')) return null
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>
    if (Array.isArray(parsed.tool_calls)) return parsed as ChatHistoryMessage
    return null
  } catch {
    return null
  }
})

const lineSteps = computed<NodeStep[]>(() =>
  selectedTrace.value ? nodeStepsFromHistoryMessage(selectedTrace.value) : [])

const lineSummary = computed<RunTraceSummary | undefined>(() => {
  if (!lineSteps.value.length) return undefined
  const hasError = lineSteps.value.some(step => step.isError)
  return {
    status: hasError ? 'error' : 'success',
    executor: undefined,
    elapsedMs: null,
    tokens: null,
    steps: lineSteps.value.length,
    loading: false,
  }
})

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

onMounted(() => {
  windowMedia = window.matchMedia(WINDOW_MIN_WIDTH)
  windowingEnabled.value = windowMedia.matches
  windowMedia.addEventListener('change', onWindowMediaChange)
  measure()
})

// Polling lives on activate/deactivate so a kept-alive but hidden Logs view
// neither tails nor reacts to document visibility changes. onActivated also
// fires on first mount, so all data ownership starts here without a duplicate
// onMounted request.
onActivated(() => {
  isActive = true
  document.addEventListener('visibilitychange', onVisibilityChange)
  if (!initialized) {
    initialized = true
    void loadData()
    return
  }
  if (initialLoadInFlight) return
  startPolling()
  void poll()
})

onDeactivated(() => {
  isActive = false
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  isActive = false
  stopPolling()
  if (searchTimer) clearTimeout(searchTimer)
  if (windowMedia) {
    windowMedia.removeEventListener('change', onWindowMediaChange)
    windowMedia = null
  }
  document.removeEventListener('visibilitychange', onVisibilityChange)
  document.removeEventListener('keydown', onDetailKeydown)
})

function startPolling() {
  if (pollInterval) return
  pollInterval = setInterval(poll, 3000)
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval)
    pollInterval = null
  }
}

function onWindowMediaChange(e: MediaQueryListEvent) {
  windowingEnabled.value = e.matches
  if (e.matches) nextTick(() => measure())
}

// Auto-scroll when filtered lines change and autoFollow is on
watch(filteredLines, () => {
  if (autoFollow.value) {
    nextTick(() => scrollToBottom())
  }
})

// Debounce search so typing doesn't re-scan the whole buffer per keystroke.
watch(searchText, (val) => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { debouncedSearch.value = val }, 150)
})

function onVisibilityChange() {
  if (isActive && !document.hidden) void poll()
}

watch(autoFollow, (val) => {
  if (val) scrollToBottom()
})

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function loadData() {
  if (initialLoadInFlight || !isActive) return
  initialLoadInFlight = true
  stopPolling()
  loadState.value = 'loading'
  try {
    await rpc.waitForConnection()
    if (!isActive) return
    cursor.value = 0
    allLines.value = []
    await loadStatus()
    if (!isActive) return
    await poll()
  } catch {
    if (allLines.value.length === 0) loadState.value = 'error'
  } finally {
    initialLoadInFlight = false
    if (isActive) startPolling()
  }
}

async function loadStatus() {
  if (!isActive) return
  try {
    status.value = await rpc.call<LogStatus>('logs.status', {})
  } catch {
    status.value = null
  }
}

async function poll() {
  if (!isActive) return
  if (pollInFlight) return
  const rpcClient = rpc.client
  if (!rpcClient) return
  if (document.hidden) return
  pollInFlight = true
  try {
    const data = await rpc.call<LogTailResponse>('logs.tail', { limit: 500, cursor: cursor.value, level: null })
    if (!isActive) return
    const lines: LogEntry[] = data.lines || data.entries || []
    if (lines.length > 0) {
      if (data.cursor != null) {
        cursor.value = data.cursor
      } else {
        cursor.value += lines.length
      }
      lines.forEach(entry => {
        if (typeof entry === 'string') {
          allLines.value.push({ level: guessLevel(entry), message: entry, raw: entry })
        } else {
          allLines.value.push({
            level: (entry.level || entry.lvl || 'INFO').toUpperCase(),
            message: entry.message || entry.msg || JSON.stringify(entry),
            ts: entry.timestamp || entry.ts || null,
            raw: typeof entry.raw === 'string' ? entry.raw : JSON.stringify(entry),
          })
        }
      })
      if (allLines.value.length > 2000) {
        allLines.value = allLines.value.slice(allLines.value.length - 2000)
      }
    }
    loadState.value = 'ready'
    pollErrorShown = false
  } catch (err) {
    loadState.value = 'error'
    if (!pollErrorShown) {
      console.warn('Log refresh failed: ' + (err instanceof Error ? err.message : 'unknown error'))
      pollErrorShown = true
    }
  } finally {
    pollInFlight = false
  }
}

function retryLoad() {
  if (allLines.value.length > 0) {
    void poll()
    return
  }
  void loadData()
}

function toggleLevel(level: string) {
  const next = new Set(activeLevels.value)
  if (next.has(level)) {
    next.delete(level)
  } else {
    next.add(level)
  }
  activeLevels.value = next
}

function openDetail(line: LogLine) {
  if (!runTraceEnabled.value) return
  detailInvokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
  detailStateScope.value = `log-line-${detailScopeSequence++}`
  selectedLine.value = line
  document.addEventListener('keydown', onDetailKeydown)
  nextTick(() => detailCloseBtn.value?.focus())
}

function closeDetail() {
  if (!selectedLine.value) return
  selectedLine.value = null
  document.removeEventListener('keydown', onDetailKeydown)
  if (detailInvokerEl && document.contains(detailInvokerEl)) detailInvokerEl.focus()
  detailInvokerEl = null
}

function onLineKeydown(event: KeyboardEvent, line: LogLine) {
  if (!runTraceEnabled.value) return
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    openDetail(line)
  }
}

// "view full" has no global modal here; the raw payload already shows in the
// drawer, so this is a no-op kept to satisfy the RunTrace contract.
function onShowResult() {}

function onDetailKeydown(event: KeyboardEvent) {
  if (!selectedLine.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDetail()
    return
  }
  if (event.key !== 'Tab') return
  const rootEl = detailRef.value
  if (!rootEl) return
  const focusables = Array.from(rootEl.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement as HTMLElement | null
  const inside = !!active && rootEl.contains(active)
  if (event.shiftKey && (!inside || active === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault()
    first.focus()
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function guessLevel(line: string): string {
  const u = line.toUpperCase()
  if (u.includes('ERROR')) return 'ERROR'
  if (u.includes('WARN')) return 'WARN'
  if (u.includes('INFO')) return 'INFO'
  if (u.includes('DEBUG')) return 'DEBUG'
  if (u.includes('TRACE')) return 'TRACE'
  return 'INFO'
}

function highlightParts(message: string): Array<{ text: string; match: boolean }> {
  const term = debouncedSearch.value
  if (!term) return [{ text: message, match: false }]
  const re = new RegExp(`(${escRegex(term)})`, 'gi')
  const parts: Array<{ text: string; match: boolean }> = []
  let lastIndex = 0
  for (const match of message.matchAll(re)) {
    const index = match.index ?? 0
    if (index > lastIndex) parts.push({ text: message.slice(lastIndex, index), match: false })
    parts.push({ text: match[0], match: true })
    lastIndex = index + match[0].length
  }
  if (lastIndex < message.length) parts.push({ text: message.slice(lastIndex), match: false })
  return parts.length ? parts : [{ text: message, match: false }]
}

function scrollToBottom() {
  // Route through the window so its internal scroll offset re-syncs to the new
  // scrollHeight after an append; otherwise the mounted slice goes stale.
  if (windowingEnabled.value) {
    scrollToEnd()
    return
  }
  const el = displayRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function escRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
</script>

<style scoped>
/* Header uses the shared .control-stage primitive; only the quiet status line
   (and the single abnormal-state warn pill in the actions) is Logs-specific. */
.lg-status-line {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  margin: var(--sp-2) 0 0;
}

.lg-status-line__seg {
  cursor: help;
}

.lg-status-line__seg--warn {
  color: var(--warn);
}

.lg-status-line__sep {
  color: var(--text-dim);
}

/* Recording indicator inside the raw-capture warn pill; inherits the pill's
   warn text color. */
.control-pill .dot {
  background: currentColor;
  border-radius: 50%;
  display: inline-block;
  flex-shrink: 0;
  height: 6px;
  width: 6px;
}

.lg-toolbar {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-4);
}

.lg-toolbar__label {
  color: var(--text-dim);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.lg-levels {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
}

.lg-levels__row {
  display: flex;
  gap: 6px;
}

.lg-level-btn {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font-size: 11px;
  font-weight: 600;
  gap: 5px;
  letter-spacing: 0.04em;
  padding: 4px 10px;
  text-transform: uppercase;
  transition: background var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.lg-level-btn:hover {
  border-color: var(--accent);
  color: var(--text);
}

.lg-level-btn.is-active {
  background: var(--bg-hover);
  color: var(--text);
}

.lg-level-btn__dot {
  border-radius: var(--radius-full);
  display: inline-block;
  height: 6px;
  width: 6px;
}

.lg-level-btn--trace .lg-level-btn__dot { background: var(--text-dim); }
.lg-level-btn--debug .lg-level-btn__dot { background: var(--accent); }
.lg-level-btn--info  .lg-level-btn__dot { background: var(--ok); }
.lg-level-btn--warn  .lg-level-btn__dot { background: var(--warn-fill); }
.lg-level-btn--error .lg-level-btn__dot { background: var(--danger); }

/* Live count inside each level chip (replaces the former count-tile band). */
.lg-level-btn__count {
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}
.lg-level-btn.is-active .lg-level-btn__count {
  color: var(--text-muted);
}

/* Stream footer readout: in-view / buffered counts, quiet mono. */
.lg-stream__foot {
  border-top: 1px solid var(--border);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: var(--sp-2) var(--sp-4);
  text-align: right;
}

.lg-search-wrap {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex: 1;
  gap: 8px;
  min-width: 200px;
  padding: 0 var(--sp-3);
}

.lg-search-icon {
  color: var(--text-dim);
  display: inline-flex;
  flex-shrink: 0;
}

.lg-search-input {
  background: transparent;
  border: none;
  color: var(--text);
  font-size: var(--fs-sm);
  outline: none;
  padding: 8px 0;
  width: 100%;
}

.lg-search-input::placeholder {
  color: var(--text-dim);
}

.lg-toggle {
  align-items: center;
  cursor: pointer;
  display: inline-flex;
  gap: 8px;
  user-select: none;
}

.lg-toggle__label {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.lg-stream {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  min-height: 320px;
  overflow: hidden;
}

.lg-display {
  flex: 1;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  max-height: 60vh;
  overflow: auto;
  padding: var(--sp-3);
}

.lg-display__placeholder {
  align-items: center;
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  justify-content: center;
  min-height: 200px;
  text-align: center;
}

.lg-display__placeholder-icon {
  color: var(--text-dim);
  display: inline-flex;
}

.lg-display__placeholder-icon--error {
  color: var(--danger);
}

.lg-breadcrumb {
  align-items: center;
  color: var(--text-dim);
  display: flex;
  font-size: var(--fs-xs);
  font-weight: 650;
  gap: 6px;
  letter-spacing: 0;
  line-height: 1.4;
}

.lg-breadcrumb__link {
  color: var(--text-muted);
  text-decoration: none;
}

.lg-breadcrumb__link:hover {
  color: var(--text);
}

.lg-breadcrumb__link:focus-visible {
  border-radius: var(--radius-sm);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.lg-breadcrumb__separator {
  color: var(--text-dim);
}

.lg-stream__error {
  align-items: center;
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--danger) 35%, var(--border));
  border-radius: var(--radius-md);
  color: var(--danger);
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-3);
  justify-content: space-between;
  margin-bottom: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
}

.lg-spinner {
  animation: lg-spin 1s linear infinite;
  border: 2px solid var(--border);
  border-radius: var(--radius-full);
  border-top-color: var(--accent);
  display: inline-block;
  height: 20px;
  width: 20px;
}

@keyframes lg-spin {
  to { transform: rotate(360deg); }
}

/* Windowed body: top/bottom padding stands in for the off-screen rows so the
   scrollbar geometry matches the full buffer. No color — padding passthrough. */
.lg-window {
  display: flow-root;
}

.lg-line {
  align-items: baseline;
  border-radius: var(--radius-sm);
  display: flex;
  gap: 10px;
  padding: 2px 6px;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Zebra by real line index, not DOM position, so the stripe stays put while
   the window mounts a moving slice. */
.lg-line--alt {
  background: color-mix(in srgb, var(--bg-elevated) 40%, transparent);
}

.lg-line__ts {
  color: var(--text-dim);
  flex-shrink: 0;
  font-size: 11px;
  width: 160px;
}

.lg-line__ts--empty {
  width: 160px;
}

.lg-line__lvl {
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  min-width: 52px;
  padding: 1px 6px;
  text-align: center;
  text-transform: uppercase;
  white-space: nowrap;
}

.lg-line__lvl--trace { background: color-mix(in srgb, var(--text-dim) 15%, transparent); color: var(--text-dim); }
.lg-line__lvl--debug { background: color-mix(in srgb, var(--accent) 12%, transparent); color: var(--accent); }
.lg-line__lvl--info  { background: color-mix(in srgb, var(--ok) 12%, transparent); color: var(--ok); }
.lg-line__lvl--warn  { background: color-mix(in srgb, var(--warn) 12%, transparent); color: var(--warn); }
.lg-line__lvl--error { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); }

.lg-line__msg {
  color: var(--text-muted);
  flex: 1;
  min-width: 0;
}

.lg-line__msg :deep(.lg-line__match) {
  background: color-mix(in srgb, var(--accent) 25%, transparent);
  border-radius: var(--radius-xs);
  color: var(--text);
  padding: 0 2px;
}

.lg-line--interactive {
  cursor: pointer;
}

.lg-line--interactive:hover {
  background: var(--bg-hover);
}

.lg-line--interactive:focus-visible {
  box-shadow: var(--focus-ring-inset);
  outline: none;
}

.lg-detail-overlay {
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: flex-end;
  position: fixed;
  z-index: 300;
}

.lg-detail {
  background: var(--bg-surface);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  height: 100%;
  width: min(560px, 100%);
}

/* Symmetric open/close: scrim fades, panel slides from the right both ways
   (was an entrance-only keyframe that popped on close). */
.lg-detail-enter-active { transition: opacity var(--dur-base) var(--ease-out); }
.lg-detail-leave-active { transition: opacity var(--dur-fast) var(--ease-in); }
.lg-detail-enter-from,
.lg-detail-leave-to { opacity: 0; }
.lg-detail-enter-active .lg-detail { transition: transform var(--dur-base) var(--ease-out); }
.lg-detail-leave-active .lg-detail { transition: transform var(--dur-fast) var(--ease-in); }
.lg-detail-enter-from .lg-detail { transform: translateX(24px); }
.lg-detail-leave-to .lg-detail { transform: translateX(24px); }

.lg-detail__head {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-2);
  justify-content: space-between;
  padding: var(--sp-3) var(--sp-4);
}

.lg-detail__title {
  color: var(--text);
  font-size: var(--fs-md);
  font-weight: 600;
}

.lg-detail__body {
  background: var(--bg);
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-4);
}

.lg-detail__raw {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (prefers-reduced-motion: reduce) {
  .lg-detail-enter-active,
  .lg-detail-leave-active,
  .lg-detail-enter-active .lg-detail,
  .lg-detail-leave-active .lg-detail {
    transition: none;
  }
}

@media (max-width: 980px) {
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .lg-stage .control-stage__header {
    flex-direction: column;
  }

  .lg-stage .control-stage__header .btn {
    align-self: flex-start;
    width: auto;
  }

  .lg-toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .lg-search-wrap {
    min-width: 0;
  }
}

@media (max-width: 480px) {
  .stat-row {
    grid-template-columns: 1fr;
  }

  .lg-line {
    flex-direction: column;
    gap: 2px;
    padding: 6px;
  }

  .lg-line__ts,
  .lg-line__ts--empty {
    width: auto;
  }
}
</style>
