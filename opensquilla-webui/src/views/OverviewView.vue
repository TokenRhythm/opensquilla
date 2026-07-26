<template>
  <div class="ov-stage control-stage control-stage--spacious">
    <!-- Status line: sits directly on the canvas (no card) per the chosen
         direction. Dot-glyph + word + inline counts left; freshness + actions
         right. The small ok-tinted glyph chip is the one warm accent. -->
    <section class="ov-statusline" :class="stripClass" :aria-label="t('sessions.overview.healthSummary')">
      <span class="ov-status-glyph" aria-hidden="true">
        <Icon :name="healthLoading ? 'refresh' : statusGlyphIcon" :size="15" />
      </span>
      <strong class="ov-status-word" :title="statusSummary">{{ statusLabelText }}</strong>
      <div v-if="!healthLoading" class="ov-status-counts">
        <button
          v-for="chip in impactChips"
          :key="chip.key"
          type="button"
          class="ov-count"
          :class="{ 'is-hot': chip.n > 0, [`is-${chip.tone}`]: chip.n > 0 }"
          @click="scrollToHealth"
        ><b>{{ chip.n }}</b> {{ chip.label }}</button>
      </div>
      <div class="ov-status-actions">
        <span v-if="!healthLoading" class="ov-freshness" aria-live="polite">
          {{ t('sessions.overview.checkedAgo', { time: relTime(healthCheckedAt) }) }} ·
          <button
            type="button"
            class="ov-rerun"
            :disabled="healthLoading"
            @click="loadHealth({ deep: true })"
          >
            {{ t('sessions.overview.rerunChecks') }}
          </button>
        </span>
        <button
          v-if="diagnoseVisible"
          class="btn btn--primary btn--sm"
          type="button"
          :title="t('sessions.overview.diagnoseWithAgent')"
          @click="diagnoseWithAgent"
        >
          <Icon name="chat" :size="14" />
          <span>{{ t('sessions.overview.diagnoseWithAgent') }}</span>
        </button>
        <button class="btn btn--ghost" :title="t('sessions.refresh')" :disabled="refreshing" @click="refresh">
          <Icon name="refresh" :size="16" />
          <span>{{ refreshing ? t('sessions.refreshing') : t('sessions.refresh') }}</span>
        </button>
        <button class="btn btn--primary" :title="t('sessions.overview.openChat')" @click="router.push('/chat')">
          <Icon name="chat" :size="16" />
          <span>{{ t('sessions.overview.openChat') }}</span>
        </button>
      </div>
    </section>

    <!-- Three real KPIs — quiet Settings-style cards, display numerals. -->
    <section class="ov-kpis" :aria-label="t('sessions.overview.title')">
      <button class="control-stat control-stat--clickable" type="button" @click="router.push('/usage')">
        <div class="control-stat__icon"><Icon name="usage" :size="18" /></div>
        <div class="control-stat__label">{{ t('sessions.overview.totalTokens') }}</div>
        <div class="control-stat__value">{{ tokensDisplay }}</div>
        <div class="control-stat__hint">{{ costLine }}</div>
      </button>

      <button class="control-stat control-stat--clickable" type="button" :title="t('sessions.overview.totalSessionsTitle')" @click="router.push('/sessions')">
        <div class="control-stat__icon"><Icon name="sessions" :size="18" /></div>
        <div class="control-stat__label">{{ t('sessions.overview.totalSessions') }}</div>
        <div class="control-stat__value">{{ sessionsCount }}</div>
        <div class="control-stat__hint">{{ t('sessions.overview.viewAll') }}</div>
      </button>

      <button v-if="channelStats.total > 0" class="control-stat control-stat--clickable" type="button" @click="router.push('/channels')">
        <div class="control-stat__icon"><Icon name="channels" :size="18" /></div>
        <div class="control-stat__label">{{ t('console.overview.channelsChip') }}</div>
        <div class="control-stat__value">{{ channelStats.total }}</div>
        <div class="control-stat__hint">{{ channelChipHint }}</div>
      </button>

      <div class="control-stat control-stat--static">
        <div class="control-stat__icon"><Icon name="cron" :size="18" /></div>
        <div class="control-stat__label">{{ t('sessions.overview.uptime') }}</div>
        <div class="control-stat__value control-stat__value--mono">{{ uptime }}</div>
        <div class="control-stat__hint">{{ versionLine }}</div>
      </div>
    </section>

    <!-- Environment readout: gateway / config path / agent / provider as a quiet
         copyable footer strip, not raw hero content. Config path is abbreviated
         with the full value in the title tooltip + a copy button. -->
    <section class="ov-readout" :aria-label="t('sessions.overview.environment')">
      <span v-for="item in readoutItems" :key="item.key" class="ov-readout__kv">
        <b>{{ item.label }}</b>
        <code :title="item.full">{{ item.display }}</code>
        <button
          v-if="item.copy"
          type="button"
          class="ov-readout__copy"
          :class="{ 'ov-readout__copy--ok': copiedCommandKey === item.key }"
          :title="copiedCommandKey === item.key ? t('setup.toast.copiedCommand') : t('sessions.overview.copyCommand')"
          :aria-label="copiedCommandKey === item.key ? t('setup.toast.copiedCommand') : t('sessions.overview.copyCommand')"
          @click="copyCommand(item.full, item.key)"
        >
          <Icon :name="copiedCommandKey === item.key ? 'check' : 'copy'" :size="13" />
        </button>
      </span>
      <span v-if="latencyLine" class="ov-readout__kv ov-readout__latency" aria-live="polite">
        <b>{{ t('sessions.overview.latency') }}</b>
        <code>{{ latencyLine }}</code>
      </span>
      <span class="ov-readout__version">v{{ statusData?.version || '—' }} · {{ uptime }}</span>
    </section>

    <section id="overview-health" class="health-findings" :aria-label="t('sessions.overview.healthFindings')">
      <template v-if="healthLoading">
        <article class="health-empty control-card">{{ t('sessions.overview.loadingHealth') }}</article>
      </template>
      <template v-else-if="groupedFindings.length === 0">
        <article class="health-empty control-card">{{ t('sessions.overview.noFindings') }}</article>
      </template>
      <template v-else>
        <section
          v-for="group in groupedFindings"
          :key="group.title"
          class="health-finding-group"
        >
          <header class="health-finding-group__header">
            <div>
              <h2>{{ group.title }}</h2>
              <p>{{ group.note }}</p>
            </div>
            <span>{{ group.findings.length }}</span>
          </header>
          <article
            v-for="(finding, fIdx) in group.findings"
            :key="finding.id || fIdx"
            class="health-finding control-card"
            :class="`is-${findingTone(findingGroupKind(finding))}`"
          >
            <div class="health-finding__marker" aria-hidden="true">
              <span class="health-finding__dot"></span>
              <span class="health-finding__line"></span>
            </div>
            <div class="health-finding__body">
              <div class="health-finding__meta">
                <span class="health-surface">{{ impactLabel(impactValue(finding)) }} · {{ finding.surface || 'system' }}</span>
                <span
                  v-if="findingBadges(finding)"
                  class="health-chip"
                  :class="findingBadgeClass(finding)"
                >
                  {{ findingBadgeText(finding) }}
                </span>
                <button
                  v-if="findingSettingsLink(finding)"
                  type="button"
                  class="health-settings-link"
                  @click="openFindingSettings(finding)"
                >
                  {{ t('sessions.overview.openSettings') }}
                </button>
              </div>
              <div class="health-finding__title">
                {{ finding.title || finding.id || t('sessions.overview.findingFallback', { n: fIdx + 1 }) }}
              </div>
              <div v-if="finding.detail" class="health-finding__detail">{{ finding.detail }}</div>
              <div v-if="visibleEvidenceEntries(finding.evidence).length" class="health-evidence" aria-label="Finding evidence">
                <span v-for="([key, value], eIdx) in visibleEvidenceEntries(finding.evidence).slice(0, 6)" :key="eIdx">
                  <b>{{ evidenceLabel(key) }}</b>{{ evidenceValue(value) }}
                </span>
              </div>
              <AdvancedCliSteps
                v-if="normalizedFixSteps(finding).length"
                :steps="normalizedFixSteps(finding)"
                :heading="stepsHeading(findingGroupKind(finding))"
              />
            </div>
          </article>
        </section>
      </template>
    </section>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, onActivated, onDeactivated } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useRpcStore } from '@/stores/rpc'
import { useRequest } from '@/composables/useRequest'
import { requestUsageSnapshot } from '@/composables/usage/useUsageQuery'
import { effectiveCnyPerUsd } from '@/composables/usage/nativeBilling'
import type { UsageSnapshot } from '@/types/usage'
import { useToasts } from '@/composables/useToasts'
import { isOwnedGatewayConnection } from '@/composables/useCliInvocation'
import { usePlatform } from '@/platform'
import { copyTextWithFallback } from '@/utils/browser'
import {
  buildAgentDiagnosisHandoff,
  formatLatencyLine,
  normalizeHomePaths,
  withoutLegacyMigrationFinding,
  providerBlocksAgent,
  settingsLinkForFinding,
  xmlEscape,
} from '@/utils/overviewDiagnostics'
import Icon from '@/components/Icon.vue'
import AdvancedCliSteps from '@/components/overview/AdvancedCliSteps.vue'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface StatusData {
  uptime_ms?: number
  version?: string
  provider?: string
}

interface FixStep {
  label: string
  command?: string
  detail?: string
}

interface Finding {
  id?: string
  severity?: 'error' | 'warn' | 'info' | 'ok'
  readinessImpact?: 'blocks_ready' | 'degrades' | 'optional' | 'none'
  surface?: string
  title?: string
  detail?: string
  evidence?: Record<string, unknown>
  fixSteps?: FixStep[]
  restartRequired?: boolean
}

interface HealthReport {
  status?: string
  ready?: boolean
  summary?: string
  gatewayUrl?: string
  configPath?: string
  requestedConfigPath?: string
  agentId?: string
  counts?: Record<string, number>
  impactCounts?: Record<string, number>
  findings?: Finding[]
}

interface FindingGroup {
  title: string
  note: string
  findings: Finding[]
}

interface UsageData {
  totalSessions?: number
  totalTokens?: number
  totalCostUsd?: number
}

// providers.status row — only the fields the overview reads. `latency` is a
// newer optional TTFT summary; older gateways omit it entirely.
interface ProviderStatusRow {
  providerId?: string
  active?: boolean
  latency?: {
    p50TtftMs?: number | null
    p95TtftMs?: number | null
    samples?: number | null
    windowMinutes?: number | null
  } | null
}

interface ProvidersStatusData {
  providers?: ProviderStatusRow[]
}

// ---------------------------------------------------------------------------
// Stores & Router
// ---------------------------------------------------------------------------

const { t } = useI18n()
const router = useRouter()
const rpc = useRpcStore()
const { pushToast } = useToasts()
const platform = usePlatform()

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const HIDDEN_EVIDENCE_KEYS = new Set(['restart_required', 'restartRequired'])

// Per-panel useRequest instances
const { data: statusData, refresh: refreshStatus } = useRequest<StatusData>(
  'status',
  undefined,
  { errorLabel: 'Failed to load status', immediate: false },
)
const usageData = ref<UsageData | null>(null)
const usageSnapshot = ref<UsageSnapshot | null>(null)

// Derived display values from status panel
const uptime = computed<string>(() => {
  const ms = statusData.value?.uptime_ms
  if (ms == null) return '—'
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  return `${h}h ${m}m ${s % 60}s`
})
const versionLine = computed<string>(() => statusData.value?.version ? `v${statusData.value.version}` : '—')
const provider = computed<string>(() => statusData.value?.provider ?? '—')

// Derived display values from usage panel
const sessionsCount = computed<string>(() =>
  usageData.value?.totalSessions != null ? String(usageData.value.totalSessions) : '—'
)
const tokensDisplay = computed<string>(() =>
  usageData.value?.totalTokens != null ? usageData.value.totalTokens.toLocaleString() : '—'
)
const costLine = computed<string>(() => {
  const cost = usageData.value?.totalCostUsd
  if (cost == null) return '—'
  // Prefer the ledger's canonical rate; 7.25 is only the legacy display
  // fallback for gateways that predate served FX rates.
  const cnyRate = effectiveCnyPerUsd(usageSnapshot.value) ?? 7.25
  const usd = '$' + Number(cost).toFixed(4)
  const cny = '¥' + (Number(cost) * cnyRate).toFixed(4)
  const cur = localStorage.getItem('opensquilla-currency') || 'USD'
  return cur === 'CNY' ? `${cny} · ${usd}` : `${usd} · ${cny}`
})

async function refreshUsage(): Promise<UsageData | null> {
  try {
    const snapshot = await requestUsageSnapshot(rpc, 'all', {
      days: false,
      models: false,
      sessions: false,
      cachedSnapshot: usageSnapshot.value,
    })
    usageSnapshot.value = snapshot
    const result = {
      totalSessions: snapshot.totals.sessions,
      totalTokens: snapshot.totals.totalTokens,
      totalCostUsd: snapshot.totals.cost,
    }
    usageData.value = result
    return result
  } catch {
    // Overview usage is an optional KPI. Preserve the last good value while
    // the primary Usage page provides a retryable error state.
    return null
  }
}

// Health panel keeps its own imperative state (special error rendering)
const healthLoading = ref(true)
const healthReport = ref<HealthReport | null>(null)
const healthError = ref<Error | null>(null)
// ISO timestamp of the last completed doctor.status, for the freshness line.
const healthCheckedAt = ref<string | undefined>(undefined)
const copiedCommandKey = ref('')
// Best-effort providers.status rows, fetched on mount and manual Refresh for
// the active-provider latency line; failures leave the list empty and silent.
const providerRows = ref<ProviderStatusRow[]>([])

let autoRefreshId: ReturnType<typeof setInterval> | null = null
let copiedCommandResetId: ReturnType<typeof setTimeout> | null = null
let hasActivated = false

// ---------------------------------------------------------------------------
// Computed
// ---------------------------------------------------------------------------

const stripClass = computed(() => {
  if (healthLoading.value) return 'is-loading'
  if (healthError.value) return 'is-unavailable'
  return `is-${classToken(healthReport.value?.status || 'unknown')}`
})

const statusLabelText = computed(() => {
  if (healthLoading.value) return t('sessions.overview.checking')
  if (healthError.value) return statusLabel('unavailable', false)
  return statusLabel(healthReport.value?.status || 'unknown', healthReport.value?.ready)
})

const statusSummary = computed(() => {
  if (healthLoading.value) return t('sessions.overview.waitingDoctor')
  if (healthError.value) return t('sessions.overview.healthUnavailable')
  return healthReport.value?.summary || healthReport.value?.status || ''
})

// Status glyph inside the tinted chip: ready → check, degraded → shield,
// action-required / unavailable → x. Tone follows stripClass.
const statusGlyphIcon = computed<'check' | 'shield' | 'x'>(() => {
  const cls = stripClass.value
  if (cls === 'is-action_required' || cls === 'is-unavailable') return 'x'
  if (cls === 'is-degraded') return 'shield'
  return 'check'
})

const impactCounts = computed(() => {
  if (healthLoading.value || healthError.value) {
    return { blocks_ready: 0, degrades: 0, optional: 0, none: 0 }
  }
  return healthReport.value?.impactCounts || impactCountsFromSeverity(healthReport.value?.counts || {})
})

// Impact counts rendered as tone-colored chips inside the readiness hero,
// replacing the four near-empty count cards. Zero-count chips read muted.
const impactChips = computed(() => [
  { key: 'blocks_ready', label: t('sessions.overview.needsAction'), tone: 'danger', n: impactCounts.value.blocks_ready || 0 },
  { key: 'degrades', label: t('sessions.overview.degraded'), tone: 'warn', n: impactCounts.value.degrades || 0 },
  { key: 'optional', label: t('sessions.overview.optional'), tone: 'info', n: impactCounts.value.optional || 0 },
  { key: 'none', label: t('sessions.overview.ready'), tone: 'ok', n: impactCounts.value.none || 0 },
])

// Environment footer readout: gateway URL, config path (abbreviated + copyable),
// agent, provider — quiet utility detail, not hero content.
function abbreviatePath(path: string): string {
  // Collapse macOS and Linux home prefixes to `~/` (same rule the diagnostics
  // copies use), then squeeze anything still too long.
  let p = normalizeHomePaths(path)
  if (p.length > 42) {
    const tail = p.slice(-30)
    const head = p.slice(0, 10)
    p = `${head}…${tail}`
  }
  return p
}

interface ReadoutItem { key: string; label: string; display: string; full: string; copy: boolean }
const readoutItems = computed<ReadoutItem[]>(() => {
  if (healthLoading.value) return []
  const items: ReadoutItem[] = []
  const gatewayUrl = healthReport.value?.gatewayUrl || gatewayContextUrl()
  if (gatewayUrl) items.push({ key: 'gateway', label: t('sessions.overview.ctxGateway'), display: gatewayUrl, full: gatewayUrl, copy: true })
  if (healthReport.value?.configPath) {
    items.push({ key: 'config', label: t('sessions.overview.ctxConfig'), display: abbreviatePath(healthReport.value.configPath), full: healthReport.value.configPath, copy: true })
  }
  if (healthReport.value?.agentId) items.push({ key: 'agent', label: t('sessions.overview.ctxAgent'), display: healthReport.value.agentId, full: healthReport.value.agentId, copy: false })
  if (provider.value && provider.value !== '—') items.push({ key: 'provider', label: t('sessions.overview.provider'), display: provider.value, full: provider.value, copy: false })
  return items
})

// Compact TTFT line for the active provider only; null hides the readout row
// (backends without latency stats, no active row, or low-sample null fields).
const latencyLine = computed<string | null>(() => {
  const row = providerRows.value.find(r => r?.active === true)
  return row ? formatLatencyLine(row.latency) : null
})

// "Diagnose with agent" needs a live report and a usable provider: when a
// provider finding blocks readiness the agent turn itself could not run, so
// the hand-off is hidden instead of dead-ending in chat.
const diagnoseVisible = computed<boolean>(() => {
  if (healthLoading.value || !healthReport.value) return false
  return !providerBlocksAgent(healthReport.value.findings)
})

const groupedFindings = computed<FindingGroup[]>(() => {
  if (healthLoading.value) return []

  const findings = healthError.value ? [gatewayUnavailableFinding()] : (healthReport.value?.findings || [])

  if (!findings.length) return []

  const groups: FindingGroup[] = [
    {
      title: t('sessions.overview.group.action.title'),
      note: t('sessions.overview.group.action.note'),
      findings: findings.filter(f => findingGroupKind(f) === 'action'),
    },
    {
      title: t('sessions.overview.group.degraded.title'),
      note: t('sessions.overview.group.degraded.note'),
      findings: findings.filter(f => findingGroupKind(f) === 'degraded'),
    },
    {
      title: t('sessions.overview.group.optional.title'),
      note: t('sessions.overview.group.optional.note'),
      findings: findings.filter(f => findingGroupKind(f) === 'optional'),
    },
    {
      title: t('sessions.overview.group.ready.title'),
      note: t('sessions.overview.group.ready.note'),
      findings: findings.filter(f => findingGroupKind(f) === 'ready'),
    },
  ]

  return groups.filter(g => g.findings.length)
})

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

onMounted(() => {
  // Latency rides alongside the doctor report but never gates it. Like the
  // deep checks, providers.status is expensive (a client per registered spec),
  // so it loads on mount and manual Refresh only — never from the 30s poll.
  void loadProviderStatus()
})

// KeepAlive owns all status loading. The first activation performs one deep
// doctor pass; returning activations refresh cached data with a shallow pass.
onActivated(() => {
  startTimers()
  const initialActivation = !hasActivated
  hasActivated = true
  void loadData({ deep: initialActivation, silentHealth: !initialActivation })
  void loadChannelStats()
})

// Channels rollup for the KPI chip — best-effort; a failure just hides it.
const channelStats = ref({ total: 0, connected: 0, failed: 0, pending: 0 })
const channelChipHint = computed(() => {
  const parts = [t('console.overview.channelsChipConnected', { connected: channelStats.value.connected })]
  if (channelStats.value.failed > 0) {
    parts.push(t('console.overview.channelsChipFailed', { failed: channelStats.value.failed }))
  }
  if (channelStats.value.pending > 0) {
    parts.push(t('console.overview.channelsChipPending', { pending: channelStats.value.pending }))
  }
  return parts.join(' · ')
})

async function loadChannelStats() {
  try {
    const res = await rpc.call<{ channels?: Array<{ status?: string; configured?: boolean; pendingPairings?: number }> }>('channels.status')
    const rows = (res.channels || []).filter(ch => ch && ch.configured !== false)
    channelStats.value = {
      total: rows.length,
      connected: rows.filter(ch => ch.status === 'connected').length,
      failed: rows.filter(ch => ch.status === 'dead' || ch.status === 'exhausted').length,
      pending: rows.reduce((sum, ch) => sum + (ch.pendingPairings || 0), 0),
    }
  } catch {
    channelStats.value = { total: 0, connected: 0, failed: 0, pending: 0 }
  }
}

onDeactivated(() => {
  stopTimers()
})

onUnmounted(() => {
  stopTimers()
  clearCopiedCommandTimer()
})

function startTimers() {
  if (!autoRefreshId) {
    autoRefreshId = setInterval(() => {
      void loadData({ deep: false, silentHealth: true })
    }, 30000)
  }
}

function stopTimers() {
  if (autoRefreshId) {
    clearInterval(autoRefreshId)
    autoRefreshId = null
  }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

const refreshing = ref(false)

// Manual refresh shows a busy state on the button; the 30s background poll
// (loadData) intentionally stays silent, so the control reacts only to clicks.
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  // Fire-and-forget: latency is optional telemetry and never gates the refresh.
  void loadProviderStatus()
  try {
    await loadData({ deep: true, silentHealth: false })
  } finally {
    refreshing.value = false
  }
}

function scrollToHealth() {
  document.getElementById('overview-health')?.scrollIntoView({ block: 'start' })
}

interface HealthLoadOptions {
  deep: boolean
  silent?: boolean
}

async function loadHealth({ deep, silent = false }: HealthLoadOptions) {
  if (!silent) {
    healthLoading.value = true
    healthError.value = null
  }

  try {
    await rpc.waitForConnection()
    const response = await rpc.call<HealthReport>('doctor.status', { agentId: 'main', deep })
    const data = withoutLegacyMigrationFinding(response)
    if (!data.gatewayUrl) data.gatewayUrl = gatewayContextUrl()
    healthError.value = null
    healthReport.value = data
    healthCheckedAt.value = new Date().toISOString()
  } catch (err) {
    healthError.value = err instanceof Error ? err : new Error(String(err))
    healthReport.value = null
  } finally {
    if (!silent) healthLoading.value = false
  }
}

async function loadProviderStatus() {
  try {
    await rpc.waitForConnection()
    const data = await rpc.call<ProvidersStatusData>('providers.status', {})
    providerRows.value = Array.isArray(data?.providers) ? data.providers : []
  } catch {
    // Latency is optional telemetry; the overview must render without it.
  }
}

function clearCopiedCommandTimer() {
  if (copiedCommandResetId) {
    clearTimeout(copiedCommandResetId)
    copiedCommandResetId = null
  }
}

function normalizedFixSteps(finding: Finding): Array<{ label: string; command?: string; detail?: string }> {
  const targetFresh = finding.evidence?.target_fresh ?? finding.evidence?.targetFresh
  const hideMigrationApply = finding.id === 'migration.legacy_home_detected'
    && targetFresh === false

  return (finding.fixSteps || []).filter((step) => {
    if (!hideMigrationApply) return true
    const command = String(step.command || '')
    return !/(?:^|\s)--apply(?:\s|$)/.test(command)
  }).map(step => ({
    label: step.label || t('sessions.overview.step'),
    command: step.command,
    detail: step.detail,
  }))
}

// Shared check-icon swap (1600ms) for copyable environment commands.
function markCopied(key: string) {
  copiedCommandKey.value = key
  clearCopiedCommandTimer()
  copiedCommandResetId = setTimeout(() => {
    copiedCommandKey.value = ''
    copiedCommandResetId = null
  }, 1600)
}

async function copyCommand(command: string, key: string) {
  if (!command) return
  try {
    await copyTextWithFallback(command)
    markCopied(key)
    pushToast(t('setup.toast.copiedCommand'), { tone: 'ok' })
  } catch (err) {
    clearCopiedCommandTimer()
    copiedCommandKey.value = ''
    const error = err instanceof Error ? err.message : String(err)
    pushToast(t('setup.toast.copyFailed', { error }), { tone: 'danger' })
  }
}

// Hand the trimmed doctor report to a fresh main-agent chat. The report is
// data, not instructions: it is XML-escaped inside an <untrusted> envelope so
// finding text cannot inject directives into the prompt.
function diagnoseWithAgent() {
  const report = healthReport.value
  if (!report) return
  const minReport = {
    status: report.status,
    ready: report.ready,
    summary: report.summary,
    counts: report.counts,
    impactCounts: report.impactCounts,
    findings: report.findings,
  }
  const ownsGatewayConnection = platform.capabilities.ownsGateway && isOwnedGatewayConnection()
  const handoff = buildAgentDiagnosisHandoff(minReport, {
    platform: platform.id,
    hasTerminalWorkflow: platform.capabilities.hasTerminalWorkflow,
    ownsGateway: platform.capabilities.ownsGateway,
    ownsGatewayConnection,
  })
  const clientContext = xmlEscape(JSON.stringify(handoff.clientContext))
  const payload = xmlEscape(normalizeHomePaths(JSON.stringify(handoff.report)))
  const text = `${t('sessions.overview.diagnosePrompt')}\n`
    + `<context source="client:diagnostic-context">${clientContext}</context>\n`
    + `<untrusted source="doctor:report">${payload}</untrusted>`
  router.push({
    path: '/chat/new',
    query: { agent: 'main' },
    // autosend fires the prefill in one step so the diagnosis actually starts
    // instead of dropping the operator at the composer.
    state: { prefill: text, autosend: true },
  }).catch(() => {})
}

function findingSettingsLink(finding: Finding) {
  return settingsLinkForFinding(finding, {
    isDesktop: platform.capabilities.isDesktop,
    ownsGatewayConnection: platform.capabilities.ownsGateway && isOwnedGatewayConnection(),
  })
}

function openFindingSettings(finding: Finding) {
  const link = findingSettingsLink(finding)
  if (!link) return
  router.push({ path: link.path, hash: link.hash, query: link.query }).catch(() => {})
}

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

interface DataLoadOptions {
  deep: boolean
  silentHealth: boolean
}

async function loadData({ deep, silentHealth }: DataLoadOptions) {
  await Promise.all([
    refreshStatus(),
    refreshUsage(),
    loadHealth({ deep, silent: silentHealth }),
  ])
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function classToken(value: string | undefined | null): string {
  return String(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]+/g, '-')
}

function impactValue(finding: Finding): string {
  const impact = String(finding?.readinessImpact || '')
  if (['blocks_ready', 'degrades', 'optional', 'none'].includes(impact)) return impact
  const severity = String(finding?.severity || 'info')
  if (severity === 'error') return 'blocks_ready'
  if (severity === 'warn') return 'degrades'
  if (severity === 'info') return 'optional'
  return 'none'
}

function findingGroupKind(finding: Finding): 'action' | 'degraded' | 'optional' | 'ready' {
  const impact = impactValue(finding)
  if (impact === 'blocks_ready') return 'action'
  if (impact === 'degrades') return 'degraded'
  if (impact === 'optional') return 'optional'
  return 'ready'
}

function findingTone(kind: 'action' | 'degraded' | 'optional' | 'ready'): 'error' | 'warn' | 'info' | 'ok' {
  if (kind === 'action') return 'error'
  if (kind === 'degraded') return 'warn'
  if (kind === 'optional') return 'info'
  return 'ok'
}

function impactLabel(impact: string): string {
  const keys: Record<string, string> = {
    blocks_ready: 'sessions.overview.impact.blocksReady',
    degrades: 'sessions.overview.impact.degrades',
    optional: 'sessions.overview.impact.optional',
    none: 'sessions.overview.impact.reference',
  }
  return t(keys[impact] || 'sessions.overview.impact.reference')
}

function statusLabel(status: string, ready: boolean | undefined): string {
  if (ready && status === 'degraded') return t('sessions.overview.statusLabel.readyWithWarnings')
  if (ready) return t('sessions.overview.statusLabel.ready')
  const keys: Record<string, string> = {
    action_required: 'sessions.overview.statusLabel.actionRequired',
    degraded: 'sessions.overview.statusLabel.degraded',
    unavailable: 'sessions.overview.statusLabel.unavailable',
    ready: 'sessions.overview.statusLabel.ready',
  }
  return keys[status] ? t(keys[status]) : status
}

function evidenceLabel(key: string): string {
  const label = String(key || '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : ''
}

function evidenceValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    const text = JSON.stringify(value)
    return text.length > 120 ? `${text.slice(0, 117)}...` : text
  } catch {
    return String(value)
  }
}

function visibleEvidenceEntries(evidence: Record<string, unknown> | undefined): [string, unknown][] {
  return Object.entries(evidence || {})
    .filter(([key, value]) => value !== undefined && value !== null && !HIDDEN_EVIDENCE_KEYS.has(key))
}

function stepsHeading(kind: 'action' | 'degraded' | 'optional' | 'ready'): string {
  if (kind === 'optional') return t('sessions.overview.steps.optional')
  if (kind === 'ready') return t('sessions.overview.steps.reference')
  return t('sessions.overview.steps.recovery')
}

function shellArg(value: string | undefined | null): string {
  const text = String(value || '')
  if (/^[A-Za-z0-9_@%+=:,./~-]+$/.test(text)) return text
  return `'${text.replace(/'/g, `'\\''`)}'`
}

function bootstrapConfigPath(): string {
  return document.getElementById('opensquilla-data')?.dataset.configPath || ''
}

function gatewayUnavailableDetail(gatewayUrl: string, err: Error | null): string {
  const reason = err?.message || String(err)
  if (!gatewayUrl) return reason
  return t('sessions.overview.gw.cannotLoad', { url: gatewayUrl, reason })
}

function gatewayUnavailableFixSteps(gatewayUrl: string): FixStep[] {
  if (!isLocalGatewayUrl(gatewayUrl)) {
    return [
      {
        label: t('sessions.overview.gw.inspectRemote'),
        command: `opensquilla gateway status --gateway ${shellArg(gatewayUrl)} --json`,
      },
      {
        label: t('sessions.overview.gw.repairRemote'),
        detail: t('sessions.overview.gw.repairRemoteDetail'),
      },
    ]
  }
  const target = gatewayStatusTarget(gatewayUrl)
  const bindArgs = target ? ` --bind ${target.host} --port ${target.port}` : ''
  const useConfigTarget = usesDefaultGatewayUrl(gatewayUrl) && Boolean(bootstrapConfigPath())
  const doctorTarget = useConfigTarget ? '' : (gatewayUrl ? ` --gateway ${shellArg(gatewayUrl)}` : '')
  const configTarget = useConfigTarget ? configOption(bootstrapConfigPath()) : ''
  const targetArgs = useConfigTarget ? '' : bindArgs
  return [
    {
      label: t('sessions.overview.gw.runLocalDoctor'),
      command: `opensquilla doctor${doctorTarget}${configTarget} --json`,
      detail: t('sessions.overview.gw.runLocalDoctorDetail'),
    },
    { label: t('sessions.overview.gw.startLocal'), command: `opensquilla gateway start${targetArgs}${configTarget}` },
    { label: t('sessions.overview.gw.inspectLocal'), command: `opensquilla gateway status${targetArgs} --json${configTarget}` },
  ]
}

function usesDefaultGatewayUrl(gatewayUrl: string): boolean {
  try {
    const requested = new URL(gatewayUrl || gatewayContextUrl(), location.href)
    const defaults = new URL(gatewayContextUrl(), location.href)
    return requested.protocol === defaults.protocol
      && requested.host === defaults.host
      && requested.pathname === defaults.pathname
  } catch {
    return false
  }
}

function configOption(configPath: string): string {
  return configPath ? ` --config ${shellArg(configPath)}` : ''
}

function isLocalGatewayUrl(gatewayUrl: string): boolean {
  const target = gatewayStatusTarget(gatewayUrl)
  if (!target) return true
  return ['127.0.0.1', '::1', 'localhost', '0.0.0.0'].includes(target.host)
}

function gatewayStatusTarget(gatewayUrl: string): { host: string; port: string } | null {
  try {
    const url = new URL(gatewayUrl || gatewayContextUrl())
    let host = url.hostname || '127.0.0.1'
    if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1)
    if (host === '0.0.0.0') host = '127.0.0.1'
    if (host === '::') host = '::1'
    const port = url.port || ((url.protocol === 'wss:' || url.protocol === 'https:') ? '443' : '18791')
    return { host, port }
  } catch {
    return null
  }
}

function gatewayUnavailableFinding(): Finding {
  const gatewayUrl = gatewayContextUrl()
  const configPath = usesDefaultGatewayUrl(gatewayUrl) ? bootstrapConfigPath() : ''
  return {
    id: 'gateway.unavailable',
    severity: 'error',
    readinessImpact: 'blocks_ready',
    surface: 'gateway',
    title: t('sessions.overview.healthUnavailable'),
    detail: gatewayUnavailableDetail(gatewayUrl, healthError.value),
    evidence: configPath ? { gatewayUrl, configPath } : { gatewayUrl },
    fixSteps: gatewayUnavailableFixSteps(gatewayUrl),
    restartRequired: false,
  }
}

function impactCountsFromSeverity(counts: Record<string, number>): Record<string, number> {
  return {
    blocks_ready: Number(counts.error || 0),
    degrades: Number(counts.warn || 0),
    optional: Number(counts.info || 0),
    none: Number(counts.ok || 0),
  }
}

function findingBadges(finding: Finding): boolean {
  const id = String(finding?.id || '')
  return id.endsWith('.diagnostic.incomplete')
    || id.endsWith('.repair.pending')
    || id === 'gateway.config.mismatch'
}

function findingBadgeText(finding: Finding): string {
  const id = String(finding?.id || '')
  if (id.endsWith('.diagnostic.incomplete')) return t('sessions.overview.badge.diagnostic')
  if (id.endsWith('.repair.pending')) return t('sessions.overview.badge.repair')
  if (id === 'gateway.config.mismatch') return t('sessions.overview.badge.config')
  return ''
}

function findingBadgeClass(finding: Finding): string {
  const id = String(finding?.id || '')
  if (id.endsWith('.diagnostic.incomplete')) return 'health-chip--diagnostic'
  if (id.endsWith('.repair.pending')) return 'health-chip--repair'
  if (id === 'gateway.config.mismatch') return 'health-chip--config'
  return ''
}

function relTime(dateStr: string | undefined): string {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return dateStr

  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffSec = Math.floor(diffMs / 1000)
  const diffMin = Math.floor(diffSec / 60)
  const diffHour = Math.floor(diffMin / 60)
  const diffDay = Math.floor(diffHour / 24)

  if (diffSec < 10) return t('sessions.relTime.justNow')
  if (diffSec < 60) return t('sessions.relTime.seconds', { n: diffSec })
  if (diffMin < 60) return t('sessions.relTime.minutes', { n: diffMin })
  if (diffHour < 24) return t('sessions.relTime.hours', { n: diffHour })
  if (diffDay < 7) return t('sessions.relTime.days', { n: diffDay })
  return d.toLocaleDateString()
}

// ---------------------------------------------------------------------------
// Gateway URL helper (the connection editor moved to Settings → Connection)
// ---------------------------------------------------------------------------

function gatewayContextUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}/ws`
}
</script>

<style scoped>
/* Status line — sits on the canvas like a page header (NOT a filled card).
   The one warm accent is the small status glyph chip. Compact single row that
   wraps gracefully; no hollow band. */
.ov-statusline {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  padding: var(--sp-2) 0 var(--sp-1);
}
.ov-status-glyph {
  align-items: center;
  background: color-mix(in srgb, var(--ok) 12%, transparent);
  border-radius: var(--radius-md);
  color: var(--ok);
  display: inline-flex;
  height: 30px;
  justify-content: center;
  width: 30px;
  flex: none;
}
.is-action_required .ov-status-glyph,
.is-unavailable .ov-status-glyph { background: color-mix(in srgb, var(--danger) 12%, transparent); color: var(--danger); }
.is-degraded .ov-status-glyph { background: color-mix(in srgb, var(--warn-fill) 14%, transparent); color: var(--warn); }
.is-loading .ov-status-glyph { background: var(--bg-surface-2); color: var(--text-dim); }

.ov-status-word {
  font-family: var(--font-display);
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1;
  margin-right: var(--sp-1);
}
.ov-status-counts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-3);
}
.ov-count {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-sm);
  padding: 2px 0;
}
.ov-count b { color: var(--text); font-weight: 600; margin-right: 4px; font-variant-numeric: tabular-nums; }
.ov-count.is-hot.is-danger b { color: var(--danger); }
.ov-count.is-hot.is-warn b { color: var(--warn); }
.ov-count.is-hot.is-info b { color: var(--info); }
.ov-count.is-hot.is-ok b { color: var(--ok); }
.ov-count:hover { color: var(--text); }
.ov-count:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }

.ov-status-actions {
  align-items: center;
  display: flex;
  gap: var(--sp-3);
  margin-left: auto;
}
/* Compact status-line diagnose button. */
.btn--sm {
  font-size: var(--fs-xs);
  padding: 5px 10px;
}
.ov-freshness { color: var(--text-dim); font-size: var(--fs-xs); white-space: nowrap; }
.ov-rerun {
  background: none; border: none; color: var(--accent); cursor: pointer;
  font: inherit; font-weight: 600; padding: 0;
}
.ov-rerun:hover { color: var(--accent-hover); }
.ov-rerun:disabled { color: var(--text-dim); cursor: default; }
.ov-rerun:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }

/* Three quiet KPI cards */
.ov-kpis {
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: var(--sp-4);
}
.ov-kpis > .control-stat { animation: control-fade-up var(--dur-enter) var(--ease-out) both; }
.ov-kpis > .control-stat:nth-child(2) { animation-delay: 40ms; }
.ov-kpis > .control-stat:nth-child(3) { animation-delay: 80ms; }

/* Environment footer readout: quiet, copyable env detail (not hero content) */
.ov-readout {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--elev-1);
  color: var(--text-dim);
  column-gap: var(--sp-5);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  padding: 11px var(--sp-4);
  row-gap: 6px;
}
.ov-readout__kv { align-items: center; display: flex; gap: 7px; min-width: 0; }
.ov-readout__kv b { color: var(--text-muted); font-weight: 600; }
.ov-readout__kv code {
  background: var(--bg-surface-2);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 2px 8px;
  white-space: nowrap;
}
.ov-readout__copy {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  height: 22px;
  justify-content: center;
  width: 22px;
}
.ov-readout__copy:hover { background: var(--bg-hover); color: var(--text); }
.ov-readout__copy--ok { color: var(--ok); }
.ov-readout__copy:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.ov-readout__version { margin-left: auto; white-space: nowrap; }

/* Readiness hero tone bar reuses control-stat--hero::before; findings keep the
   dot tones below. The former .health-status__rail / .health-score /
   .health-count grid was retired with the 10-tile band. */
.health-finding,
.health-empty {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  color: var(--text);
  overflow: hidden;
  position: relative;
}

.health-finding.is-error .health-finding__dot {
  background: var(--danger);
}

.health-finding.is-warn .health-finding__dot {
  background: var(--warn-fill);
}

.health-finding.is-info .health-finding__dot {
  background: var(--accent);
}

.health-finding.is-ok .health-finding__dot {
  background: var(--ok);
}

.health-findings {
  display: grid;
  gap: var(--sp-3);
}

.health-finding-group {
  display: grid;
  gap: var(--sp-3);
}

.health-finding-group__header {
  align-items: end;
  border-bottom: 1px solid var(--border);
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  padding: 0 2px var(--sp-2);
}

.health-finding-group__header h2 {
  font-size: var(--fs-md);
  letter-spacing: 0;
  margin: 0;
}

.health-finding-group__header p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: 3px 0 0;
}

.health-finding-group__header span {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  font-variant-numeric: tabular-nums;
}

.health-finding {
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: 20px minmax(0, 1fr);
  padding: var(--sp-4);
}

.health-finding__marker {
  align-items: center;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 4px;
}

.health-finding__dot {
  background: var(--text-dim);
  border-radius: var(--radius-full);
  box-shadow: 0 0 0 4px color-mix(in srgb, currentColor 10%, transparent);
  display: block;
  height: 10px;
  width: 10px;
}

.health-finding__line {
  background: var(--border);
  border-radius: var(--radius-full);
  flex: 1;
  min-height: 32px;
  width: 1px;
}

.health-finding__body {
  min-width: 0;
}

.health-finding__meta {
  align-items: center;
  color: var(--text-dim);
  display: flex;
  flex-wrap: wrap;
  font-size: 10.5px;
  font-weight: 700;
  gap: 6px;
  letter-spacing: 0.12em;
  min-width: 0;
  overflow-wrap: anywhere;
  text-transform: uppercase;
}

.health-impact,
.health-surface,
.health-chip {
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  color: var(--text-muted);
  display: inline-flex;
  letter-spacing: 0.08em;
  padding: 2px 8px;
}

.health-chip {
  color: var(--warn);
}

.health-chip--diagnostic {
  background: color-mix(in srgb, var(--warn) 10%, transparent);
  border-color: color-mix(in srgb, var(--warn) 40%, var(--border));
  color: var(--warn);
}

.health-chip--repair {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  color: var(--accent);
}

.health-chip--config {
  background: color-mix(in srgb, var(--danger) 8%, transparent);
  border-color: color-mix(in srgb, var(--danger) 36%, var(--border));
  color: var(--danger);
}

/* Deep link from a finding to its settings section: quiet accent text button
   that inherits the meta row's small uppercase scale. */
.health-settings-link {
  background: none;
  border: none;
  color: var(--accent);
  cursor: pointer;
  font: inherit;
  letter-spacing: 0.08em;
  padding: 2px 0;
  transition: color var(--dur-fast) var(--ease-standard);
}
.health-settings-link:hover { color: var(--accent-hover); }
.health-settings-link:focus-visible {
  border-radius: var(--radius-sm);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.health-finding__title {
  font-size: var(--fs-lg);
  font-weight: 700;
  letter-spacing: 0;
  margin-top: var(--sp-2);
  min-width: 0;
  overflow-wrap: anywhere;
}

.health-finding__detail {
  color: var(--text-muted);
  line-height: 1.5;
  margin-top: 4px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.health-evidence {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: var(--sp-3);
  min-width: 0;
  overflow-wrap: anywhere;
}

.health-evidence span {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  display: inline-flex;
  font-family: var(--font-mono);
  font-size: 11px;
  gap: 6px;
  line-height: 1.5;
  max-width: 100%;
  min-width: 0;
  overflow-wrap: anywhere;
  padding: 3px 7px;
}

.health-evidence span b {
  color: var(--text-dim);
  font-family: inherit;
  font-weight: 700;
}

.health-empty {
  color: var(--text-muted);
  padding: var(--sp-4);
}

@media (max-width: 980px) {
  .ov-kpis {
    grid-template-columns: 1fr 1fr;
  }
  .ov-readiness__actions {
    flex-basis: 100%;
    margin-left: 0;
  }
}

@media (max-width: 760px) {
  .health-finding {
    grid-template-columns: 16px minmax(0, 1fr);
    padding: var(--sp-3);
  }
  .ov-readout__version {
    margin-left: 0;
  }
}

@media (max-width: 480px) {
  .ov-kpis {
    grid-template-columns: 1fr;
  }
  .ov-readiness__counts {
    flex-wrap: wrap;
    gap: var(--sp-2) var(--sp-4);
  }
}
</style>
