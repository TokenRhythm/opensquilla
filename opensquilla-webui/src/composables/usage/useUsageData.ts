import { ref, computed, onUnmounted, onActivated, onDeactivated } from 'vue'
import { useRouter } from 'vue-router'
import { useDocumentEvent } from '@/composables/useDocumentEvent'
import { useUsagePreferences } from '@/composables/usage/useUsagePreferences'
import {
  naturalRangeStartMs,
  requestUsageSnapshot,
} from '@/composables/usage/useUsageQuery'
import { useUsageTotals } from '@/composables/usage/useUsageTotals'
import { useUsageChartRows } from '@/composables/usage/useUsageChartRows'
import { useUsageModelCards } from '@/composables/usage/useUsageModelCards'
import { useUsageSessionRows } from '@/composables/usage/useUsageSessionRows'
import { formatUsageCost, effectiveCnyPerUsd } from '@/composables/usage/nativeBilling'
import { buildUsageCsv } from '@/composables/usage/usageCsv'
import { useRpcStore } from '@/stores/rpc'
import { downloadText } from '@/utils/browser'
import i18n from '@/i18n'
import type {
  BreakdownRow,
  ModelBreakdownItem,
  ModelCard,
  SessionRow,
  TableColumn,
  UsageRangeSelection,
  UsageSnapshot,
  UsageStatusData,
} from '@/types/usage'

const t = i18n.global.t

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Display fallback only — used when neither the gateway nor the snapshot's
// receipts provide the canonical CNY-per-USD rate (see `cnyRate` below).
const FALLBACK_CNY_RATE = 7.25

type CostFormatOptions = {
  decimals?: number
  source?: object
}

// Column labels are resolved through i18n in the `tableColumns` computed so they
// react to locale changes; this maps each column key to its message key.
const TABLE_COLUMN_KEYS: Array<{ key: string; labelKey: string }> = [
  { key: 'session', labelKey: 'usageLogs.columns.session' },
  { key: 'updated_at', labelKey: 'usageLogs.columns.modified' },
  { key: 'input_tokens', labelKey: 'usageLogs.columns.input' },
  { key: 'output_tokens', labelKey: 'usageLogs.columns.output' },
  { key: 'cache_read_tokens', labelKey: 'usageLogs.columns.cacheRead' },
  { key: 'cache_write_tokens', labelKey: 'usageLogs.columns.cacheWrite' },
  { key: 'cost_usd', labelKey: 'usageLogs.columns.cost' },
  { key: 'cost_source', labelKey: 'usageLogs.columns.source' },
  { key: 'model', labelKey: 'usageLogs.columns.model' },
]

const SORTABLE_COLS = ['session', 'updated_at', 'input_tokens', 'output_tokens', 'cost_usd', 'model']

export function useUsageData() {
// ---------------------------------------------------------------------------
// Stores & Router
// ---------------------------------------------------------------------------

const router = useRouter()
const rpc = useRpcStore()

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const {
  currency,
  range,
  setCurrency,
  setRange: persistRange,
} = useUsagePreferences()
const sortCol = ref('updated_at')
const sortAsc = ref(false)
const chartMode = ref<'tokens' | 'cost'>('tokens')
const expandedSessions = ref<Set<string>>(new Set())

const usageSnapshot = ref<UsageSnapshot | null>(null)
const usageLoading = ref(false)
const usageError = ref<string | null>(null)
const sessions = computed<SessionRow[]>(() => usageSnapshot.value?.sessions || [])
const lastStatus = computed<UsageStatusData | null>(() => {
  const snapshot = usageSnapshot.value
  if (!snapshot) return null
  return {
    sessions: snapshot.sessions,
    totalSessions: snapshot.totals.sessions,
    totalTokens: snapshot.totals.totalTokens,
    totalCostUsd: snapshot.totals.cost,
  }
})

let autoRefreshId: ReturnType<typeof setInterval> | null = null
let loadGeneration = 0

// The rate the ledger normalized CNY receipts with, so every derived CNY
// figure (totals hint, CSV export, per-row conversions) agrees with
// receipt-exact amounts instead of drifting on a hardcoded display rate.
const cnyRate = computed(() => effectiveCnyPerUsd(usageSnapshot.value) ?? FALLBACK_CNY_RATE)

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const tableColumns = computed<TableColumn[]>(() =>
  TABLE_COLUMN_KEYS.map(({ key, labelKey }) => ({ key, label: t(labelKey) })))
const sortableCols = computed(() => SORTABLE_COLS)

// usage.query already returns server-attributed rows for the requested range.
// The legacy normalizer applies its explicit approximation before publishing
// the snapshot, so renderers never re-filter authoritative ledger rows.
const visibleSessions = computed(() => sessions.value)

const undatedHiddenCount = computed(() => {
  return 0
})

const rangeHiddenHint = computed(() => {
  const snapshot = usageSnapshot.value
  if (!snapshot) return ''
  const notices: string[] = []
  if (snapshot.timezoneFallback) {
    notices.push(t('usageLogs.coverage.timezoneFallback', {
      requested: snapshot.timezoneFallback.requestedTimezone,
      effective: snapshot.timezoneFallback.effectiveTimezone,
    }))
  }
  if (snapshot.mode === 'session_approximation') {
    notices.push(t('usageLogs.coverage.approximate'))
  } else if (snapshot.mode === 'ledger_partial') {
    notices.push(t('usageLogs.coverage.partial'))
  }
  if (snapshot.coverage.legacyIncludedInTotals) {
    notices.push(t('usageLogs.coverage.legacyIncluded'))
  }
  if (snapshot.totals.estimatedEventCount > 0) {
    notices.push(t('usageLogs.coverage.estimated', {
      count: snapshot.totals.estimatedEventCount,
    }))
  }
  if (snapshot.totals.missingCostEntries > 0) {
    notices.push(t('usageLogs.coverage.unpriced', { count: snapshot.totals.missingCostEntries }))
  }
  if (snapshot.coverage.nativeBilling.pendingReceiptCount > 0) {
    notices.push(t('usageLogs.coverage.pendingBilling', {
      count: snapshot.coverage.nativeBilling.pendingReceiptCount,
    }))
  }
  if (snapshot.coverage.nativeBilling.missingConfirmedReceiptCount > 0) {
    notices.push(t('usageLogs.coverage.nativeBillingMissing', {
      count: snapshot.coverage.nativeBilling.missingConfirmedReceiptCount,
    }))
  }
  return notices.join(' · ')
})

const serverTotals = computed(() => usageSnapshot.value?.totals || null)
const serverModels = computed(() =>
  usageSnapshot.value?.source === 'usage_ledger' ? usageSnapshot.value.models : null)
const serverDays = computed(() =>
  usageSnapshot.value?.source === 'usage_ledger' ? usageSnapshot.value.days : null)

const {
  usageTotals,
  totalTokensDisplay,
  tokensBreakdownParts,
  totalCostDisplay,
  costHintText,
  costHintTitle,
  sessionCountDisplay,
  avgCostDisplay,
} = useUsageTotals({
  visibleSessions,
  serverTotals,
  currency,
  cnyRate,
  rowVal,
  fmtCost,
  sourceCompositionHint,
})

const { chartCaption, chartRows } = useUsageChartRows({
  visibleSessions,
  serverDays,
  chartMode,
  rowVal,
  fmtCost,
  fmtNum,
})

const { modelCards, modelsMeta } = useUsageModelCards({
  visibleSessions,
  serverModels,
  rowVal,
})

const { sortedRows, sessionsMeta } = useUsageSessionRows({
  visibleSessions,
  rangeHiddenHint,
  sortCol,
  sortAsc,
  rowVal,
  numericRowVal,
  sessionTimestamp,
  relTime,
  sortVal,
})

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// The initial fetch and the 60s refresh timer both live on activate/deactivate,
// so a kept-alive but hidden Usage view stops polling. onActivated fires on
// first mount too, so it owns the one-time fetch as well — no separate
// onMounted fetch, which would double-fetch Usage data on first paint.
onActivated(() => {
  if (!autoRefreshId) autoRefreshId = setInterval(loadData, 60000)
  // A returning view refreshes immediately so cached numbers don't linger.
  loadData()
})

onDeactivated(() => {
  if (autoRefreshId) {
    clearInterval(autoRefreshId)
    autoRefreshId = null
  }
})

onUnmounted(() => {
  if (autoRefreshId) {
    clearInterval(autoRefreshId)
    autoRefreshId = null
  }
})

useDocumentEvent('visibilitychange', onVisibilityChange)

function onVisibilityChange() {
  if (document.visibilityState === 'visible') loadData()
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function setSort(col: string) {
  if (sortCol.value === col) {
    sortAsc.value = !sortAsc.value
  } else {
    sortCol.value = col
    sortAsc.value = false
  }
}

function openSession(key: string) {
  if (key && key !== '—') {
    router.push({ path: '/chat', query: { session: key } })
  }
}

function toggleModelExpand(row: { raw: SessionRow; rowIdentity: string }) {
  const key = row.rowIdentity
  if (expandedSessions.value.has(key)) {
    expandedSessions.value.delete(key)
  } else {
    expandedSessions.value.add(key)
  }
}

// 'superseded' means a newer load took over (auto-refresh tick, visibility
// refresh); that newer load fetches with the freshest range selection, so the
// superseded caller must neither report failure nor roll anything back.
type LoadOutcome = 'loaded' | 'superseded' | 'failed' | 'hidden'

async function requestLoad(): Promise<LoadOutcome> {
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return 'hidden'
  const generation = ++loadGeneration
  usageLoading.value = true
  usageError.value = null
  try {
    const snapshot = await requestUsageSnapshot(
      rpc,
      range.value as UsageRangeSelection,
      { cachedSnapshot: usageSnapshot.value },
    )
    if (generation !== loadGeneration) return 'superseded'
    usageSnapshot.value = snapshot
    return 'loaded'
  } catch (error) {
    if (generation !== loadGeneration) return 'superseded'
    // A refresh or range request must never replace already-rendered,
    // trustworthy data with a page-level error.  The caller that changed the
    // range restores the previous selection below, keeping the cached
    // snapshot and its visible range label aligned.
    if (!usageSnapshot.value) {
      usageError.value = error instanceof Error ? error.message : String(error)
    }
    return 'failed'
  } finally {
    if (generation === loadGeneration) usageLoading.value = false
  }
}

async function loadData(): Promise<boolean> {
  return (await requestLoad()) === 'loaded'
}

function setRange(nextRange: string) {
  const previousRange = range.value
  persistRange(nextRange)
  void requestLoad().then(outcome => {
    // Only this call's own failure (or a hidden-document no-op) may revert:
    // a superseded request means a concurrent refresh already fetched the
    // new range and published its snapshot, so rolling the selection back
    // would mislabel that fresher data and persist the wrong preference.
    if (
      (outcome === 'failed' || outcome === 'hidden')
      && range.value === nextRange
      && usageSnapshot.value
    ) {
      persistRange(previousRange)
    }
  })
}

function exportCsv() {
  const snapshot = usageSnapshot.value
  const rate = cnyRate.value
  const csv = buildUsageCsv(snapshot, visibleSessions.value, rate)
  const suffix = range.value === 'all' ? 'all' : `${range.value}d`
  const coverageSuffix = snapshot?.mode === 'session_approximation'
    ? '-approximate'
    : snapshot?.mode === 'ledger_partial' ? '-partial' : ''
  download(`opensquilla-usage-${suffix}${coverageSuffix}-cny${rate}.csv`, 'text/csv', csv)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function rangeCutoffMs(r: string): number | null {
  return naturalRangeStartMs(r as UsageRangeSelection)
}

function fmtCost(usd: number | null | undefined, opts?: CostFormatOptions): string {
  const decimals = (opts && opts.decimals != null) ? opts.decimals : 4
  return formatUsageCost(
    usd,
    currency.value,
    cnyRate.value,
    decimals,
    opts?.source as Record<string, unknown> | undefined,
  )
}

function fmtNum(n: number | null | undefined): string {
  if (n == null) return '—'
  const v = Number(n)
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1_000) return (v / 1_000).toFixed(1) + 'K'
  return String(v)
}

function rowVal(row: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (row[key] != null) return row[key]
  }
  return null
}

function numericRowVal(row: Record<string, unknown>, ...keys: string[]): number | null {
  const value = rowVal(row, ...keys)
  if (value == null || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function sessionTimestamp(row: SessionRow): number | null {
  for (const key of ['endedAt', 'ended_at', 'updatedAt', 'updated_at', 'startedAt', 'started_at', 'createdAt', 'created_at']) {
    const value = numericRowVal(row, key)
    if (value != null) return value
  }
  return null
}

function sortVal(row: SessionRow, key: string): string | number {
  switch (key) {
    case 'session':
      return (rowVal(row, 'session', 'sessionKey', 'key') || '') as string
    case 'updated_at':
      return sessionTimestamp(row) || 0
    case 'input_tokens':
      return Number(rowVal(row, 'input_tokens', 'inputTokens') || 0)
    case 'output_tokens':
      return Number(rowVal(row, 'output_tokens', 'outputTokens') || 0)
    case 'cache_read_tokens':
      return Number(rowVal(row, 'cache_read_tokens', 'cacheReadTokens') || 0)
    case 'cache_write_tokens':
      return Number(rowVal(row, 'cache_write_tokens', 'cacheWriteTokens') || 0)
    case 'cost_usd':
      return Number(rowVal(row, 'cost_usd', 'costUsd') || 0)
    default:
      return (rowVal(row, key) || '') as string
  }
}

function costSource(row: SessionRow | ModelBreakdownItem): string {
  return String(rowVal(row as Record<string, unknown>, 'cost_source', 'costSource') || 'none')
}

function costSourceClass(source: string): string {
  const known = ['provider_billed', 'provider_billed_prorated', 'opensquilla_estimate', 'mixed', 'unavailable', 'none']
  if (known.includes(source)) return source
  return 'none'
}

// A stable source key (independent of locale) used both for labels and for the
// composition-hint tally; the user-facing strings are looked up from it.
function costSourceKey(row: SessionRow | ModelBreakdownItem): string {
  const source = costSource(row)
  const ephemeral = Boolean(rowVal(row as Record<string, unknown>, 'cost_ephemeral', 'costEphemeral'))
  if (ephemeral) return 'ephemeral'
  switch (source) {
    case 'provider_billed': return 'actual'
    case 'provider_billed_prorated': return 'actual'
    case 'opensquilla_estimate': return 'estimated'
    case 'mixed': return 'mixed'
    case 'unavailable': return 'unpriced'
    default: return 'none'
  }
}

function costSourceLabel(row: SessionRow | ModelBreakdownItem): string {
  return t(`usageLogs.costSource.${costSourceKey(row)}.label`)
}

function costSourceTooltip(row: SessionRow | ModelBreakdownItem): string {
  return t(`usageLogs.costSource.${costSourceKey(row)}.tooltip`)
}

function costSourceClasses(row: SessionRow | ModelBreakdownItem): Record<string, boolean> {
  const source = costSource(row)
  const ephemeral = Boolean(rowVal(row as Record<string, unknown>, 'cost_ephemeral', 'costEphemeral'))
  return {
    [`usage-source--${costSourceClass(source)}`]: true,
    'usage-source--ephemeral': ephemeral,
  }
}

function costSourceClassesForBreakdown(m: BreakdownRow): Record<string, boolean> {
  return costSourceClasses(m as unknown as ModelBreakdownItem)
}

function costSourceLabelForBreakdown(m: BreakdownRow): string {
  return costSourceLabel(m as unknown as ModelBreakdownItem)
}

function costSourceTooltipForBreakdown(m: BreakdownRow): string {
  return costSourceTooltip(m as unknown as ModelBreakdownItem)
}

function costSourceClassesForModelCard(m: ModelCard): Record<string, boolean> {
  return costSourceClasses(m as unknown as ModelBreakdownItem)
}

function costSourceLabelForModelCard(m: ModelCard): string {
  return costSourceLabel(m as unknown as ModelBreakdownItem)
}

function costSourceTooltipForModelCard(m: ModelCard): string {
  const base = costSourceTooltip(m as unknown as ModelBreakdownItem)
  if (m.anyCacheBlind) {
    return `${base} ${t('usageLogs.costSource.cacheBlindHint')}`
  }
  return base
}

function sourceCompositionHint(rows: SessionRow[]): string {
  const order = ['actual', 'estimated', 'mixed', 'unpriced', 'ephemeral']
  const counts: Record<string, number> = { actual: 0, estimated: 0, mixed: 0, unpriced: 0, ephemeral: 0 }
  rows.forEach(row => {
    const key = costSourceKey(row)
    if (counts[key] != null) counts[key] += 1
  })
  return order
    .filter(key => counts[key] > 0)
    .map(key => `${t(`usageLogs.costSource.${key}.short`)} ${counts[key]}`)
    .join(' · ')
}

function modelDisplayLabel(row: SessionRow): string {
  const bd = row.modelBreakdown
  if (Array.isArray(bd) && bd.length > 0) {
    return bd.length > 1
      ? t('usageLogs.sessions.autoModels', { count: bd.length })
      : (bd[0].model || row.model || '—')
  }
  return row.model || '—'
}

function rowKey(row: SessionRow): string {
  return (rowVal(row, 'session', 'sessionKey', 'key') || '') as string
}

function rowBreakdown(row: SessionRow): BreakdownRow[] {
  const bd = row.modelBreakdown || []
  const totalCost = bd.reduce((acc, m) => acc + (Number(m.costUsd) || 0), 0)
  return bd.map(m => {
    const tokens = (Number(m.inputTokens) || 0) + (Number(m.outputTokens) || 0)
    const cost = Number(m.costUsd) || 0
    const share = totalCost > 0 ? (cost / totalCost) * 100 : 0
    const provider = (m.model || '').split('/')[0] || ''
    const name = (m.model || '').split('/').slice(1).join('/') || m.model || 'unknown'
    return {
      model: m.model || '',
      provider,
      name,
      tokens,
      cost,
      share,
      costSource: m.costSource,
      cost_source: m.cost_source,
      costSourceCounts: m.costSourceCounts,
      nativeBilledByCurrency: m.nativeBilledByCurrency,
      pendingBillingReceiptCount: m.pendingBillingReceiptCount,
      nativeBillingExpectedReceiptCount: m.nativeBillingExpectedReceiptCount,
      nativeBillingMissingConfirmedReceiptCount:
        m.nativeBillingMissingConfirmedReceiptCount,
    }
  })
}

function rowBreakdownTotalTokens(row: SessionRow): number {
  const bd = row.modelBreakdown || []
  return bd.reduce((acc, m) => acc + (Number(m.inputTokens) || 0) + (Number(m.outputTokens) || 0), 0)
}

function rowBreakdownTotalCost(row: SessionRow): number {
  const bd = row.modelBreakdown || []
  return bd.reduce((acc, m) => acc + (Number(m.costUsd) || 0), 0)
}

function rowBreakdownAnyProrated(row: SessionRow): boolean {
  const bd = row.modelBreakdown || []
  return bd.some(m => {
    const src = String(m.costSource || m.cost_source || '')
    return src === 'provider_billed_prorated'
  })
}

function relTime(timestamp: number | string): string {
  const d = typeof timestamp === 'number' ? new Date(timestamp) : new Date(timestamp)
  if (isNaN(d.getTime())) return String(timestamp)

  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 10) return t('usageLogs.relTime.justNow')
  if (diffSec < 60) return t('usageLogs.relTime.seconds', { n: diffSec })
  if (diffMin < 60) return t('usageLogs.relTime.minutes', { n: diffMin })
  if (diffHour < 24) return t('usageLogs.relTime.hours', { n: diffHour })
  if (diffDay < 7) return t('usageLogs.relTime.days', { n: diffDay })
  return d.toLocaleDateString()
}

function download(filename: string, mime: string, content: string) {
  downloadText(filename, mime, content)
}

  return {
    currency,
    cnyRate,
    sessions,
    sortCol,
    sortAsc,
    chartMode,
    range,
    lastStatus,
    usageLoading,
    usageError,
    expandedSessions,
    tableColumns,
    sortableCols,
    visibleSessions,
    undatedHiddenCount,
    rangeHiddenHint,
    usageTotals,
    totalTokensDisplay,
    tokensBreakdownParts,
    totalCostDisplay,
    costHintText,
    costHintTitle,
    sessionCountDisplay,
    avgCostDisplay,
    chartCaption,
    chartRows,
    modelCards,
    modelsMeta,
    sortedRows,
    sessionsMeta,
    setCurrency,
    setRange,
    setSort,
    openSession,
    toggleModelExpand,
    loadData,
    exportCsv,
    rangeCutoffMs,
    fmtCost,
    fmtNum,
    rowVal,
    numericRowVal,
    sessionTimestamp,
    sortVal,
    costSource,
    costSourceClass,
    costSourceLabel,
    costSourceTooltip,
    costSourceClasses,
    costSourceClassesForBreakdown,
    costSourceLabelForBreakdown,
    costSourceTooltipForBreakdown,
    costSourceClassesForModelCard,
    costSourceLabelForModelCard,
    costSourceTooltipForModelCard,
    sourceCompositionHint,
    modelDisplayLabel,
    rowKey,
    rowBreakdown,
    rowBreakdownTotalTokens,
    rowBreakdownTotalCost,
    rowBreakdownAnyProrated,
    relTime,
    download,
  }
}
