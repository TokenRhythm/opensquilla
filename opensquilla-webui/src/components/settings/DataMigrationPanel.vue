<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useConfirm } from '@/composables/useConfirm'
import { useToasts } from '@/composables/useToasts'
import { usePlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import {
  formatByteSize,
  summarizeMigrationReport,
  type MigrationReportSummary,
} from '@/utils/migrationReport'
import {
  formatEstimatedActivity,
  profileSourceLabelKey,
  profileSourceGroup,
  type ProfileSourceKind,
} from '@/utils/profileSourceKind'

type MigrationProvider = 'desktop' | 'gateway'
type PanelState = 'loading' | 'ready' | 'empty' | 'unsupported' | 'error' | 'recovery'

interface MigrationCandidate {
  id: string
  provider: MigrationProvider
  kind: string
  path?: string
  version?: string | null
  estimatedActivityAt?: string | null
  sessionCount?: number | null
  sizeBytes?: number | null
  previouslyImported?: boolean
}

interface DesktopMigrationCandidate {
  kind: string
  path: string
  version?: string | null
  estimated_activity_at?: string | null
  session_count?: number | null
  size_bytes?: number | null
  previously_imported?: boolean
}

interface MigrationTerminalResult {
  ok: boolean
  migrationApplied?: boolean
  restartOk?: boolean
  requiresProviderSetup?: boolean
  source?: string
  sourceKind?: ProfileSourceKind
  targetReplaced?: boolean
  failureCode?: string
  failureStage?: 'preflight' | 'apply' | 'restart'
  detail?: string
}

type CleanupMode = 'reset-current-settings' | 'delete-current-profile' | 'delete-all-user-data'
interface CleanupItem {
  kind: string
  path: string
  exists: boolean
  identity: string | null
}
interface CleanupReport {
  schema_version: 1
  outcome: 'ready' | 'blocked' | 'complete' | 'partial'
  stable_code: string
  mode: CleanupMode
  items: CleanupItem[]
  transaction_id: string
  revision: number
  scope_fingerprint: string
}
interface CleanupResult {
  ok: boolean
  aborted?: boolean
  scheduled?: boolean
  partial?: boolean
  previewId?: string | null
  report?: CleanupReport
  profile?: { kind: 'primary' | 'recovery'; recoveryId: string | null }
  detail?: string
}

interface DesktopMigrationBridge {
  getDesktopProfileKind?: () => Promise<unknown>
  getRecoveryState?: () => Promise<unknown>
  chooseLegacyAgentDataLocation?: (payload?: Record<string, never>) => Promise<unknown>
  migrationSummary?: (payload?: { source?: string }) => Promise<{
    ok: boolean
    candidates?: DesktopMigrationCandidate[]
    candidate: DesktopMigrationCandidate | null
    report: unknown | null
    previewId?: string
    raw?: string
    requiresSelection?: boolean
  }>
  migrationRun?: (opts: { overwrite?: boolean; previewId: string }) => Promise<
    MigrationTerminalResult & { aborted?: boolean; report?: unknown }
  >
  migrationBrowseSource?: (payload: { kind: ProfileSourceKind }) => Promise<{
    ok: boolean
    aborted?: boolean
    candidate?: DesktopMigrationCandidate | null
    detail?: string
    error?: string
  }>
  migrationTakeLastResult?: () => Promise<MigrationTerminalResult | null>
  migrationPeekLastResult?: () => Promise<MigrationTerminalResult | null>
  migrationDismissLastResult?: () => Promise<{ ok: boolean }>
  revealRecoveryPath?: (payload: { target: 'backups' }) => Promise<boolean>
  onMigrationProgress?: (cb: (state: { phase: string; detail?: string }) => void) => () => void
  inspectDesktopCleanup?: (payload: { mode: CleanupMode }) => Promise<{
    ok: boolean
    previewId: string | null
    report: CleanupReport
    profile: { kind: 'primary' | 'recovery'; recoveryId: string | null }
  }>
  discardDesktopCleanup?: (payload: { previewId: string }) => Promise<boolean>
  applyDesktopCleanup?: (payload: {
    previewId: string
    acknowledged: boolean
    confirmation: string
  }) => Promise<CleanupResult>
  revealDesktopUserData?: () => Promise<boolean>
}

interface GatewayCandidate {
  candidateId?: unknown
  sourceKind?: unknown
  version?: unknown
  estimatedActivityAt?: unknown
  sessionCount?: unknown
  sizeBytes?: unknown
  previouslyImported?: unknown
}

interface GatewaySourcesResponse {
  schemaVersion?: unknown
  mode?: unknown
  capabilities?: {
    discover?: unknown
    preview?: unknown
    apply?: unknown
    manualSource?: unknown
  }
  candidates?: unknown
}

interface GatewayPreviewResponse {
  schemaVersion?: unknown
  mode?: unknown
  previewStatus?: unknown
  targetAction?: unknown
  summary?: {
    sessionCount?: unknown
    itemCounts?: { planned?: unknown; skipped?: unknown; error?: unknown }
    pausedJobCount?: unknown
    diskRequiredBytes?: unknown
    diskFreeBytes?: unknown
  }
  blockers?: unknown
  notices?: unknown
}

interface RecoveryInspection {
  outcome?: string
  stable_code?: string
  allowed_actions?: string[]
}

const MANUAL_SOURCE_KINDS: ProfileSourceKind[] = [
  'cli-home',
  'desktop-home',
  'windows-portable',
]
const ATTENTION_VISIBLE_CODES = new Set([
  'legacy_workspace_pinned',
  'legacy_workspace_deferred',
  'workspace_conflict',
])
const ATTENTION_TECHNICAL_CODES = new Set([
  'legacy_layout_conflict',
  'legacy_layout_unsafe',
  'layout_reconcile_deferred',
  'layout_marker_unsafe',
  'layout_marker_write_failed',
])
const RECOVERY_MIGRATION_DETAILS = new Set([
  'Import source selection is available only in the primary profile.',
  'Recovery profiles cannot import another profile.',
  'Return to the primary profile before importing data.',
])
const MIGRATION_FAILURE_DETAIL_KEYS: Record<string, string> = {
  source_snapshot_locked: 'setup.runtime.migrationFailureLocked',
  source_snapshot_changed: 'setup.runtime.migrationFailureChanged',
  source_snapshot_unreadable: 'setup.runtime.migrationFailureUnreadable',
  migration_apply_failed: 'setup.runtime.migrationFailureApply',
  gateway_restart_failed: 'setup.runtime.migrationFailureRestart',
}

const { t, locale } = useI18n()
const { confirm } = useConfirm()
const { pushToast } = useToasts()
const platform = usePlatform()
const rpc = useRpcStore()
const desktopBridge = (globalThis as unknown as {
  opensquillaDesktop?: DesktopMigrationBridge
}).opensquillaDesktop
const hasDesktopMigrationBridge = computed(() => Boolean(
  platform.capabilities.isDesktop
  && desktopBridge?.migrationSummary
  && desktopBridge?.migrationRun,
))
const canRestartGateway = computed(() => Boolean(platform.gateway.retryStartup))
const canCleanup = computed(() => Boolean(
  platform.capabilities.isDesktop
  && desktopBridge?.inspectDesktopCleanup
  && desktopBridge?.discardDesktopCleanup
  && desktopBridge?.applyDesktopCleanup,
))

const panelState = ref<PanelState>('loading')
const busy = ref(false)
const candidates = ref<MigrationCandidate[]>([])
const selectedCandidate = ref<MigrationCandidate | null>(null)
const desktopSummary = shallowRef<MigrationReportSummary | null>(null)
const gatewayPreview = shallowRef<GatewayPreviewResponse | null>(null)
const previewId = ref('')
const overwrite = ref(false)
const phase = ref('')
const inlineError = ref('')
const lastResult = shallowRef<MigrationTerminalResult | null>(null)
const profileKind = ref<'primary' | 'recovery' | 'unknown'>('unknown')
const recoveryInspection = shallowRef<RecoveryInspection | null>(null)
const headingEl = ref<HTMLElement | null>(null)
const cleanupOpen = ref(false)
const cleanupBusy = ref(false)
const cleanupPreviewId = ref('')
const cleanupReport = shallowRef<CleanupReport | null>(null)
const cleanupProfile = ref<{ kind: 'primary' | 'recovery'; recoveryId: string | null } | null>(null)
const cleanupAcknowledged = ref(false)
const cleanupConfirmation = ref('')
const cleanupTitleEl = ref<HTMLElement | null>(null)
const cleanupReturnFocusEl = ref<HTMLElement | null>(null)
let progressUnsub: (() => void) | null = null
const DELETE_ALL_CONFIRMATION = 'DELETE ALL OPENSQUILLA DATA'

const isGatewayPreview = computed(() => selectedCandidate.value?.provider === 'gateway')
const knownAttention = computed(() => {
  const inspection = recoveryInspection.value
  return inspection?.outcome === 'attention'
    && ATTENTION_VISIBLE_CODES.has(inspection.stable_code || '')
    ? inspection
    : null
})
const technicalAttention = computed(() => {
  const inspection = recoveryInspection.value
  return inspection?.outcome === 'attention'
    && ATTENTION_TECHNICAL_CODES.has(inspection.stable_code || '')
    ? inspection
    : null
})
const canChooseLegacyAgentData = computed(() => Boolean(
  knownAttention.value
  && desktopBridge?.chooseLegacyAgentDataLocation,
))

const candidateGroups = computed(() => ([
  {
    key: 'supported',
    label: t('setup.runtime.migrationSupportedSources'),
    candidates: candidates.value.filter(candidate => profileSourceGroup(candidate.kind) === 'supported'),
  },
  {
    key: 'historical',
    label: t('setup.runtime.migrationHistoricalSources'),
    candidates: candidates.value.filter(candidate => profileSourceGroup(candidate.kind) === 'historical'),
  },
  {
    key: 'other',
    label: t('setup.runtime.migrationOtherSources'),
    candidates: candidates.value.filter(candidate => profileSourceGroup(candidate.kind) === 'unknown'),
  },
]).filter(group => group.candidates.length > 0))

const gatewayBlockers = computed(() => stringCodes(gatewayPreview.value?.blockers))
const gatewayNotices = computed(() => stringCodes(gatewayPreview.value?.notices))
const gatewaySessionCount = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.sessionCount))
const gatewayPlannedCount = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.itemCounts?.planned))
const gatewaySkippedCount = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.itemCounts?.skipped))
const gatewayErrorCount = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.itemCounts?.error))
const gatewayPausedJobCount = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.pausedJobCount))
const gatewayDiskRequired = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.diskRequiredBytes))
const gatewayDiskFree = computed(() => finiteNonNegative(gatewayPreview.value?.summary?.diskFreeBytes))
const hasBlockingErrors = computed(() => {
  if (isGatewayPreview.value) return gatewayBlockers.value.length > 0
  return !desktopSummary.value || desktopSummary.value.errorNotes.length > 0
})
const runLabel = computed(() => t(
  desktopSummary.value?.needsOverwrite
    ? 'setup.runtime.migrationBackupAndReplace'
    : 'setup.runtime.migrationCopyAndUse',
))
const failureReason = computed(() => localizedMigrationDetail(
  lastResult.value?.detail,
  lastResult.value?.failureCode,
))
const cleanupExistingCount = computed(() => (
  cleanupReport.value?.items.filter(item => item.exists).length ?? 0
))
const cleanupNeedsAcknowledgement = computed(() => (
  cleanupReport.value?.outcome === 'ready'
  && Boolean(cleanupPreviewId.value)
  && cleanupReport.value?.mode !== 'reset-current-settings'
))
const cleanupCanApply = computed(() => {
  const report = cleanupReport.value
  if (!report || report.outcome !== 'ready' || !cleanupPreviewId.value) return false
  if (cleanupNeedsAcknowledgement.value && !cleanupAcknowledged.value) return false
  return report.mode !== 'delete-all-user-data'
    || cleanupConfirmation.value === DELETE_ALL_CONFIRMATION
})
const cleanupModeTitle = computed(() => {
  const mode = cleanupReport.value?.mode
  return mode ? t(`setup.runtime.cleanup.${mode}.title`) : ''
})
const cleanupModeWarning = computed(() => {
  const mode = cleanupReport.value?.mode
  return mode ? t(`setup.runtime.cleanup.${mode}.warning`) : ''
})
const cleanupApplyLabel = computed(() => {
  const mode = cleanupReport.value?.mode
  return mode ? t(`setup.runtime.cleanup.${mode}.apply`) : ''
})

function finiteNonNegative(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function stringCodes(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((entry) => {
    if (typeof entry === 'string' && entry.trim()) return [entry.trim()]
    if (entry && typeof entry === 'object') {
      const code = nonEmptyString((entry as Record<string, unknown>).code)
      return code ? [code] : []
    }
    return []
  })
}

function localizedMigrationDetail(value: unknown, failureCode?: string): string {
  const failureKey = failureCode ? MIGRATION_FAILURE_DETAIL_KEYS[failureCode] : undefined
  if (failureKey) return t(failureKey)
  const detail = typeof value === 'string' ? value.trim() : ''
  return RECOVERY_MIGRATION_DETAILS.has(detail)
    ? t('setup.runtime.migrationRecoveryProfile')
    : t('settings.dataMigration.loadFailed')
}

function presentationError(error: unknown): string {
  // RPC and older Electron bridges may include an absolute source/target path
  // in a free-form error. Keep the primary surface stable and path-free.
  void error
  return t('settings.dataMigration.loadFailed')
}

function sourceLabel(kind: string): string {
  return t(profileSourceLabelKey(kind))
}

function activityLabel(value?: string | null): string {
  if (!value) return ''
  const relative = formatEstimatedActivity(value, locale.value)
  return relative
    ? t('setup.runtime.migrationCandidateActivityEstimate', { value: relative })
    : ''
}

function localizedCode(prefix: 'blockers' | 'notices', code: string): string {
  const key = `settings.dataMigration.${prefix}.${code}`
  const translated = t(key)
  return translated === key ? code.replace(/_/g, ' ') : translated
}

function isDesktopCandidate(value: unknown): value is DesktopMigrationCandidate {
  return value !== null
    && typeof value === 'object'
    && Boolean(nonEmptyString((value as DesktopMigrationCandidate).path))
    && Boolean(nonEmptyString((value as DesktopMigrationCandidate).kind))
}

function safeDisplayVersion(value: unknown): string | null {
  const candidate = nonEmptyString(value)
  if (!candidate || candidate.length > 80) return null
  return /^[vV]?\d+\.\d+(?:\.\d+)?(?:(?:a|b|rc)\d+)?(?:-[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?$/.test(candidate)
    ? candidate
    : null
}

function normalizeDesktopCandidate(candidate: DesktopMigrationCandidate): MigrationCandidate {
  return {
    id: `${candidate.kind}\u0000${candidate.path}`,
    provider: 'desktop',
    kind: candidate.kind,
    path: candidate.path,
    version: safeDisplayVersion(candidate.version),
    estimatedActivityAt: nonEmptyString(candidate.estimated_activity_at) || null,
    sessionCount: finiteNonNegative(candidate.session_count),
    sizeBytes: finiteNonNegative(candidate.size_bytes),
    previouslyImported: candidate.previously_imported === true,
  }
}

function normalizeGatewayCandidate(value: unknown): MigrationCandidate | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const candidate = value as GatewayCandidate
  const id = nonEmptyString(candidate.candidateId)
  const kind = nonEmptyString(candidate.sourceKind)
  if (!id || !kind) return null
  return {
    id,
    provider: 'gateway',
    kind,
    version: safeDisplayVersion(candidate.version),
    estimatedActivityAt: nonEmptyString(candidate.estimatedActivityAt),
    sessionCount: finiteNonNegative(candidate.sessionCount),
    sizeBytes: finiteNonNegative(candidate.sizeBytes),
    previouslyImported: candidate.previouslyImported === true,
  }
}

function uniqueCandidates(items: MigrationCandidate[]): MigrationCandidate[] {
  const seen = new Set<string>()
  return items.filter((candidate) => {
    if (seen.has(candidate.id)) return false
    seen.add(candidate.id)
    return true
  })
}

function resetPreview(): void {
  selectedCandidate.value = null
  desktopSummary.value = null
  gatewayPreview.value = null
  previewId.value = ''
  overwrite.value = false
  phase.value = ''
  inlineError.value = ''
}

async function focusHeading(): Promise<void> {
  await nextTick()
  headingEl.value?.focus()
}

function subscribeProgress(): void {
  if (progressUnsub || !desktopBridge?.onMigrationProgress) return
  progressUnsub = desktopBridge.onMigrationProgress((state) => {
    if (!state || typeof state.phase !== 'string') return
    // Electron progress detail is intentionally excluded from the live region:
    // older shells may include a local source/target path in free-form detail.
    phase.value = state.phase
  })
}

function unsubscribeProgress(): void {
  progressUnsub?.()
  progressUnsub = null
}

async function loadRecoveryContext(): Promise<void> {
  if (!platform.capabilities.isDesktop) return
  try {
    const raw = await desktopBridge?.getRecoveryState?.()
    if (raw && typeof raw === 'object') {
      const inspection = (raw as Record<string, unknown>).inspection
      if (inspection && typeof inspection === 'object') {
        recoveryInspection.value = inspection as RecoveryInspection
      }
    }
  } catch {
    // Compatibility context is optional; migration discovery remains usable.
  }
}

async function loadProfileKind(): Promise<void> {
  if (!hasDesktopMigrationBridge.value) return
  try {
    const kind = await desktopBridge?.getDesktopProfileKind?.()
    profileKind.value = kind === 'primary' || kind === 'recovery' ? kind : 'unknown'
  } catch {
    profileKind.value = 'unknown'
  }
}

async function loadLastResult(): Promise<void> {
  if (profileKind.value !== 'primary') return
  const read = desktopBridge?.migrationPeekLastResult ?? desktopBridge?.migrationTakeLastResult
  if (!read) return
  try {
    lastResult.value = await read()
  } catch (error) {
    inlineError.value = localizedMigrationDetail(error instanceof Error ? error.message : String(error))
  }
}

async function scanDesktopSources(): Promise<void> {
  if (!desktopBridge?.migrationSummary) return
  await loadProfileKind()
  await loadLastResult()
  if (profileKind.value === 'recovery') {
    panelState.value = 'recovery'
    return
  }
  const result = await desktopBridge.migrationSummary()
  const detected = Array.isArray(result.candidates)
    ? result.candidates.filter(isDesktopCandidate).map(normalizeDesktopCandidate)
    : []
  if (isDesktopCandidate(result.candidate)) detected.push(normalizeDesktopCandidate(result.candidate))
  candidates.value = uniqueCandidates(detected)
  panelState.value = candidates.value.length > 0 ? 'ready' : 'empty'
}

async function scanGatewaySources(): Promise<void> {
  await rpc.waitForConnection()
  if (!rpc.supportsMethod('migration.sources.list')) {
    panelState.value = 'unsupported'
    return
  }
  const response = await rpc.call<GatewaySourcesResponse>('migration.sources.list', {})
  if (response?.schemaVersion !== 1 || response.mode !== 'preview_only') {
    throw new Error(t('settings.dataMigration.invalidResponse'))
  }
  if (response.capabilities?.discover !== true || response.capabilities?.preview !== true) {
    panelState.value = 'unsupported'
    return
  }
  const rawCandidates = Array.isArray(response.candidates) ? response.candidates : []
  candidates.value = uniqueCandidates(rawCandidates.flatMap((candidate) => {
    const normalized = normalizeGatewayCandidate(candidate)
    return normalized ? [normalized] : []
  }))
  panelState.value = candidates.value.length > 0 ? 'ready' : 'empty'
}

async function refreshSources(): Promise<void> {
  busy.value = true
  panelState.value = 'loading'
  candidates.value = []
  resetPreview()
  try {
    await loadRecoveryContext()
    if (hasDesktopMigrationBridge.value) await scanDesktopSources()
    else await scanGatewaySources()
  } catch (error) {
    if ((error as { code?: unknown } | null)?.code === 'METHOD_NOT_FOUND') {
      panelState.value = 'unsupported'
      return
    }
    inlineError.value = presentationError(error)
    panelState.value = 'error'
  } finally {
    busy.value = false
  }
}

async function previewDesktopCandidate(candidate: MigrationCandidate): Promise<void> {
  if (!desktopBridge?.migrationSummary || !candidate.path) return
  const result = await desktopBridge.migrationSummary({ source: candidate.path })
  if (
    result.report == null
    || !nonEmptyString(result.previewId)
    || !isDesktopCandidate(result.candidate)
    || result.candidate.path !== candidate.path
  ) {
    throw new Error(localizedMigrationDetail(result.raw))
  }
  selectedCandidate.value = normalizeDesktopCandidate(result.candidate)
  desktopSummary.value = summarizeMigrationReport(result.report)
  previewId.value = result.previewId as string
  subscribeProgress()
}

async function previewGatewayCandidate(candidate: MigrationCandidate): Promise<void> {
  if (!rpc.supportsMethod('migration.sources.preview')) {
    panelState.value = 'unsupported'
    return
  }
  const response = await rpc.call<GatewayPreviewResponse>('migration.sources.preview', {
    candidateId: candidate.id,
  })
  if (response?.schemaVersion !== 1 || response.mode !== 'preview_only') {
    throw new Error(t('settings.dataMigration.invalidResponse'))
  }
  selectedCandidate.value = candidate
  gatewayPreview.value = response
}

async function previewCandidate(candidate: MigrationCandidate): Promise<void> {
  busy.value = true
  resetPreview()
  try {
    if (candidate.provider === 'desktop') await previewDesktopCandidate(candidate)
    else await previewGatewayCandidate(candidate)
    await focusHeading()
  } catch (error) {
    if ((error as { code?: unknown } | null)?.code === 'METHOD_NOT_FOUND') {
      panelState.value = 'unsupported'
      return
    }
    inlineError.value = presentationError(error)
  } finally {
    busy.value = false
  }
}

async function browseSource(kind: ProfileSourceKind): Promise<void> {
  if (!desktopBridge?.migrationBrowseSource) return
  busy.value = true
  inlineError.value = ''
  try {
    const result = await desktopBridge.migrationBrowseSource({ kind })
    if (result.aborted) return
    if (!result.ok || !isDesktopCandidate(result.candidate)) {
      throw new Error(localizedMigrationDetail(result.detail || result.error))
    }
    const candidate = normalizeDesktopCandidate(result.candidate)
    candidates.value = uniqueCandidates([...candidates.value, candidate])
    panelState.value = 'ready'
    await previewDesktopCandidate(candidate)
    await focusHeading()
  } catch (error) {
    inlineError.value = presentationError(error)
  } finally {
    busy.value = false
  }
}

async function runMigration(): Promise<void> {
  if (!desktopBridge?.migrationRun || !previewId.value || !desktopSummary.value) return
  if (!overwrite.value) {
    const approved = await confirm({
      title: t('setup.runtime.migrationConfirmTitle'),
      body: t('setup.runtime.migrationConfirmBody'),
      primaryLabel: t('setup.runtime.migrationConfirmPrimary'),
    })
    if (!approved) return
  }
  busy.value = true
  inlineError.value = ''
  try {
    const result = await desktopBridge.migrationRun({
      overwrite: overwrite.value,
      previewId: previewId.value,
    })
    if (result.aborted) return
    const failure = result.ok ? '' : localizedMigrationDetail(result.detail, result.failureCode)
    lastResult.value = result
    resetPreview()
    inlineError.value = failure
  } catch (error) {
    inlineError.value = localizedMigrationDetail(error instanceof Error ? error.message : String(error))
  } finally {
    busy.value = false
  }
}

async function dismissLastResult(): Promise<void> {
  try {
    await desktopBridge?.migrationDismissLastResult?.()
  } finally {
    lastResult.value = null
  }
}

async function recheckLastSource(): Promise<void> {
  const source = lastResult.value?.source
  if (!source || !desktopBridge?.migrationSummary) return
  busy.value = true
  inlineError.value = ''
  try {
    const candidate: MigrationCandidate = {
      id: `recheck\u0000${source}`,
      provider: 'desktop',
      kind: lastResult.value?.sourceKind || 'unknown',
      path: source,
    }
    await previewDesktopCandidate(candidate)
    panelState.value = 'ready'
    await focusHeading()
  } catch (error) {
    inlineError.value = presentationError(error)
  } finally {
    busy.value = false
  }
}

async function restartAfterMigration(): Promise<void> {
  if (!platform.gateway.retryStartup) return
  busy.value = true
  inlineError.value = ''
  try {
    const result = await platform.gateway.retryStartup()
    if (!result.ok) {
      inlineError.value = t('setup.runtime.restartFailed', {
        error: result.error || t('errorBoundary.defaultMessage'),
      })
      return
    }
    const status = await platform.gateway.getStatus()
    if (status.status !== 'ready') {
      inlineError.value = t('setup.runtime.restartFailed', {
        error: status.error || t('errorBoundary.defaultMessage'),
      })
      return
    }
    await dismissLastResult()
  } catch (error) {
    inlineError.value = t('setup.runtime.restartFailed', {
      error: error instanceof Error ? error.message : t('errorBoundary.defaultMessage'),
    })
  } finally {
    busy.value = false
  }
}

async function revealBackups(): Promise<void> {
  try {
    await desktopBridge?.revealRecoveryPath?.({ target: 'backups' })
  } catch (error) {
    inlineError.value = presentationError(error)
  }
}

async function chooseLegacyAgentDataLocation(): Promise<void> {
  if (!desktopBridge?.chooseLegacyAgentDataLocation) return
  busy.value = true
  inlineError.value = ''
  try {
    const result = await desktopBridge.chooseLegacyAgentDataLocation({})
    if (
      result
      && typeof result === 'object'
      && (result as { ok?: unknown }).ok === false
    ) {
      const error = nonEmptyString((result as { error?: unknown }).error)
      throw new Error(error || t('settings.dataMigration.loadFailed'))
    }
    await loadRecoveryContext()
  } catch (error) {
    inlineError.value = presentationError(error)
  } finally {
    busy.value = false
  }
}

async function openCleanup(mode: CleanupMode, trigger?: EventTarget | null): Promise<void> {
  if (!desktopBridge?.inspectDesktopCleanup) return
  if (trigger instanceof HTMLElement) cleanupReturnFocusEl.value = trigger
  cleanupBusy.value = true
  cleanupOpen.value = false
  cleanupPreviewId.value = ''
  cleanupReport.value = null
  cleanupAcknowledged.value = false
  cleanupConfirmation.value = ''
  try {
    const result = await desktopBridge.inspectDesktopCleanup({ mode })
    cleanupReport.value = result.report
    cleanupProfile.value = result.profile
    cleanupPreviewId.value = result.previewId || ''
    cleanupOpen.value = true
    await nextTick()
    cleanupTitleEl.value?.focus()
  } catch (error) {
    pushToast(t('setup.runtime.cleanup.inspectFailed', {
      detail: error instanceof Error ? error.message : String(error),
    }), { tone: 'danger' })
  } finally {
    cleanupBusy.value = false
  }
}

function clearCleanupState(): void {
  cleanupOpen.value = false
  cleanupPreviewId.value = ''
  cleanupReport.value = null
  cleanupProfile.value = null
  cleanupAcknowledged.value = false
  cleanupConfirmation.value = ''
}

async function closeCleanupAndRestoreFocus(): Promise<void> {
  const returnFocus = cleanupReturnFocusEl.value
  clearCleanupState()
  cleanupReturnFocusEl.value = null
  await nextTick()
  returnFocus?.focus()
}

async function cancelCleanup(): Promise<void> {
  const previewId = cleanupPreviewId.value
  if (previewId && desktopBridge?.discardDesktopCleanup) {
    cleanupBusy.value = true
    try {
      await desktopBridge.discardDesktopCleanup({ previewId })
    } catch (error) {
      pushToast(t('setup.runtime.cleanup.applyFailed', {
        detail: error instanceof Error ? error.message : String(error),
      }), { tone: 'danger' })
      cleanupBusy.value = false
      return
    }
    cleanupBusy.value = false
  }
  await closeCleanupAndRestoreFocus()
}

async function presentCleanupResult(result: CleanupResult): Promise<void> {
  if (result.report) cleanupReport.value = result.report
  cleanupPreviewId.value = result.previewId || ''
  if (result.profile) cleanupProfile.value = result.profile
  cleanupAcknowledged.value = false
  cleanupConfirmation.value = ''
  cleanupOpen.value = Boolean(cleanupReport.value)
  await nextTick()
  cleanupTitleEl.value?.focus()
}

async function revealCleanupLocation(): Promise<void> {
  if (!desktopBridge?.revealDesktopUserData) return
  try {
    await desktopBridge.revealDesktopUserData()
  } catch (error) {
    pushToast(t('setup.runtime.cleanup.revealFailed', {
      detail: error instanceof Error ? error.message : String(error),
    }), { tone: 'danger' })
  }
}

async function applyCleanup(): Promise<void> {
  const report = cleanupReport.value
  if (!desktopBridge?.applyDesktopCleanup || !report || !cleanupCanApply.value) return
  if (report.mode === 'reset-current-settings') {
    const approved = await confirm({
      title: t('setup.runtime.cleanup.resetConfirmTitle'),
      body: t('setup.runtime.cleanup.resetConfirmBody'),
      primaryLabel: cleanupApplyLabel.value,
    })
    if (!approved) return
  }
  cleanupBusy.value = true
  try {
    const result = await desktopBridge.applyDesktopCleanup({
      previewId: cleanupPreviewId.value,
      acknowledged: cleanupAcknowledged.value,
      confirmation: cleanupConfirmation.value,
    })
    if (result.aborted) {
      await presentCleanupResult(result)
      return
    }
    if (!result.ok) {
      await presentCleanupResult(result)
      pushToast(t(
        result.partial
          ? 'setup.runtime.cleanup.partial'
          : 'setup.runtime.cleanup.applyFailed',
        { detail: result.detail || result.report?.stable_code || '' },
      ), { tone: 'danger' })
      return
    }
    pushToast(t(
      result.scheduled
        ? 'setup.runtime.cleanup.deleteAllScheduled'
        : report.mode === 'reset-current-settings'
          ? 'setup.runtime.cleanup.resetDone'
          : 'setup.runtime.cleanup.deleteDone',
    ))
    await closeCleanupAndRestoreFocus()
  } catch (error) {
    pushToast(t('setup.runtime.cleanup.applyFailed', {
      detail: error instanceof Error ? error.message : String(error),
    }), { tone: 'danger' })
  } finally {
    cleanupBusy.value = false
  }
}

onMounted(refreshSources)
onUnmounted(unsubscribeProgress)
</script>

<template>
  <section class="control-section data-migration" :aria-busy="busy || cleanupBusy">
    <div class="control-section__head data-migration__head">
      <div>
        <h3
          id="data-migration-title"
          ref="headingEl"
          class="control-section__title"
          tabindex="-1"
          data-testid="data-migration-heading"
        >
          {{ t('settings.dataMigration.title') }}
        </h3>
        <p class="control-section__desc">{{ t('settings.dataMigration.desc') }}</p>
      </div>
      <button
        type="button"
        class="btn btn--ghost"
        :disabled="busy"
        data-testid="data-migration-refresh"
        @click="refreshSources"
      >
        <Icon name="refresh" :size="15" aria-hidden="true" />
        {{ t('settings.dataMigration.refresh') }}
      </button>
    </div>

    <div v-if="knownAttention" class="data-migration__compat" data-testid="data-migration-compatibility">
      <strong>{{ t('settings.dataMigration.compatibilityTitle') }}</strong>
      <p>{{ t('settings.dataMigration.compatibilityDesc') }}</p>
      <button
        v-if="canChooseLegacyAgentData"
        type="button"
        class="btn btn--ghost"
        :disabled="busy"
        @click="chooseLegacyAgentDataLocation"
      >
        {{ t('settings.dataMigration.chooseLegacyAgentData') }}
      </button>
      <details>
        <summary>{{ t('setup.runtime.migrationTechnicalDetails') }}</summary>
        <code>{{ knownAttention.stable_code }}</code>
      </details>
    </div>

    <details v-if="technicalAttention" class="data-migration__technical-attention">
      <summary>{{ t('setup.runtime.migrationTechnicalDetails') }}</summary>
      <code>{{ technicalAttention.stable_code }}</code>
    </details>

    <p v-if="inlineError" class="data-migration__error" role="alert" data-testid="data-migration-error">
      {{ inlineError }}
    </p>

    <div v-if="lastResult" class="data-migration__result" :class="{ 'is-error': !lastResult.ok }">
      <strong>
        {{ t(lastResult.ok
          ? 'setup.runtime.migrationCompleteTitle'
          : lastResult.migrationApplied
            ? 'setup.runtime.migrationAppliedRestartTitle'
            : 'setup.runtime.migrationNotAppliedTitle') }}
      </strong>
      <p v-if="lastResult.ok">{{ t('setup.runtime.migrationCompleteCopied') }}</p>
      <p v-else>{{ failureReason }}</p>
      <details v-if="lastResult.source || lastResult.detail">
        <summary>{{ t('setup.runtime.migrationTechnicalDetails') }}</summary>
        <code v-if="lastResult.source">{{ lastResult.source }}</code>
        <p v-if="lastResult.detail">{{ lastResult.detail }}</p>
      </details>
      <div class="data-migration__actions">
        <button
          v-if="!lastResult.ok && !lastResult.migrationApplied && lastResult.source && desktopBridge?.migrationSummary"
          type="button"
          class="btn btn--ghost"
          :disabled="busy"
          @click="recheckLastSource"
        >
          {{ t('setup.runtime.migrationRecheckSource') }}
        </button>
        <button
          v-if="lastResult.migrationApplied && !lastResult.restartOk && canRestartGateway"
          type="button"
          class="btn btn--ghost"
          :disabled="busy"
          data-testid="data-migration-restart"
          @click="restartAfterMigration"
        >
          {{ t('setup.runtime.restartRuntime') }}
        </button>
        <button
          v-if="lastResult.targetReplaced && desktopBridge?.revealRecoveryPath"
          type="button"
          class="btn btn--ghost"
          @click="revealBackups"
        >
          {{ t('setup.runtime.migrationCompleteShowBackup') }}
        </button>
        <button type="button" class="btn btn--ghost" @click="dismissLastResult">
          {{ t('setup.runtime.migrationCompleteDismiss') }}
        </button>
      </div>
    </div>

    <div v-if="panelState === 'loading'" class="data-migration__state" role="status">
      {{ t('settings.dataMigration.scanning') }}
    </div>
    <div v-else-if="panelState === 'unsupported'" class="data-migration__state" data-testid="data-migration-unsupported">
      <strong>{{ t('settings.dataMigration.unsupportedTitle') }}</strong>
      <p>{{ t('settings.dataMigration.unsupportedDesc') }}</p>
    </div>
    <div v-else-if="panelState === 'recovery'" class="data-migration__state">
      <strong>{{ t('settings.dataMigration.recoveryTitle') }}</strong>
      <p>{{ t('setup.runtime.migrationRecoveryProfile') }}</p>
    </div>
    <div v-else-if="panelState === 'error'" class="data-migration__state">
      <strong>{{ t('settings.dataMigration.loadFailed') }}</strong>
    </div>
    <div v-else-if="panelState === 'empty'" class="data-migration__state" data-testid="data-migration-empty">
      <strong>{{ t('settings.dataMigration.emptyTitle') }}</strong>
      <p>{{ t('settings.dataMigration.emptyDesc') }}</p>
      <div v-if="hasDesktopMigrationBridge && desktopBridge?.migrationBrowseSource" class="data-migration__actions">
        <button
          v-for="kind in MANUAL_SOURCE_KINDS"
          :key="kind"
          type="button"
          class="btn btn--ghost"
          :disabled="busy"
          @click="browseSource(kind)"
        >
          {{ t('setup.runtime.migrationBrowseSourceKind', { source: sourceLabel(kind) }) }}
        </button>
      </div>
    </div>

    <template v-else-if="panelState === 'ready'">
      <template v-if="!selectedCandidate">
        <div class="data-migration__intro">
          <h4>{{ t('setup.runtime.migrationChooseSource') }}</h4>
          <p>{{ t('settings.dataMigration.chooseSourceDesc') }}</p>
        </div>
        <section v-for="group in candidateGroups" :key="group.key" class="migration-candidate-group">
          <h4>{{ group.label }}</h4>
          <ul class="migration-candidates">
            <li v-for="candidate in group.candidates" :key="candidate.id">
              <button
                type="button"
                class="migration-candidate"
                :disabled="busy"
                :aria-label="t('settings.dataMigration.previewSource', { source: sourceLabel(candidate.kind) })"
                @click="previewCandidate(candidate)"
              >
                <span class="migration-candidate__head">
                  <strong>{{ sourceLabel(candidate.kind) }}</strong>
                  <span v-if="candidate.version">{{ candidate.version }}</span>
                </span>
                <span class="migration-candidate__meta">
                  <span v-if="candidate.sessionCount != null">
                    {{ t('setup.runtime.migrationCandidateSessions', { n: candidate.sessionCount }) }}
                  </span>
                  <span v-if="candidate.sizeBytes != null">{{ formatByteSize(candidate.sizeBytes) }}</span>
                  <span v-if="candidate.estimatedActivityAt">{{ activityLabel(candidate.estimatedActivityAt) }}</span>
                  <span v-if="candidate.previouslyImported">
                    {{ t('setup.runtime.migrationCandidatePreviouslyImported') }}
                  </span>
                </span>
              </button>
            </li>
          </ul>
        </section>
        <div v-if="hasDesktopMigrationBridge && desktopBridge?.migrationBrowseSource" class="data-migration__actions">
          <button
            v-for="kind in MANUAL_SOURCE_KINDS"
            :key="kind"
            type="button"
            class="btn btn--ghost"
            :disabled="busy"
            @click="browseSource(kind)"
          >
            {{ t('setup.runtime.migrationBrowseSourceKind', { source: sourceLabel(kind) }) }}
          </button>
        </div>
      </template>

      <div v-else class="migration-summary" data-testid="data-migration-preview">
        <div class="migration-summary__head">
          <h4 class="migration-summary__title">{{ t('setup.runtime.migrationSummaryTitle') }}</h4>
          <span class="migration-summary__kind">{{ sourceLabel(selectedCandidate.kind) }}</span>
        </div>

        <template v-if="isGatewayPreview">
          <p class="data-migration__readonly">{{ t('settings.dataMigration.webReadOnly') }}</p>
          <ul class="migration-summary__content">
            <li v-if="gatewaySessionCount != null">
              {{ t('setup.runtime.migrationContentChats', { n: gatewaySessionCount }) }}
            </li>
            <li v-if="gatewayPlannedCount != null">
              {{ t('setup.runtime.migrationCountPlanned', { n: gatewayPlannedCount }) }}
            </li>
            <li v-if="gatewaySkippedCount != null">
              {{ t('setup.runtime.migrationCountSkipped', { n: gatewaySkippedCount }) }}
            </li>
            <li v-if="gatewayErrorCount != null">
              {{ t('setup.runtime.migrationCountError', { n: gatewayErrorCount }) }}
            </li>
            <li v-if="gatewayPausedJobCount != null">
              {{ t('setup.runtime.migrationContentJobs', { n: gatewayPausedJobCount }) }}
            </li>
          </ul>
          <ul v-if="gatewayBlockers.length" class="migration-summary__errors">
            <li v-for="code in gatewayBlockers" :key="code">{{ localizedCode('blockers', code) }}</li>
          </ul>
          <ul v-if="gatewayNotices.length" class="migration-summary__notes">
            <li v-for="code in gatewayNotices" :key="code">{{ localizedCode('notices', code) }}</li>
          </ul>
          <details class="migration-summary__technical">
            <summary>{{ t('setup.runtime.migrationTechnicalDetails') }}</summary>
            <p v-if="gatewayDiskRequired != null && gatewayDiskFree != null">
              {{ t('setup.runtime.migrationDisk', {
                required: formatByteSize(gatewayDiskRequired),
                free: formatByteSize(gatewayDiskFree),
              }) }}
            </p>
            <p>{{ t('settings.dataMigration.hostCliHint') }}</p>
            <code>opensquilla migrate opensquilla --help</code>
          </details>
        </template>

        <template v-else-if="desktopSummary">
          <div class="data-migration__primary-summary" data-testid="data-migration-primary-summary">
          <ul class="migration-summary__content">
            <li>{{ t('setup.runtime.migrationContentIdentity') }}</li>
            <li v-if="selectedCandidate.sessionCount != null">
              {{ t('setup.runtime.migrationContentChats', { n: selectedCandidate.sessionCount }) }}
            </li>
            <li v-else>{{ t('setup.runtime.migrationContentChatsUnknown') }}</li>
            <li>{{ t('setup.runtime.migrationContentSettings') }}</li>
            <li>{{ t('setup.runtime.migrationContentAssets') }}</li>
            <li>{{ t('setup.runtime.migrationContentJobs', { n: desktopSummary.pausedJobs }) }}</li>
          </ul>
          <p v-if="desktopSummary.errorNotes.length" class="migration-summary__errors">
            {{ localizedCode('blockers', 'preview_unavailable') }}
          </p>
          <div v-if="desktopSummary.needsOverwrite" class="migration-summary__replacement">
            <strong>{{ t('setup.runtime.migrationReplacementTitle') }}</strong>
            <p>{{ localizedCode('notices', 'whole_profile_replacement') }}</p>
            <label>
              <input v-model="overwrite" type="checkbox" />
              <span>{{ t('setup.runtime.migrationOverwrite') }}</span>
            </label>
          </div>
          </div>
          <details class="migration-summary__technical">
            <summary>{{ t('setup.runtime.migrationTechnicalDetails') }}</summary>
            <code v-if="selectedCandidate.path">{{ selectedCandidate.path }}</code>
            <ul class="migration-summary__facts">
              <li>
                {{ t('setup.runtime.migrationDisk', {
                  required: formatByteSize(desktopSummary.diskRequiredBytes),
                  free: formatByteSize(desktopSummary.diskFreeBytes),
                }) }}
              </li>
            </ul>
            <ul v-if="desktopSummary.notes.length" class="migration-summary__notes">
              <li v-for="note in desktopSummary.notes" :key="note">{{ note }}</li>
            </ul>
            <ul v-if="desktopSummary.replacementReason || desktopSummary.errorNotes.length" class="migration-summary__errors">
              <li v-if="desktopSummary.replacementReason">{{ desktopSummary.replacementReason }}</li>
              <li v-for="note in desktopSummary.errorNotes" :key="note">{{ note }}</li>
            </ul>
          </details>
          <p v-if="phase" class="migration-summary__phase" role="status" aria-live="polite">
            {{ t('setup.runtime.migrationPhase', { phase }) }}
          </p>
        </template>

        <div class="migration-summary__actions">
          <button type="button" class="btn btn--ghost" :disabled="busy" @click="resetPreview">
            {{ t('setup.runtime.migrationBackToSources') }}
          </button>
          <button
            v-if="!isGatewayPreview && desktopSummary"
            type="button"
            class="btn"
            :disabled="busy || hasBlockingErrors || (desktopSummary.needsOverwrite && !overwrite)"
            data-testid="data-migration-run"
            @click="runMigration"
          >
            {{ runLabel }}
          </button>
        </div>
      </div>
    </template>

    <section v-if="canCleanup" class="data-migration__cleanup-zone" aria-labelledby="data-cleanup-title">
      <div class="data-migration__cleanup-head">
        <h4 id="data-cleanup-title">{{ t('setup.runtime.cleanup.label') }}</h4>
        <p>{{ t('setup.runtime.cleanup.desc') }}</p>
      </div>
      <div
        class="data-migration__cleanup-actions"
        role="group"
        :aria-label="t('setup.runtime.cleanup.actionsLabel')"
      >
        <button
          type="button"
          class="btn btn--ghost"
          :disabled="busy || cleanupBusy"
          data-testid="data-migration-cleanup-reset"
          @click="openCleanup('reset-current-settings', $event.currentTarget)"
        >
          {{ t('setup.runtime.cleanup.resetAction') }}
        </button>
        <button
          type="button"
          class="btn btn--ghost data-migration__cleanup-danger"
          :disabled="busy || cleanupBusy"
          data-testid="data-migration-cleanup-profile"
          @click="openCleanup('delete-current-profile', $event.currentTarget)"
        >
          {{ t('setup.runtime.cleanup.profileAction') }}
        </button>
        <button
          type="button"
          class="btn btn--ghost data-migration__cleanup-danger"
          :disabled="busy || cleanupBusy"
          data-testid="data-migration-cleanup-all"
          @click="openCleanup('delete-all-user-data', $event.currentTarget)"
        >
          {{ t('setup.runtime.cleanup.allAction') }}
        </button>
      </div>

      <section
        v-if="cleanupOpen && cleanupReport"
        class="cleanup-summary"
        aria-labelledby="cleanup-summary-title"
        data-testid="data-migration-cleanup-summary"
      >
        <div class="cleanup-summary__head">
          <h4 id="cleanup-summary-title" ref="cleanupTitleEl" tabindex="-1">{{ cleanupModeTitle }}</h4>
          <span class="cleanup-summary__profile">
            {{ cleanupProfile?.kind === 'recovery'
              ? t('setup.runtime.cleanup.recoveryProfile')
              : t('setup.runtime.cleanup.primaryProfile') }}
          </span>
        </div>
        <p class="cleanup-summary__warning">{{ cleanupModeWarning }}</p>
        <p class="cleanup-summary__count">
          {{ t('setup.runtime.cleanup.inventoryCount', {
            existing: cleanupExistingCount,
            total: cleanupReport.items.length,
          }) }}
        </p>
        <ul class="cleanup-summary__items" :aria-label="t('setup.runtime.cleanup.inventoryLabel')">
          <li v-for="item in cleanupReport.items" :key="`${item.kind}:${item.path}`">
            <span class="cleanup-summary__item-kind">{{ item.kind }}</span>
            <code>{{ item.path }}</code>
            <span :class="item.exists ? 'cleanup-summary__present' : 'cleanup-summary__missing'">
              {{ item.exists ? t('setup.runtime.cleanup.present') : t('setup.runtime.cleanup.missing') }}
            </span>
          </li>
        </ul>
        <div v-if="cleanupReport.outcome === 'blocked'" class="cleanup-summary__blocked" role="alert">
          <strong>{{ t('setup.runtime.cleanup.blocked') }}</strong>
          <code>{{ cleanupReport.stable_code }}</code>
          <p>{{ t('setup.runtime.cleanup.blockedHelp') }}</p>
        </div>
        <label v-if="cleanupNeedsAcknowledgement" class="cleanup-summary__ack">
          <input v-model="cleanupAcknowledged" type="checkbox" />
          <span>{{ t('setup.runtime.cleanup.acknowledge') }}</span>
        </label>
        <label
          v-if="cleanupReport.outcome === 'ready' && cleanupPreviewId && cleanupReport.mode === 'delete-all-user-data'"
          class="cleanup-summary__phrase"
        >
          <span>{{ t('setup.runtime.cleanup.typePhrase', { phrase: DELETE_ALL_CONFIRMATION }) }}</span>
          <input
            v-model="cleanupConfirmation"
            type="text"
            autocomplete="off"
            spellcheck="false"
            :aria-label="t('setup.runtime.cleanup.phraseLabel')"
          />
        </label>
        <div class="cleanup-summary__actions">
          <button type="button" class="btn btn--ghost" :disabled="cleanupBusy" @click="revealCleanupLocation">
            {{ t('setup.runtime.cleanup.showLocation') }}
          </button>
          <button
            type="button"
            class="btn btn--ghost"
            :disabled="cleanupBusy"
            data-testid="data-migration-cleanup-cancel"
            @click="cancelCleanup"
          >
            {{ t('setup.runtime.cleanup.cancel') }}
          </button>
          <button
            v-if="cleanupReport.outcome === 'ready' && cleanupPreviewId"
            type="button"
            class="btn"
            :disabled="cleanupBusy || !cleanupCanApply"
            data-testid="data-migration-cleanup-apply"
            @click="applyCleanup"
          >
            {{ cleanupApplyLabel }}
          </button>
          <button v-else type="button" class="btn" :disabled="cleanupBusy" @click="openCleanup(cleanupReport.mode)">
            {{ t('setup.runtime.cleanup.retry') }}
          </button>
        </div>
        <p class="cleanup-summary__status" role="status" aria-live="polite">
          {{ cleanupBusy ? t('setup.runtime.cleanup.working') : '' }}
        </p>
      </section>
    </section>
  </section>
</template>

<style scoped>
.data-migration__head,
.data-migration__actions,
.migration-summary__head,
.migration-candidate__head,
.migration-candidate__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.data-migration__head {
  justify-content: space-between;
}

.data-migration__head > div {
  min-width: 0;
}

.data-migration__state,
.data-migration__compat,
.data-migration__result,
.data-migration__primary-summary,
.migration-summary {
  display: grid;
  gap: var(--sp-2);
}

.data-migration__state,
.data-migration__compat,
.data-migration__result,
.migration-summary {
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
}

.data-migration__state p,
.data-migration__compat p,
.data-migration__result p,
.data-migration__intro p,
.data-migration__readonly {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.data-migration__compat {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
}

.data-migration__result {
  border-color: var(--success, var(--ok));
}

.data-migration__result.is-error,
.data-migration__error {
  border-color: var(--danger);
  color: var(--danger);
}

.data-migration__technical-attention {
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.data-migration__error {
  margin: 0;
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--danger) 8%, transparent);
  font-size: var(--fs-sm);
}

.data-migration__actions {
  justify-content: flex-end;
}

.data-migration__intro h4,
.migration-candidate-group h4,
.migration-summary__title {
  margin: 0;
}

.migration-candidate-group {
  display: grid;
  gap: var(--sp-2);
}

.migration-candidates {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.migration-candidate {
  display: grid;
  width: 100%;
  gap: var(--sp-2);
  min-height: 48px;
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: inherit;
  background: var(--bg);
  text-align: left;
  cursor: pointer;
}

.migration-candidate:hover,
.migration-candidate:focus-visible {
  border-color: var(--accent);
}

.migration-candidate__head {
  justify-content: space-between;
}

.migration-candidate__meta {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.migration-summary__kind {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.migration-summary__facts,
.migration-summary__content,
.migration-summary__errors,
.migration-summary__notes {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-sm);
}

.migration-summary__errors {
  color: var(--danger);
}

.migration-summary__notes {
  color: var(--text-muted);
}

.migration-summary__replacement {
  display: grid;
  gap: var(--sp-2);
  padding: var(--sp-3);
  border: 1px solid var(--warn);
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-elevated));
  font-size: var(--fs-sm);
}

.migration-summary__replacement p {
  margin: 0;
}

.migration-summary__replacement label {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
}

.migration-summary__technical {
  min-width: 0;
}

.migration-summary__technical summary,
.data-migration__compat summary,
.data-migration__result summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.migration-summary__technical code,
.data-migration__result code {
  display: block;
  margin-top: var(--sp-2);
  overflow-wrap: anywhere;
  font-size: var(--fs-xs);
}

.migration-summary__phase {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.migration-summary__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--sp-2);
}

.data-migration__cleanup-zone {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-5);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--border);
}

.data-migration__cleanup-head h4,
.data-migration__cleanup-head p,
.cleanup-summary__head h4,
.cleanup-summary__warning,
.cleanup-summary__count,
.cleanup-summary__blocked p,
.cleanup-summary__status {
  margin: 0;
}

.data-migration__cleanup-head p {
  margin-top: var(--sp-1);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.data-migration__cleanup-actions,
.cleanup-summary__head,
.cleanup-summary__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.data-migration__cleanup-danger,
.cleanup-summary__warning,
.cleanup-summary__blocked,
.cleanup-summary__present {
  color: var(--danger);
}

.cleanup-summary {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3);
  border: 1px solid color-mix(in srgb, var(--danger) 40%, var(--border));
  border-radius: var(--radius-md);
  background: var(--bg-elevated);
}

.cleanup-summary__head,
.cleanup-summary__actions {
  align-items: center;
  justify-content: space-between;
}

.cleanup-summary__head h4:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.cleanup-summary__profile,
.cleanup-summary__count,
.cleanup-summary__missing {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.cleanup-summary__items {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-height: 240px;
  overflow: auto;
  margin: 0;
  padding: 0;
  list-style: none;
}

.cleanup-summary__items li {
  display: grid;
  grid-template-columns: minmax(110px, auto) minmax(160px, 1fr) auto;
  align-items: baseline;
  gap: var(--sp-2);
  font-size: var(--fs-xs);
}

.cleanup-summary__items code {
  overflow-wrap: anywhere;
}

.cleanup-summary__item-kind {
  font-weight: 650;
}

.cleanup-summary__blocked {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  padding: var(--sp-2);
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--danger) 8%, transparent);
}

.cleanup-summary__ack,
.cleanup-summary__phrase {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  font-size: var(--fs-sm);
}

.cleanup-summary__phrase {
  flex-direction: column;
}

.cleanup-summary__phrase input {
  width: min(100%, 420px);
}

@media (max-width: 640px) {
  .data-migration__head {
    align-items: flex-start;
  }

  .cleanup-summary__items li {
    grid-template-columns: 1fr auto;
  }

  .cleanup-summary__items code {
    grid-column: 1 / -1;
  }
}
</style>
