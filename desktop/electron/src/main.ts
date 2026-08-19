import { app, BrowserWindow, clipboard, dialog, Menu, ipcMain, nativeTheme, protocol, safeStorage, shell, Tray } from 'electron'
import electronUpdater from 'electron-updater'
import { spawn, spawnSync, type ChildProcess, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { createWriteStream, existsSync, lstatSync, mkdirSync, opendirSync, readFileSync, readdirSync, realpathSync, statSync, writeFileSync, type Stats } from 'node:fs'
import { access, constants, open, readFile, readdir, rename, rm, stat, unlink, writeFile } from 'node:fs/promises'
import net from 'node:net'
import { homedir, tmpdir } from 'node:os'
import { basename, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  DESKTOP_LOCALES,
  normalizeGatewayLocale,
  resolveLocaleFromTags,
  type DesktopLocale,
} from './desktop-locale.js'
import {
  allProfileContexts as enumerateLegacyDesktopProfiles,
  isRecoveryProfileId,
  primaryProfilePaths,
  type DesktopProfilePaths,
} from './desktop-profile-context.js'
import { DesktopWriterAdmission } from './desktop-writer-admission.js'
import {
  createDesktopGatewayInstanceNonce,
  desktopGatewayOwnershipMatchesLaunch,
  desktopProfileFingerprint,
  loadDesktopGatewayOwnershipRecord,
  requestVerifiedDesktopGatewayShutdown,
  verifyDesktopGatewayLaunchOwnership,
  waitForDesktopGatewayOwnershipRelease,
  type DesktopGatewayOwnershipRecord,
} from './desktop-gateway-ownership.js'
import {
  DesktopGatewayOwnershipVerificationCoordinator,
} from './desktop-gateway-ownership-verification.js'
import {
  lifecycleAllowsProcessSpawn,
  stopAndJoinLifecycleProcesses,
  waitForGatewayReadiness,
} from './gateway-lifecycle.js'
import {
  OnboardingFlowCoordinator,
  type CoordinatedOnboardingFlow,
} from './onboarding-flow-coordinator.js'
import { OnboardingSaveTelemetry } from './onboarding-save-telemetry.js'
import { buildCliInvocation } from './cli-invocation.js'
import {
  cleanupSelectorArgs,
  desktopCleanupScopeIsContained,
  DesktopCleanupPreviewStore,
  parseDesktopCleanupMode,
  parseDesktopCleanupProtocol,
  sameDesktopCleanupScope,
  type DesktopCleanupMode,
  type DesktopCleanupReport,
  type DesktopCleanupSelection,
  type TrustedDesktopCleanupPreview,
} from './desktop-cleanup.js'
import { secretStorageBackendForPolicy, shouldUseChromiumMockKeychainForPolicy } from './secret-storage-policy.js'
import { freshDesktopSandboxConfigLines } from './desktop-sandbox-default.js'
import {
  GITHUB_UPDATE_OWNER,
  GITHUB_UPDATE_REPO,
  parseOpenSquillaReleaseTag,
} from './update-feed-resolver.js'
import {
  candidateFromUpdateChannel,
  orderedUpdateSources,
  updateAssetUrl,
  updateChannelManifestFromReleaseInventory,
  updateChannelManifestUrl,
  updateChannelPathForVersion,
  updateFeedBaseUrl,
  UPDATE_GITHUB_RELEASES_API_URL,
  UpdateChannelError,
  type DesktopUpdateCandidate,
  type DesktopUpdatePlatform,
  type DesktopUpdateSource,
} from './update-channel.js'
import {
  parseSha256SumsForAsset,
  readResponseTextWithLimit,
  streamResponseToVerifiedFile,
} from './update-verification.js'
import { isUpdateCheckAllowed, UpdateCheckScheduler } from './update-check-scheduler.js'
import {
  canRevealDesktopApp,
  defaultDesktopPreferences,
  mainWindowCloseAction,
  normalizeDesktopPreferences,
  serializeDesktopPreferences,
  type DesktopExitPhase,
  type DesktopMainWindowCloseBehavior,
  type DesktopPreferencesFile,
  type DesktopWorkbenchPreviewMode,
} from './desktop-window-lifecycle.js'
import {
  DESKTOP_DEEP_LINK_SCHEME,
  desktopDeepLinkArguments,
  parseDesktopDeepLink,
} from './desktop-deep-link.js'
import { projectDirectoryDialogOptions } from './project-directory-picker.js'
import {
  NATIVE_WORKBENCH_CAPABILITIES,
  NATIVE_WORKBENCH_ARTIFACT_SCHEME,
  parseNativeWorkbenchCreateRequest,
  parseNativeWorkbenchNavigationRequest,
  parseNativeWorkbenchPermissionResponse,
  parseNativeWorkbenchSurfaceId,
  parseNativeWorkbenchSurfaceRectRequest,
  type NativeWorkbenchSurfaceEvent,
} from './native-workbench-surface-contract.js'
import {
  NativeWorkbenchSurfaceManager,
} from './native-workbench-surface.js'
import { installDesktopZoomShortcuts } from './desktop-zoom-shortcuts.js'
import {
  buildRendererConsoleLogEntry,
  buildRendererGoneLogEntry,
  buildRendererStateLogEntry,
  RendererConsoleLogLimiter,
} from './desktop-renderer-log.js'
import { appendDesktopLogRecord } from './desktop-log-file.js'
import {
  ArtifactPreviewLeaseBroker,
} from './artifact-preview-lease-broker.js'

protocol.registerSchemesAsPrivileged([{
  scheme: NATIVE_WORKBENCH_ARTIFACT_SCHEME,
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: true,
    corsEnabled: true,
    stream: true,
  },
}])

interface GatewayState {
  url: string
  port: number
  owned: boolean
  status: 'starting' | 'ready' | 'stopped' | 'error'
  logPath: string
  error?: string
}

type SecretEncryption = 'safeStorage' | 'plain'
type DesktopConfigAuthority = 'generated' | 'profile'
type RouterMode = 'recommended' | 'openrouter-mix' | 'disabled'
type ModelRoutingMode = 'squilla_router' | 'direct' | 'llm_ensemble'
type StaticEnsembleSelectionMode = 'static_openrouter_b5' | 'static_tokenrhythm_b5'
type TextRouterTier = 'c0' | 'c1' | 'c2' | 'c3'

interface ProviderCatalogEntry {
  id: string
  label: string
  model: string
  baseUrl: string
  apiKeyEnv: string
  requiresApiKey: boolean
  routerSupported: boolean
  ensembleSelectionMode?: StaticEnsembleSelectionMode
  deployment: 'cloud' | 'local'
  note: string
}

interface SearchProviderCatalogEntry {
  providerId: string
  label: string
  envKey: string
  requiresApiKey: boolean
  note: string
  keyPlaceholder: string
}

interface RouterTier {
  provider: string
  model: string
  description?: string
  supportsImage?: boolean
  imageOnly?: boolean
  thinkingLevel?: string
}

interface DesktopConnection {
  provider: string
  model: string
  baseUrl: string
  apiKeyEnv: string
  encryptedApiKey?: string
  modelRoutingMode: ModelRoutingMode
  routerMode: RouterMode
  routerDefaultTier: TextRouterTier
  routerTiers: Record<string, RouterTier>
  searchProvider: string
  searchApiKeyEnv: string
  encryptedSearchApiKey?: string
  encryption: SecretEncryption
  disableNetworkObservability: boolean
  configAuthority: DesktopConfigAuthority
  importTransactionId: string
  createdAt: string
  updatedAt: string
}

interface OnboardingPayload {
  provider?: unknown
  model?: unknown
  baseUrl?: unknown
  apiKey?: unknown
  modelRoutingMode?: unknown
  routerMode?: unknown
  routerDefaultTier?: unknown
  routerTiers?: unknown
  searchProvider?: unknown
  searchApiKey?: unknown
  disableNetworkObservability?: unknown
  locale?: unknown
}

interface OnboardingProbePayload {
  provider?: unknown
  model?: unknown
  baseUrl?: unknown
  apiKey?: unknown
}

interface OnboardingProbeResult {
  ok: boolean
  failureKind: string
  message: string
  latencyMs: number
}

type OnboardingSaveErrorCode =
  | 'onboarding_inactive'
  | 'save_in_progress'
  | 'recovery_required'
  | 'lifecycle_deferred'

type OnboardingSaveResult =
  | { ok: true }
  | { ok: false; code: OnboardingSaveErrorCode; error: string }

interface OnboardingFlow extends CoordinatedOnboardingFlow<
  OnboardingPayload,
  OnboardingSaveResult
> {
  window: BrowserWindow | null
  resolve: ((credential: DesktopConnection) => void) | null
  reject: ((error: Error) => void) | null
}

interface DesktopSettingsPayload extends OnboardingPayload {}

interface DesktopSettingsSnapshot {
  provider: string
  model: string
  baseUrl: string
  apiKeyConfigured: boolean
  modelRoutingMode: ModelRoutingMode
  routerMode: RouterMode
  routerDefaultTier: TextRouterTier
  routerTiers: Record<string, RouterTier>
  searchProvider: string
  searchApiKeyEnv: string
  searchApiKeyConfigured: boolean
  disableNetworkObservability: boolean
  configManagedByProfile: boolean
  searchProviders: SearchProviderCatalogEntry[]
  providers?: { providerId: string; label: string; model: string; baseUrl: string }[]
  gateway: GatewayState
}

interface DesktopPreferencesPayload {
  mainWindowCloseBehavior?: unknown
  workbenchPreviewMode?: unknown
  workbenchPreviewNoticeShown?: unknown
  sandboxUnavailableWarningSuppressed?: unknown
}

interface DesktopPreferencesSnapshot {
  schemaVersion: 3
  mainWindowCloseBehavior: DesktopMainWindowCloseBehavior
  workbenchPreviewMode: DesktopWorkbenchPreviewMode
  effectiveWorkbenchPreviewMode: DesktopWorkbenchPreviewMode
  workbenchPreviewNoticeShown: boolean
  sandboxUnavailableWarningSuppressed: boolean
  workbenchPreviewForcedOffline: boolean
  canRunInBackground: boolean
  platform: 'darwin' | 'win32' | 'linux' | 'other'
}

interface SandboxUnavailablePayload {
  state: 'failed' | 'unavailable'
  message?: string
}

interface RuntimeLaunch {
  command: string
  args: string[]
  cwd: string
  mode: 'bundled' | 'dev'
}

type RecoveryOutcome = 'ready' | 'attention' | 'recovery_required'

interface RecoveryCandidate {
  kind: string
  path: string
  exists: boolean
  valid: boolean
  configured: boolean
  identity?: string
  modified_at_ns?: number
}

interface RecoveryProtocolResult {
  schema_version: number
  outcome: RecoveryOutcome
  stable_code: string
  primary_home: string
  effective_workspace: string | null
  candidates: RecoveryCandidate[]
  allowed_actions: string[]
  transaction_id: string | null
  revision: number
  detail: string | null
}

type DesktopProfileConsolidationOutcome = 'noop' | 'consolidated' | 'blocked'
type DesktopCredentialAdoptionStatus = 'pending' | 'complete' | 'not_required'

interface DesktopProfileConsolidationResult {
  schema_version: 1
  outcome: DesktopProfileConsolidationOutcome
  stable_code: string
  primary_home: string
  configuration_source_recovery_id: string | null
  configuration_source_credential_path: string | null
  configuration_source_credential_sha256: string | null
  configuration_source_credential_size: number | null
  consumed_recovery_ids: string[]
  backup_path: string | null
  receipt_path: string | null
  credential_adoption_status: DesktopCredentialAdoptionStatus
  revision: number
  errors: string[]
}

interface DesktopProfileConsolidationMaintenance {
  kind: 'profile-consolidation'
  stable_code: string
  retryable: true
  recovery_profile_count: number
}

interface DesktopRecoveryViewState {
  inspection: RecoveryProtocolResult | null
  maintenance: DesktopProfileConsolidationMaintenance | null
  blocked: boolean
  busy: boolean
  error: string | null
}

type BootPhaseId = 'profile' | 'gateway-start' | 'gateway-health' | 'control' | 'ready'

interface BootStatus {
  phaseId: BootPhaseId
  label: string
  at: string
}

interface BootError {
  message: string
  at: string
}

interface MacInstallContext {
  appBundlePath: string | null
  translocated: boolean
  inApplications: boolean
  blocked: boolean
}

const __dirname = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(__dirname, '..')
const defaultRepoRoot = resolve(packageRoot, '..', '..')
const repoRoot = process.env.OPENSQUILLA_DESKTOP_REPO_ROOT
  ? resolve(process.env.OPENSQUILLA_DESKTOP_REPO_ROOT)
  : defaultRepoRoot
const shouldUseNativeApplicationMenu = process.platform === 'darwin'

let mainWindow: BrowserWindow | null = null
let onboardingWindow: BrowserWindow | null = null
let windowsTray: Tray | null = null
let appExitPhase: DesktopExitPhase = 'running'
let systemSessionEnding = false
let windowsSessionEndPreviousPhase: DesktopExitPhase | null = null
let windowsSessionEndResetTimer: NodeJS.Timeout | null = null
let mainWindowClosePrompt: Promise<void> | null = null
let pendingDesktopDeepLinkOpen = false
let desktopDeepLinkActivationReady = false
let desktopPreferencesCache: {
  value: DesktopPreferencesFile
  writable: boolean
} | null = null
let desktopPreferencesWritePromise: Promise<void> = Promise.resolve()
let sandboxUnavailableWarningShownThisLaunch = false

type DesktopNativeThemeSource = 'light' | 'dark' | 'system'

const DESKTOP_LIGHT_BACKGROUND_COLOR = '#F7F7F8'
const DESKTOP_DARK_BACKGROUND_COLOR = '#18181A'

function desktopWindowBackgroundColor(): string {
  return nativeTheme.shouldUseDarkColors
    ? DESKTOP_DARK_BACKGROUND_COLOR
    : DESKTOP_LIGHT_BACKGROUND_COLOR
}

function normalizeDesktopNativeThemeSource(payload: unknown): DesktopNativeThemeSource {
  const source = typeof payload === 'string'
    ? payload
    : payload && typeof payload === 'object' && 'source' in payload
      ? (payload as { source?: unknown }).source
      : undefined
  return source === 'light' || source === 'dark' || source === 'system' ? source : 'system'
}

function applyDesktopNativeTheme(source: DesktopNativeThemeSource): { source: DesktopNativeThemeSource; shouldUseDarkColors: boolean } {
  nativeTheme.themeSource = source
  const backgroundColor = desktopWindowBackgroundColor()
  for (const window of [mainWindow, onboardingWindow]) {
    if (window && !window.isDestroyed()) window.setBackgroundColor(backgroundColor)
  }
  return { source, shouldUseDarkColors: nativeTheme.shouldUseDarkColors }
}
let gatewayProcess: ChildProcessWithoutNullStreams | null = null
let gatewayProfileKey: string | null = null
let isQuitting = false
// A child remains lifecycle-owned until its exit event, even after stopGateway
// clears the current slot so a replacement cannot accidentally reuse it. Quit,
// update, cleanup, and recovery all join this set before Electron may exit.
const gatewayStoppingProcesses = new Set<ChildProcessWithoutNullStreams>()
const gatewayProcessOwnershipContexts = new WeakMap<ChildProcessWithoutNullStreams, {
  nonce: string
  ownershipDir: string
  profileFingerprint: string
  port: number
}>()
// Opt stopGateway into the Windows HTTP graceful-drain path even while isQuitting
// is set, for the update/uninstall flows that keep the main process alive and
// await the child's exit (so the fire-and-forget drain is not racing app teardown).
let allowGracefulShutdownWhileQuitting = false

// Main-process lifecycle log (distinct from the gateway child's gateway.log).
// Records launch, single-instance-lock acquisition, and quit phases so a
// "second launch does nothing" report (issue #446) is diagnosable from a user
// machine. Synchronous append: lifecycle events are rare, renderer events are
// rate-limited, and every record must survive an imminent app.exit(). The file
// sink caps individual records and rotates a bounded backup set.
function desktopLog(event: string, detail?: Record<string, unknown>): void {
  try {
    appendDesktopLogRecord(
      join(app.getPath('userData'), 'logs', 'desktop.log'),
      event,
      detail,
    )
  } catch {
    // Logging must never break the lifecycle it observes.
  }
}

function nativeWorkbenchFailureReason(event: NativeWorkbenchSurfaceEvent): string {
  if (event.type === 'error') return 'load-failed'
  const reason = event.detail?.reason
  if (
    reason === 'unresponsive'
    || reason === 'owner-unresponsive'
    || reason === 'clean-exit'
    || reason === 'abnormal-exit'
    || reason === 'killed'
    || reason === 'crashed'
    || reason === 'oom'
    || reason === 'launch-failed'
    || reason === 'integrity-failure'
  ) return reason
  return 'unknown'
}

let gatewayStartPromise: Promise<GatewayState> | null = null
let onboardingSaveTelemetryAttempt = 0
const onboardingFlows = new OnboardingFlowCoordinator<
  OnboardingPayload,
  OnboardingSaveResult,
  OnboardingFlow
>()
let secretStorageBackendCache: SecretEncryption | null = null
let macCodeSignatureDiagnosticCache: string | null = null
let bootStatus: BootStatus = {
  phaseId: 'profile',
  label: 'Preparing desktop profile',
  at: new Date().toISOString(),
}
let bootError: BootError | null = null
let forceOnboardingOnNextStartup = false
let recoveryInspection: RecoveryProtocolResult | null = null
let primaryRecoveryInspection: RecoveryProtocolResult | null = null
let recoveryOperationBusy = false
let recoveryOperationError: string | null = null
const desktopCleanupPreviews = new DesktopCleanupPreviewStore()
let desktopCleanupBusy = false
// Keep the post-exit delete-all helper and its open stdin pipe strongly
// reachable until Electron exits. The pipe is unref'ed (so it cannot delay
// exit) but must not be garbage-collected and closed while Chromium still owns
// userData handles.
let pendingDeleteAllHelper: ChildProcess | null = null
const gatewayProcessTreeChildren = new WeakSet<ChildProcessWithoutNullStreams>()
const desktopWriters = new DesktopWriterAdmission()
let desktopOpenFlowRevision = 0
let desktopOpenFlowPromise: Promise<void> | null = null
let bootResumePromise: Promise<{ ok: boolean; error?: string }> | null = null

function invalidateDesktopOpenFlow(): number {
  desktopOpenFlowRevision += 1
  return desktopOpenFlowRevision
}

function desktopOpenAuthorityIsCurrent(revision: number, profileKey: string): boolean {
  return !isQuitting
    && revision === desktopOpenFlowRevision
    && profileKey === desktopProfileKey()
}

function beginDesktopWriterOperation(label: string): () => void {
  return desktopWriters.begin(label)
}

function waitForDesktopWriterOperations(maximumActive = 0): Promise<void> {
  return desktopWriters.waitForAtMost(maximumActive)
}

const gatewayState: GatewayState = {
  url: '',
  port: 0,
  owned: false,
  status: 'stopped',
  logPath: '',
}

const artifactPreviewLeaseBroker = new ArtifactPreviewLeaseBroker({
  getOwnedGatewayUrl: () => (
    gatewayState.owned && gatewayState.status === 'ready'
      ? gatewayState.url
      : null
  ),
})

const nativeWorkbenchSurfaces = new NativeWorkbenchSurfaceManager({
  forceArtifactPreviewsOffline: process.env.OPENSQUILLA_PREVIEW_FORCE_OFFLINE === '1',
  getPrivilegedGatewayUrl: () => (
    gatewayState.status === 'ready' && gatewayState.url
      ? gatewayState.url
      : null
  ),
  getWindow: () => currentMainWindow(),
  emit: event => {
    if (event.type === 'error' || event.type === 'crashed') {
      desktopLog('native_workbench_surface_failed', {
        platform: process.platform,
        type: event.type,
        reason: nativeWorkbenchFailureReason(event),
      })
    }
    const window = currentMainWindow()
    if (
      !window
      || !gatewayState.owned
      || !gatewayState.url
      || !isCurrentWindowAtControlUi(window, gatewayState.url)
    ) return
    window.webContents.send('desktop:workbench:surface-event', event)
  },
})
function activeDesktopProfile(): DesktopProfilePaths {
  return primaryProfilePaths(app.getPath('userData'))
}

function primaryDesktopProfile(): DesktopProfilePaths {
  return primaryProfilePaths(app.getPath('userData'))
}

function legacyRecoveryProfiles(): DesktopProfilePaths[] {
  return enumerateLegacyDesktopProfiles(app.getPath('userData')).filter(
    (profile) => profile.kind === 'recovery',
  )
}

function desktopHome(): string {
  return activeDesktopProfile().home
}

function primaryDesktopHome(): string {
  return primaryDesktopProfile().home
}

function desktopConfigPath(): string {
  return join(desktopHome(), 'config.toml')
}

function desktopStateDir(): string {
  return join(desktopHome(), 'state')
}

function desktopGatewayOwnershipDir(profile = activeDesktopProfile()): string {
  // Keep lifecycle control metadata out of the profile's data state directory.
  // A config may intentionally point state_dir elsewhere, and creating a
  // previously-missing H/state here would change legacy-lock exclusion during
  // upgrades. userData is process-control state, keyed by the canonical profile.
  return join(
    app.getPath('userData'),
    'gateway-ownership',
    desktopProfileFingerprint(profile.home),
  )
}

function credentialPath(): string {
  return activeDesktopProfile().credentialPath
}

function desktopLogsDir(): string {
  return activeDesktopProfile().logsDir
}

function desktopProfileKey(profile = activeDesktopProfile()): string {
  return profile.kind
}

function desktopChildEnvironment(
  profile: DesktopProfilePaths,
  additions: NodeJS.ProcessEnv = {},
): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = { ...process.env }
  return {
    ...environment,
    ...additions,
    OPENSQUILLA_DESKTOP: '1',
    OPENSQUILLA_INSTALL_METHOD: 'desktop',
    OPENSQUILLA_PROFILE_KIND: 'desktop-primary',
    OPENSQUILLA_GATEWAY_CONFIG_PATH: join(profile.home, 'config.toml'),
    // Historical name retained for compatibility: this is H, never H/state.
    OPENSQUILLA_STATE_DIR: profile.home,
  }
}

// ── RC4 Desktop layout recovery boundary ───────────────────────────────
// RC3's direct TypeScript relocation is intentionally retired. RC4 evaluates
// historical layouts through the Python recovery engine before any profile
// writer starts; ambiguous or conflicting trees are never moved here.

// ── Legacy home import detection ────────────────────────────────────────────
// Quick read-only scan for a legacy OpenSquilla home this desktop profile could
// import. The shape check mirrors the Python migrator's home validator
// (config.toml, state/, or workspace/ inside), so both sides agree on what
// counts as a home — but the import itself always runs through the bundled CLI
// (`opensquilla migrate opensquilla`) so the migration logic exists exactly
// once, in Python; Electron only owns trusted source selection and lifecycle.
type MigrationSourceKind = 'cli-home' | 'desktop-home' | 'windows-portable'

interface LegacyImportCandidate {
  kind: MigrationSourceKind
  path: string
  version: string | null
  estimated_activity_at: string | null
  session_count: number | null
  size_bytes: number | null
  previously_imported: boolean
}

function parseMigrationSourceKind(payload: unknown): MigrationSourceKind | null {
  const record = migrationRecord(payload)
  const kind = record?.kind
  return kind === 'cli-home' || kind === 'desktop-home' || kind === 'windows-portable'
    ? kind
    : null
}

const manuallyApprovedMigrationCandidates = new Map<string, LegacyImportCandidate>()

function looksLikeOpenSquillaHome(path: string): boolean {
  try {
    const info = lstatSync(path)
    if (!info.isDirectory() || info.isSymbolicLink()) return false
  } catch {
    return false
  }
  for (const [name, expected] of [
    ['config.toml', 'file'],
    ['state', 'directory'],
    ['workspace', 'directory'],
  ] as const) {
    try {
      const info = lstatSync(join(path, name))
      if (info.isSymbolicLink()) continue
      if (expected === 'file' ? info.isFile() : info.isDirectory()) return true
    } catch {
      // Missing or unreadable shape probes do not make this a candidate.
    }
  }
  return false
}

const LEGACY_METADATA_MAX_CANDIDATES = 12
const LEGACY_METADATA_MAX_DIRECTORY_ENTRIES = 256
const LEGACY_METADATA_TIMEOUT_MS = 5_000
const LEGACY_METADATA_WORKERS = 3

function legacyCandidateActivity(home: string): string | null {
  try {
    const info = lstatSync(home)
    if (!info.isDirectory() || info.isSymbolicLink()) return null
    return new Date(info.mtimeMs).toISOString()
  } catch {
    return null
  }
}

function legacyImportCandidate(
  kind: LegacyImportCandidate['kind'],
  path: string,
): LegacyImportCandidate {
  return {
    kind,
    path,
    version: null,
    estimated_activity_at: legacyCandidateActivity(path),
    session_count: null,
    size_bytes: null,
    // Python enriches this advisory field while inspecting the candidate. It
    // never suppresses an otherwise valid source.
    previously_imported: false,
  }
}

function legacyCandidateIdentity(path: string, info: Stats): string {
  const device = Number(info.dev)
  const inode = Number(info.ino)
  if (
    Number.isSafeInteger(device)
    && Number.isSafeInteger(inode)
    && (device !== 0 || inode !== 0)
  ) return `stat:${device}:${inode}`

  let canonical: string
  try {
    canonical = realpathSync(path)
  } catch {
    canonical = resolve(path)
  }
  return `path:${process.platform === 'win32' ? canonical.toLowerCase() : canonical}`
}

// Compare via realpath so a symlinked/relocated desktop home is never offered
// to itself as an import source.
function resolvedPathsEqual(a: string, b: string): boolean {
  const canonical = (path: string): string => {
    let value: string
    try {
      value = realpathSync(path)
    } catch {
      value = resolve(path)
    }
    // Python persists comparison paths through os.path.normcase(), which
    // lowercases Windows paths. Keep the trusted Electron boundary on the
    // same contract so equivalent drive/path casing is not rejected after a
    // successful verifier round trip.
    return process.platform === 'win32' ? value.toLowerCase() : value
  }
  return canonical(a) === canonical(b)
}

const MIGRATION_ITEM_STATUSES = new Set(['migrated', 'planned', 'skipped', 'error'])

interface MigrationReportExpectation {
  source: string
  sourceKind?: LegacyImportCandidate['kind']
  target: string
  apply: boolean
}

function migrationRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function migrationReportValidationError(
  value: unknown,
  expected: MigrationReportExpectation,
): string | null {
  const report = migrationRecord(value)
  if (!report) return 'migration report root is not an object'
  if (typeof report.source !== 'string' || !resolvedPathsEqual(report.source, expected.source)) {
    return 'migration report source does not match the approved preview'
  }
  if (
    typeof report.source_kind !== 'string'
    || !['cli-home', 'windows-portable', 'desktop-home'].includes(report.source_kind)
    || (expected.sourceKind !== undefined && report.source_kind !== expected.sourceKind)
  ) {
    return 'migration report source kind is invalid'
  }
  if (typeof report.target !== 'string' || !resolvedPathsEqual(report.target, expected.target)) {
    return 'migration report target does not match the desktop profile'
  }
  if (report.apply !== expected.apply) return 'migration report apply mode is invalid'
  if (typeof report.output_dir !== 'string') return 'migration report output directory is invalid'

  const items = report.items
  if (!Array.isArray(items)) return 'migration report items are missing'
  for (const value of items) {
    const item = migrationRecord(value)
    if (
      !item
      || typeof item.kind !== 'string'
      || typeof item.status !== 'string'
      || !MIGRATION_ITEM_STATUSES.has(item.status)
      || typeof item.reason !== 'string'
      || (item.source !== null && typeof item.source !== 'string')
      || (item.destination !== null && typeof item.destination !== 'string')
      || !migrationRecord(item.details)
    ) {
      return 'migration report contains an invalid item'
    }
  }

  for (const key of [
    'candidates',
    'config_transforms',
    'secret_relocations',
    'paused_jobs',
    'notes',
  ]) {
    if (!Array.isArray(report[key])) return `migration report ${key} is invalid`
  }
  const preflight = migrationRecord(report.preflight)
  if (
    !preflight
    || typeof preflight.source_gateway_running !== 'boolean'
    || typeof preflight.target_gateway_running !== 'boolean'
    || typeof preflight.schema_ahead !== 'boolean'
    || typeof preflight.disk_required_bytes !== 'number'
    || typeof preflight.disk_free_bytes !== 'number'
  ) {
    return 'migration report preflight data is invalid'
  }
  return null
}

function migrationReportErrors(report: Record<string, unknown>): Record<string, unknown>[] {
  if (!Array.isArray(report.items)) return []
  return report.items
    .map((item) => migrationRecord(item))
    .filter((item): item is Record<string, unknown> => item?.status === 'error')
}

const DESKTOP_MIGRATION_FAILURE_CODES = new Set([
  'source_snapshot_locked',
  'source_snapshot_changed',
  'source_snapshot_unreadable',
  'migration_apply_failed',
  'gateway_restart_failed',
])

function migrationFailureFromReport(report: Record<string, unknown> | null): {
  failureCode: string
  failureStage: DesktopMigrationFailureStage
  detail: string
} | null {
  if (!report) return null
  const item = migrationReportErrors(report)[0]
  if (!item) return null
  const details = migrationRecord(item.details)
  const stableCode = typeof details?.stable_code === 'string'
    && DESKTOP_MIGRATION_FAILURE_CODES.has(details.stable_code)
    ? details.stable_code
    : 'migration_apply_failed'
  const kind = typeof item.kind === 'string' ? item.kind : ''
  const detail = typeof item.reason === 'string' && item.reason.trim()
    ? item.reason.trim().slice(0, 1000)
    : 'Data transfer did not complete.'
  return {
    failureCode: stableCode,
    failureStage: kind.startsWith('preflight/') || kind === 'source' || kind === 'target'
      ? 'preflight'
      : 'apply',
    detail,
  }
}

function conciseMigrationProcessError(value: string): string {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith('{') && !line.startsWith('['))
    ?.slice(0, 1000) || 'The migration command did not return a valid result.'
}

const MIGRATION_TRANSACTION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const IMPORTED_PROVIDER_API_KEY_ENV_RE = /^[A-Za-z_][A-Za-z0-9_]*_(?:KEY|TOKEN)$/i

function migrationTransactionIdFromReport(report: Record<string, unknown> | null): string | null {
  if (!report || typeof report.output_dir !== 'string') return null
  const transactionId = basename(report.output_dir)
  return MIGRATION_TRANSACTION_ID_RE.test(transactionId)
    && resolvedPathsEqual(dirname(report.output_dir), migrationReceiptRoot())
    ? transactionId
    : null
}

function windowsPortableHomeRoots(): string[] {
  const roots: string[] = []
  if (process.env.LOCALAPPDATA) {
    roots.push(join(process.env.LOCALAPPDATA, 'OpenSquilla', 'portable'))
  }
  roots.push(join(process.env.TEMP || tmpdir(), 'OpenSquilla', 'portable'))
  return roots
}

function detectWindowsPortableImportCandidates(): LegacyImportCandidate[] {
  if (process.platform !== 'win32') return []

  const candidates: LegacyImportCandidate[] = []
  const identities = new Set<string>()
  for (const root of windowsPortableHomeRoots()) {
    let directory: ReturnType<typeof opendirSync> | null = null
    try {
      const rootInfo = lstatSync(root)
      if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) continue
      directory = opendirSync(root)
    } catch {
      continue
    }
    try {
      for (let inspected = 0; inspected < LEGACY_METADATA_MAX_DIRECTORY_ENTRIES; inspected += 1) {
        const entry = directory.readSync()
        if (!entry) break
        const path = join(root, entry.name)
        if (!looksLikeOpenSquillaHome(path) || resolvedPathsEqual(path, primaryDesktopHome())) {
          continue
        }
        try {
          const info = lstatSync(path)
          if (!info.isDirectory() || info.isSymbolicLink()) continue
          const identity = legacyCandidateIdentity(path, info)
          if (identities.has(identity)) continue
          identities.add(identity)
          candidates.push(legacyImportCandidate('windows-portable', path))
        } catch {
          // A candidate can disappear between enumeration and display.
        }
        if (candidates.length >= LEGACY_METADATA_MAX_CANDIDATES) break
      }
    } finally {
      directory.closeSync()
    }
    if (candidates.length >= LEGACY_METADATA_MAX_CANDIDATES) break
  }
  return candidates.sort((left, right) => left.path.localeCompare(right.path))
}

function detectLegacyImportCandidates(): LegacyImportCandidate[] {
  const candidates: LegacyImportCandidate[] = []
  const identities = new Set<string>()
  const addCandidate = (candidate: LegacyImportCandidate) => {
    try {
      const info = lstatSync(candidate.path)
      if (!info.isDirectory() || info.isSymbolicLink()) return
      const identity = legacyCandidateIdentity(candidate.path, info)
      if (identities.has(identity)) return
      identities.add(identity)
      candidates.push(candidate)
    } catch {
      // A candidate can disappear between enumeration and display.
    }
  }

  // Explicitly browsed source classifications win over automatic heuristics.
  for (const candidate of manuallyApprovedMigrationCandidates.values()) {
    if (
      looksLikeOpenSquillaHome(candidate.path)
      && !resolvedPathsEqual(candidate.path, primaryDesktopHome())
    ) addCandidate(legacyImportCandidate(candidate.kind, candidate.path))
  }

  const cliHome = join(homedir(), '.opensquilla')
  if (
    looksLikeOpenSquillaHome(cliHome)
    && !resolvedPathsEqual(cliHome, primaryDesktopHome())
  ) {
    addCandidate(legacyImportCandidate('cli-home', cliHome))
  }
  for (const candidate of detectWindowsPortableImportCandidates()) addCandidate(candidate)
  return candidates.sort((left, right) => (
    Number(manuallyApprovedMigrationCandidates.has(resolve(right.path)))
    - Number(manuallyApprovedMigrationCandidates.has(resolve(left.path)))
    || left.kind.localeCompare(right.kind)
    || left.path.localeCompare(right.path)
  )).slice(0, LEGACY_METADATA_MAX_CANDIDATES)
}

function bootPagePath(): string {
  return app.isPackaged
    ? join(process.resourcesPath, 'boot.html')
    : join(packageRoot, 'src', 'boot.html')
}

function appIconPath(): string {
  // Only icon.icns (macOS) and icon.ico (Windows) ship in assets/ — there is no
  // icon.png, so the previous path resolved to a missing file everywhere. On
  // macOS BrowserWindow.icon is ignored (the bundle icon is used), so pointing at
  // the platform icon that exists is correct for the surfaces that do read it.
  const iconFile = process.platform === 'win32' ? 'icon.ico' : 'icon.icns'
  return app.isPackaged
    ? join(process.resourcesPath, 'app.asar', 'assets', iconFile)
    : join(packageRoot, 'assets', iconFile)
}

const MAC_APP_TRANSLOCATION_SEGMENT = '/AppTranslocation/'
const MAC_APP_RESOURCES_SUFFIX = '.app/Contents/Resources'

function normalizedPosixPath(path: string): string {
  return path.replace(/\\/g, '/').replace(/\/+$/, '')
}

function macAppBundlePath(resourcesPath = process.resourcesPath): string | null {
  const normalized = normalizedPosixPath(resourcesPath)
  const markerIndex = normalized.indexOf(MAC_APP_RESOURCES_SUFFIX)
  if (markerIndex < 0) return null
  return normalized.slice(0, markerIndex + '.app'.length)
}

function isMacApplicationsBundlePath(bundlePath: string | null): boolean {
  if (!bundlePath) return false
  const normalized = normalizedPosixPath(bundlePath)
  return normalized === '/Applications/OpenSquilla.app' || normalized.startsWith('/Applications/')
}

function macDesktopInstallContext(): MacInstallContext {
  if (process.platform !== 'darwin' || !app.isPackaged) {
    return {
      appBundlePath: null,
      translocated: false,
      inApplications: true,
      blocked: false,
    }
  }

  const resourcesPath = normalizedPosixPath(process.resourcesPath)
  const appBundlePath = macAppBundlePath(resourcesPath)
  const installPath = appBundlePath || resourcesPath
  const translocated = installPath.includes(MAC_APP_TRANSLOCATION_SEGMENT)
  const inApplications = isMacApplicationsBundlePath(appBundlePath)
  return {
    appBundlePath,
    translocated,
    inApplications,
    blocked: translocated,
  }
}

function macDesktopInstallBlockerMessage(context = macDesktopInstallContext()): string | null {
  if (!context.blocked) return null
  const currentLocation = context.appBundlePath ? ` Current location: ${context.appBundlePath}` : ''
  return (
    'OpenSquilla is running from a temporary macOS AppTranslocation location. ' +
    'Quit OpenSquilla, drag OpenSquilla.app from the DMG into Applications if you are installing it, ' +
    'eject the DMG, then open OpenSquilla again.' +
    currentLocation
  )
}

function assertSupportedMacInstallLocation(): void {
  const message = macDesktopInstallBlockerMessage()
  if (message) throw new Error(message)
}

function sendBootStatus(phaseId: BootPhaseId): void {
  bootStatus = { phaseId, label: desktopT('boot.' + phaseId), at: new Date().toISOString() }
  bootError = null
  mainWindow?.webContents.send('desktop:boot:status', bootStatus)
}

function sendBootError(error: unknown): void {
  bootError = {
    message: error instanceof Error ? error.message : String(error),
    at: new Date().toISOString(),
  }
  mainWindow?.webContents.send('desktop:boot:error', bootError)
}

const TEXT_ROUTER_TIERS: TextRouterTier[] = ['c0', 'c1', 'c2', 'c3']
// Legacy desktop builds (and any credential.json written before the c0-c3
// rename) used t0-t3. Canonicalize those on read so upgrading users don't end
// up with duplicate tier keys in their generated config.
const LEGACY_TEXT_TIER_ALIASES: Record<string, TextRouterTier> = {
  t0: 'c0',
  t1: 'c1',
  t2: 'c2',
  t3: 'c3',
}

function canonicalTierKey(name: string): string {
  return LEGACY_TEXT_TIER_ALIASES[name] ?? name
}
const ROUTER_PROFILE_IDS = new Set(['tokenrhythm', 'openrouter', 'dashscope', 'deepseek', 'gemini', 'volcengine', 'openai', 'zhipu', 'moonshot'])
const INLINE_ROUTER_PROFILE_IDS = new Set(['tokenrhythm'])
const TOKENRHYTHM_REGISTER_URL = 'https://tokenrhythm.studio/register'
const DESKTOP_ENSEMBLE_PROFILES: Record<StaticEnsembleSelectionMode, {
  provider: string
  proposers: string[]
  aggregator: string
}> = {
  static_tokenrhythm_b5: {
    provider: 'tokenrhythm',
    proposers: ['deepseek-v4-pro', 'glm-5.2', 'kimi-k2.7-code', 'qwen3.7-max'],
    aggregator: 'glm-5.2',
  },
  static_openrouter_b5: {
    provider: 'openrouter',
    proposers: [
      'deepseek/deepseek-v4-pro',
      'z-ai/glm-5.2',
      'moonshotai/kimi-k2.7-code',
      'qwen/qwen3.7-max',
    ],
    aggregator: 'z-ai/glm-5.2',
  },
}

const PROVIDER_CATALOG: ProviderCatalogEntry[] = [
  {
    id: 'tokenrhythm',
    label: 'TokenRhythm',
    model: 'deepseek-v4-pro',
    baseUrl: 'https://tokenrhythm.studio/v1',
    apiKeyEnv: 'TOKENRHYTHM_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    ensembleSelectionMode: 'static_tokenrhythm_b5',
    deployment: 'cloud',
    note: 'DeepSeek, GLM, MiniMax and Kimi model families on one key.',
  },
  {
    id: 'openrouter',
    label: 'OpenRouter',
    model: 'deepseek/deepseek-v4-pro',
    baseUrl: 'https://openrouter.ai/api/v1',
    apiKeyEnv: 'OPENROUTER_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    ensembleSelectionMode: 'static_openrouter_b5',
    deployment: 'cloud',
    note: 'One account for mixed-model routing.',
  },
  {
    id: 'openai',
    label: 'OpenAI',
    model: 'gpt-5.4-mini',
    baseUrl: 'https://api.openai.com/v1',
    apiKeyEnv: 'OPENAI_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'OpenAI-only tier profile.',
  },
  {
    id: 'openai_responses',
    label: 'OpenAI (Responses API)',
    model: 'gpt-5.5',
    baseUrl: 'https://api.openai.com/v1',
    apiKeyEnv: 'OPENAI_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'OpenAI Responses-API shape (chat + responses).',
  },
  {
    id: 'anthropic',
    label: 'Anthropic',
    model: 'claude-sonnet-4-5',
    baseUrl: 'https://api.anthropic.com',
    apiKeyEnv: 'ANTHROPIC_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Direct Claude access without SquillaRouter tiers.',
  },
  {
    id: 'dashscope',
    label: 'Aliyun DashScope',
    model: 'qwen3.7-plus',
    baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    apiKeyEnv: 'DASHSCOPE_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'Qwen tier profile for Mainland-friendly access.',
  },
  {
    id: 'bailian_coding_cn',
    label: 'Bailian Coding (Mainland China)',
    model: 'qwen3.7-plus',
    baseUrl: 'https://coding.dashscope.aliyuncs.com/v1',
    apiKeyEnv: 'BAILIAN_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Mainland China Coding Plan. Requires a dedicated sk-sp- API key.',
  },
  {
    id: 'bailian_coding',
    label: 'Bailian Coding (International)',
    model: 'qwen3.7-plus',
    baseUrl: 'https://coding-intl.dashscope.aliyuncs.com/v1',
    apiKeyEnv: 'BAILIAN_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'International Coding Plan. Requires a dedicated sk-sp- API key.',
  },
  {
    id: 'deepseek',
    label: 'DeepSeek',
    model: 'deepseek-v4-flash',
    baseUrl: 'https://api.deepseek.com',
    apiKeyEnv: 'DEEPSEEK_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'DeepSeek-only fast and pro routing.',
  },
  {
    id: 'gemini',
    label: 'Google Gemini',
    model: 'gemini-3.5-flash',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
    apiKeyEnv: 'GEMINI_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'Gemini OpenAI-compatible tier profile.',
  },
  {
    id: 'moonshot',
    label: 'Moonshot AI',
    model: 'kimi-k2.6',
    baseUrl: 'https://api.moonshot.ai/v1',
    apiKeyEnv: 'MOONSHOT_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'Kimi text and image-capable routes.',
  },
  {
    id: 'ollama',
    label: 'Ollama (local)',
    model: '',
    baseUrl: 'http://localhost:11434',
    apiKeyEnv: '',
    requiresApiKey: false,
    routerSupported: false,
    deployment: 'local',
    note: 'Local direct model path.',
  },
  {
    id: 'qianfan',
    label: 'Baidu Qianfan',
    model: 'ernie-4.5-turbo-128k',
    baseUrl: 'https://qianfan.baidubce.com/v2',
    apiKeyEnv: 'QIANFAN_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Direct provider model id required.',
  },
  {
    id: 'kimi_coding_openai',
    label: 'Kimi Coding OpenAI-compatible',
    model: 'kimi-for-coding',
    baseUrl: 'https://api.kimi.com/coding/v1',
    apiKeyEnv: 'KIMI_CODING_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Kimi coding-plan endpoint.',
  },
  {
    id: 'kimi_coding_anthropic',
    label: 'Kimi Coding Anthropic-compatible',
    model: 'kimi-for-coding',
    baseUrl: 'https://api.kimi.com/coding',
    apiKeyEnv: 'KIMI_CODING_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Kimi coding-plan Anthropic-shaped endpoint.',
  },
  {
    id: 'minimax',
    label: 'MiniMax',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimaxi.com/anthropic',
    apiKeyEnv: 'MINIMAX_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax Anthropic-compatible endpoint.',
  },
  {
    id: 'minimax_cn',
    label: 'MiniMax Mainland',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimaxi.com/anthropic',
    apiKeyEnv: 'MINIMAX_CN_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax mainland Anthropic-compatible endpoint.',
  },
  {
    id: 'minimax_global',
    label: 'MiniMax Global',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimax.io/anthropic',
    apiKeyEnv: 'MINIMAX_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax global Anthropic-compatible endpoint.',
  },
  {
    id: 'minimax_openai',
    label: 'MiniMax OpenAI-compatible',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimax.io/v1',
    apiKeyEnv: 'MINIMAX_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax OpenAI-compatible endpoint.',
  },
  {
    id: 'minimax_coding_openai',
    label: 'MiniMax Coding OpenAI-compatible',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimaxi.com/v1',
    apiKeyEnv: 'MINIMAX_CODING_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax coding-plan OpenAI-compatible endpoint.',
  },
  {
    id: 'minimax_coding_anthropic',
    label: 'MiniMax Coding Anthropic-compatible',
    model: 'MiniMax-M2.7',
    baseUrl: 'https://api.minimaxi.com/anthropic',
    apiKeyEnv: 'MINIMAX_CODING_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiniMax coding-plan Anthropic-shaped endpoint.',
  },
  {
    id: 'mimo_openai',
    label: 'MiMo OpenAI-compatible',
    model: 'mimo-v2.5',
    baseUrl: 'https://token-plan-cn.xiaomimimo.com/v1',
    apiKeyEnv: 'MIMO_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiMo coding-plan endpoint.',
  },
  {
    id: 'mimo_anthropic',
    label: 'MiMo Anthropic-compatible',
    model: 'mimo-v2.5',
    baseUrl: 'https://token-plan-cn.xiaomimimo.com/anthropic',
    apiKeyEnv: 'MIMO_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'MiMo coding-plan Anthropic-shaped endpoint.',
  },
  {
    id: 'volcengine',
    label: 'Volcengine Ark',
    model: 'doubao-seed-2-0-lite-260215',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
    apiKeyEnv: 'VOLCENGINE_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'Doubao tier profile.',
  },
  {
    id: 'volcengine_coding_plan',
    label: 'Volcengine Coding Plan',
    model: 'doubao-seed-2.0-pro',
    baseUrl: 'https://ark.cn-beijing.volces.com/api/coding/v3',
    apiKeyEnv: 'VOLCENGINE_API_KEY',
    requiresApiKey: true,
    routerSupported: false,
    deployment: 'cloud',
    note: 'Volcengine coding-plan endpoint.',
  },
  {
    id: 'zhipu',
    label: 'Zhipu (Z.AI)',
    model: 'glm-5',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    apiKeyEnv: 'ZAI_API_KEY',
    requiresApiKey: true,
    routerSupported: true,
    deployment: 'cloud',
    note: 'GLM tier profile.',
  },
]

const PROVIDER_BY_ID = new Map(PROVIDER_CATALOG.map((provider) => [provider.id, provider]))

const SEARCH_PROVIDER_CATALOG: SearchProviderCatalogEntry[] = [
  {
    providerId: 'duckduckgo',
    label: 'DuckDuckGo',
    envKey: '',
    requiresApiKey: false,
    note: 'No key required. Good default for getting started.',
    keyPlaceholder: 'not required',
  },
  {
    providerId: 'bocha',
    label: 'Bocha',
    envKey: 'BOCHA_SEARCH_API_KEY',
    requiresApiKey: true,
    note: 'Web search with inline summaries and freshness support.',
    keyPlaceholder: 'BOCHA_SEARCH_API_KEY',
  },
  {
    providerId: 'brave',
    label: 'Brave Search',
    envKey: 'BRAVE_SEARCH_API_KEY',
    requiresApiKey: true,
    note: 'Managed search access with freshness support.',
    keyPlaceholder: 'BRAVE_SEARCH_API_KEY',
  },
  {
    providerId: 'tavily',
    label: 'Tavily',
    envKey: 'TAVILY_API_KEY',
    requiresApiKey: true,
    note: 'Freshness-oriented web search for current research.',
    keyPlaceholder: 'TAVILY_API_KEY',
  },
  {
    providerId: 'exa',
    label: 'Exa',
    envKey: 'EXA_API_KEY',
    requiresApiKey: true,
    note: 'Semantic and content-oriented search for research workflows.',
    keyPlaceholder: 'EXA_API_KEY',
  },
  {
    providerId: 'iqs',
    label: 'Alibaba Cloud IQS',
    envKey: 'IQS_SEARCH_API_KEY',
    requiresApiKey: true,
    note: 'Alibaba Cloud web search tuned for agents, with strong Chinese-web coverage.',
    keyPlaceholder: 'IQS_SEARCH_API_KEY',
  },
]

const SEARCH_PROVIDER_BY_ID = new Map(
  SEARCH_PROVIDER_CATALOG.map((provider) => [provider.providerId, provider]),
)

function textRouterProfile(
  provider: string,
  c0: string,
  c1: string,
  c2: string,
  c3: string,
  subject: string,
): Record<string, RouterTier> {
  return {
    c0: { provider, model: c0, description: `${subject} fast route`, thinkingLevel: 'off' },
    c1: { provider, model: c1, description: `${subject} balanced route`, thinkingLevel: 'low' },
    c2: { provider, model: c2, description: `${subject} strong route`, thinkingLevel: 'medium' },
    c3: { provider, model: c3, description: `${subject} highest route`, thinkingLevel: 'high' },
  }
}

function minimaxRouterProfile(provider: string): Record<string, RouterTier> {
  return textRouterProfile(
    provider,
    'MiniMax-M2.7',
    'MiniMax-M2.7',
    'MiniMax-M3',
    'MiniMax-M3',
    'MiniMax',
  )
}

const ROUTER_PROFILES: Record<string, Record<string, RouterTier>> = {
  tokenrhythm: {
    c0: { provider: 'tokenrhythm', model: 'deepseek-v4-flash', description: 'Fast DeepSeek route for simple work', supportsImage: false },
    c1: { provider: 'tokenrhythm', model: 'deepseek-v4-pro', description: 'Balanced DeepSeek route for normal agent work', supportsImage: false },
    c2: { provider: 'tokenrhythm', model: 'kimi-k2.7-code', description: 'Strong Kimi route for harder coding and analysis', supportsImage: false },
    c3: { provider: 'tokenrhythm', model: 'glm-5.2', description: 'Highest-tier GLM route for deep review and planning', supportsImage: false },
    image_model: { provider: 'tokenrhythm', model: 'kimi-k2.6', description: 'Vision route for image attachments', supportsImage: true, imageOnly: true },
  },
  openrouter: {
    c0: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash', description: 'Fast everyday work', thinkingLevel: 'high' },
    c1: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro', description: 'Balanced agent work', thinkingLevel: 'high' },
    c2: { provider: 'openrouter', model: 'z-ai/glm-5.2', description: 'Complex reasoning', thinkingLevel: 'high' },
    c3: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8', description: 'Highest quality review and planning', thinkingLevel: 'high' },
    image_model: { provider: 'openrouter', model: 'moonshotai/kimi-k2.6', description: 'Vision route for image attachments', supportsImage: true, imageOnly: true, thinkingLevel: 'medium' },
  },
  openai: {
    c0: { provider: 'openai', model: 'gpt-5.4-nano', description: 'Fast simple work', thinkingLevel: 'none' },
    c1: { provider: 'openai', model: 'gpt-5.4-mini', description: 'Balanced agent work', thinkingLevel: 'low' },
    c2: { provider: 'openai', model: 'gpt-5.5', description: 'Complex text tasks', thinkingLevel: 'medium' },
    c3: { provider: 'openai', model: 'gpt-5.5', description: 'Deep review and analysis', thinkingLevel: 'high' },
  },
  dashscope: {
    c0: { provider: 'dashscope', model: 'qwen3.6-flash', description: 'Fast simple work' },
    c1: { provider: 'dashscope', model: 'qwen3.7-plus', description: 'Balanced agent work' },
    c2: { provider: 'dashscope', model: 'qwen3.7-max', description: 'Complex text tasks' },
    c3: { provider: 'dashscope', model: 'qwen3.7-max', description: 'Deep reasoning' },
  },
  deepseek: {
    c0: { provider: 'deepseek', model: 'deepseek-v4-flash', description: 'Fast simple work' },
    c1: { provider: 'deepseek', model: 'deepseek-v4-flash', description: 'Balanced agent work' },
    c2: { provider: 'deepseek', model: 'deepseek-v4-pro', description: 'Complex text tasks' },
    c3: { provider: 'deepseek', model: 'deepseek-v4-pro', description: 'Deep reasoning' },
  },
  gemini: {
    c0: { provider: 'gemini', model: 'gemini-3.1-flash-lite', description: 'Fast simple work' },
    c1: { provider: 'gemini', model: 'gemini-3.5-flash', description: 'Balanced agent work', thinkingLevel: 'low' },
    c2: { provider: 'gemini', model: 'gemini-3.1-pro-preview', description: 'Complex text tasks', thinkingLevel: 'medium' },
    c3: { provider: 'gemini', model: 'gemini-3.1-pro-preview', description: 'Deep reasoning', thinkingLevel: 'high' },
  },
  moonshot: {
    c0: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Fast multimodal work', supportsImage: true, thinkingLevel: 'low' },
    c1: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Balanced multimodal work', supportsImage: true, thinkingLevel: 'medium' },
    c2: { provider: 'moonshot', model: 'kimi-k2.6', description: 'Complex text and image work', supportsImage: true, thinkingLevel: 'medium' },
    c3: { provider: 'moonshot', model: 'kimi-k2.7-code', description: 'Code-heavy deep reasoning', supportsImage: true, thinkingLevel: 'high' },
  },
  kimi_coding_openai: textRouterProfile(
    'kimi_coding_openai',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'Kimi Coding',
  ),
  kimi_coding_anthropic: textRouterProfile(
    'kimi_coding_anthropic',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'kimi-for-coding',
    'Kimi Coding',
  ),
  volcengine: {
    c0: { provider: 'volcengine', model: 'doubao-seed-2-0-lite-260215', description: 'Fast simple work' },
    c1: { provider: 'volcengine', model: 'doubao-seed-2-0-lite-260215', description: 'Balanced agent work' },
    c2: { provider: 'volcengine', model: 'doubao-seed-2-0-pro-260215', description: 'Complex text tasks' },
    c3: { provider: 'volcengine', model: 'doubao-seed-2-0-pro-260215', description: 'Deep review and analysis' },
  },
  volcengine_coding_plan: textRouterProfile(
    'volcengine_coding_plan',
    'doubao-seed-2.0-lite',
    'doubao-seed-2.0-pro',
    'doubao-seed-2.0-code',
    'doubao-seed-2.0-code',
    'Volcengine Coding Plan',
  ),
  zhipu: {
    c0: { provider: 'zhipu', model: 'glm-5-turbo', description: 'Fast simple work' },
    c1: { provider: 'zhipu', model: 'glm-5', description: 'Balanced agent work' },
    c2: { provider: 'zhipu', model: 'glm-5.1', description: 'Complex text tasks' },
    c3: { provider: 'zhipu', model: 'glm-5.2', description: 'Deep reasoning', thinkingLevel: 'high' },
  },
  minimax: minimaxRouterProfile('minimax'),
  minimax_cn: minimaxRouterProfile('minimax_cn'),
  minimax_global: minimaxRouterProfile('minimax_global'),
  minimax_coding_openai: minimaxRouterProfile('minimax_coding_openai'),
  minimax_coding_anthropic: minimaxRouterProfile('minimax_coding_anthropic'),
  mimo_openai: textRouterProfile(
    'mimo_openai',
    'mimo-v2.5',
    'mimo-v2.5',
    'mimo-v2.5-pro',
    'mimo-v2.5-pro',
    'MiMo',
  ),
  mimo_anthropic: textRouterProfile(
    'mimo_anthropic',
    'mimo-v2.5',
    'mimo-v2.5',
    'mimo-v2.5-pro',
    'mimo-v2.5-pro',
    'MiMo',
  ),
}

function cloneRouterTiers(tiers: Record<string, RouterTier>): Record<string, RouterTier> {
  return Object.fromEntries(Object.entries(tiers).map(([name, tier]) => [name, { ...tier }]))
}

function providerDefaults(provider: string): { model: string; baseUrl: string; apiKeyEnv: string; requiresApiKey: boolean; routerSupported: boolean } {
  const defaults = PROVIDER_BY_ID.get(provider) || PROVIDER_BY_ID.get('openrouter')!
  return {
    model: defaults.model,
    baseUrl: defaults.baseUrl,
    apiKeyEnv: defaults.apiKeyEnv,
    requiresApiKey: defaults.requiresApiKey,
    routerSupported: defaults.routerSupported,
  }
}

function normalizeProvider(raw: unknown): string {
  // Preserve any configured provider id; the desktop UI and gateway both accept
  // ids beyond the local catalog, and collapsing them to openrouter silently
  // loses the user's choice on load/save.
  return String(raw || '').trim().toLowerCase() || 'openrouter'
}

function normalizeTextTier(raw: unknown): TextRouterTier {
  const value = String(raw || '').trim().toLowerCase()
  const canonical = canonicalTierKey(value)
  return TEXT_ROUTER_TIERS.includes(canonical as TextRouterTier) ? canonical as TextRouterTier : 'c1'
}

function normalizeRouterMode(raw: unknown, provider: string): RouterMode {
  const value = String(raw || '').trim().toLowerCase()
  if (value === 'disabled') return 'disabled'
  if (value === 'openrouter-mix' && provider === 'openrouter') return 'openrouter-mix'
  if (ROUTER_PROFILE_IDS.has(provider)) return 'recommended'
  return 'disabled'
}

function modelRoutingModeAllowed(mode: ModelRoutingMode, provider: string): boolean {
  if (mode === 'direct') return true
  if (mode === 'llm_ensemble') {
    return Boolean(PROVIDER_BY_ID.get(provider)?.ensembleSelectionMode)
  }
  return ROUTER_PROFILE_IDS.has(provider)
}

function modelRoutingModeForRouterMode(routerMode: RouterMode, provider: string): ModelRoutingMode {
  if (routerMode === 'disabled') return 'direct'
  if (routerMode === 'openrouter-mix' && provider === 'openrouter') return 'llm_ensemble'
  return modelRoutingModeAllowed('squilla_router', provider) ? 'squilla_router' : 'direct'
}

function normalizeModelRoutingMode(raw: unknown, provider: string, fallbackRouterMode?: RouterMode): ModelRoutingMode {
  const value = String(raw || '').trim().toLowerCase()
  const requested = ['squilla_router', 'direct', 'llm_ensemble'].includes(value)
    ? value as ModelRoutingMode
    : fallbackRouterMode
      ? modelRoutingModeForRouterMode(fallbackRouterMode, provider)
      : modelRoutingModeAllowed('squilla_router', provider)
        ? 'squilla_router'
        : 'direct'
  if (modelRoutingModeAllowed(requested, provider)) return requested
  return modelRoutingModeAllowed('squilla_router', provider) ? 'squilla_router' : 'direct'
}

function routerModeForModelRoutingMode(mode: ModelRoutingMode, provider: string): RouterMode {
  if (mode === 'direct') return 'disabled'
  if (mode === 'llm_ensemble' && modelRoutingModeAllowed(mode, provider)) return 'recommended'
  return normalizeRouterMode('recommended', provider)
}

function defaultRouterTiers(provider: string, mode: RouterMode): Record<string, RouterTier> {
  if (mode === 'disabled') return {}
  if (mode === 'openrouter-mix') return cloneRouterTiers(ROUTER_PROFILES.openrouter)
  return cloneRouterTiers(ROUTER_PROFILES[provider] || ROUTER_PROFILES.openrouter)
}

function normalizeRouterTiers(raw: unknown, fallback: Record<string, RouterTier>): Record<string, RouterTier> {
  if (!raw || typeof raw !== 'object') return cloneRouterTiers(fallback)
  const source = raw as Record<string, unknown>
  const out = cloneRouterTiers(fallback)
  for (const [rawName, value] of Object.entries(source)) {
    if (!value || typeof value !== 'object') continue
    const name = canonicalTierKey(rawName)
    // Tier keys are emitted raw into TOML table headers ([squilla_router.tiers.NAME]),
    // so a key that is not a TOML bare key (spaces, dots, quotes, brackets, newlines)
    // would produce an unparseable config the gateway rejects on every boot. Drop
    // such keys and fall back to the profile defaults instead.
    if (!/^[A-Za-z0-9_-]+$/.test(name)) continue
    const tier = value as Record<string, unknown>
    const provider = String(tier.provider || out[name]?.provider || '').trim()
    const model = String(tier.model || out[name]?.model || '').trim()
    if (!provider || !model) continue
    out[name] = {
      ...out[name],
      provider,
      model,
      description: String(tier.description || out[name]?.description || ''),
      supportsImage: Boolean(tier.supportsImage ?? tier.supports_image ?? out[name]?.supportsImage),
      imageOnly: Boolean(tier.imageOnly ?? tier.image_only ?? out[name]?.imageOnly),
      thinkingLevel: String(tier.thinkingLevel ?? tier.thinking_level ?? out[name]?.thinkingLevel ?? ''),
    }
  }
  return out
}

function routerDefaultModel(tiers: Record<string, RouterTier>, defaultTier: TextRouterTier): string {
  return tiers[defaultTier]?.model || tiers.c1?.model || tiers.c0?.model || ''
}

function searchProviderDefaults(provider: string): SearchProviderCatalogEntry {
  return SEARCH_PROVIDER_BY_ID.get(provider) || SEARCH_PROVIDER_BY_ID.get('duckduckgo')!
}

function normalizeSearchProvider(raw: unknown): string {
  const provider = String(raw || '').trim().toLowerCase()
  return SEARCH_PROVIDER_BY_ID.has(provider) ? provider : 'duckduckgo'
}

function normalizeBooleanSetting(raw: unknown, fallback: boolean): boolean {
  if (typeof raw === 'boolean') return raw
  if (typeof raw === 'number') return raw !== 0
  if (typeof raw === 'string') {
    const value = raw.trim().toLowerCase()
    if (['1', 'true', 'yes', 'on'].includes(value)) return true
    if (['0', 'false', 'no', 'off', ''].includes(value)) return false
  }
  return fallback
}

function truthyEnv(raw: string | undefined): boolean {
  return normalizeBooleanSetting(raw, false)
}

function tomlString(value: string): string {
  return JSON.stringify(value)
}

function inlineScriptJson(value: unknown): string {
  const serialized = JSON.stringify(value)
  if (serialized === undefined) return 'null'
  return serialized
    .replace(/</g, '\\u003c')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029')
}

function routerTierTomlLines(name: string, tier: RouterTier): string[] {
  const lines = [
    `[squilla_router.tiers.${name}]`,
    `provider = ${tomlString(tier.provider)}`,
    `model = ${tomlString(tier.model)}`,
  ]
  if (tier.description) lines.push(`description = ${tomlString(tier.description)}`)
  if (tier.supportsImage !== undefined) lines.push(`supports_image = ${tier.supportsImage ? 'true' : 'false'}`)
  if (tier.imageOnly !== undefined) lines.push(`image_only = ${tier.imageOnly ? 'true' : 'false'}`)
  if (tier.thinkingLevel) lines.push(`thinking_level = ${tomlString(tier.thinkingLevel)}`)
  return lines
}

function routerConfigTomlLines(credential: DesktopConnection): string[] {
  if (credential.routerMode === 'disabled') {
    return [
      '[squilla_router]',
      'enabled = false',
    ]
  }
  const tierLines = Object.entries(credential.routerTiers)
    .filter(([, tier]) => tier.provider && tier.model)
    .flatMap(([name, tier]) => ['', ...routerTierTomlLines(name, tier)])
  return [
    '[squilla_router]',
    'enabled = true',
    'rollout_phase = "full"',
    `default_tier = ${tomlString(credential.routerDefaultTier)}`,
    ...(credential.routerMode === 'recommended' && !INLINE_ROUTER_PROFILE_IDS.has(credential.provider)
      ? [`tier_profile = ${tomlString(credential.provider)}`]
      : []),
    ...tierLines,
  ]
}

function ensembleConfigTomlLines(credential: DesktopConnection): string[] {
  if (credential.modelRoutingMode !== 'llm_ensemble') {
    return [
      '',
      '[llm_ensemble]',
      'enabled = false',
    ]
  }
  const selectionMode = PROVIDER_BY_ID.get(credential.provider)?.ensembleSelectionMode
  if (!selectionMode) {
    throw new Error(`LLM Ensemble is not supported for provider ${credential.provider}.`)
  }
  const profile = DESKTOP_ENSEMBLE_PROFILES[selectionMode]
  const candidates = profile.proposers.flatMap(model => [
    '',
    '[[llm_ensemble.candidates]]',
    `provider = ${tomlString(profile.provider)}`,
    `model = ${tomlString(model)}`,
    'source = "custom"',
    'enabled = true',
    'role = "proposer"',
  ])
  candidates.push(
    '',
    '[[llm_ensemble.candidates]]',
    `provider = ${tomlString(profile.provider)}`,
    `model = ${tomlString(profile.aggregator)}`,
    'source = "custom"',
    'enabled = true',
    'role = "aggregator"',
  )
  return [
    '',
    '[llm_ensemble]',
    'enabled = true',
    'selection_mode = "custom_b5"',
    ...candidates,
  ]
}

function desktopConfigShouldWritePrivacySection(credential: DesktopConnection): boolean {
  return credential.disableNetworkObservability || readDesktopConfigNetworkObservabilitySetting() !== null
}

function privacyConfigTomlLines(credential: DesktopConnection): string[] {
  if (!desktopConfigShouldWritePrivacySection(credential)) return []
  return [
    '',
    '[privacy]',
    `disable_network_observability = ${credential.disableNetworkObservability ? 'true' : 'false'}`,
  ]
}

function plainSecret(secret: string): { value: string; encryption: SecretEncryption } {
  return {
    value: Buffer.from(secret, 'utf8').toString('base64'),
    encryption: 'plain',
  }
}

function macCodeSignatureDiagnostic(): string {
  if (macCodeSignatureDiagnosticCache !== null) return macCodeSignatureDiagnosticCache
  if (process.platform !== 'darwin' || !app.isPackaged) return ''
  const result = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', process.execPath], { encoding: 'utf8' })
  macCodeSignatureDiagnosticCache = `${result.stdout || ''}\n${result.stderr || ''}`
  return macCodeSignatureDiagnosticCache
}

function configureChromiumKeychainPolicy(): void {
  if (shouldUseChromiumMockKeychainForPolicy({
    envMode: process.env.OPENSQUILLA_DESKTOP_SECRET_STORAGE,
    platform: process.platform,
    appPackaged: app.isPackaged,
    codesignDiagnostic: macCodeSignatureDiagnostic(),
  })) {
    app.commandLine.appendSwitch('use-mock-keychain')
  }
}

function desktopSecretStoragePolicyBackend(): SecretEncryption {
  return secretStorageBackendForPolicy({
    envMode: process.env.OPENSQUILLA_DESKTOP_SECRET_STORAGE,
    platform: process.platform,
    appPackaged: app.isPackaged,
    codesignDiagnostic: macCodeSignatureDiagnostic(),
  })
}

function desktopSecretStorageBackend(): SecretEncryption {
  if (secretStorageBackendCache) return secretStorageBackendCache
  const selected = desktopSecretStoragePolicyBackend()
  secretStorageBackendCache = selected === 'safeStorage' && safeStorage.isEncryptionAvailable() ? 'safeStorage' : 'plain'
  return secretStorageBackendCache
}

function encryptSecret(secret: string): { value: string; encryption: SecretEncryption } {
  const policyBackend = desktopSecretStoragePolicyBackend()
  const availableBackend = desktopSecretStorageBackend()
  if (policyBackend === 'safeStorage') {
    if (availableBackend !== 'safeStorage') {
      throw new Error(
        'The OS keychain is unavailable. Unlock it and reopen OpenSquilla before saving credentials.'
      )
    }
    try {
      return {
        value: safeStorage.encryptString(secret).toString('base64'),
        encryption: 'safeStorage',
      }
    } catch (error) {
      throw new Error(
        `The OS keychain could not encrypt the credential: ${
          error instanceof Error ? error.message : String(error)
        }`
      )
    }
  }
  return plainSecret(secret)
}

function decryptSecret(encryptedValue: string | undefined, encryption: SecretEncryption): string {
  if (!encryptedValue) return ''
  const payload = Buffer.from(encryptedValue, 'base64')
  if (encryption === 'safeStorage') {
    if (desktopSecretStorageBackend() !== 'safeStorage') {
      throw new Error('Saved desktop credential requires macOS Keychain, but this local build uses plain credential storage.')
    }
    return safeStorage.decryptString(payload)
  }
  return payload.toString('utf8')
}

function decryptApiKey(record: DesktopConnection): string {
  if (!record.encryptedApiKey) return ''
  return decryptSecret(record.encryptedApiKey, record.encryption)
}

function decryptSearchApiKey(record: DesktopConnection): string {
  if (!record.encryptedSearchApiKey) return ''
  return decryptSecret(record.encryptedSearchApiKey, record.encryption)
}

function isConnectionReady(record: DesktopConnection): boolean {
  return !providerDefaults(record.provider).requiresApiKey || Boolean(decryptApiKey(record))
}

function normalizeDesktopCredential(parsed: Partial<DesktopConnection>): DesktopConnection {
  const provider = normalizeProvider(parsed.provider)
  const defaults = providerDefaults(provider)
  const legacyRouterMode = normalizeRouterMode(parsed.routerMode, provider)
  const modelRoutingMode = normalizeModelRoutingMode(parsed.modelRoutingMode, provider, legacyRouterMode)
  const routerMode = routerModeForModelRoutingMode(modelRoutingMode, provider)
  const routerDefaultTier = normalizeTextTier(parsed.routerDefaultTier)
  const defaultTiers = defaultRouterTiers(provider, routerMode)
  const routerTiers = normalizeRouterTiers(parsed.routerTiers, defaultTiers)
  const searchProvider = normalizeSearchProvider(parsed.searchProvider)
  const searchDefaults = searchProviderDefaults(searchProvider)
  const configAuthority: DesktopConfigAuthority = parsed.configAuthority === 'profile'
    ? 'profile'
    : 'generated'
  const importTransactionId = typeof parsed.importTransactionId === 'string'
    && MIGRATION_TRANSACTION_ID_RE.test(parsed.importTransactionId)
    ? parsed.importTransactionId
    : ''
  if (
    (configAuthority === 'profile' && !importTransactionId)
    || (configAuthority === 'generated' && importTransactionId)
  ) {
    throw new Error('Desktop credential config authority does not match its import transaction.')
  }
  const now = new Date().toISOString()
  return {
    provider,
    model: parsed.model || routerDefaultModel(routerTiers, routerDefaultTier) || defaults.model,
    baseUrl: parsed.baseUrl || defaults.baseUrl,
    apiKeyEnv: parsed.apiKeyEnv || defaults.apiKeyEnv,
    encryptedApiKey: parsed.encryptedApiKey || '',
    modelRoutingMode,
    routerMode,
    routerDefaultTier,
    routerTiers,
    searchProvider,
    searchApiKeyEnv: parsed.searchApiKeyEnv || searchDefaults.envKey,
    encryptedSearchApiKey: parsed.encryptedSearchApiKey || '',
    encryption: parsed.encryption === 'safeStorage' ? 'safeStorage' : 'plain',
    disableNetworkObservability: normalizeBooleanSetting(parsed.disableNetworkObservability, false),
    configAuthority,
    importTransactionId,
    createdAt: parsed.createdAt || now,
    updatedAt: parsed.updatedAt || now,
  }
}

const DESKTOP_CREDENTIAL_CONFIGURATION_FIELDS = [
  'provider',
  'model',
  'baseUrl',
  'apiKeyEnv',
  'encryptedApiKey',
  'modelRoutingMode',
  'routerMode',
  'routerDefaultTier',
  'routerTiers',
  'searchProvider',
  'searchApiKeyEnv',
  'encryptedSearchApiKey',
  'disableNetworkObservability',
  'configAuthority',
  'importTransactionId',
] as const

function desktopCredentialHasUserConfiguration(raw: string): boolean {
  try {
    const parsed = recoveryRecord(JSON.parse(raw))
    if (!parsed) return true
    return DESKTOP_CREDENTIAL_CONFIGURATION_FIELDS.some((field) => {
      if (!Object.prototype.hasOwnProperty.call(parsed, field)) return false
      const value = parsed[field]
      if (typeof value === 'string') return Boolean(value.trim())
      if (typeof value === 'boolean') return true
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        return Object.keys(value).length > 0
      }
      // Preserve malformed-but-present configuration fields instead of
      // overwriting bytes that may contain the user's only setup.
      return value !== null && value !== undefined
    })
  } catch {
    return true
  }
}

// Write via a temp file + atomic rename so a crash, power loss, or full disk
// mid-write cannot leave a truncated credential (silent re-onboarding + lost
// key) or a truncated config.toml (which, since it is only reseeded when
// missing, would wedge boot on every launch).
async function atomicWriteFile(filePath: string, data: string, mode: number): Promise<void> {
  const tmpPath = `${filePath}.${randomUUID()}.tmp`
  try {
    await writeFile(tmpPath, data, { mode })
    await rename(tmpPath, filePath)
  } catch (err) {
    await rm(tmpPath, { force: true }).catch(() => null)
    throw err
  }
}

function desktopPreferencesPath(): string {
  return join(app.getPath('userData'), 'desktop-preferences.json')
}

function desktopPlatformName(): DesktopPreferencesSnapshot['platform'] {
  if (process.platform === 'darwin' || process.platform === 'win32' || process.platform === 'linux') {
    return process.platform
  }
  return 'other'
}

function canRunDesktopInBackground(): boolean {
  return process.platform === 'darwin' || (process.platform === 'win32' && windowsTray !== null)
}

function loadDesktopPreferencesRecord(): {
  value: DesktopPreferencesFile
  writable: boolean
} {
  if (desktopPreferencesCache) return desktopPreferencesCache
  try {
    const parsed = JSON.parse(readFileSync(desktopPreferencesPath(), 'utf8')) as unknown
    desktopPreferencesCache = normalizeDesktopPreferences(parsed, process.platform)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') {
      desktopLog('desktop_preferences_load_failed', {
        error: error instanceof Error ? error.message : String(error),
      })
    }
    desktopPreferencesCache = {
      value: defaultDesktopPreferences(process.platform),
      writable: true,
    }
  }
  return desktopPreferencesCache
}

function desktopPreferencesSnapshot(): DesktopPreferencesSnapshot {
  const preferences = loadDesktopPreferencesRecord().value
  const previewForcedOffline = process.env.OPENSQUILLA_PREVIEW_FORCE_OFFLINE === '1'
  return {
    schemaVersion: 3,
    mainWindowCloseBehavior: preferences.main_window_close_behavior,
    workbenchPreviewMode: preferences.workbench_preview_mode,
    effectiveWorkbenchPreviewMode: previewForcedOffline
      ? 'offline'
      : preferences.workbench_preview_mode,
    workbenchPreviewNoticeShown: preferences.workbench_preview_notice_shown,
    sandboxUnavailableWarningSuppressed:
      preferences.sandbox_unavailable_warning_suppressed,
    workbenchPreviewForcedOffline: previewForcedOffline,
    canRunInBackground: canRunDesktopInBackground(),
    platform: desktopPlatformName(),
  }
}

function enqueueDesktopPreferencesUpdate(
  update: (current: DesktopPreferencesFile) => DesktopPreferencesFile,
): Promise<DesktopPreferencesSnapshot> {
  const operation = desktopPreferencesWritePromise.then(async () => {
    const loaded = loadDesktopPreferencesRecord()
    if (!loaded.writable) {
      throw new Error(
        'Desktop preferences were written by a newer OpenSquilla version and were not changed.',
      )
    }
    const previous = loaded.value
    const next = update(previous)
    try {
      await atomicWriteFile(
        desktopPreferencesPath(),
        serializeDesktopPreferences(next),
        0o600,
      )
      desktopPreferencesCache = { value: next, writable: true }
    } catch (error) {
      desktopPreferencesCache = { value: previous, writable: true }
      throw error
    }
    return desktopPreferencesSnapshot()
  })
  desktopPreferencesWritePromise = operation.then(() => undefined, () => undefined)
  return operation
}

async function saveDesktopPreferences(
  payload: DesktopPreferencesPayload,
): Promise<DesktopPreferencesSnapshot> {
  const behavior = payload?.mainWindowCloseBehavior
  const previewMode = payload?.workbenchPreviewMode
  const previewNoticeShown = payload?.workbenchPreviewNoticeShown
  const sandboxWarningSuppressed = payload?.sandboxUnavailableWarningSuppressed
  if (
    behavior !== undefined
    && behavior !== 'background'
    && behavior !== 'quit'
    && behavior !== 'ask'
  ) {
    throw new Error('Choose a supported main-window close behavior.')
  }
  if (behavior !== undefined && behavior !== 'quit' && !canRunDesktopInBackground()) {
    throw new Error('Background window mode is unavailable because no restore surface is active.')
  }
  if (previewMode !== undefined && previewMode !== 'full' && previewMode !== 'offline') {
    throw new Error('Choose a supported Workbench preview mode.')
  }
  if (previewNoticeShown !== undefined && typeof previewNoticeShown !== 'boolean') {
    throw new Error('The Workbench preview notice state is invalid.')
  }
  if (sandboxWarningSuppressed !== undefined && typeof sandboxWarningSuppressed !== 'boolean') {
    throw new Error('The sandbox availability warning preference is invalid.')
  }
  if (
    behavior === undefined
    && previewMode === undefined
    && previewNoticeShown === undefined
    && sandboxWarningSuppressed === undefined
  ) {
    throw new Error('No supported Desktop preference was provided.')
  }
  return await enqueueDesktopPreferencesUpdate((current) => ({
    ...current,
    ...(behavior !== undefined ? { main_window_close_behavior: behavior } : {}),
    ...(previewMode !== undefined ? { workbench_preview_mode: previewMode } : {}),
    ...(previewNoticeShown !== undefined
      ? { workbench_preview_notice_shown: previewNoticeShown }
      : {}),
    ...(sandboxWarningSuppressed !== undefined
      ? { sandbox_unavailable_warning_suppressed: sandboxWarningSuppressed }
      : {}),
  }))
}

function normalizeSandboxUnavailablePayload(raw: unknown): SandboxUnavailablePayload {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('The sandbox availability report is invalid.')
  }
  const payload = raw as Record<string, unknown>
  if (payload.state !== 'failed' && payload.state !== 'unavailable') {
    throw new Error('The sandbox availability report is invalid.')
  }
  if (
    payload.message !== undefined
    && (typeof payload.message !== 'string' || payload.message.length > 2_000)
  ) {
    throw new Error('The sandbox availability report is invalid.')
  }
  return {
    state: payload.state,
    ...(typeof payload.message === 'string' && payload.message.trim()
      ? { message: payload.message.trim() }
      : {}),
  }
}

async function reportSandboxUnavailable(raw: unknown): Promise<{
  shown: boolean
  suppressed: boolean
}> {
  normalizeSandboxUnavailablePayload(raw)
  const preferences = loadDesktopPreferencesRecord().value
  if (
    preferences.sandbox_unavailable_warning_suppressed
    || sandboxUnavailableWarningShownThisLaunch
  ) {
    return {
      shown: false,
      suppressed: preferences.sandbox_unavailable_warning_suppressed,
    }
  }

  // Reserve the single prompt slot before awaiting the native dialog so
  // concurrent renderer reports cannot open duplicate prompts.
  sandboxUnavailableWarningShownThisLaunch = true
  const options: Electron.MessageBoxOptions = {
    type: 'warning',
    title: desktopT('sandboxUnavailable.title'),
    message: desktopT('sandboxUnavailable.message'),
    detail: desktopT('sandboxUnavailable.detail'),
    buttons: [
      desktopT('sandboxUnavailable.acknowledge'),
      desktopT('sandboxUnavailable.suppress'),
    ],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
  }
  const window = currentMainWindow()
  const result = window
    ? await dialog.showMessageBox(window, options)
    : await dialog.showMessageBox(options)
  if (result.response !== 1) {
    return { shown: true, suppressed: false }
  }

  try {
    await enqueueDesktopPreferencesUpdate((current) => ({
      ...current,
      sandbox_unavailable_warning_suppressed: true,
    }))
  } catch (error) {
    desktopLog('sandbox_unavailable_warning_persist_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
    return { shown: true, suppressed: false }
  }
  return { shown: true, suppressed: true }
}

function markBackgroundCloseNoticeShown(): void {
  const loaded = loadDesktopPreferencesRecord()
  if (!loaded.writable || loaded.value.background_close_notice_shown) return
  // Update the in-memory snapshot immediately so repeated close events in the
  // same session cannot emit duplicate balloons while the atomic write queues.
  desktopPreferencesCache = {
    value: { ...loaded.value, background_close_notice_shown: true },
    writable: true,
  }
  void enqueueDesktopPreferencesUpdate((current) => ({
    ...current,
    background_close_notice_shown: true,
  })).catch((error) => {
    desktopLog('background_close_notice_persist_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
  })
}

async function loadDesktopCredential(): Promise<DesktopConnection | null> {
  try {
    const raw = await readFile(credentialPath(), 'utf8')
    return normalizeDesktopCredential(JSON.parse(raw) as Partial<DesktopConnection>)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw new Error('Saved Desktop credential is invalid or unreadable.', { cause: error })
  }
}

async function saveDesktopCredential(
  payload: OnboardingPayload,
  writerReserved = false,
): Promise<DesktopConnection> {
  const targetProfile = activeDesktopProfile()
  const expectedCredential = await readOptionalDesktopText(targetProfile.credentialPath)
  const existing = await loadDesktopCredential()
  if (existing?.configAuthority === 'profile') {
    throw new Error(
      'This imported profile keeps config.toml authoritative; change settings in the Control UI.',
    )
  }
  const provider = normalizeProvider(payload.provider ?? existing?.provider)
  const defaults = providerDefaults(provider)
  const legacyRouterMode = normalizeRouterMode(payload.routerMode ?? existing?.routerMode, provider)
  const hasModelRoutingMode = Object.prototype.hasOwnProperty.call(payload, 'modelRoutingMode')
  const hasRouterMode = Object.prototype.hasOwnProperty.call(payload, 'routerMode')
  const rawModelRoutingMode = hasModelRoutingMode
    ? payload.modelRoutingMode
    : hasRouterMode
      ? undefined
      : existing?.modelRoutingMode
  const modelRoutingMode = normalizeModelRoutingMode(rawModelRoutingMode, provider, legacyRouterMode)
  const routerMode = routerModeForModelRoutingMode(modelRoutingMode, provider)
  const routerDefaultTier = normalizeTextTier(payload.routerDefaultTier ?? existing?.routerDefaultTier)
  const defaultTiers = defaultRouterTiers(provider, routerMode)
  const existingTiers = existing && existing.provider === provider && existing.routerMode === routerMode
    ? existing.routerTiers
    : defaultTiers
  const routerTiers = normalizeRouterTiers(payload.routerTiers ?? existingTiers, defaultTiers)
  const searchProvider = normalizeSearchProvider(payload.searchProvider ?? existing?.searchProvider)
  const searchDefaults = searchProviderDefaults(searchProvider)
  const apiKey = String(payload.apiKey || '').trim()
  const routerModel = routerDefaultModel(routerTiers, routerDefaultTier)
  const directModel = String(payload.model || existing?.model || defaults.model).trim()
  const model = routerMode === 'disabled'
    ? directModel
    : routerModel || directModel
  const baseUrl = String(payload.baseUrl || existing?.baseUrl || defaults.baseUrl).trim() || defaults.baseUrl
  const searchApiKey = String(payload.searchApiKey || '').trim()
  const resolvedApiKey = apiKey || (existing && provider === existing.provider ? decryptApiKey(existing) : '')
  const resolvedSearchApiKey = searchDefaults.requiresApiKey
    ? searchApiKey || (existing && searchProvider === existing.searchProvider ? decryptSearchApiKey(existing) : '')
    : ''
  const apiKeySecret = resolvedApiKey ? encryptSecret(resolvedApiKey) : null
  const searchApiKeySecret = resolvedSearchApiKey ? encryptSecret(resolvedSearchApiKey) : null
  const encryptedApiKey = apiKeySecret?.value || ''
  const encryptedSearchApiKey = searchApiKeySecret?.value || ''
  const encryption = apiKeySecret?.encryption || searchApiKeySecret?.encryption || 'plain'
  const configDisableNetworkObservability = readDesktopConfigNetworkObservabilitySetting()
  const disableNetworkObservability = Object.prototype.hasOwnProperty.call(payload, 'disableNetworkObservability')
    ? normalizeBooleanSetting(payload.disableNetworkObservability, existing?.disableNetworkObservability ?? false)
    : configDisableNetworkObservability ?? existing?.disableNetworkObservability ?? false
  const configLocale = desktopLocaleChoice(payload.locale) ?? desktopLocale

  if (defaults.requiresApiKey && !encryptedApiKey) throw new Error('API key is required.')
  if (modelRoutingMode === 'llm_ensemble' && !modelRoutingModeAllowed(modelRoutingMode, provider)) {
    throw new Error('LLM Ensemble requires OpenRouter or TokenRhythm in desktop onboarding.')
  }
  if (!routerModel && routerMode !== 'disabled') throw new Error('Router tiers require a default model.')
  if (!model) throw new Error('Model is required.')
  if (searchDefaults.requiresApiKey && !encryptedSearchApiKey) {
    throw new Error(`${searchDefaults.label} search API key is required.`)
  }

  const now = new Date().toISOString()
  const credential: DesktopConnection = {
    provider,
    model,
    baseUrl,
    apiKeyEnv: defaults.apiKeyEnv,
    encryptedApiKey,
    modelRoutingMode,
    routerMode,
    routerDefaultTier,
    routerTiers,
    searchProvider,
    searchApiKeyEnv: searchDefaults.envKey,
    encryptedSearchApiKey,
    encryption,
    disableNetworkObservability,
    configAuthority: 'generated',
    importTransactionId: '',
    createdAt: existing?.createdAt || now,
    updatedAt: now,
  }

  const finishWriter = writerReserved
    ? () => {}
    : beginDesktopWriterOperation('save desktop settings')
  try {
    await applyDesktopSettingsPair(
      targetProfile,
      credential,
      JSON.stringify(credential, null, 2),
      expectedCredential,
      writerReserved,
      configLocale,
    )
    return credential
  } finally {
    finishWriter()
  }
}

function buildImportedDesktopCredential(
  prefill: MigrationProviderPrefill,
  importTransactionId: string,
  apiKeyOverride = '',
): DesktopConnection {
  const provider = normalizeProvider(prefill.provider)
  const defaults = providerDefaults(provider)
  const apiKey = apiKeyOverride.trim() || prefill.apiKey.trim()
  if (defaults.requiresApiKey && !apiKey) throw new Error('API key is required.')
  const secret = apiKey ? encryptSecret(apiKey) : null
  if (!MIGRATION_TRANSACTION_ID_RE.test(importTransactionId)) {
    throw new Error('A verified import transaction is required before credential adoption.')
  }
  const now = new Date().toISOString()
  return normalizeDesktopCredential({
    provider,
    model: prefill.model || defaults.model,
    baseUrl: prefill.baseUrl || defaults.baseUrl,
    apiKeyEnv: prefill.apiKeyEnv || defaults.apiKeyEnv,
    encryptedApiKey: secret?.value || '',
    encryption: secret?.encryption || 'plain',
    disableNetworkObservability: readDesktopConfigNetworkObservabilitySetting() ?? false,
    configAuthority: 'profile',
    importTransactionId,
    createdAt: now,
    updatedAt: now,
  })
}

async function saveImportedDesktopCredential(
  prefill: MigrationProviderPrefill,
  importTransactionId: string,
  apiKeyOverride = '',
  writerReserved = false,
): Promise<DesktopConnection> {
  const profile = primaryDesktopProfile()
  const expectedCredential = await readOptionalDesktopText(profile.credentialPath)
  const importedConfig = await readOptionalDesktopText(join(profile.home, 'config.toml'))
  if (importedConfig === null) {
    throw new Error('The imported profile config.toml is missing; recover the profile before adoption.')
  }
  if (gatewayState.url && await healthCheck(gatewayState.url)) {
    throw new Error('A gateway is still serving this profile; stop it before adopting credentials.')
  }
  const credential = buildImportedDesktopCredential(prefill, importTransactionId, apiKeyOverride)
  const finishWriter = writerReserved
    ? () => {}
    : beginDesktopWriterOperation('adopt imported desktop credential')
  try {
    const inspection = await preflightDesktopConfigWrite(profile)
    const result = await runRecoveryCli(
      profile,
      [
        'apply-settings',
        '--home', profile.home,
        '--transaction-id', inspection.transaction_id ?? '',
        '--expected-revision', String(inspection.revision),
        '--json',
      ],
      JSON.stringify({
        expected_config: importedConfig,
        config: importedConfig,
        expected_credential: expectedCredential,
        credential: JSON.stringify(credential, null, 2),
      }),
      true,
    )
    recoveryInspection = result
    primaryRecoveryInspection = result
    publishRecoveryState()
    if (result.outcome === 'recovery_required') {
      throw new Error(`Imported credential was not adopted (${result.stable_code}).`)
    }
    const readback = await loadDesktopCredential()
    const expectedKey = apiKeyOverride.trim() || prefill.apiKey.trim()
    if (
      !readback
      || readback.configAuthority !== 'profile'
      || readback.importTransactionId !== importTransactionId
      || decryptApiKey(readback) !== expectedKey
    ) {
      throw new Error('Imported credential readback did not match the verified transaction.')
    }
    return readback
  } finally {
    finishWriter()
  }
}

// Sections the desktop config template owns and regenerates from the credential
// on every write. Everything else in config.toml is treated as foreign
// (Control-UI/RPC-owned) and preserved verbatim across regenerations.
const DESKTOP_OWNED_CONFIG_SECTIONS = ['llm', 'squilla_router', 'llm_ensemble', 'privacy', 'control_ui']
// Top-level (pre-section) keys the desktop template emits itself. Any OTHER
// top-level key present in config.toml was written by the Control UI / RPC (which
// serializes the whole GatewayConfig, so scalar fields like
// llm_request_timeout_seconds land in the TOML preamble) and must be preserved.
const DESKTOP_OWNED_CONFIG_PREAMBLE_KEYS = ['search_provider', 'search_api_key_env']

function isDesktopOwnedConfigSection(header: string): boolean {
  const name = header.trim()
  return DESKTOP_OWNED_CONFIG_SECTIONS.some((owned) => name === owned || name.startsWith(`${owned}.`))
}

// Return the lines of every top-level section that the desktop template does not
// own, so they survive a config regeneration. Line-based (like the privacy-config
// reader) to avoid taking on a TOML parser dependency.
function foreignConfigSectionLines(raw: string): string[] {
  const out: string[] = []
  let keeping = false
  for (const rawLine of raw.split(/\r?\n/)) {
    const header = rawLine.trim().match(/^\[+\s*([^\]]+?)\s*\]+$/)
    if (header) keeping = !isDesktopOwnedConfigSection(header[1] ?? '')
    if (keeping) out.push(rawLine)
  }
  while (out.length && out[out.length - 1].trim() === '') out.pop()
  return out
}

// Return the top-level (pre-first-section) key lines the desktop template does
// NOT emit itself, so RPC-written global scalars (llm_request_timeout_seconds,
// log_level, workspace_dir, diagnostics_enabled, …) survive a regeneration. These
// must be re-emitted in the preamble (before any [section]) to stay top-level.
function foreignConfigPreambleLines(raw: string): string[] {
  const out: string[] = []
  for (const rawLine of raw.split(/\r?\n/)) {
    if (/^\s*\[/.test(rawLine)) break // reached the first section header
    const key = rawLine.match(/^\s*(?:([A-Za-z0-9_-]+)|"([A-Za-z0-9_-]+)"|'([A-Za-z0-9_-]+)')\s*=/)
    if (!key) continue // blank line or comment
    const keyName = key[1] || key[2] || key[3] || ''
    if (DESKTOP_OWNED_CONFIG_PREAMBLE_KEYS.includes(keyName)) continue
    out.push(rawLine)
  }
  return out
}

// The Desktop owns the control-ui section's boot fields, while the Gateway
// owns the operator's persisted notice locale. Preserve only that scalar
// instead of retaining a whole section whose other settings Desktop
// must regenerate deterministically.
function persistedControlUiDefaultLocale(raw: string | null): DesktopLocale | null {
  if (raw === null) return null
  let inControlUi = false
  for (const rawLine of raw.split(/\r?\n/)) {
    const header = rawLine.trim().match(/^\[\s*([^\]]+?)\s*\](?:\s*#.*)?$/)
    if (header) {
      inControlUi = header[1] === 'control_ui'
      continue
    }
    if (!inControlUi) continue
    const match = rawLine.match(/^\s*default_locale\s*=\s*["']([^"']*)["']\s*(?:#.*)?$/)
    if (!match) continue
    return normalizeGatewayLocale(match[1])
  }
  return null
}

async function readOptionalDesktopText(path: string): Promise<string | null> {
  try {
    return await readFile(path, 'utf8')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
}

async function preflightDesktopConfigWrite(
  profile = activeDesktopProfile(),
): Promise<RecoveryProtocolResult> {
  const inspection = await inspectDesktopProfile(profile)
  if (inspection.outcome === 'recovery_required') {
    if (desktopProfileKey() === desktopProfileKey(profile)) {
      recoveryInspection = inspection
      publishRecoveryState()
    }
    throw new Error(
      `Desktop profile requires recovery before settings can be written (${inspection.stable_code}).`,
    )
  }
  return inspection
}

function renderDesktopConfigAfterPreflight(
  profile: DesktopProfilePaths,
  credential: DesktopConnection,
  inspection: RecoveryProtocolResult,
  existingRaw: string | null,
  defaultLocale: DesktopLocale,
): string {
  let preservedForeignSections: string[] = []
  let preservedForeignPreamble: string[] = []
  const preservedControlUiLocale = persistedControlUiDefaultLocale(existingRaw)
  if (existingRaw !== null) {
    preservedForeignSections = foreignConfigSectionLines(existingRaw)
    preservedForeignPreamble = foreignConfigPreambleLines(existingRaw)
  }
  const hasPersistedState = preservedForeignPreamble.some((line) => (
    /^\s*(?:state_dir|"state_dir"|'state_dir')\s*=/.test(line)
  ))
  const authoritativeState = inspection.candidates.find((candidate) => candidate.kind === 'state')?.path
  return [
    ...(!hasPersistedState && authoritativeState && resolvedPathsEqual(authoritativeState, join(profile.home, 'state'))
      ? [`state_dir = ${tomlString(join(profile.home, 'state'))}`]
      : []),
    `search_provider = ${tomlString(credential.searchProvider)}`,
    ...(credential.searchApiKeyEnv ? [`search_api_key_env = ${tomlString(credential.searchApiKeyEnv)}`] : []),
    ...preservedForeignPreamble,
    '',
    '[llm]',
    `provider = ${tomlString(credential.provider)}`,
    `model = ${tomlString(credential.model)}`,
    ...(credential.apiKeyEnv ? [`api_key_env = ${tomlString(credential.apiKeyEnv)}`] : []),
    `base_url = ${tomlString(credential.baseUrl)}`,
    '',
    ...routerConfigTomlLines(credential),
    ...ensembleConfigTomlLines(credential),
    ...privacyConfigTomlLines(credential),
    '',
    ...freshDesktopSandboxConfigLines(existingRaw, process.platform),
    '[control_ui]',
    'enabled = true',
    'base_path = "/control"',
    `default_locale = ${tomlString(preservedControlUiLocale ?? defaultLocale)}`,
    '',
    ...(preservedForeignSections.length ? [...preservedForeignSections, ''] : []),
  ].join('\n')
}

async function applyDesktopSettingsPair(
  profile: DesktopProfilePaths,
  credential: DesktopConnection,
  candidateCredential: string,
  expectedCredential: string | null,
  writerReserved = false,
  defaultLocale = desktopLocale,
): Promise<RecoveryProtocolResult> {
  const targetProfileKey = desktopProfileKey(profile)
  if (desktopProfileKey() !== targetProfileKey) {
    throw new Error('The active Desktop profile changed before settings could be saved; retry.')
  }
  const ownedGatewayWasRunning = Boolean(gatewayProcess && gatewayState.owned)
  if (!ownedGatewayWasRunning && gatewayState.url && await healthCheck(gatewayState.url)) {
    throw new Error('A gateway is still serving this profile; stop it and retry the settings save.')
  }
  if (ownedGatewayWasRunning) await stopOwnedGatewayAndWait()
  let restartSafe = false
  try {
    if (desktopProfileKey() !== targetProfileKey) {
      throw new Error('The active Desktop profile changed while its gateway was stopping; retry.')
    }
    const inspection = await preflightDesktopConfigWrite(profile)
    const expectedConfig = await readOptionalDesktopText(join(profile.home, 'config.toml'))
    const currentCredential = await readOptionalDesktopText(profile.credentialPath)
    if (currentCredential !== expectedCredential) {
      throw new Error('Desktop credential changed while settings were being prepared; retry.')
    }
    const candidateConfig = renderDesktopConfigAfterPreflight(
      profile,
      credential,
      inspection,
      expectedConfig,
      defaultLocale,
    )
    const result = await runRecoveryCli(
      profile,
      [
        'apply-settings',
        '--home', profile.home,
        '--transaction-id', inspection.transaction_id ?? '',
        '--expected-revision', String(inspection.revision),
        '--json',
      ],
      JSON.stringify({
        expected_config: expectedConfig,
        config: candidateConfig,
        expected_credential: expectedCredential,
        credential: candidateCredential,
      }),
      writerReserved,
    )
    recoveryInspection = result
    primaryRecoveryInspection = result
    restartSafe = result.outcome !== 'recovery_required'
    publishRecoveryState()
    if (!restartSafe) {
      throw new Error(`Desktop settings were not applied (${result.stable_code}).`)
    }
    return result
  } finally {
    if (ownedGatewayWasRunning && desktopProfileKey() === targetProfileKey) {
      if (!restartSafe) {
        const after = await inspectDesktopProfile(profile)
        recoveryInspection = after
        primaryRecoveryInspection = after
        restartSafe = after.outcome !== 'recovery_required'
      }
      if (restartSafe) {
        clearReusableGatewayState()
        bootError = null
        void openOrResumeDesktopApp()
      } else {
        await restoreMainWindowToBootPage()
        publishRecoveryState()
      }
    }
  }
}

async function writeDesktopConfig(credential: DesktopConnection): Promise<void> {
  if (credential.configAuthority === 'profile') {
    throw new Error('Imported profile config.toml is authoritative and cannot be regenerated.')
  }
  const profile = activeDesktopProfile()
  const expectedCredential = await readOptionalDesktopText(profile.credentialPath)
  if (expectedCredential === null) {
    throw new Error('Desktop credential disappeared before config initialization.')
  }
  const finishWriter = beginDesktopWriterOperation('write desktop config')
  try {
    await applyDesktopSettingsPair(profile, credential, expectedCredential, expectedCredential, true)
  } finally {
    finishWriter()
  }
}

function settingsSnapshot(connection: DesktopConnection | null): DesktopSettingsSnapshot {
  const provider = normalizeProvider(connection?.provider)
  const defaults = providerDefaults(provider)
  const legacyRouterMode = normalizeRouterMode(connection?.routerMode, provider)
  const modelRoutingMode = normalizeModelRoutingMode(connection?.modelRoutingMode, provider, legacyRouterMode)
  const routerMode = routerModeForModelRoutingMode(modelRoutingMode, provider)
  const routerDefaultTier = normalizeTextTier(connection?.routerDefaultTier)
  const routerTiers = normalizeRouterTiers(connection?.routerTiers, defaultRouterTiers(provider, routerMode))
  const searchProvider = normalizeSearchProvider(connection?.searchProvider)
  const searchDefaults = searchProviderDefaults(searchProvider)
  return {
    provider,
    model: connection?.model || routerDefaultModel(routerTiers, routerDefaultTier) || defaults.model,
    baseUrl: connection?.baseUrl || defaults.baseUrl,
    apiKeyConfigured: Boolean(connection?.encryptedApiKey),
    modelRoutingMode,
    routerMode,
    routerDefaultTier,
    routerTiers,
    searchProvider,
    searchApiKeyEnv: connection?.searchApiKeyEnv || searchDefaults.envKey,
    searchApiKeyConfigured: Boolean(connection?.encryptedSearchApiKey),
    disableNetworkObservability: connection?.disableNetworkObservability ?? false,
    configManagedByProfile: connection?.configAuthority === 'profile',
    searchProviders: SEARCH_PROVIDER_CATALOG,
    providers: PROVIDER_CATALOG.map((entry) => ({
      providerId: entry.id,
      label: entry.label,
      model: entry.model,
      baseUrl: entry.baseUrl,
    })),
    gateway: { ...gatewayState },
  }
}

async function loadDesktopSettings(): Promise<DesktopSettingsSnapshot> {
  return settingsSnapshot(await loadDesktopCredential())
}

async function saveDesktopSettings(payload: DesktopSettingsPayload): Promise<DesktopSettingsSnapshot> {
  const connection = await saveDesktopCredential(payload)
  return settingsSnapshot(connection)
}

function clearReusableGatewayState(): void {
  artifactPreviewLeaseBroker.clear()
  gatewayState.url = ''
  gatewayState.port = 0
  gatewayState.owned = false
  gatewayState.status = 'stopped'
  gatewayState.error = undefined
  gatewayProfileKey = null
}

interface ArtifactOpenRequest {
  data?: ArrayBuffer | Uint8Array
  name?: unknown
  mime?: unknown
}

const MIME_EXTENSIONS: Record<string, string> = {
  'application/pdf': '.pdf',
  'text/html': '.html',
  'application/xhtml+xml': '.xhtml',
  'text/plain': '.txt',
  'text/markdown': '.md',
  'text/csv': '.csv',
  'application/json': '.json',
  'application/xml': '.xml',
  'text/xml': '.xml',
  'application/zip': '.zip',
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
  'image/webp': '.webp',
  'image/svg+xml': '.svg',
}

// basename() already drops directory components; here we only neutralize path
// separators and OS-reserved characters, preserving unicode letters, dots,
// dashes and spaces so the extension (and a readable name) survive. The unique
// prefix added at write time means a leading-dot or reserved name cannot escape
// the temp directory or shadow a dotfile.
function safeArtifactFileName(raw: unknown): string {
  const base = basename(String(raw ?? '')).trim()
  const cleaned = base.replace(/[/\\:*?"<>|\x00-\x1f]+/g, '_')
  return cleaned || 'artifact'
}

function artifactMimeKey(mime: unknown): string {
  return String(mime ?? '').split(';', 1)[0].trim().toLowerCase()
}

// Append an extension implied by the MIME type unless the name already ends with
// a recognized document/image extension. The previous "any .xxx suffix counts as
// an extension" heuristic misclassified version/date suffixes (report.v2,
// plan.rev3), so the authoritative MIME extension was dropped and shell.openPath
// hit a missing/incorrect OS association.
function artifactExtension(name: string, mime: unknown): string {
  const lower = name.toLowerCase()
  if (Object.values(MIME_EXTENSIONS).some((ext) => lower.endsWith(ext))) return ''
  return MIME_EXTENSIONS[artifactMimeKey(mime)] || ''
}

// Artifacts opened this session, so the prune never deletes a file that an
// external viewer (Preview, Excel, a browser) still has open — deleting it out
// from under the app loses the document and any unsaved edits made there.
const openedArtifactPaths = new Set<string>()

// Best-effort prune so opened artifacts do not accumulate unboundedly in temp.
// Skips files opened this session and only removes prior-session leftovers older
// than a day, so a document a user is actively viewing is never yanked away.
async function pruneArtifactCache(dir: string): Promise<void> {
  try {
    const now = Date.now()
    const entries = await readdir(dir)
    await Promise.all(entries.map(async (entry) => {
      const full = join(dir, entry)
      if (openedArtifactPaths.has(full)) return
      try {
        const info = await stat(full)
        if (now - info.mtimeMs > 24 * 60 * 60 * 1000) await unlink(full)
      } catch {}
    }))
  } catch {}
}

async function openArtifactWithDefaultApp(payload: ArtifactOpenRequest): Promise<{ ok: boolean; message?: string }> {
  const raw = payload?.data
  if (!raw) return { ok: false, message: 'No artifact data to open.' }
  try {
    // Reuse the received bytes directly; fs.writeFile accepts a Uint8Array, so
    // Buffer.from() here would just memcpy a second full copy of the payload
    // (hundreds of MB for media artifacts) into the main process.
    const bytes = raw instanceof Uint8Array ? raw : new Uint8Array(raw)
    const dir = join(app.getPath('temp'), 'opensquilla-artifacts')
    mkdirSync(dir, { recursive: true, mode: 0o700 })
    void pruneArtifactCache(dir)
    const name = safeArtifactFileName(payload?.name)
    // A random prefix guarantees a unique, non-colliding, non-dotfile path even
    // for two opens in the same millisecond.
    const filePath = join(dir, `${randomUUID()}-${name}${artifactExtension(name, payload?.mime)}`)
    await writeFile(filePath, bytes, { mode: 0o600 })
    openedArtifactPaths.add(filePath)
    const error = await shell.openPath(filePath)
    if (error) return { ok: false, message: error }
    return { ok: true }
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
}

// --- Desktop native-shell i18n ---
// The embedded Web UI carries its own vue-i18n layer; this small catalog covers
// the main-process surfaces that live OUTSIDE the BrowserWindow (app-authored
// menu group labels and the onboarding window title), keyed off the OS locale.
// Role-based menu items (Cut/Copy/Paste/…) are localized by Electron itself.
const DESKTOP_LOCALE_LABELS: Record<DesktopLocale, string> = {
  en: 'English',
  'zh-Hans': '简体中文',
  ja: '日本語',
  fr: 'Français',
  de: 'Deutsch',
  es: 'Español',
}
let desktopLocale: DesktopLocale = 'en'

const PROVIDER_NOTE_MESSAGES: Record<DesktopLocale, Record<string, string>> = {
  en: {
    tokenrhythm: 'DeepSeek, GLM, MiniMax and Kimi model families on one key.',
    openrouter: 'One account for mixed-model routing.',
    openai: 'OpenAI-only tier profile.',
    openai_responses: 'OpenAI Responses-API shape (chat + responses).',
    anthropic: 'Direct Claude access without SquillaRouter tiers.',
    dashscope: 'Qwen tier profile for Mainland-friendly access.',
    deepseek: 'DeepSeek-only fast and pro routing.',
    gemini: 'Gemini OpenAI-compatible tier profile.',
    moonshot: 'Kimi text and image-capable routes.',
    ollama: 'Local direct model path.',
    qianfan: 'Direct provider model id required.',
    volcengine: 'Doubao tier profile.',
    zhipu: 'GLM tier profile.',
  },
  'zh-Hans': {
    tokenrhythm: '一把密钥即可使用 DeepSeek、GLM、MiniMax、Kimi 全系模型。',
    openrouter: '通过一个账户进行混合模型路由。',
    openai: '仅使用 OpenAI 的层级配置。',
    openai_responses: 'OpenAI Responses API 格式（chat + responses）。',
    anthropic: '直接访问 Claude，不使用 SquillaRouter 层级。',
    dashscope: '面向大陆访问的 Qwen 层级配置。',
    deepseek: '仅使用 DeepSeek 的 fast/pro 路由。',
    gemini: 'Gemini 的 OpenAI 兼容层级配置。',
    moonshot: 'Kimi 文本和图像能力路由。',
    ollama: '本地直连模型路径。',
    qianfan: '需要填写直连 provider 模型 ID。',
    volcengine: '豆包层级配置。',
    zhipu: 'GLM 层级配置。',
  },
  ja: {
    tokenrhythm: 'DeepSeek・GLM・MiniMax・Kimi の各モデルを 1 つのキーで利用できます。',
    openrouter: '1 つのアカウントで複数モデルをルーティングできます。',
    openai: 'OpenAI のみを使うティアプロファイルです。',
    openai_responses: 'OpenAI Responses API 形式（chat + responses）です。',
    anthropic: 'SquillaRouter ティアを使わず Claude に直接アクセスします。',
    dashscope: '中国本土から使いやすい Qwen ティアプロファイルです。',
    deepseek: 'DeepSeek の fast/pro のみでルーティングします。',
    gemini: 'Gemini の OpenAI 互換ティアプロファイルです。',
    moonshot: 'Kimi のテキストと画像対応ルートです。',
    ollama: 'ローカル直接モデルのパスです。',
    qianfan: '直接 provider のモデル ID が必要です。',
    volcengine: 'Doubao ティアプロファイルです。',
    zhipu: 'GLM ティアプロファイルです。',
  },
  fr: {
    tokenrhythm: 'Les familles DeepSeek, GLM, MiniMax et Kimi avec une seule clé.',
    openrouter: 'Routage de plusieurs modèles avec un seul compte.',
    openai: 'Profil de niveaux limité à OpenAI.',
    openai_responses: 'Format OpenAI Responses API (chat + responses).',
    anthropic: 'Accès direct à Claude sans niveaux SquillaRouter.',
    dashscope: 'Profil de niveaux Qwen adapté à un accès depuis la Chine continentale.',
    deepseek: 'Routage fast/pro limité à DeepSeek.',
    gemini: 'Profil de niveaux Gemini compatible OpenAI.',
    moonshot: 'Routes Kimi pour le texte et les capacités image.',
    ollama: 'Chemin de modèle direct local.',
    qianfan: 'ID de modèle provider direct requis.',
    volcengine: 'Profil de niveaux Doubao.',
    zhipu: 'Profil de niveaux GLM.',
  },
  de: {
    tokenrhythm: 'DeepSeek-, GLM-, MiniMax- und Kimi-Modelle mit einem einzigen Schlüssel.',
    openrouter: 'Routing mehrerer Modelle über ein Konto.',
    openai: 'Nur-OpenAI-Stufenprofil.',
    openai_responses: 'OpenAI Responses-API-Format (chat + responses).',
    anthropic: 'Direkter Claude-Zugriff ohne SquillaRouter-Stufen.',
    dashscope: 'Qwen-Stufenprofil für gut erreichbaren Zugriff vom chinesischen Festland.',
    deepseek: 'Nur DeepSeek fast/pro Routing.',
    gemini: 'OpenAI-kompatibles Gemini-Stufenprofil.',
    moonshot: 'Kimi-Routen für Text- und Bildfähigkeiten.',
    ollama: 'Lokaler direkter Modellpfad.',
    qianfan: 'Direkte provider-Modell-ID erforderlich.',
    volcengine: 'Doubao-Stufenprofil.',
    zhipu: 'GLM-Stufenprofil.',
  },
  es: {
    tokenrhythm: 'Las familias DeepSeek, GLM, MiniMax y Kimi con una sola clave.',
    openrouter: 'Enrutamiento de varios modelos con una sola cuenta.',
    openai: 'Perfil de niveles solo con OpenAI.',
    openai_responses: 'Formato OpenAI Responses API (chat + responses).',
    anthropic: 'Acceso directo a Claude sin niveles SquillaRouter.',
    dashscope: 'Perfil de niveles Qwen para acceso cómodo desde China continental.',
    deepseek: 'Enrutamiento fast/pro solo con DeepSeek.',
    gemini: 'Perfil de niveles Gemini compatible con OpenAI.',
    moonshot: 'Rutas Kimi para texto y capacidades de imagen.',
    ollama: 'Ruta de modelo directo local.',
    qianfan: 'Se requiere el ID del modelo provider directo.',
    volcengine: 'Perfil de niveles Doubao.',
    zhipu: 'Perfil de niveles GLM.',
  },
}

// Localized descriptive notes for the search providers, mirroring
// PROVIDER_NOTE_MESSAGES. Without these the onboarding search cards always
// rendered the hardcoded English catalog notes, so their localized fallbacks
// were unreachable in every non-English locale.
const SEARCH_PROVIDER_NOTE_MESSAGES: Record<DesktopLocale, Record<string, string>> = {
  en: {
    duckduckgo: 'No key required. Good default for getting started.',
    bocha: 'Web search with inline summaries and freshness support.',
    brave: 'Managed search access with freshness support.',
    tavily: 'Freshness-oriented web search for current research.',
    exa: 'Semantic and content-oriented search for research workflows.',
    iqs: 'Alibaba Cloud web search tuned for agents, with strong Chinese-web coverage.',
  },
  'zh-Hans': {
    duckduckgo: '无需密钥。适合入门的默认选项。',
    bocha: '带内联摘要和时效性支持的网络搜索。',
    brave: '带时效性支持的托管搜索访问。',
    tavily: '面向时效性的网络搜索，适合最新研究。',
    exa: '面向语义和内容的搜索，适合研究工作流。',
    iqs: '阿里云面向智能体的联网搜索，中文网页覆盖广。',
  },
  ja: {
    duckduckgo: 'キー不要。始めるのに適したデフォルトです。',
    bocha: 'インライン要約と鮮度対応を備えたウェブ検索です。',
    brave: '鮮度対応を備えたマネージド検索アクセスです。',
    tavily: '最新の調査向けの、鮮度重視のウェブ検索です。',
    exa: '調査ワークフロー向けのセマンティック／コンテンツ指向検索です。',
    iqs: 'エージェント向けに調整された Alibaba Cloud のウェブ検索。中国語ウェブのカバレッジに優れています。',
  },
  fr: {
    duckduckgo: 'Aucune clé requise. Bon choix par défaut pour démarrer.',
    bocha: 'Recherche web avec résumés en ligne et prise en charge de la fraîcheur.',
    brave: 'Accès de recherche géré avec prise en charge de la fraîcheur.',
    tavily: 'Recherche web axée sur la fraîcheur pour la recherche actuelle.',
    exa: 'Recherche sémantique et orientée contenu pour les flux de recherche.',
    iqs: 'Recherche web Alibaba Cloud conçue pour les agents, avec une forte couverture du web chinois.',
  },
  de: {
    duckduckgo: 'Kein Schlüssel erforderlich. Gute Voreinstellung für den Einstieg.',
    bocha: 'Websuche mit Inline-Zusammenfassungen und Aktualitätsunterstützung.',
    brave: 'Verwalteter Suchzugriff mit Aktualitätsunterstützung.',
    tavily: 'Aktualitätsorientierte Websuche für aktuelle Recherche.',
    exa: 'Semantische und inhaltsorientierte Suche für Recherche-Workflows.',
    iqs: 'Alibaba-Cloud-Websuche für Agenten, mit starker Abdeckung des chinesischen Webs.',
  },
  es: {
    duckduckgo: 'No se requiere clave. Buena opción predeterminada para empezar.',
    bocha: 'Búsqueda web con resúmenes en línea y soporte de actualidad.',
    brave: 'Acceso de búsqueda gestionado con soporte de actualidad.',
    tavily: 'Búsqueda web orientada a la actualidad para investigación actual.',
    exa: 'Búsqueda semántica y orientada a contenido para flujos de investigación.',
    iqs: 'Búsqueda web de Alibaba Cloud orientada a agentes, con amplia cobertura de la web china.',
  },
}

function resolveDesktopLocale(): DesktopLocale {
  const preferred = typeof app.getPreferredSystemLanguages === 'function'
    ? app.getPreferredSystemLanguages()
    : []
  return resolveLocaleFromTags([...preferred, app.getLocale()])
}

function desktopLocalePath(): string {
  return join(app.getPath('userData'), 'desktop-locale')
}

// Persist the locale the user picked during onboarding so every main-process
// surface (menu, dialogs, boot splash, next onboarding) honors it across
// launches instead of reverting to the OS locale.
function loadPersistedDesktopLocale(): DesktopLocale | null {
  try {
    const raw = readFileSync(desktopLocalePath(), 'utf8').trim()
    return desktopLocaleChoice(raw)
  } catch {
    return null
  }
}

function desktopLocaleChoice(raw: unknown): DesktopLocale | null {
  const requested = String(raw ?? '')
  return DESKTOP_LOCALES.includes(requested as DesktopLocale)
    ? requested as DesktopLocale
    : null
}

function persistDesktopLocale(locale: DesktopLocale): void {
  try {
    mkdirSync(app.getPath('userData'), { recursive: true })
    writeFileSync(desktopLocalePath(), locale, 'utf8')
  } catch {
    // Best-effort; a failed persist just means the next launch re-resolves the OS locale.
  }
}

function applyDesktopLocaleChoice(raw: unknown): void {
  const requested = desktopLocaleChoice(raw)
  if (requested === null) return
  if (requested !== desktopLocale) {
    desktopLocale = requested
    createApplicationMenu()
    rebuildWindowsTrayMenu()
  }
  persistDesktopLocale(desktopLocale)
}

const DESKTOP_MESSAGES: Record<DesktopLocale, Record<string, string>> = {
  en: {
    'menu.edit': 'Edit',
    'menu.view': 'View',
    'menu.window': 'Window',
    'menu.checkForUpdates': 'Check for Updates…',
    'menu.relaunchToUpdate': 'Relaunch to Update',
    'menu.downloadDiagnostics': 'Download Diagnostics…',
    'tray.open': 'Open OpenSquilla',
    'tray.running': 'OpenSquilla is running in the background',
    'tray.quit': 'Quit OpenSquilla',
    'tray.backgroundTitle': 'OpenSquilla is still running',
    'tray.backgroundDetail': 'Tasks, schedules, and connected channels continue in the background. Open or quit OpenSquilla from the system tray.',
    'closePrompt.title': 'Close OpenSquilla?',
    'closePrompt.message': 'What should happen when the main window closes?',
    'closePrompt.detail': 'Background mode keeps tasks, schedules, and connected channels running. Explicit Quit safely stops the local runtime.',
    'closePrompt.background': 'Keep running in background',
    'closePrompt.quit': 'Quit OpenSquilla',
    'closePrompt.cancel': 'Cancel',
    'closePrompt.remember': 'Remember my choice',
    'sandboxUnavailable.title': 'Safe mode is unavailable',
    'sandboxUnavailable.message': 'OpenSquilla cannot start its sandbox on this device.',
    'sandboxUnavailable.detail': 'Safe mode has been disabled. Tasks can use Full Access, which runs with host permissions and has additional security risk.',
    'sandboxUnavailable.acknowledge': 'I understand',
    'sandboxUnavailable.suppress': "Don't remind me again",
    'update.newVersionTitle': 'A new version is available',
    'update.newVersionDetail': 'OpenSquilla {version} is available. Download it now?',
    'update.download': 'Download',
    'update.later': 'Later',
    'update.readyTitle': 'Update ready to install',
    'update.readyDetail': 'OpenSquilla {version} has been downloaded. Restart to finish updating?',
    'update.restartNow': 'Restart now',
    'update.upToDateTitle': "You're up to date",
    'update.upToDateDetail': 'OpenSquilla {version} is the latest version.',
    'update.errorTitle': 'Update check failed',
    'update.manifestInvalid': 'The update information is invalid. Please try again later.',
    'update.sourceUnavailable': 'The update service is temporarily unavailable. Please try again later.',
    'update.checksumUnavailable': 'The installer cannot be verified because the official checksum is unavailable. No installer was opened.',
    'update.integrityFailed': 'The downloaded installer failed integrity verification and was deleted.',
    'update.downloadFailed': 'The update could not be downloaded. Please try again.',
    'update.installFailed': 'The update installer could not be opened. Please try again.',
    'update.moveToApplications': 'Move OpenSquilla to your Applications folder to enable automatic updates, then try again.',
    'update.gatewayShutdownTimeout': 'OpenSquilla could not stop the local runtime. Try relaunching to update again.',
    'update.mockInstallTitle': 'Mock update restart',
    'update.mockInstallDetail': 'Mock mode: OpenSquilla would restart now to install {version}. No files were changed.',
    'uninstall.confirmTitle': 'Delete local OpenSquilla desktop data?',
    'uninstall.confirmMessage': 'This permanently deletes the local desktop profile on this machine.',
    'uninstall.confirmDetail': 'Sessions, configuration, and secrets will be removed. The installed app itself will remain; remove it through your OS after the app closes.',
    'uninstall.cancel': 'Cancel',
    'uninstall.deleteEverything': 'Delete everything',
    'cleanup.moreItems': 'more locations',
    'cleanup.cancel': 'Cancel',
    'cleanup.deleteProfileConfirm': 'Delete profile',
    'cleanup.deleteProfileTitle': 'Delete the current profile?',
    'cleanup.deleteProfileMessage': 'This permanently deletes the listed primary profile data, credential, and logs. Backups are kept.',
    'cleanup.deleteAllConfirm': 'Delete all data',
    'cleanup.deleteAllTitle': 'Delete all OpenSquilla user data?',
    'cleanup.deleteAllMessage': 'OpenSquilla will close first. The deletion starts only after the app and local runtime have fully exited.',
    'migration.overwriteTitle': 'Replace conflicting desktop data?',
    'migration.overwriteMessage': 'The selected installation will replace the current Desktop data.',
    'migration.overwriteDetail': 'A complete timestamped backup will be retained. Confirm the source below before continuing.',
    'migration.overwriteNoMerge': 'Profile files and chat databases are never merged.',
    'migration.overwriteSourceUntouched': 'The selected source profile remains unchanged.',
    'migration.overwriteNoSync': 'The Desktop profile and source will not sync after transfer.',
    'migration.overwriteCancel': 'Cancel',
    'migration.overwriteConfirm': 'Back up and replace',
    'launch.alreadyRunningTitle': 'OpenSquilla is already running',
    'launch.alreadyRunningMessage': 'Another OpenSquilla window is already open on this machine. Bringing it to the front.',
    'window.onboarding': 'Set up OpenSquilla',
    'boot.profile': 'Preparing desktop profile',
    'boot.gateway-start': 'Starting local runtime',
    'boot.gateway-health': 'Checking gateway health',
    'boot.control': 'Loading Control UI',
    'boot.ready': 'Ready',
    'onboarding.title': 'Set up OpenSquilla',
    'onboarding.rail.title': 'Desktop setup',
    'onboarding.rail.subtitle': 'Set up OpenSquilla on this device.',
    'onboarding.rail.foot': 'OpenSquilla keeps this profile local to this device.',
    'onboarding.language.label': 'Language',
    'onboarding.aria.setupSteps': 'Setup steps',
    'onboarding.aria.setupDepth': 'Setup depth',
    'onboarding.aria.modelRoutingMode': 'Routing mode',
    'onboarding.aria.searchProvider': 'Search provider',
    'onboarding.aria.language': 'Onboarding language',
    'onboarding.nav.mode.title': 'Mode',
    'onboarding.nav.mode.sub': 'Setup depth',
    'onboarding.nav.provider.title': 'Provider',
    'onboarding.nav.provider.sub': 'Model access',
    'onboarding.nav.routing.title': 'Routing',
    'onboarding.nav.routing.sub': 'Mode',
    'onboarding.nav.tiers.title': 'Tiers',
    'onboarding.nav.tiers.sub': 'Default models',
    'onboarding.nav.search.title': 'Search',
    'onboarding.nav.search.sub': 'Optional web access',
    'onboarding.step1.badge': 'Start',
    'onboarding.step1.heading': 'Choose setup depth',
    'onboarding.step1.subtitle': 'Start with the shortest working path, or open the full router and tier controls now.',
    'onboarding.step1.simpleTitle': 'Simple setup',
    'onboarding.step1.simpleDesc': 'Pick one provider, add its key, choose search, and start OpenSquilla with defaults.',
    'onboarding.step1.advancedTitle': 'Advanced setup',
    'onboarding.step1.advancedDesc': 'Review tier defaults and direct model details before startup.',
    'onboarding.step1.note': 'You can change provider, router, and search settings later from the desktop Settings page.',
    'onboarding.step1.quit': 'Quit',
    'onboarding.step1.continue': 'Continue',
    'onboarding.step2.badge': 'Required',
    'onboarding.step2.heading': 'Model service setup',
    'onboarding.step2.subtitle': 'Enter an API key to get started.',
    'onboarding.step2.tokenrhythmTitle': 'TokenRhythm limited-time offer',
    'onboarding.step2.tokenrhythmValue': 'TokenRhythm API calls are free for a limited time.',
    'onboarding.step2.tokenrhythmRegistration': 'Register to claim ¥68 in free tokens.',
    'onboarding.step2.tokenrhythmCta': 'Claim for free',
    'onboarding.step2.tokenrhythmCtaExternalLabel': 'Claim for free (opens in external browser)',
    'onboarding.step2.otherProviders': 'Other providers',
    'onboarding.step2.apiKey': 'API key',
    'onboarding.step2.endpointSummary': 'Endpoint and direct model',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': 'Model name',
    'onboarding.step2.back': 'Back',
    'onboarding.step2.next': 'Next',
    'onboarding.step3.badge': 'Advanced',
    'onboarding.step3.heading': 'Choose routing mode',
    'onboarding.step3.subtitle': 'Decide whether OpenSquilla should use Smart Router tiers, call one fixed model, or use the current provider\'s ensemble.',
    'onboarding.step3.back': 'Back',
    'onboarding.step3.next': 'Next',
    'onboarding.step3.directModel': 'Direct model',
    'onboarding.step4.badge': 'Models',
    'onboarding.step4.heading': 'Review tier models',
    'onboarding.step4.subtitle': 'Pick the default text tier and keep the CLI defaults, or customize the model ids before startup.',
    'onboarding.step4.back': 'Back',
    'onboarding.step4.next': 'Next',
    'onboarding.step5.badge': 'Optional',
    'onboarding.step5.heading': 'Choose web search',
    'onboarding.step5.subtitle': 'Search is optional. Start without another key, or connect a runtime-supported search provider.',
    'onboarding.step5.searchKey': 'Search API key',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo is enough to start.',
    'onboarding.step5.back': 'Back',
    'onboarding.step5.finish': 'Start OpenSquilla',
  },
  'zh-Hans': {
    'menu.edit': '编辑',
    'menu.view': '视图',
    'menu.window': '窗口',
    'menu.checkForUpdates': '检查更新…',
    'menu.relaunchToUpdate': '重启以更新',
    'menu.downloadDiagnostics': '下载诊断信息…',
    'tray.open': '打开 OpenSquilla',
    'tray.running': 'OpenSquilla 正在后台运行',
    'tray.quit': '退出 OpenSquilla',
    'tray.backgroundTitle': 'OpenSquilla 仍在运行',
    'tray.backgroundDetail': '任务、定时任务和已连接渠道会继续在后台运行。可从系统托盘打开或退出 OpenSquilla。',
    'closePrompt.title': '关闭 OpenSquilla？',
    'closePrompt.message': '关闭主窗口时要执行什么操作？',
    'closePrompt.detail': '后台模式会继续运行任务、定时任务和已连接渠道；显式退出会安全停止本地运行时。',
    'closePrompt.background': '继续在后台运行',
    'closePrompt.quit': '退出 OpenSquilla',
    'closePrompt.cancel': '取消',
    'closePrompt.remember': '记住我的选择',
    'sandboxUnavailable.title': '安全模式当前不可用',
    'sandboxUnavailable.message': 'OpenSquilla 无法在此设备上启动沙箱。',
    'sandboxUnavailable.detail': '安全模式已禁用。任务只能使用完全访问，并将以宿主机权限运行，存在额外的安全风险。',
    'sandboxUnavailable.acknowledge': '我知道了',
    'sandboxUnavailable.suppress': '不再提醒',
    'update.newVersionTitle': '有新版本可用',
    'update.newVersionDetail': 'OpenSquilla {version} 已发布，现在下载吗？',
    'update.download': '下载',
    'update.later': '稍后',
    'update.readyTitle': '更新已就绪',
    'update.readyDetail': 'OpenSquilla {version} 已下载完成。是否重启以完成更新？',
    'update.restartNow': '立即重启',
    'update.upToDateTitle': '已是最新版本',
    'update.upToDateDetail': 'OpenSquilla {version} 已是最新版本。',
    'update.errorTitle': '检查更新失败',
    'update.manifestInvalid': '更新信息无效，请稍后重试。',
    'update.sourceUnavailable': '更新服务暂时不可用，请稍后重试。',
    'update.checksumUnavailable': '无法获取官方校验和，因此不能验证安装包；未打开任何安装包。',
    'update.integrityFailed': '下载的安装包未通过完整性校验，已将其删除。',
    'update.downloadFailed': '更新下载安装失败，请重试。',
    'update.installFailed': '无法打开更新安装包，请重试。',
    'update.moveToApplications': '请先将 OpenSquilla 移动到"应用程序"文件夹以启用自动更新，然后重试。',
    'update.gatewayShutdownTimeout': 'OpenSquilla 无法停止本地运行时。请再次尝试重启以更新。',
    'update.mockInstallTitle': '模拟重启更新',
    'update.mockInstallDetail': '模拟模式：OpenSquilla 现在会重启并安装 {version}。没有修改任何文件。',
    'uninstall.confirmTitle': '删除本地 OpenSquilla 桌面数据？',
    'uninstall.confirmMessage': '这将永久删除本机上的本地桌面配置。',
    'uninstall.confirmDetail': '会话、配置和密钥都将被移除。已安装的应用本身会保留；应用关闭后请通过操作系统将其卸载。',
    'uninstall.cancel': '取消',
    'uninstall.deleteEverything': '全部删除',
    'cleanup.moreItems': '个其他位置',
    'cleanup.cancel': '取消',
    'cleanup.deleteProfileConfirm': '删除配置文件',
    'cleanup.deleteProfileTitle': '删除当前配置文件？',
    'cleanup.deleteProfileMessage': '这会永久删除列出的主配置数据、凭据和日志。备份会保留。',
    'cleanup.deleteAllConfirm': '删除全部数据',
    'cleanup.deleteAllTitle': '删除全部 OpenSquilla 用户数据？',
    'cleanup.deleteAllMessage': 'OpenSquilla 会先退出。只有应用和本地运行时完全退出后，删除才会开始。',
    'migration.overwriteTitle': '替换冲突的桌面数据？',
    'migration.overwriteMessage': '所选安装的数据将替换当前桌面数据。',
    'migration.overwriteDetail': '系统会保留完整的时间戳备份。继续前请确认下方的数据来源。',
    'migration.overwriteNoMerge': '配置文件和聊天数据库绝不会合并。',
    'migration.overwriteSourceUntouched': '所选来源配置保持原样。',
    'migration.overwriteNoSync': '转移后，桌面端数据与来源不会自动同步。',
    'migration.overwriteCancel': '取消',
    'migration.overwriteConfirm': '备份并替换',
    'launch.alreadyRunningTitle': 'OpenSquilla 已在运行',
    'launch.alreadyRunningMessage': '本机已打开另一个 OpenSquilla 窗口。正在将其置于前台。',
    'window.onboarding': '设置 OpenSquilla',
    'boot.profile': '正在准备桌面配置',
    'boot.gateway-start': '正在启动本地运行时',
    'boot.gateway-health': '正在检查网关健康状态',
    'boot.control': '正在加载控制界面',
    'boot.ready': '就绪',
    'onboarding.title': '设置 OpenSquilla',
    'onboarding.rail.title': '桌面设置',
    'onboarding.rail.subtitle': '在本机完成 OpenSquilla 的基本设置。',
    'onboarding.rail.foot': '桌面端设置会保存在本机。',
    'onboarding.language.label': '语言',
    'onboarding.aria.setupSteps': '设置步骤',
    'onboarding.aria.setupDepth': '设置深度',
    'onboarding.aria.modelRoutingMode': '路由模式',
    'onboarding.aria.searchProvider': '搜索提供商',
    'onboarding.aria.language': 'onboarding 语言',
    'onboarding.nav.mode.title': '模式',
    'onboarding.nav.mode.sub': '设置深度',
    'onboarding.nav.provider.title': '提供商',
    'onboarding.nav.provider.sub': '模型访问',
    'onboarding.nav.routing.title': '路由',
    'onboarding.nav.routing.sub': '模式',
    'onboarding.nav.tiers.title': '层级',
    'onboarding.nav.tiers.sub': '默认模型',
    'onboarding.nav.search.title': '搜索',
    'onboarding.nav.search.sub': '可选的网络访问',
    'onboarding.step1.badge': '开始',
    'onboarding.step1.heading': '选择设置深度',
    'onboarding.step1.subtitle': '从最短的可用路径开始，或者现在就打开完整的路由器和层级控件。',
    'onboarding.step1.simpleTitle': '简单设置',
    'onboarding.step1.simpleDesc': '选择一个提供商，添加其密钥，选择搜索，然后使用默认设置启动 OpenSquilla。',
    'onboarding.step1.advancedTitle': '高级设置',
    'onboarding.step1.advancedDesc': '在启动前查看层级默认值和直连模型详情。',
    'onboarding.step1.note': '稍后可在桌面设置页面更改提供商、路由器和搜索设置。',
    'onboarding.step1.quit': '退出',
    'onboarding.step1.continue': '继续',
    'onboarding.step2.badge': '必填',
    'onboarding.step2.heading': '模型服务配置',
    'onboarding.step2.subtitle': '输入 API 密钥即可开始使用',
    'onboarding.step2.tokenrhythmTitle': 'TokenRhythm 限时福利',
    'onboarding.step2.tokenrhythmValue': 'TokenRhythm API 调用限时免费。',
    'onboarding.step2.tokenrhythmRegistration': '注册即领价值 68 元 Token',
    'onboarding.step2.tokenrhythmCta': '免费领取',
    'onboarding.step2.tokenrhythmCtaExternalLabel': '免费领取价值 68 元 TokenRhythm Token（在外部浏览器中打开）',
    'onboarding.step2.otherProviders': '其他提供商',
    'onboarding.step2.apiKey': 'API 密钥',
    'onboarding.step2.endpointSummary': '端点和直连模型',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': '模型名称',
    'onboarding.step2.back': '返回',
    'onboarding.step2.next': '下一步',
    'onboarding.step3.badge': '高级',
    'onboarding.step3.heading': '选择路由模式',
    'onboarding.step3.subtitle': '选择 OpenSquilla 使用 Smart Router 层级、直连一个固定模型，还是使用当前提供商的 Ensemble。',
    'onboarding.step3.back': '返回',
    'onboarding.step3.next': '下一步',
    'onboarding.step3.directModel': '直连模型',
    'onboarding.step4.badge': '模型',
    'onboarding.step4.heading': '候选模型池',
    'onboarding.step4.subtitle': '选择默认文本层级并保留 CLI 默认值，或在启动前自定义模型 id。',
    'onboarding.step4.back': '返回',
    'onboarding.step4.next': '下一步',
    'onboarding.step5.badge': '可选',
    'onboarding.step5.heading': '选择网络搜索',
    'onboarding.step5.subtitle': '搜索为可选项。可以不添加其他密钥直接开始，或连接运行时支持的搜索提供商。',
    'onboarding.step5.searchKey': '搜索 API 密钥',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo 足以开始使用。',
    'onboarding.step5.back': '返回',
    'onboarding.step5.finish': '启动 OpenSquilla',
  },
  ja: {
    'menu.edit': '編集',
    'menu.view': '表示',
    'menu.window': 'ウインドウ',
    'menu.checkForUpdates': 'アップデートを確認…',
    'menu.relaunchToUpdate': '再起動してアップデート',
    'menu.downloadDiagnostics': '診断情報をダウンロード…',
    'tray.open': 'OpenSquilla を開く',
    'tray.running': 'OpenSquilla はバックグラウンドで実行中です',
    'tray.quit': 'OpenSquilla を終了',
    'tray.backgroundTitle': 'OpenSquilla は引き続き実行中です',
    'tray.backgroundDetail': 'タスク、スケジュール、接続済みチャンネルはバックグラウンドで続行します。システムトレイから開くか終了できます。',
    'closePrompt.title': 'OpenSquilla を閉じますか？',
    'closePrompt.message': 'メインウィンドウを閉じるときの動作を選択してください。',
    'closePrompt.detail': 'バックグラウンドモードではタスク、スケジュール、接続済みチャンネルが継続します。明示的に終了するとローカルランタイムを安全に停止します。',
    'closePrompt.background': 'バックグラウンドで続行',
    'closePrompt.quit': 'OpenSquilla を終了',
    'closePrompt.cancel': 'キャンセル',
    'closePrompt.remember': 'この選択を記憶する',
    'update.newVersionTitle': '新しいバージョンが利用可能です',
    'update.newVersionDetail': 'OpenSquilla {version} が利用可能です。今すぐダウンロードしますか？',
    'update.download': 'ダウンロード',
    'update.later': '後で',
    'update.readyTitle': 'アップデートの準備が完了しました',
    'update.readyDetail': 'OpenSquilla {version} をダウンロードしました。再起動して更新を完了しますか？',
    'update.restartNow': '今すぐ再起動',
    'update.upToDateTitle': '最新の状態です',
    'update.upToDateDetail': 'OpenSquilla {version} が最新バージョンです。',
    'update.errorTitle': 'アップデートの確認に失敗しました',
    'update.manifestInvalid': 'アップデート情報が無効です。しばらくしてから再試行してください。',
    'update.sourceUnavailable': 'アップデートサービスを一時的に利用できません。後でもう一度お試しください。',
    'update.checksumUnavailable': '正規のチェックサムを取得できないため、インストーラを検証できません。インストーラは開かれていません。',
    'update.integrityFailed': 'ダウンロードしたインストーラは整合性検証に失敗したため削除されました。',
    'update.downloadFailed': 'アップデートをダウンロードできませんでした。もう一度お試しください。',
    'update.installFailed': 'アップデートインストーラを開けませんでした。もう一度お試しください。',
    'update.moveToApplications': '自動アップデートを有効にするには、OpenSquilla を「アプリケーション」フォルダに移動してから再試行してください。',
    'update.gatewayShutdownTimeout': 'ローカルランタイムを停止できませんでした。もう一度、再起動してアップデートをお試しください。',
    'uninstall.confirmTitle': 'ローカルの OpenSquilla デスクトップデータを削除しますか？',
    'uninstall.confirmMessage': 'このマシン上のローカルデスクトッププロファイルを完全に削除します。',
    'uninstall.confirmDetail': 'セッション、設定、シークレットが削除されます。インストール済みのアプリ自体は残ります。アプリを閉じた後、OS から削除してください。',
    'uninstall.cancel': 'キャンセル',
    'uninstall.deleteEverything': 'すべて削除',
    'cleanup.moreItems': '件のその他の場所',
    'cleanup.cancel': 'キャンセル',
    'cleanup.deleteProfileConfirm': 'プロファイルを削除',
    'cleanup.deleteProfileTitle': '現在のプロファイルを削除しますか？',
    'cleanup.deleteProfileMessage': '一覧のプライマリプロファイルデータ、認証情報、ログを完全に削除します。バックアップは保持されます。',
    'cleanup.deleteAllConfirm': 'すべてのデータを削除',
    'cleanup.deleteAllTitle': 'OpenSquilla のすべてのユーザーデータを削除しますか？',
    'cleanup.deleteAllMessage': 'OpenSquilla を先に終了します。アプリとローカルランタイムが完全に終了した後にのみ削除を開始します。',
    'migration.overwriteTitle': '競合するデスクトップデータを置き換えますか？',
    'migration.overwriteMessage': '選択したインストールのデータで現在の Desktop データを置き換えます。',
    'migration.overwriteDetail': 'タイムスタンプ付きの完全なバックアップが保持されます。続行する前に以下の移行元を確認してください。',
    'migration.overwriteNoMerge': 'プロファイルファイルとチャット DB は結合されません。',
    'migration.overwriteSourceUntouched': '選択した移行元プロファイルは変更されません。',
    'migration.overwriteNoSync': '転送後、Desktop と移行元は同期されません。',
    'migration.overwriteCancel': 'キャンセル',
    'migration.overwriteConfirm': 'バックアップして置換',
    'launch.alreadyRunningTitle': 'OpenSquilla はすでに実行中です',
    'launch.alreadyRunningMessage': 'このマシンでは別の OpenSquilla ウィンドウがすでに開いています。前面に表示します。',
    'window.onboarding': 'OpenSquilla をセットアップ',
    'boot.profile': 'デスクトッププロファイルを準備しています',
    'boot.gateway-start': 'ローカルランタイムを起動しています',
    'boot.gateway-health': 'ゲートウェイの稼働状況を確認しています',
    'boot.control': 'コントロール UI を読み込んでいます',
    'boot.ready': '準備完了',
    'onboarding.title': 'OpenSquilla をセットアップ',
    'onboarding.rail.title': 'デスクトップ設定',
    'onboarding.rail.subtitle': 'このデバイスで OpenSquilla を設定します。',
    'onboarding.rail.foot': 'OpenSquilla はこのプロファイルをこのデバイス内に保持します。',
    'onboarding.language.label': '言語',
    'onboarding.aria.setupSteps': 'セットアップ手順',
    'onboarding.aria.setupDepth': 'セットアップの詳細度',
    'onboarding.aria.searchProvider': '検索プロバイダー',
    'onboarding.aria.language': 'オンボーディングの言語',
    'onboarding.nav.mode.title': 'モード',
    'onboarding.nav.mode.sub': 'セットアップの詳細度',
    'onboarding.nav.provider.title': 'プロバイダー',
    'onboarding.nav.provider.sub': 'モデルアクセス',
    'onboarding.nav.tiers.title': 'ティア',
    'onboarding.nav.tiers.sub': 'デフォルトモデル',
    'onboarding.nav.search.title': '検索',
    'onboarding.nav.search.sub': '任意のウェブアクセス',
    'onboarding.step1.badge': '開始',
    'onboarding.step1.heading': 'セットアップの詳細度を選択',
    'onboarding.step1.subtitle': '最短で動作する経路から始めるか、ここでルーターとティアの設定をすべて開きます。',
    'onboarding.step1.simpleTitle': 'シンプルセットアップ',
    'onboarding.step1.simpleDesc': 'プロバイダーを 1 つ選び、キーを追加して検索を選択し、デフォルト設定で OpenSquilla を起動します。',
    'onboarding.step1.advancedTitle': '詳細セットアップ',
    'onboarding.step1.advancedDesc': '起動前にティアのデフォルトと直接モデルの詳細を確認します。',
    'onboarding.step1.note': 'プロバイダー、ルーター、検索の設定は後でデスクトップの設定ページから変更できます。',
    'onboarding.step1.quit': '終了',
    'onboarding.step1.continue': '続行',
    'onboarding.step2.badge': '必須',
    'onboarding.step2.heading': 'モデルサービス設定',
    'onboarding.step2.subtitle': 'API キーを入力して利用を開始します。',
    'onboarding.step2.tokenrhythmTitle': 'TokenRhythm 期間限定特典',
    'onboarding.step2.tokenrhythmValue': 'TokenRhythm API は期間限定で無料です。',
    'onboarding.step2.tokenrhythmRegistration': '登録で68元相当のTokenを無料進呈',
    'onboarding.step2.tokenrhythmCta': '無料で受け取る',
    'onboarding.step2.tokenrhythmCtaExternalLabel': '無料で受け取る（外部ブラウザーで開きます）',
    'onboarding.step2.otherProviders': 'その他のプロバイダー',
    'onboarding.step2.apiKey': 'API キー',
    'onboarding.step2.endpointSummary': 'エンドポイントと直接モデル',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': 'モデル名',
    'onboarding.step2.back': '戻る',
    'onboarding.step2.next': '次へ',
    'onboarding.nav.routing.title': 'ルーティング',
    'onboarding.nav.routing.sub': 'モード',
    'onboarding.aria.modelRoutingMode': 'ルーティングモード',
    'onboarding.step3.badge': '詳細',
    'onboarding.step3.heading': 'ルーティングモードを選択',
    'onboarding.step3.subtitle': 'OpenSquilla が Smart Router のティアを使うか、固定モデルを 1 つ呼び出すか、現在のプロバイダーのアンサンブルを使うかを選びます。',
    'onboarding.step3.back': '戻る',
    'onboarding.step3.next': '次へ',
    'onboarding.step3.directModel': '直接モデル',
    'onboarding.step4.badge': 'モデル',
    'onboarding.step4.heading': 'ティアのモデルを確認',
    'onboarding.step4.subtitle': 'デフォルトのテキストティアを選んで CLI のデフォルトを維持するか、起動前にモデル id をカスタマイズします。',
    'onboarding.step4.back': '戻る',
    'onboarding.step4.next': '次へ',
    'onboarding.step5.badge': '任意',
    'onboarding.step5.heading': 'ウェブ検索を選択',
    'onboarding.step5.subtitle': '検索は任意です。別のキーなしで開始するか、ランタイムが対応する検索プロバイダーを接続します。',
    'onboarding.step5.searchKey': '検索 API キー',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo で始めるには十分です。',
    'onboarding.step5.back': '戻る',
    'onboarding.step5.finish': 'OpenSquilla を起動',
  },
  fr: {
    'menu.edit': 'Édition',
    'menu.view': 'Affichage',
    'menu.window': 'Fenêtre',
    'menu.checkForUpdates': 'Rechercher les mises à jour…',
    'menu.relaunchToUpdate': 'Relancer pour mettre à jour',
    'menu.downloadDiagnostics': 'Télécharger le diagnostic…',
    'tray.open': 'Ouvrir OpenSquilla',
    'tray.running': 'OpenSquilla fonctionne en arrière-plan',
    'tray.quit': 'Quitter OpenSquilla',
    'tray.backgroundTitle': 'OpenSquilla fonctionne toujours',
    'tray.backgroundDetail': 'Les tâches, planifications et canaux connectés continuent en arrière-plan. Ouvrez ou quittez OpenSquilla depuis la zone de notification.',
    'closePrompt.title': 'Fermer OpenSquilla ?',
    'closePrompt.message': 'Que doit-il se passer à la fermeture de la fenêtre principale ?',
    'closePrompt.detail': 'Le mode arrière-plan maintient les tâches, planifications et canaux connectés. Quitter explicitement arrête le runtime local en toute sécurité.',
    'closePrompt.background': 'Continuer en arrière-plan',
    'closePrompt.quit': 'Quitter OpenSquilla',
    'closePrompt.cancel': 'Annuler',
    'closePrompt.remember': 'Mémoriser mon choix',
    'update.newVersionTitle': 'Une nouvelle version est disponible',
    'update.newVersionDetail': 'OpenSquilla {version} est disponible. Télécharger maintenant ?',
    'update.download': 'Télécharger',
    'update.later': 'Plus tard',
    'update.readyTitle': 'Mise à jour prête à installer',
    'update.readyDetail': 'OpenSquilla {version} a été téléchargée. Redémarrer pour terminer la mise à jour ?',
    'update.restartNow': 'Redémarrer maintenant',
    'update.upToDateTitle': 'Vous êtes à jour',
    'update.upToDateDetail': 'OpenSquilla {version} est la dernière version.',
    'update.errorTitle': 'Échec de la recherche de mises à jour',
    'update.manifestInvalid': 'Les informations de mise à jour sont invalides. Réessayez plus tard.',
    'update.sourceUnavailable': 'Le service de mise à jour est temporairement indisponible. Réessayez plus tard.',
    'update.checksumUnavailable': 'Le programme d’installation ne peut pas être vérifié car la somme de contrôle officielle est indisponible. Aucun programme n’a été ouvert.',
    'update.integrityFailed': 'Le programme d’installation téléchargé a échoué au contrôle d’intégrité et a été supprimé.',
    'update.downloadFailed': 'Impossible de télécharger la mise à jour. Réessayez.',
    'update.installFailed': 'Impossible d’ouvrir le programme d’installation. Réessayez.',
    'update.moveToApplications': 'Déplacez OpenSquilla dans votre dossier Applications pour activer les mises à jour automatiques, puis réessayez.',
    'update.gatewayShutdownTimeout': 'OpenSquilla n\'a pas pu arrêter le runtime local. Réessayez de relancer la mise à jour.',
    'uninstall.confirmTitle': 'Supprimer les données locales du bureau OpenSquilla ?',
    'uninstall.confirmMessage': 'Cela supprime définitivement le profil de bureau local sur cette machine.',
    'uninstall.confirmDetail': 'Les sessions, la configuration et les secrets seront supprimés. L’application installée elle-même sera conservée ; supprimez-la via votre système d’exploitation après la fermeture de l’application.',
    'uninstall.cancel': 'Annuler',
    'uninstall.deleteEverything': 'Tout supprimer',
    'cleanup.moreItems': 'autres emplacements',
    'cleanup.cancel': 'Annuler',
    'cleanup.deleteProfileConfirm': 'Supprimer le profil',
    'cleanup.deleteProfileTitle': 'Supprimer le profil actuel ?',
    'cleanup.deleteProfileMessage': 'Cette action supprime définitivement les données, l’identifiant et les journaux listés du profil principal. Les sauvegardes sont conservées.',
    'cleanup.deleteAllConfirm': 'Supprimer toutes les données',
    'cleanup.deleteAllTitle': 'Supprimer toutes les données utilisateur OpenSquilla ?',
    'cleanup.deleteAllMessage': 'OpenSquilla va d’abord se fermer. La suppression ne commence qu’après l’arrêt complet de l’application et de l’environnement local.',
    'migration.overwriteTitle': 'Remplacer les données de bureau en conflit ?',
    'migration.overwriteMessage': 'L’installation sélectionnée remplacera les données Desktop actuelles.',
    'migration.overwriteDetail': 'Une sauvegarde complète horodatée sera conservée. Vérifiez la source ci-dessous avant de continuer.',
    'migration.overwriteNoMerge': 'Les fichiers de profil et bases de conversations ne sont jamais fusionnés.',
    'migration.overwriteSourceUntouched': 'Le profil source sélectionné reste inchangé.',
    'migration.overwriteNoSync': 'Après le transfert, le profil Desktop et la source ne seront pas synchronisés.',
    'migration.overwriteCancel': 'Annuler',
    'migration.overwriteConfirm': 'Sauvegarder et remplacer',
    'launch.alreadyRunningTitle': 'OpenSquilla est déjà en cours d’exécution',
    'launch.alreadyRunningMessage': 'Une autre fenêtre OpenSquilla est déjà ouverte sur cette machine. Elle va être mise au premier plan.',
    'window.onboarding': 'Configurer OpenSquilla',
    'boot.profile': 'Préparation du profil de bureau',
    'boot.gateway-start': 'Démarrage du runtime local',
    'boot.gateway-health': 'Vérification de l\'état de la passerelle',
    'boot.control': 'Chargement de l\'interface de contrôle',
    'boot.ready': 'Prêt',
    'onboarding.title': 'Configurer OpenSquilla',
    'onboarding.rail.title': 'Configuration du bureau',
    'onboarding.rail.subtitle': 'Configurez OpenSquilla sur cet appareil.',
    'onboarding.rail.foot': 'OpenSquilla conserve ce profil en local sur cet appareil.',
    'onboarding.language.label': 'Langue',
    'onboarding.aria.setupSteps': 'Étapes de configuration',
    'onboarding.aria.setupDepth': 'Niveau de configuration',
    'onboarding.aria.searchProvider': 'Fournisseur de recherche',
    'onboarding.aria.language': 'Langue de l’onboarding',
    'onboarding.nav.mode.title': 'Mode',
    'onboarding.nav.mode.sub': 'Niveau de configuration',
    'onboarding.nav.provider.title': 'Fournisseur',
    'onboarding.nav.provider.sub': 'Accès aux modèles',
    'onboarding.nav.tiers.title': 'Niveaux',
    'onboarding.nav.tiers.sub': 'Modèles par défaut',
    'onboarding.nav.search.title': 'Recherche',
    'onboarding.nav.search.sub': 'Accès web facultatif',
    'onboarding.step1.badge': 'Démarrer',
    'onboarding.step1.heading': 'Choisir le niveau de configuration',
    'onboarding.step1.subtitle': 'Commencez par le chemin fonctionnel le plus court, ou ouvrez dès maintenant tous les réglages de routeur et de niveaux.',
    'onboarding.step1.simpleTitle': 'Configuration simple',
    'onboarding.step1.simpleDesc': 'Choisissez un fournisseur, ajoutez sa clé, sélectionnez la recherche et démarrez OpenSquilla avec les valeurs par défaut.',
    'onboarding.step1.advancedTitle': 'Configuration avancée',
    'onboarding.step1.advancedDesc': 'Examinez les niveaux par défaut et les détails du modèle direct avant le démarrage.',
    'onboarding.step1.note': 'Vous pourrez modifier les paramètres de fournisseur, de routeur et de recherche plus tard depuis la page Paramètres du bureau.',
    'onboarding.step1.quit': 'Quitter',
    'onboarding.step1.continue': 'Continuer',
    'onboarding.step2.badge': 'Requis',
    'onboarding.step2.heading': 'Configuration du service de modèles',
    'onboarding.step2.subtitle': 'Saisissez une clé API pour commencer.',
    'onboarding.step2.tokenrhythmTitle': 'Offre limitée TokenRhythm',
    'onboarding.step2.tokenrhythmValue': 'Les appels à l’API TokenRhythm sont gratuits pendant une durée limitée.',
    'onboarding.step2.tokenrhythmRegistration': 'Inscrivez-vous pour recevoir 68 ¥ de tokens gratuits.',
    'onboarding.step2.tokenrhythmCta': 'Obtenir gratuitement',
    'onboarding.step2.tokenrhythmCtaExternalLabel': 'Obtenir gratuitement (s’ouvre dans le navigateur externe)',
    'onboarding.step2.otherProviders': 'Autres fournisseurs',
    'onboarding.step2.apiKey': 'Clé API',
    'onboarding.step2.endpointSummary': 'Point de terminaison et modèle direct',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': 'Nom du modèle',
    'onboarding.step2.back': 'Retour',
    'onboarding.step2.next': 'Suivant',
    'onboarding.nav.routing.title': 'Routage',
    'onboarding.nav.routing.sub': 'Mode',
    'onboarding.aria.modelRoutingMode': 'Mode de routage',
    'onboarding.step3.badge': 'Avancé',
    'onboarding.step3.heading': 'Choisir le mode de routage',
    'onboarding.step3.subtitle': "Décidez si OpenSquilla doit utiliser les niveaux du Smart Router, appeler un seul modèle fixe, ou utiliser l'ensemble du fournisseur actuel.",
    'onboarding.step3.back': 'Retour',
    'onboarding.step3.next': 'Suivant',
    'onboarding.step3.directModel': 'Modèle direct',
    'onboarding.step4.badge': 'Modèles',
    'onboarding.step4.heading': 'Examiner les modèles par niveau',
    'onboarding.step4.subtitle': 'Choisissez le niveau de texte par défaut et conservez les valeurs par défaut de la CLI, ou personnalisez les id de modèle avant le démarrage.',
    'onboarding.step4.back': 'Retour',
    'onboarding.step4.next': 'Suivant',
    'onboarding.step5.badge': 'Facultatif',
    'onboarding.step5.heading': 'Choisir la recherche web',
    'onboarding.step5.subtitle': 'La recherche est facultative. Démarrez sans autre clé, ou connectez un fournisseur de recherche pris en charge par le runtime.',
    'onboarding.step5.searchKey': 'Clé API de recherche',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo suffit pour démarrer.',
    'onboarding.step5.back': 'Retour',
    'onboarding.step5.finish': 'Démarrer OpenSquilla',
  },
  de: {
    'menu.edit': 'Bearbeiten',
    'menu.view': 'Ansicht',
    'menu.window': 'Fenster',
    'menu.checkForUpdates': 'Nach Updates suchen…',
    'menu.relaunchToUpdate': 'Zum Aktualisieren neu starten',
    'menu.downloadDiagnostics': 'Diagnose herunterladen…',
    'tray.open': 'OpenSquilla öffnen',
    'tray.running': 'OpenSquilla wird im Hintergrund ausgeführt',
    'tray.quit': 'OpenSquilla beenden',
    'tray.backgroundTitle': 'OpenSquilla wird weiter ausgeführt',
    'tray.backgroundDetail': 'Aufgaben, Zeitpläne und verbundene Kanäle laufen im Hintergrund weiter. Öffnen oder beenden Sie OpenSquilla über den Infobereich.',
    'closePrompt.title': 'OpenSquilla schließen?',
    'closePrompt.message': 'Was soll beim Schließen des Hauptfensters geschehen?',
    'closePrompt.detail': 'Im Hintergrundmodus laufen Aufgaben, Zeitpläne und verbundene Kanäle weiter. Beim expliziten Beenden wird die lokale Laufzeit sicher gestoppt.',
    'closePrompt.background': 'Im Hintergrund weiter ausführen',
    'closePrompt.quit': 'OpenSquilla beenden',
    'closePrompt.cancel': 'Abbrechen',
    'closePrompt.remember': 'Auswahl merken',
    'update.newVersionTitle': 'Eine neue Version ist verfügbar',
    'update.newVersionDetail': 'OpenSquilla {version} ist verfügbar. Jetzt herunterladen?',
    'update.download': 'Herunterladen',
    'update.later': 'Später',
    'update.readyTitle': 'Update bereit zur Installation',
    'update.readyDetail': 'OpenSquilla {version} wurde heruntergeladen. Neu starten, um das Update abzuschließen?',
    'update.restartNow': 'Jetzt neu starten',
    'update.upToDateTitle': 'Sie sind auf dem neuesten Stand',
    'update.upToDateDetail': 'OpenSquilla {version} ist die neueste Version.',
    'update.errorTitle': 'Update-Prüfung fehlgeschlagen',
    'update.manifestInvalid': 'Die Update-Informationen sind ungültig. Versuchen Sie es später erneut.',
    'update.sourceUnavailable': 'Der Update-Dienst ist vorübergehend nicht verfügbar. Versuchen Sie es später erneut.',
    'update.checksumUnavailable': 'Das Installationsprogramm kann nicht geprüft werden, weil die offizielle Prüfsumme nicht verfügbar ist. Es wurde nichts geöffnet.',
    'update.integrityFailed': 'Das heruntergeladene Installationsprogramm hat die Integritätsprüfung nicht bestanden und wurde gelöscht.',
    'update.downloadFailed': 'Das Update konnte nicht heruntergeladen werden. Versuchen Sie es erneut.',
    'update.installFailed': 'Das Update-Installationsprogramm konnte nicht geöffnet werden. Versuchen Sie es erneut.',
    'update.moveToApplications': 'Verschieben Sie OpenSquilla in Ihren Programme-Ordner, um automatische Updates zu aktivieren, und versuchen Sie es erneut.',
    'update.gatewayShutdownTimeout': 'OpenSquilla konnte die lokale Laufzeitumgebung nicht stoppen. Versuchen Sie erneut, zum Aktualisieren neu zu starten.',
    'uninstall.confirmTitle': 'Lokale OpenSquilla-Desktop-Daten löschen?',
    'uninstall.confirmMessage': 'Dies löscht das lokale Desktop-Profil auf diesem Gerät dauerhaft.',
    'uninstall.confirmDetail': 'Sitzungen, Konfiguration und Secrets werden entfernt. Die installierte App selbst bleibt erhalten; entfernen Sie sie nach dem Schließen der App über Ihr Betriebssystem.',
    'uninstall.cancel': 'Abbrechen',
    'uninstall.deleteEverything': 'Alles löschen',
    'cleanup.moreItems': 'weitere Speicherorte',
    'cleanup.cancel': 'Abbrechen',
    'cleanup.deleteProfileConfirm': 'Profil löschen',
    'cleanup.deleteProfileTitle': 'Aktuelles Profil löschen?',
    'cleanup.deleteProfileMessage': 'Die aufgeführten Daten, Zugangsdaten und Protokolle des Hauptprofils werden dauerhaft gelöscht. Sicherungen bleiben erhalten.',
    'cleanup.deleteAllConfirm': 'Alle Daten löschen',
    'cleanup.deleteAllTitle': 'Alle OpenSquilla-Benutzerdaten löschen?',
    'cleanup.deleteAllMessage': 'OpenSquilla wird zuerst beendet. Die Löschung beginnt erst, wenn App und lokale Laufzeit vollständig beendet sind.',
    'migration.overwriteTitle': 'Konfliktierende Desktop-Daten ersetzen?',
    'migration.overwriteMessage': 'Die ausgewählte Installation ersetzt die aktuellen Desktop-Daten.',
    'migration.overwriteDetail': 'Eine vollständige Sicherung mit Zeitstempel bleibt erhalten. Prüfen Sie vor dem Fortfahren die Quelle unten.',
    'migration.overwriteNoMerge': 'Profildateien und Chat-Datenbanken werden nie zusammengeführt.',
    'migration.overwriteSourceUntouched': 'Das ausgewählte Quellprofil bleibt unverändert.',
    'migration.overwriteNoSync': 'Nach dem Transfer werden Desktop-Profil und Quelle nicht synchronisiert.',
    'migration.overwriteCancel': 'Abbrechen',
    'migration.overwriteConfirm': 'Sichern und ersetzen',
    'launch.alreadyRunningTitle': 'OpenSquilla läuft bereits',
    'launch.alreadyRunningMessage': 'Auf diesem Gerät ist bereits ein anderes OpenSquilla-Fenster geöffnet. Es wird in den Vordergrund geholt.',
    'window.onboarding': 'OpenSquilla einrichten',
    'boot.profile': 'Desktop-Profil wird vorbereitet',
    'boot.gateway-start': 'Lokale Laufzeitumgebung wird gestartet',
    'boot.gateway-health': 'Gateway-Zustand wird geprüft',
    'boot.control': 'Control-UI wird geladen',
    'boot.ready': 'Bereit',
    'onboarding.title': 'OpenSquilla einrichten',
    'onboarding.rail.title': 'Desktop-Einrichtung',
    'onboarding.rail.subtitle': 'Richten Sie OpenSquilla auf diesem Gerät ein.',
    'onboarding.rail.foot': 'OpenSquilla behält dieses Profil lokal auf diesem Gerät.',
    'onboarding.language.label': 'Sprache',
    'onboarding.aria.setupSteps': 'Einrichtungsschritte',
    'onboarding.aria.setupDepth': 'Einrichtungstiefe',
    'onboarding.aria.searchProvider': 'Suchanbieter',
    'onboarding.aria.language': 'Onboarding-Sprache',
    'onboarding.nav.mode.title': 'Modus',
    'onboarding.nav.mode.sub': 'Einrichtungstiefe',
    'onboarding.nav.provider.title': 'Anbieter',
    'onboarding.nav.provider.sub': 'Modellzugriff',
    'onboarding.nav.tiers.title': 'Stufen',
    'onboarding.nav.tiers.sub': 'Standardmodelle',
    'onboarding.nav.search.title': 'Suche',
    'onboarding.nav.search.sub': 'Optionaler Webzugriff',
    'onboarding.step1.badge': 'Start',
    'onboarding.step1.heading': 'Einrichtungstiefe wählen',
    'onboarding.step1.subtitle': 'Beginnen Sie mit dem kürzesten funktionierenden Weg, oder öffnen Sie jetzt die vollständigen Router- und Stufeneinstellungen.',
    'onboarding.step1.simpleTitle': 'Einfache Einrichtung',
    'onboarding.step1.simpleDesc': 'Wählen Sie einen Anbieter, fügen Sie seinen Schlüssel hinzu, wählen Sie die Suche und starten Sie OpenSquilla mit den Standardwerten.',
    'onboarding.step1.advancedTitle': 'Erweiterte Einrichtung',
    'onboarding.step1.advancedDesc': 'Prüfen Sie vor dem Start die Stufenstandards und die Details des direkten Modells.',
    'onboarding.step1.note': 'Sie können Anbieter-, Router- und Sucheinstellungen später auf der Desktop-Seite Einstellungen ändern.',
    'onboarding.step1.quit': 'Beenden',
    'onboarding.step1.continue': 'Weiter',
    'onboarding.step2.badge': 'Erforderlich',
    'onboarding.step2.heading': 'Modellservice konfigurieren',
    'onboarding.step2.subtitle': 'Geben Sie einen API-Schlüssel ein, um zu beginnen.',
    'onboarding.step2.tokenrhythmTitle': 'TokenRhythm-Aktion',
    'onboarding.step2.tokenrhythmValue': 'TokenRhythm-API-Aufrufe sind für kurze Zeit kostenlos.',
    'onboarding.step2.tokenrhythmRegistration': 'Registrieren und 68 ¥ Gratis-Token erhalten.',
    'onboarding.step2.tokenrhythmCta': 'Kostenlos erhalten',
    'onboarding.step2.tokenrhythmCtaExternalLabel': 'Kostenlos erhalten (wird im externen Browser geöffnet)',
    'onboarding.step2.otherProviders': 'Weitere Anbieter',
    'onboarding.step2.apiKey': 'API-Schlüssel',
    'onboarding.step2.endpointSummary': 'Endpunkt und direktes Modell',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': 'Modellname',
    'onboarding.step2.back': 'Zurück',
    'onboarding.step2.next': 'Weiter',
    'onboarding.nav.routing.title': 'Routing',
    'onboarding.nav.routing.sub': 'Modus',
    'onboarding.aria.modelRoutingMode': 'Routing-Modus',
    'onboarding.step3.badge': 'Erweitert',
    'onboarding.step3.heading': 'Routing-Modus wählen',
    'onboarding.step3.subtitle': 'Legen Sie fest, ob OpenSquilla die Smart-Router-Stufen verwenden, ein festes Modell aufrufen oder das Ensemble des aktuellen Anbieters nutzen soll.',
    'onboarding.step3.back': 'Zurück',
    'onboarding.step3.next': 'Weiter',
    'onboarding.step3.directModel': 'Direktes Modell',
    'onboarding.step4.badge': 'Modelle',
    'onboarding.step4.heading': 'Stufenmodelle prüfen',
    'onboarding.step4.subtitle': 'Wählen Sie die Standard-Textstufe und behalten Sie die CLI-Standards bei, oder passen Sie die Modell-ids vor dem Start an.',
    'onboarding.step4.back': 'Zurück',
    'onboarding.step4.next': 'Weiter',
    'onboarding.step5.badge': 'Optional',
    'onboarding.step5.heading': 'Websuche wählen',
    'onboarding.step5.subtitle': 'Die Suche ist optional. Starten Sie ohne weiteren Schlüssel, oder verbinden Sie einen von der Laufzeitumgebung unterstützten Suchanbieter.',
    'onboarding.step5.searchKey': 'Such-API-Schlüssel',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo reicht für den Start.',
    'onboarding.step5.back': 'Zurück',
    'onboarding.step5.finish': 'OpenSquilla starten',
  },
  es: {
    'menu.edit': 'Edición',
    'menu.view': 'Ver',
    'menu.window': 'Ventana',
    'menu.checkForUpdates': 'Buscar actualizaciones…',
    'menu.relaunchToUpdate': 'Reiniciar para actualizar',
    'menu.downloadDiagnostics': 'Descargar diagnóstico…',
    'tray.open': 'Abrir OpenSquilla',
    'tray.running': 'OpenSquilla se ejecuta en segundo plano',
    'tray.quit': 'Salir de OpenSquilla',
    'tray.backgroundTitle': 'OpenSquilla sigue en ejecución',
    'tray.backgroundDetail': 'Las tareas, programaciones y canales conectados continúan en segundo plano. Abre o cierra OpenSquilla desde la bandeja del sistema.',
    'closePrompt.title': '¿Cerrar OpenSquilla?',
    'closePrompt.message': '¿Qué debe ocurrir al cerrar la ventana principal?',
    'closePrompt.detail': 'El modo en segundo plano mantiene las tareas, programaciones y canales conectados. Salir explícitamente detiene el entorno local de forma segura.',
    'closePrompt.background': 'Seguir ejecutando en segundo plano',
    'closePrompt.quit': 'Salir de OpenSquilla',
    'closePrompt.cancel': 'Cancelar',
    'closePrompt.remember': 'Recordar mi elección',
    'update.newVersionTitle': 'Hay una nueva versión disponible',
    'update.newVersionDetail': 'OpenSquilla {version} está disponible. ¿Descargar ahora?',
    'update.download': 'Descargar',
    'update.later': 'Más tarde',
    'update.readyTitle': 'Actualización lista para instalar',
    'update.readyDetail': 'OpenSquilla {version} se ha descargado. ¿Reiniciar para finalizar la actualización?',
    'update.restartNow': 'Reiniciar ahora',
    'update.upToDateTitle': 'Estás al día',
    'update.upToDateDetail': 'OpenSquilla {version} es la última versión.',
    'update.errorTitle': 'Error al buscar actualizaciones',
    'update.manifestInvalid': 'La información de actualización no es válida. Inténtalo más tarde.',
    'update.sourceUnavailable': 'El servicio de actualizaciones no está disponible temporalmente. Inténtalo más tarde.',
    'update.checksumUnavailable': 'No se puede verificar el instalador porque la suma de comprobación oficial no está disponible. No se abrió ningún instalador.',
    'update.integrityFailed': 'El instalador descargado no superó la verificación de integridad y se eliminó.',
    'update.downloadFailed': 'No se pudo descargar la actualización. Inténtalo de nuevo.',
    'update.installFailed': 'No se pudo abrir el instalador de la actualización. Inténtalo de nuevo.',
    'update.moveToApplications': 'Mueve OpenSquilla a tu carpeta de Aplicaciones para habilitar las actualizaciones automáticas e inténtalo de nuevo.',
    'update.gatewayShutdownTimeout': 'OpenSquilla no pudo detener el runtime local. Intenta reiniciar para actualizar de nuevo.',
    'uninstall.confirmTitle': '¿Eliminar los datos locales de escritorio de OpenSquilla?',
    'uninstall.confirmMessage': 'Esto elimina permanentemente el perfil de escritorio local en esta máquina.',
    'uninstall.confirmDetail': 'Se eliminarán las sesiones, la configuración y los secretos. La aplicación instalada en sí permanecerá; elimínala a través de tu sistema operativo después de cerrar la aplicación.',
    'uninstall.cancel': 'Cancelar',
    'uninstall.deleteEverything': 'Eliminar todo',
    'cleanup.moreItems': 'ubicaciones adicionales',
    'cleanup.cancel': 'Cancelar',
    'cleanup.deleteProfileConfirm': 'Eliminar perfil',
    'cleanup.deleteProfileTitle': '¿Eliminar el perfil actual?',
    'cleanup.deleteProfileMessage': 'Esto elimina permanentemente los datos, la credencial y los registros enumerados del perfil principal. Se conservan las copias de seguridad.',
    'cleanup.deleteAllConfirm': 'Eliminar todos los datos',
    'cleanup.deleteAllTitle': '¿Eliminar todos los datos de usuario de OpenSquilla?',
    'cleanup.deleteAllMessage': 'OpenSquilla se cerrará primero. La eliminación solo comienza cuando la app y el entorno local hayan terminado por completo.',
    'migration.overwriteTitle': '¿Reemplazar los datos de escritorio en conflicto?',
    'migration.overwriteMessage': 'La instalación seleccionada reemplazará los datos actuales de Desktop.',
    'migration.overwriteDetail': 'Se conservará una copia de seguridad completa con marca de tiempo. Confirma la fuente indicada abajo antes de continuar.',
    'migration.overwriteNoMerge': 'Los archivos de perfil y las bases de chats nunca se combinan.',
    'migration.overwriteSourceUntouched': 'El perfil de origen seleccionado permanece sin cambios.',
    'migration.overwriteNoSync': 'Después de transferir, el perfil Desktop y el origen no se sincronizarán.',
    'migration.overwriteCancel': 'Cancelar',
    'migration.overwriteConfirm': 'Respaldar y reemplazar',
    'launch.alreadyRunningTitle': 'OpenSquilla ya se está ejecutando',
    'launch.alreadyRunningMessage': 'Ya hay otra ventana de OpenSquilla abierta en esta máquina. Se traerá al frente.',
    'window.onboarding': 'Configurar OpenSquilla',
    'boot.profile': 'Preparando el perfil de escritorio',
    'boot.gateway-start': 'Iniciando el runtime local',
    'boot.gateway-health': 'Comprobando el estado de la pasarela',
    'boot.control': 'Cargando la interfaz de control',
    'boot.ready': 'Listo',
    'onboarding.title': 'Configurar OpenSquilla',
    'onboarding.rail.title': 'Configuración de escritorio',
    'onboarding.rail.subtitle': 'Configura OpenSquilla en este dispositivo.',
    'onboarding.rail.foot': 'OpenSquilla mantiene este perfil local en este dispositivo.',
    'onboarding.language.label': 'Idioma',
    'onboarding.aria.setupSteps': 'Pasos de configuración',
    'onboarding.aria.setupDepth': 'Nivel de configuración',
    'onboarding.aria.searchProvider': 'Proveedor de búsqueda',
    'onboarding.aria.language': 'Idioma de onboarding',
    'onboarding.nav.mode.title': 'Modo',
    'onboarding.nav.mode.sub': 'Nivel de configuración',
    'onboarding.nav.provider.title': 'Proveedor',
    'onboarding.nav.provider.sub': 'Acceso a modelos',
    'onboarding.nav.tiers.title': 'Niveles',
    'onboarding.nav.tiers.sub': 'Modelos predeterminados',
    'onboarding.nav.search.title': 'Búsqueda',
    'onboarding.nav.search.sub': 'Acceso web opcional',
    'onboarding.step1.badge': 'Inicio',
    'onboarding.step1.heading': 'Elige el nivel de configuración',
    'onboarding.step1.subtitle': 'Empieza por el camino funcional más corto, o abre ahora todos los controles de enrutador y niveles.',
    'onboarding.step1.simpleTitle': 'Configuración simple',
    'onboarding.step1.simpleDesc': 'Elige un proveedor, añade su clave, selecciona la búsqueda e inicia OpenSquilla con los valores predeterminados.',
    'onboarding.step1.advancedTitle': 'Configuración avanzada',
    'onboarding.step1.advancedDesc': 'Revisa los valores predeterminados de niveles y los detalles del modelo directo antes del inicio.',
    'onboarding.step1.note': 'Puedes cambiar los ajustes de proveedor, enrutador y búsqueda más tarde desde la página Ajustes del escritorio.',
    'onboarding.step1.quit': 'Salir',
    'onboarding.step1.continue': 'Continuar',
    'onboarding.step2.badge': 'Obligatorio',
    'onboarding.step2.heading': 'Configuración del servicio de modelos',
    'onboarding.step2.subtitle': 'Introduce una clave API para empezar.',
    'onboarding.step2.tokenrhythmTitle': 'Oferta limitada de TokenRhythm',
    'onboarding.step2.tokenrhythmValue': 'Las llamadas a la API de TokenRhythm son gratis por tiempo limitado.',
    'onboarding.step2.tokenrhythmRegistration': 'Regístrate y recibe 68 ¥ en tokens gratis.',
    'onboarding.step2.tokenrhythmCta': 'Obtener gratis',
    'onboarding.step2.tokenrhythmCtaExternalLabel': 'Obtener gratis (se abre en el navegador externo)',
    'onboarding.step2.otherProviders': 'Otros proveedores',
    'onboarding.step2.apiKey': 'Clave API',
    'onboarding.step2.endpointSummary': 'Endpoint y modelo directo',
    'onboarding.step2.baseUrl': 'Base URL',
    'onboarding.step2.directModel': 'Nombre del modelo',
    'onboarding.step2.back': 'Atrás',
    'onboarding.step2.next': 'Siguiente',
    'onboarding.nav.routing.title': 'Enrutamiento',
    'onboarding.nav.routing.sub': 'Modo',
    'onboarding.aria.modelRoutingMode': 'Modo de enrutamiento',
    'onboarding.step3.badge': 'Avanzado',
    'onboarding.step3.heading': 'Elige el modo de enrutamiento',
    'onboarding.step3.subtitle': 'Decide si OpenSquilla debe usar los niveles del Smart Router, llamar a un único modelo fijo o usar el ensemble del proveedor actual.',
    'onboarding.step3.back': 'Atrás',
    'onboarding.step3.next': 'Siguiente',
    'onboarding.step3.directModel': 'Modelo directo',
    'onboarding.step4.badge': 'Modelos',
    'onboarding.step4.heading': 'Revisa los modelos por nivel',
    'onboarding.step4.subtitle': 'Elige el nivel de texto predeterminado y mantén los valores predeterminados de la CLI, o personaliza los id de modelo antes del inicio.',
    'onboarding.step4.back': 'Atrás',
    'onboarding.step4.next': 'Siguiente',
    'onboarding.step5.badge': 'Opcional',
    'onboarding.step5.heading': 'Elige la búsqueda web',
    'onboarding.step5.subtitle': 'La búsqueda es opcional. Empieza sin otra clave, o conecta un proveedor de búsqueda compatible con el runtime.',
    'onboarding.step5.searchKey': 'Clave API de búsqueda',
    'onboarding.step5.searchHintDefault': 'DuckDuckGo es suficiente para empezar.',
    'onboarding.step5.back': 'Atrás',
    'onboarding.step5.finish': 'Iniciar OpenSquilla',
  },
}

// Runtime string bag for the onboarding inline <script>. These literals are
// built dynamically in the browser (validateStep messages, mode/provider/search
// hints, More/Hide toggles), so they cannot use desktopT() server-side. The bag
// is JSON-serialized into the page. Placeholders like {label} are substituted at
// runtime so word order stays correct per language.
const ONBOARDING_SCRIPT_MESSAGES: Record<DesktopLocale, Record<string, string>> = {
  en: {
    tierDefaultsAvailable: 'Tier defaults available.',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: 'Use the existing layered Squilla Router defaults for this provider.',
    modeDirectTitle: 'Direct single model',
    modeDirectDesc: 'Send every request to one provider model without tier routing or ensemble.',
    modeEnsembleTitle: 'Ensemble',
    modeEnsembleDesc: "Use this provider's static B5 ensemble and skip the tier table.",
    modeSmartRouterUnavailable: 'This provider does not have desktop tier defaults yet.',
    modeEnsembleUnavailable: 'Ensemble setup currently requires OpenRouter or TokenRhythm.',
    directModelPrompt: 'Requests will use this model directly.',
    directModelLabel: 'Direct model',
    noModel: 'No model',
    directModelNote: 'Smart Router is off. Every request uses this model directly.',
    defaultPill: 'default',
    providerField: 'Provider',
    selectProviderPlaceholder: 'Choose a provider',
    providerGroupRecommended: 'Recommended',
    providerGroupCloud: 'Cloud services',
    providerGroupLocal: 'Local services',
    limitedFreeBadge: 'Limited-time free',
    recommendedModel: 'Recommended model',
    editModel: 'Edit',
    doneEditingModel: 'Done',
    modelField: 'Model',
    customizeTiers: 'Customize tier models',
    requiresApiKey: 'Requires an API key.',
    noKeyRequired: 'No key required.',
    searchAvailable: '{label} will be available to browser-capable agents.',
    searchHintDefault: 'DuckDuckGo is enough to start.',
    billingFree: 'Free',
    billingPaid: 'Paid',
    searchFreeGroup: 'Free search',
    searchPaidGroup: 'Paid search services',
    apiKeyRequired: '{label} API key is required.',
    verifyConfiguration: 'Verify configuration',
    verifyingConfiguration: 'Verifying…',
    configurationVerified: 'Configuration verified',
    configurationVerifiedWithLatency: 'Verified · {ms} ms',
    configurationVerificationFailed: 'Verification failed: {detail}',
    directModelRequiredDisabled: 'Direct model is required when Smart Router is disabled.',
    directModelRequiredDirect: 'Direct model is required for Direct single model mode.',
    defaultTierRequiresModel: 'Default router tier requires a model.',
    searchApiKeyRequired: '{label} search API key is required.',
    savingSetup: 'Saving setup…',
    setupTakingLonger: 'First-time setup usually takes 10–20 seconds. Please keep this window open.',
    stepLabel: 'Step {n}',
  },
  'zh-Hans': {
    tierDefaultsAvailable: '提供层级默认值。',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: '使用此提供商现有的 Squilla Router 层级默认值。',
    modeDirectTitle: '直连单模型',
    modeDirectDesc: '每个请求都发送到一个固定模型，不使用层级路由或 Ensemble。',
    modeEnsembleTitle: 'Ensemble',
    modeEnsembleDesc: '使用当前提供商的 static B5 Ensemble，并跳过层级表。',
    modeSmartRouterUnavailable: '此提供商尚无桌面层级默认值。',
    modeEnsembleUnavailable: '当前 onboarding 中 Ensemble 需要 OpenRouter 或 TokenRhythm。',
    directModelPrompt: '请求会直接使用此模型。',
    directModelLabel: '直连模型',
    noModel: '无模型',
    directModelNote: 'Smart Router 已关闭。每个请求都直接使用此模型。',
    defaultPill: '默认',
    providerField: '提供商',
    selectProviderPlaceholder: '选择提供商',
    providerGroupRecommended: '推荐',
    providerGroupCloud: '云端服务',
    providerGroupLocal: '本地服务',
    limitedFreeBadge: '限时免费',
    recommendedModel: '推荐模型',
    editModel: '修改',
    doneEditingModel: '完成',
    modelField: '模型',
    customizeTiers: '自定义层级模型',
    requiresApiKey: '需要 API 密钥。',
    noKeyRequired: '无需密钥。',
    searchAvailable: '{label} 将可供具备浏览能力的 agent 使用。',
    searchHintDefault: 'DuckDuckGo 足以开始使用。',
    billingFree: '免费',
    billingPaid: '付费',
    searchFreeGroup: '免费搜索',
    searchPaidGroup: '付费搜索服务',
    apiKeyRequired: '需要 {label} API 密钥。',
    verifyConfiguration: '验证配置',
    verifyingConfiguration: '正在验证…',
    configurationVerified: '配置验证成功',
    configurationVerifiedWithLatency: '验证成功 · {ms} ms',
    configurationVerificationFailed: '验证失败：{detail}',
    directModelRequiredDisabled: '禁用 Smart Router 时需要直连模型。',
    directModelRequiredDirect: '直连单模型模式需要直连模型。',
    defaultTierRequiresModel: '默认路由层级需要一个模型。',
    searchApiKeyRequired: '需要 {label} 搜索 API 密钥。',
    savingSetup: '正在保存设置…',
    setupTakingLonger: '首次设置通常需要 10–20 秒，请保持此窗口打开。',
    stepLabel: '步骤 {n}',
  },
  ja: {
    tierDefaultsAvailable: 'ティアのデフォルトを利用できます。',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: 'このプロバイダー向けの既存の階層化された Squilla Router のデフォルトを使用します。',
    modeDirectTitle: '直接単一モデル',
    modeDirectDesc: 'すべてのリクエストを、ティアルーティングやアンサンブルなしで 1 つのプロバイダーモデルに送信します。',
    modeEnsembleTitle: 'アンサンブル',
    modeEnsembleDesc: '現在のプロバイダーの static B5 アンサンブルを使用し、ティア表をスキップします。',
    modeSmartRouterUnavailable: 'このプロバイダーにはまだデスクトップ用のティアデフォルトがありません。',
    modeEnsembleUnavailable: 'アンサンブルの設定には現在 OpenRouter または TokenRhythm が必要です。',
    directModelPrompt: 'リクエストはこのモデルを直接使用します。',
    directModelRequiredDirect: '直接単一モデルモードには直接モデルが必要です。',
    directModelLabel: '直接モデル',
    noModel: 'モデルなし',
    directModelNote: 'Smart Router はオフです。すべてのリクエストでこのモデルを直接使用します。',
    defaultPill: 'デフォルト',
    providerField: 'プロバイダー',
    selectProviderPlaceholder: 'プロバイダーを選択',
    providerGroupRecommended: 'おすすめ',
    providerGroupCloud: 'クラウドサービス',
    providerGroupLocal: 'ローカルサービス',
    limitedFreeBadge: '期間限定無料',
    recommendedModel: '推奨モデル',
    editModel: '編集',
    doneEditingModel: '完了',
    modelField: 'モデル',
    customizeTiers: 'ティアモデルをカスタマイズ',
    requiresApiKey: 'API キーが必要です。',
    noKeyRequired: 'キーは不要です。',
    searchAvailable: '{label} はブラウザ対応のエージェントで利用できるようになります。',
    searchHintDefault: 'DuckDuckGo で始めるには十分です。',
    billingFree: '無料',
    billingPaid: '有料',
    searchFreeGroup: '無料検索',
    searchPaidGroup: '有料検索サービス',
    apiKeyRequired: '{label} の API キーが必要です。',
    verifyConfiguration: '構成を検証',
    verifyingConfiguration: '検証中…',
    configurationVerified: '構成を検証しました',
    configurationVerifiedWithLatency: '検証済み · {ms} ms',
    configurationVerificationFailed: '検証に失敗しました：{detail}',
    directModelRequiredDisabled: 'Smart Router を無効にする場合は直接モデルが必要です。',
    defaultTierRequiresModel: 'デフォルトのルーターティアにはモデルが必要です。',
    searchApiKeyRequired: '{label} の検索 API キーが必要です。',
    savingSetup: '設定を保存しています…',
    setupTakingLonger: '初回セットアップには通常10～20秒かかります。このウィンドウを開いたままお待ちください。',
    stepLabel: 'ステップ {n}',
  },
  fr: {
    tierDefaultsAvailable: 'Valeurs de niveau par défaut disponibles.',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: 'Utiliser les valeurs par défaut existantes du Squilla Router en couches pour ce fournisseur.',
    modeDirectTitle: 'Modèle unique direct',
    modeDirectDesc: 'Envoyer chaque requête à un seul modèle du fournisseur, sans routage par niveaux ni ensemble.',
    modeEnsembleTitle: 'Ensemble',
    modeEnsembleDesc: "Utiliser l'ensemble statique B5 de ce fournisseur et ignorer le tableau des niveaux.",
    modeSmartRouterUnavailable: "Ce fournisseur n'a pas encore de niveaux par défaut pour le bureau.",
    modeEnsembleUnavailable: "La configuration de l'ensemble nécessite actuellement OpenRouter ou TokenRhythm.",
    directModelPrompt: 'Les requêtes utiliseront directement ce modèle.',
    directModelRequiredDirect: 'Un modèle direct est requis pour le mode Modèle unique direct.',
    directModelLabel: 'Modèle direct',
    noModel: 'Aucun modèle',
    directModelNote: 'Smart Router est désactivé. Chaque requête utilise directement ce modèle.',
    defaultPill: 'par défaut',
    providerField: 'Fournisseur',
    selectProviderPlaceholder: 'Choisir un fournisseur',
    providerGroupRecommended: 'Recommandé',
    providerGroupCloud: 'Services cloud',
    providerGroupLocal: 'Services locaux',
    limitedFreeBadge: 'Gratuit temporairement',
    recommendedModel: 'Modèle recommandé',
    editModel: 'Modifier',
    doneEditingModel: 'Terminé',
    modelField: 'Modèle',
    customizeTiers: 'Personnaliser les modèles de niveau',
    requiresApiKey: 'Nécessite une clé API.',
    noKeyRequired: 'Aucune clé requise.',
    searchAvailable: '{label} sera disponible pour les agents capables de naviguer.',
    searchHintDefault: 'DuckDuckGo suffit pour démarrer.',
    billingFree: 'Gratuit',
    billingPaid: 'Payant',
    searchFreeGroup: 'Recherche gratuite',
    searchPaidGroup: 'Services de recherche payants',
    apiKeyRequired: 'La clé API {label} est requise.',
    verifyConfiguration: 'Vérifier la configuration',
    verifyingConfiguration: 'Vérification…',
    configurationVerified: 'Configuration vérifiée',
    configurationVerifiedWithLatency: 'Vérifiée · {ms} ms',
    configurationVerificationFailed: 'Échec de la vérification : {detail}',
    directModelRequiredDisabled: 'Un modèle direct est requis lorsque Smart Router est désactivé.',
    defaultTierRequiresModel: 'Le niveau de routeur par défaut nécessite un modèle.',
    searchApiKeyRequired: 'La clé API de recherche {label} est requise.',
    savingSetup: 'Enregistrement…',
    setupTakingLonger: 'La configuration initiale prend généralement 10 à 20 secondes. Gardez cette fenêtre ouverte.',
    stepLabel: 'Étape {n}',
  },
  de: {
    tierDefaultsAvailable: 'Stufenstandards verfügbar.',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: 'Die vorhandenen mehrstufigen Squilla-Router-Standards für diesen Anbieter verwenden.',
    modeDirectTitle: 'Einzelnes Direktmodell',
    modeDirectDesc: 'Jede Anfrage an ein einzelnes Anbietermodell senden, ohne Stufenrouting oder Ensemble.',
    modeEnsembleTitle: 'Ensemble',
    modeEnsembleDesc: 'Das statische B5-Ensemble dieses Anbieters verwenden und die Stufentabelle überspringen.',
    modeSmartRouterUnavailable: 'Dieser Anbieter hat noch keine Desktop-Stufenstandards.',
    modeEnsembleUnavailable: 'Die Ensemble-Einrichtung erfordert derzeit OpenRouter oder TokenRhythm.',
    directModelPrompt: 'Anfragen verwenden dieses Modell direkt.',
    directModelRequiredDirect: 'Für den Modus „Einzelnes Direktmodell“ ist ein direktes Modell erforderlich.',
    directModelLabel: 'Direktes Modell',
    noModel: 'Kein Modell',
    directModelNote: 'Smart Router ist aus. Jede Anfrage verwendet dieses Modell direkt.',
    defaultPill: 'Standard',
    providerField: 'Anbieter',
    selectProviderPlaceholder: 'Anbieter auswählen',
    providerGroupRecommended: 'Empfohlen',
    providerGroupCloud: 'Cloud-Dienste',
    providerGroupLocal: 'Lokale Dienste',
    limitedFreeBadge: 'Zeitlich kostenlos',
    recommendedModel: 'Empfohlenes Modell',
    editModel: 'Ändern',
    doneEditingModel: 'Fertig',
    modelField: 'Modell',
    customizeTiers: 'Stufenmodelle anpassen',
    requiresApiKey: 'Erfordert einen API-Schlüssel.',
    noKeyRequired: 'Kein Schlüssel erforderlich.',
    searchAvailable: '{label} wird für browserfähige Agenten verfügbar sein.',
    searchHintDefault: 'DuckDuckGo reicht für den Start.',
    billingFree: 'Kostenlos',
    billingPaid: 'Kostenpflichtig',
    searchFreeGroup: 'Kostenlose Suche',
    searchPaidGroup: 'Kostenpflichtige Suchdienste',
    apiKeyRequired: 'Der API-Schlüssel für {label} ist erforderlich.',
    verifyConfiguration: 'Konfiguration überprüfen',
    verifyingConfiguration: 'Wird überprüft…',
    configurationVerified: 'Konfiguration überprüft',
    configurationVerifiedWithLatency: 'Überprüft · {ms} ms',
    configurationVerificationFailed: 'Überprüfung fehlgeschlagen: {detail}',
    directModelRequiredDisabled: 'Ein direktes Modell ist erforderlich, wenn Smart Router deaktiviert ist.',
    defaultTierRequiresModel: 'Die Standard-Routerstufe erfordert ein Modell.',
    searchApiKeyRequired: 'Der Such-API-Schlüssel für {label} ist erforderlich.',
    savingSetup: 'Einrichtung wird gespeichert…',
    setupTakingLonger: 'Die Ersteinrichtung dauert normalerweise 10–20 Sekunden. Lassen Sie dieses Fenster geöffnet.',
    stepLabel: 'Schritt {n}',
  },
  es: {
    tierDefaultsAvailable: 'Valores de nivel predeterminados disponibles.',
    modeSmartRouterTitle: 'Smart Router',
    modeSmartRouterDesc: 'Usar los valores predeterminados por niveles existentes del Squilla Router para este proveedor.',
    modeDirectTitle: 'Modelo único directo',
    modeDirectDesc: 'Enviar cada solicitud a un único modelo del proveedor, sin enrutamiento por niveles ni ensemble.',
    modeEnsembleTitle: 'Ensemble',
    modeEnsembleDesc: 'Usar el ensemble estático B5 de este proveedor y omitir la tabla de niveles.',
    modeSmartRouterUnavailable: 'Este proveedor aún no tiene valores de nivel predeterminados para el escritorio.',
    modeEnsembleUnavailable: 'La configuración del ensemble requiere actualmente OpenRouter o TokenRhythm.',
    directModelPrompt: 'Las solicitudes usarán este modelo directamente.',
    directModelRequiredDirect: 'Se requiere un modelo directo para el modo Modelo único directo.',
    directModelLabel: 'Modelo directo',
    noModel: 'Sin modelo',
    directModelNote: 'Smart Router está desactivado. Cada solicitud usa este modelo directamente.',
    defaultPill: 'predeterminado',
    providerField: 'Proveedor',
    selectProviderPlaceholder: 'Elegir un proveedor',
    providerGroupRecommended: 'Recomendado',
    providerGroupCloud: 'Servicios en la nube',
    providerGroupLocal: 'Servicios locales',
    limitedFreeBadge: 'Gratis por tiempo limitado',
    recommendedModel: 'Modelo recomendado',
    editModel: 'Editar',
    doneEditingModel: 'Listo',
    modelField: 'Modelo',
    customizeTiers: 'Personalizar modelos de nivel',
    requiresApiKey: 'Requiere una clave API.',
    noKeyRequired: 'No se requiere clave.',
    searchAvailable: '{label} estará disponible para los agentes con capacidad de navegación.',
    searchHintDefault: 'DuckDuckGo es suficiente para empezar.',
    billingFree: 'Gratis',
    billingPaid: 'De pago',
    searchFreeGroup: 'Búsqueda gratuita',
    searchPaidGroup: 'Servicios de búsqueda de pago',
    apiKeyRequired: 'Se requiere la clave API de {label}.',
    verifyConfiguration: 'Verificar configuración',
    verifyingConfiguration: 'Verificando…',
    configurationVerified: 'Configuración verificada',
    configurationVerifiedWithLatency: 'Verificada · {ms} ms',
    configurationVerificationFailed: 'Error de verificación: {detail}',
    directModelRequiredDisabled: 'Se requiere un modelo directo cuando Smart Router está desactivado.',
    defaultTierRequiresModel: 'El nivel de enrutador predeterminado requiere un modelo.',
    searchApiKeyRequired: 'Se requiere la clave API de búsqueda de {label}.',
    savingSetup: 'Guardando la configuración…',
    setupTakingLonger: 'La configuración inicial suele tardar entre 10 y 20 segundos. Mantén esta ventana abierta.',
    stepLabel: 'Paso {n}',
  },
}

function desktopT(key: string): string {
  return DESKTOP_MESSAGES[desktopLocale][key] ?? DESKTOP_MESSAGES.en[key] ?? key
}

function createApplicationMenu(): void {
  if (!shouldUseNativeApplicationMenu) {
    Menu.setApplicationMenu(null)
    return
  }
  const appSubmenu: Electron.MenuItemConstructorOptions[] = [{ role: 'about' }]
  if (desktopUpdateMenuEnabled()) {
    appSubmenu.push({ type: 'separator' })
    if (downloadedUpdateVersion !== null) {
      appSubmenu.push(
        {
          label: desktopT('menu.relaunchToUpdate'),
          click: () => {
            void applyDownloadedUpdate()
          },
        },
        { type: 'separator' },
      )
    }
    appSubmenu.push({
      label: desktopT('menu.checkForUpdates'),
      click: () => {
        void checkForUpdates(true)
      },
    })
  }
  appSubmenu.push(
    { type: 'separator' },
    {
      label: desktopT('menu.downloadDiagnostics'),
      click: () => {
        void downloadDiagnostics()
      },
    },
  )
  appSubmenu.push({ type: 'separator' }, { role: 'quit' })

  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: app.name,
      submenu: appSubmenu,
    },
    {
      label: desktopT('menu.edit'),
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'pasteAndMatchStyle' },
        { role: 'delete' },
        { type: 'separator' },
        { role: 'selectAll' },
      ],
    },
    {
      label: desktopT('menu.view'),
      submenu: [
        // Disable reload while the onboarding wizard is open: its state lives only
        // in the renderer of a one-shot data: URL, so a reload would silently wipe
        // the in-progress setup (typed key, provider, step, tier edits).
        { role: 'reload', enabled: currentOnboardingWindow() === null },
        { role: 'forceReload', enabled: currentOnboardingWindow() === null },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
      ],
    },
    {
      label: desktopT('menu.window'),
      submenu: [
        { role: 'minimize' },
        { role: 'zoom' },
        { role: 'front' },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

function currentOnboardingWindow(): BrowserWindow | null {
  return onboardingWindow && !onboardingWindow.isDestroyed() ? onboardingWindow : null
}

function focusOnboardingWindow(): boolean {
  const window = currentOnboardingWindow()
  if (!window) return false
  if (window.isMinimized()) window.restore()
  window.show()
  window.focus()
  return true
}

function focusMainWindow(): boolean {
  if (focusOnboardingWindow()) return true
  if (!mainWindow || mainWindow.isDestroyed()) return false
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
  return true
}

function setAppExitPhase(next: DesktopExitPhase, reason: string): void {
  if (appExitPhase === next) return
  desktopLog('desktop_exit_phase', { from: appExitPhase, to: next, reason })
  appExitPhase = next
  rebuildWindowsTrayMenu()
}

function destroyWindowsTray(): void {
  if (!windowsTray) return
  windowsTray.destroy()
  windowsTray = null
}

function rebuildWindowsTrayMenu(): void {
  if (!windowsTray) return
  windowsTray.setContextMenu(Menu.buildFromTemplate([
    {
      label: desktopT('tray.open'),
      enabled: canRevealDesktopApp(appExitPhase),
      click: () => revealDesktopApp(),
    },
    {
      label: desktopT('tray.running'),
      enabled: false,
    },
    { type: 'separator' },
    {
      label: desktopT('tray.quit'),
      click: () => app.quit(),
    },
  ]))
}

function createWindowsTray(): boolean {
  if (process.platform !== 'win32') return false
  if (windowsTray) return true
  try {
    const tray = new Tray(appIconPath())
    windowsTray = tray
    tray.setToolTip('OpenSquilla')
    tray.on('click', () => revealDesktopApp())
    tray.on('balloon-click', () => revealDesktopApp())
    rebuildWindowsTrayMenu()
    desktopLog('windows_tray_ready')
    return true
  } catch (error) {
    windowsTray = null
    desktopLog('windows_tray_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
    return false
  }
}

function showWindowsBackgroundCloseNotice(): void {
  if (process.platform !== 'win32' || !windowsTray) return
  const preferences = loadDesktopPreferencesRecord()
  if (preferences.value.background_close_notice_shown) return
  try {
    windowsTray.displayBalloon({
      title: desktopT('tray.backgroundTitle'),
      content: desktopT('tray.backgroundDetail'),
      iconType: 'info',
      noSound: true,
      respectQuietTime: true,
    })
    markBackgroundCloseNoticeShown()
  } catch (error) {
    desktopLog('windows_tray_notice_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
  }
}

function hideMainWindow(window: BrowserWindow): void {
  if (!window.webContents.isDestroyed()) {
    window.webContents.send('desktop:window:hidden')
  }
  window.hide()
  desktopLog('main_window_hidden', { platform: process.platform })
  showWindowsBackgroundCloseNotice()
}

async function activateMainWindow(source = 'desktop-activation'): Promise<void> {
  if (!canRevealDesktopApp(appExitPhase)) {
    desktopLog('main_window_activation_ignored', { source, appExitPhase })
    return
  }

  // Surface an existing window immediately while any single-flight startup or
  // Gateway recovery continues in the background.
  if (process.platform === 'darwin') app.focus({ steal: true })
  const focusedImmediately = focusMainWindow()

  // Profile migration, cleanup, update, and quit drains may safely reveal an
  // existing renderer, but must not create a replacement window or Gateway.
  if (isQuitting) {
    desktopLog('main_window_activation_during_shutdown', {
      source,
      focused: focusedImmediately,
    })
    return
  }

  // Activating an existing window is complete after the synchronous focus
  // sequence above. Starting another open flow here can invalidate a startup
  // that is still settling and later re-show a window the user has hidden.
  if (focusedImmediately) {
    desktopLog('main_window_activated', {
      source,
      created: false,
      focused: true,
    })
    return
  }

  await openOrResumeDesktopApp()

  // openOrResumeDesktopApp creates the window when it was absent. Repeat the
  // idempotent focus sequence after that asynchronous boundary.
  if (process.platform === 'darwin') app.focus({ steal: true })
  const focused = focusMainWindow()
  desktopLog('main_window_activated', {
    source,
    created: true,
    focused,
  })
}

function revealDesktopApp(): void {
  void activateMainWindow('desktop-reveal')
}

function handleDeepLink(rawUrl: unknown, source = 'unknown'): boolean {
  const action = parseDesktopDeepLink(rawUrl)
  if (action !== 'open') {
    // Never persist an untrusted URL: query strings may contain credentials or
    // other private browser state even though this parser rejects them.
    desktopLog('deep_link_ignored', { source })
    return false
  }

  desktopLog('deep_link_accepted', {
    source,
    action,
    activationReady: desktopDeepLinkActivationReady,
  })
  if (!desktopDeepLinkActivationReady) {
    pendingDesktopDeepLinkOpen = true
    return true
  }

  void activateMainWindow(`deep-link:${source}`)
  return true
}

function handleDeepLinksFromCommandLine(
  commandLine: readonly string[],
  source: string,
): boolean {
  const candidates = desktopDeepLinkArguments(commandLine)
  for (const candidate of candidates) handleDeepLink(candidate, source)
  return candidates.length > 0
}

function activatePendingDesktopDeepLink(): boolean {
  if (!pendingDesktopDeepLinkOpen) return false
  pendingDesktopDeepLinkOpen = false
  void activateMainWindow('deep-link:pending')
  return true
}

function registerDesktopDeepLinkProtocolClient(): void {
  if (!app.isPackaged) {
    desktopLog('deep_link_protocol_registration_skipped', { reason: 'development' })
    return
  }
  try {
    const registered = app.setAsDefaultProtocolClient(DESKTOP_DEEP_LINK_SCHEME)
    desktopLog('deep_link_protocol_registered', { registered })
  } catch (error) {
    desktopLog('deep_link_protocol_registration_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
  }
}

async function promptForMainWindowClose(window: BrowserWindow): Promise<void> {
  if (mainWindowClosePrompt) return await mainWindowClosePrompt
  mainWindowClosePrompt = (async () => {
    const result = await dialog.showMessageBox(window, {
      type: 'question',
      title: desktopT('closePrompt.title'),
      message: desktopT('closePrompt.message'),
      detail: desktopT('closePrompt.detail'),
      buttons: [
        desktopT('closePrompt.background'),
        desktopT('closePrompt.quit'),
        desktopT('closePrompt.cancel'),
      ],
      defaultId: 0,
      cancelId: 2,
      checkboxLabel: desktopT('closePrompt.remember'),
      checkboxChecked: false,
      noLink: true,
    })
    if (window.isDestroyed() || appExitPhase !== 'running') return
    if (result.response === 0) {
      if (result.checkboxChecked) {
        await saveDesktopPreferences({
          mainWindowCloseBehavior: 'background',
        }).catch((error) => {
          desktopLog('desktop_preferences_save_failed', {
            error: error instanceof Error ? error.message : String(error),
          })
        })
      }
      hideMainWindow(window)
      return
    }
    if (result.response === 1) {
      if (result.checkboxChecked) {
        await saveDesktopPreferences({
          mainWindowCloseBehavior: 'quit',
        }).catch((error) => {
          desktopLog('desktop_preferences_save_failed', {
            error: error instanceof Error ? error.message : String(error),
          })
        })
      }
      app.quit()
    }
  })().finally(() => {
    mainWindowClosePrompt = null
  })
  return await mainWindowClosePrompt
}

function handleMainWindowClose(window: BrowserWindow, event: Electron.Event): void {
  const action = mainWindowCloseAction({
    platform: process.platform,
    exitPhase: appExitPhase,
    systemSessionEnding,
    onboardingOpen: currentOnboardingWindow() !== null,
    behavior: loadDesktopPreferencesRecord().value.main_window_close_behavior,
    windowsTrayReady: windowsTray !== null,
  })
  if (action === 'allow') return
  event.preventDefault()
  if (action === 'focus-onboarding') {
    focusOnboardingWindow()
    return
  }
  if (action === 'hide') {
    hideMainWindow(window)
    return
  }
  if (action === 'quit') {
    app.quit()
    return
  }
  void promptForMainWindowClose(window)
}

function installEditingContextMenu(window: BrowserWindow): void {
  window.webContents.on('context-menu', (_event, params) => {
    if (!params.isEditable) return
    Menu.buildFromTemplate([
      { role: 'cut', enabled: params.editFlags.canCut },
      { role: 'copy', enabled: params.editFlags.canCopy },
      { role: 'paste', enabled: params.editFlags.canPaste },
      { type: 'separator' },
      { role: 'selectAll', enabled: params.editFlags.canSelectAll },
    ]).popup({ window })
  })
}

// Server-side HTML escape for localized strings interpolated into the
// onboarding template. Mirrors the browser-side escapeHtml() in the inline
// script so static translated text is safe in both text content and attributes.
function escapeHtmlServer(value: string): string {
  return String(value).replace(/[&<>"']/g, (char) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char] as string
  ))
}

// Localized server-rendered onboarding string, HTML-escaped for safe insertion.
function ot(key: string): string {
  return escapeHtmlServer(desktopT(key))
}

function localeOptionsHtml(): string {
  return DESKTOP_LOCALES.map((locale) => (
    `<option value="${escapeHtmlServer(locale)}"${locale === desktopLocale ? ' selected' : ''}>${escapeHtmlServer(DESKTOP_LOCALE_LABELS[locale])}</option>`
  )).join('')
}

function onboardingHtml(
  pendingProviderSetup: MigrationProviderPrefill | null = null,
): string {
  return `<!doctype html>
<html lang="${desktopLocale}">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:;">
  <title>${ot('onboarding.title')}</title>
  <style>
    :root {
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
      --bg: #F4F5F7;
      --paper: #FFFFFF;
      --surface-subtle: #F8F9FA;
      --ink: #15181C;
      --muted: #565D66;
      --dim: #7A818A;
      --line: #E1E4E8;
      --line-strong: #C9CED5;
      --accent: #BA4D0F;
      --accent-hover: #A5440C;
      --accent-deep: #8E3A0A;
      --accent-secondary: #B6501C;
      --accent-soft: rgba(186, 77, 15, 0.035);
      --primary: #343A40;
      --primary-hover: #272C31;
      color: var(--ink);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      overflow: hidden;
      background: var(--bg);
    }
    main {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      padding: 20px 28px 28px;
      gap: 16px;
    }
    .topbar {
      width: min(700px, 100%);
      min-height: 36px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      color: var(--ink);
      font-size: 13px;
      font-weight: 620;
      letter-spacing: -0.01em;
    }
    .brand-mark {
      width: 7px;
      height: 7px;
      border-radius: 2px;
      background: var(--accent);
    }
    .language-picker {
      display: inline-flex;
      color: var(--muted);
      font-size: 12px;
      font-weight: 520;
    }
    .language-picker select {
      width: auto;
      min-width: 112px;
      min-height: 32px;
      border-color: var(--line);
      border-radius: 7px;
      background: var(--paper);
      font-size: 12px;
      font-weight: 520;
      padding: 0 28px 0 10px;
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    .deck {
      position: relative;
      width: min(700px, 100%);
      margin: 0 auto;
      min-width: 0;
      min-height: 0;
      display: grid;
      place-items: center;
    }
    .deck > .error {
      position: absolute;
      left: 32px;
      right: 32px;
      bottom: 20px;
      z-index: 3;
    }
    [hidden] {
      display: none !important;
    }
    .setup-card {
      position: absolute;
      top: 50%;
      left: 50%;
      width: 100%;
      height: min(700px, 100%);
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 22px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: var(--paper);
      box-shadow: 0 18px 48px rgba(16, 20, 26, 0.06);
      opacity: 0;
      pointer-events: none;
      transform: translate(-50%, calc(-50% + 8px));
      transition: opacity 160ms ease, transform 180ms cubic-bezier(.2,.8,.2,1);
      padding: 32px;
    }
    .setup-card.active {
      opacity: 1;
      pointer-events: auto;
      transform: translate(-50%, -50%);
    }
    .setup-card.leaving {
      opacity: 0;
      pointer-events: none;
      transform: translate(-50%, calc(-50% - 4px));
    }
    .card-head {
      display: block;
    }
    .context-label {
      margin: 0 0 8px;
      color: var(--dim);
      font-size: 11px;
      font-weight: 650;
      letter-spacing: 0.04em;
    }
    h2 {
      margin: 0;
      max-width: 540px;
      font-size: 26px;
      font-weight: 610;
      line-height: 1.2;
      letter-spacing: -0.02em;
    }
    p {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
      margin: 8px 0 0;
    }
    .card-body {
      display: grid;
      gap: 16px;
      align-content: start;
      min-height: 0;
      overflow-x: hidden;
      overflow-y: auto;
      padding-right: 4px;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }
    .provider-promo-copy {
      min-width: 0;
      display: flex;
      align-items: baseline;
      justify-self: start;
      gap: 8px;
      overflow: hidden;
      white-space: nowrap;
    }
    .provider-promo-copy strong {
      color: var(--accent);
      font-size: 10.5px;
      font-weight: 560;
      line-height: 1.35;
    }
    .provider-promo-copy span {
      color: var(--accent);
      font-size: 10.5px;
      font-weight: 420;
      line-height: 1.4;
    }
    .provider-promo-cta {
      display: inline-flex;
      min-height: 30px;
      align-items: center;
      justify-content: center;
      gap: 6px;
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      box-shadow: 0 3px 10px rgba(186, 77, 15, 0.14);
      color: #FFFFFF;
      font-size: 10.5px;
      font-weight: 600;
      padding: 0 13px;
      text-decoration: none;
      white-space: nowrap;
      transition: background 150ms ease, box-shadow 150ms ease, transform 150ms ease;
    }
    .provider-promo-cta::after {
      content: "↗";
      font-size: 11px;
      line-height: 1;
    }
    .provider-promo-cta:hover {
      background: var(--accent-hover);
      box-shadow: 0 5px 14px rgba(165, 68, 12, 0.18);
      color: #FFFFFF;
      transform: translateY(-1px);
    }
    .provider-promo-cta:focus-visible {
      outline: 2px solid rgba(186, 77, 15, 0.42);
      outline-offset: 2px;
    }
    .provider-grid {
      display: grid;
      gap: 0;
      grid-template-columns: 1fr;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--paper);
    }
    .provider-feature {
      position: relative;
      width: 100%;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 20px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--paper);
      padding: 18px 20px;
      transition: border-color 150ms ease, background 150ms ease, box-shadow 150ms ease;
    }
    .provider-feature.active {
      border-color: var(--line);
      background: var(--paper);
      box-shadow: inset 2px 0 0 var(--accent);
    }
    .provider-feature-select {
      appearance: none;
      min-height: 0;
      display: grid;
      gap: 5px;
      border: 0;
      background: transparent;
      color: var(--ink);
      cursor: pointer;
      padding: 0;
      text-align: left;
    }
    .provider-feature-select strong {
      padding-right: 8px;
      font-size: 15px;
      font-weight: 600;
      line-height: 1.3;
    }
    .provider-feature-value {
      color: var(--ink);
      font-size: 11.5px;
      font-weight: 500;
      line-height: 1.45;
    }
    .provider-feature-registration {
      color: var(--muted);
      font-size: 10.5px;
      font-weight: 400;
      line-height: 1.4;
    }
    .provider-feature-cta {
      display: inline-flex;
      min-height: 38px;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: var(--paper);
      color: #39414A;
      box-shadow: none;
      font-size: 11.5px;
      font-weight: 560;
      padding: 0 14px;
      text-decoration: none;
      white-space: nowrap;
      transition: background 150ms ease, border-color 150ms ease, box-shadow 150ms ease, transform 150ms ease;
    }
    .provider-feature-cta::after {
      content: "↗";
      font-size: 13px;
      font-weight: 500;
      line-height: 1;
    }
    .provider-feature-cta:hover {
      border-color: #AEB4BB;
      background: var(--surface-subtle);
      box-shadow: 0 4px 12px rgba(16, 20, 24, 0.06);
      transform: translateY(-1px);
    }
    .provider-disclosure {
      display: grid;
      gap: 8px;
    }
    .provider-disclosure-toggle {
      appearance: none;
      width: 100%;
      min-height: 40px;
      display: flex;
      align-items: center;
      gap: 9px;
      border: 0;
      border-radius: 7px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-size: 11.5px;
      font-weight: 520;
      padding: 0 4px;
      text-align: left;
    }
    .provider-disclosure-toggle:hover {
      color: var(--ink);
    }
    .provider-disclosure-selection {
      min-width: 0;
      overflow: hidden;
      color: var(--ink);
      font-weight: 560;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .provider-disclosure-selection::before {
      content: "·";
      margin-right: 7px;
      color: var(--dim);
      font-weight: 450;
    }
    .provider-disclosure-toggle::before {
      content: "";
      width: 7px;
      height: 7px;
      border-right: 2px solid #747a73;
      border-bottom: 2px solid #747a73;
      transform: rotate(-45deg);
      transition: transform 180ms ease, border-color 180ms ease;
    }
    .provider-disclosure-toggle[aria-expanded="true"]::before {
      border-color: var(--ink);
      transform: rotate(45deg);
    }
    .provider-select-field {
      display: grid;
      gap: 0;
    }
    .provider-field-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 11.5px;
      font-weight: 540;
    }
    .provider-inline-cta {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--accent-deep);
      font-size: 10.5px;
      font-weight: 520;
      text-decoration: none;
      white-space: nowrap;
    }
    .provider-inline-cta::after {
      content: "↗";
      font-size: 11px;
      line-height: 1;
    }
    .provider-inline-cta:hover {
      color: var(--ink);
    }
    .provider-combobox {
      position: relative;
      z-index: 4;
    }
    .provider-combobox-toggle {
      appearance: none;
      width: 100%;
      min-height: 42px;
      display: flex;
      align-items: center;
      gap: 8px;
      border: 0;
      border-radius: 8px;
      background: #F7F8F7;
      color: var(--ink);
      cursor: pointer;
      font: inherit;
      padding: 8px 11px;
      text-align: left;
      transition: background 150ms ease, box-shadow 150ms ease;
    }
    .provider-combobox-toggle:hover {
      background: #F2F3F2;
    }
    .provider-combobox-toggle[aria-expanded="true"] {
      background: #F2F3F2;
      box-shadow: 0 0 0 2px rgba(86, 93, 102, 0.12);
    }
    .provider-combobox-label {
      flex: 0 0 auto;
      color: var(--dim);
      font-size: 11.5px;
      font-weight: 500;
    }
    .provider-combobox-value {
      min-width: 0;
      flex: 1;
      overflow: hidden;
      color: #343A40;
      font-size: 11.5px;
      font-weight: 540;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .provider-selected-badges,
    .provider-option-badges {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      flex: 0 0 auto;
    }
    .provider-badge {
      display: inline-flex;
      min-height: 18px;
      align-items: center;
      border-radius: 999px;
      padding: 1px 7px;
      font-size: 9.5px;
      font-weight: 560;
      line-height: 1.4;
      white-space: nowrap;
    }
    .provider-badge.free {
      border: 1px solid #E8D8CE;
      background: #F8F1EC;
      color: var(--accent-deep);
    }
    .provider-combobox-chevron {
      position: relative;
      width: 14px;
      height: 14px;
      flex: 0 0 auto;
      transition: transform 160ms ease;
    }
    .provider-combobox-chevron::before {
      content: "";
      position: absolute;
      top: 2px;
      left: 3px;
      width: 6px;
      height: 6px;
      border-right: 1.5px solid #6E756F;
      border-bottom: 1.5px solid #6E756F;
      transform: rotate(45deg);
    }
    .provider-combobox-toggle[aria-expanded="true"] .provider-combobox-chevron {
      transform: rotate(180deg);
    }
    .provider-select-panel {
      position: absolute;
      z-index: 30;
      top: calc(100% + 6px);
      left: 0;
      width: 100%;
      max-height: min(330px, 46vh);
      overflow: hidden;
      border: 1px solid #D6D9DC;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.98);
      box-shadow: 0 18px 44px rgba(22, 27, 31, 0.13), 0 3px 10px rgba(22, 27, 31, 0.05);
    }
    .provider-options {
      max-height: min(330px, 46vh);
      overflow-y: auto;
      padding: 5px;
      scrollbar-width: thin;
      scrollbar-color: #C9CDD1 transparent;
    }
    .provider-option-group + .provider-option-group {
      margin-top: 5px;
      padding-top: 5px;
      border-top: 1px solid #F0F1F2;
    }
    .provider-option-group-label {
      padding: 5px 8px 4px;
      color: #858B91;
      font-size: 9.5px;
      font-weight: 560;
      letter-spacing: 0.04em;
    }
    .provider-option {
      appearance: none;
      width: 100%;
      min-height: 36px;
      display: flex;
      align-items: center;
      gap: 8px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: #252A2F;
      cursor: pointer;
      font: inherit;
      font-size: 11.5px;
      font-weight: 450;
      padding: 6px 8px;
      text-align: left;
    }
    .provider-option:hover,
    .provider-option:focus-visible {
      background: #F5F6F6;
      outline: none;
    }
    .provider-option[aria-selected="true"] {
      background: #F8F3EF;
      color: var(--accent-deep);
    }
    .provider-option-label {
      min-width: 0;
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .provider-option-check {
      width: 16px;
      flex: 0 0 16px;
      color: var(--accent-secondary);
      font-size: 13px;
      text-align: center;
    }
    .provider-options-empty {
      padding: 24px 12px;
      color: var(--muted);
      font-size: 11px;
      text-align: center;
    }
    .provider-combobox-toggle:focus-visible {
      outline: 2px solid rgba(86, 93, 102, 0.36);
      outline-offset: 2px;
    }
    .provider:focus-visible,
    .provider-feature-select:focus-visible,
    .provider-feature-cta:focus-visible,
    .provider-disclosure-toggle:focus-visible {
      outline: 2px solid rgba(186, 77, 15, 0.48);
      outline-offset: 2px;
    }
    .provider-picker {
      min-height: 0;
      max-height: min(228px, 31vh);
      overflow-x: hidden;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--line-strong) transparent;
    }
    .provider-picker .provider-grid {
      margin-right: 3px;
    }
    .provider-grid.single-provider {
      grid-template-columns: 1fr;
    }
    .provider, .choice {
      appearance: none;
      position: relative;
      display: grid;
      gap: 4px;
      min-height: 64px;
      border: 1px solid var(--line);
      border-radius: 9px;
      background: var(--paper);
      color: var(--ink);
      cursor: pointer;
      padding: 12px 14px;
      text-align: left;
      transition: border-color 150ms ease, background 150ms ease, box-shadow 150ms ease;
    }
    .provider {
      min-height: 54px;
      grid-template-columns: minmax(180px, 0.7fr) minmax(0, 1.3fr);
      align-items: center;
      gap: 16px;
      border: 0;
      border-bottom: 1px solid var(--line);
      border-radius: 0;
      padding: 10px 13px;
    }
    .provider:last-child { border-bottom: 0; }
    .provider:hover, .choice:hover {
      border-color: var(--line-strong);
      background: var(--surface-subtle);
    }
    .provider.active, .choice.active {
      border-color: var(--line-strong);
      background: var(--accent-soft);
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .provider:disabled, .choice:disabled {
      opacity: 0.48;
      cursor: not-allowed;
      transform: none;
    }
    .provider strong, .choice strong {
      display: block;
      padding-right: 8px;
      font-size: 13px;
      font-weight: 580;
    }
    .choice {
      padding-right: 72px;
    }
    .search-provider-billing {
      position: absolute;
      top: 12px;
      right: 14px;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: var(--dim);
      font-size: 10px;
      font-weight: 520;
      line-height: 1;
    }
    .search-provider-billing::before {
      content: "";
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.72;
    }
    .search-provider-billing.free {
      color: #4F7865;
    }
    .search-provider-billing.paid {
      color: #7A6657;
    }
    .provider small, .choice small {
      color: var(--muted);
      display: block;
      padding-right: 4px;
      font-size: 10.5px;
      font-weight: 450;
      line-height: 1.38;
    }
    .choice-row {
      display: grid;
      gap: 8px;
      grid-template-columns: 1fr;
    }
    .search-provider-list {
      display: grid;
      gap: 8px;
    }
    .inline-search-section {
      display: grid;
      gap: 10px;
      border-top: 1px solid var(--line);
      margin-top: 8px;
      padding-top: 20px;
    }
    .inline-search-toggle {
      appearance: none;
      width: 100%;
      min-height: 32px;
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      border: 0;
      border-radius: 7px;
      background: transparent;
      cursor: pointer;
      font: inherit;
      padding: 0;
      text-align: left;
    }
    .inline-search-title {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: #343A40;
      font-size: 12.5px;
      font-weight: 580;
      line-height: 1.4;
    }
    .inline-search-title::before {
      content: "";
      width: 7px;
      height: 7px;
      border-right: 1.5px solid #737A82;
      border-bottom: 1.5px solid #737A82;
      transform: rotate(-45deg);
      transition: transform 160ms ease, border-color 160ms ease;
    }
    .inline-search-toggle[aria-expanded="true"] .inline-search-title::before {
      border-color: var(--ink);
      transform: rotate(45deg);
    }
    .inline-search-toggle:hover .inline-search-title {
      color: var(--ink);
    }
    .inline-search-toggle:focus-visible {
      outline: 2px solid rgba(186, 77, 15, 0.36);
      outline-offset: 3px;
    }
    .inline-search-optional {
      color: var(--dim);
      font-size: 9.5px;
      font-weight: 500;
    }
    .inline-search-panel {
      display: grid;
      gap: 8px;
      padding-top: 2px;
    }
    .inline-search-section .choice {
      min-height: 52px;
      padding: 9px 64px 9px 12px;
    }
    .inline-search-section .choice.active {
      border-color: #D9DCDE;
      background: #F7F8F7;
      box-shadow: none;
    }
    .inline-search-section .search-provider-billing {
      top: 11px;
      right: 12px;
    }
    .inline-search-section .search-provider-billing.free {
      color: var(--accent-deep);
    }
    .inline-search-section .search-paid-toggle {
      min-height: 32px;
    }
    .search-provider-group-label {
      margin: 0 4px -2px;
      color: var(--dim);
      font-size: 10.5px;
      font-weight: 520;
      line-height: 1.4;
    }
    .search-paid-disclosure {
      display: grid;
      gap: 8px;
    }
    .search-paid-toggle {
      appearance: none;
      width: 100%;
      min-height: 38px;
      display: flex;
      align-items: center;
      gap: 9px;
      border: 0;
      background: transparent;
      color: var(--muted);
      font: inherit;
      font-size: 11.5px;
      font-weight: 520;
      padding: 0 4px;
      text-align: left;
    }
    .search-paid-toggle::before {
      content: "";
      width: 7px;
      height: 7px;
      border-right: 2px solid #7A818A;
      border-bottom: 2px solid #7A818A;
      transform: rotate(-45deg);
      transition: transform 180ms ease, border-color 180ms ease;
    }
    .search-paid-toggle[aria-expanded="true"]::before {
      border-color: var(--ink);
      transform: rotate(45deg);
    }
    .search-paid-toggle:hover {
      color: var(--ink);
    }
    .search-paid-count {
      color: var(--dim);
      font-size: 10px;
      font-weight: 450;
    }
    .search-paid-panel {
      display: grid;
      gap: 8px;
    }
    .search-provider-option {
      display: grid;
      gap: 8px;
    }
    .search-provider-option .choice {
      width: 100%;
    }
    .search-key-field {
      padding: 0 4px 4px;
    }
    label {
      display: grid;
      gap: 7px;
      color: var(--muted);
      font-size: 11.5px;
      font-weight: 540;
    }
    .field-label-text {
      display: inline-flex;
      align-items: baseline;
      gap: 3px;
      width: fit-content;
    }
    .required-marker {
      color: #C2382E;
      font-size: 12px;
      font-weight: 620;
      line-height: 1;
    }
    input, select {
      width: 100%;
      min-height: 40px;
      border: 1px solid #D9DCDE;
      border-radius: 8px;
      background: #FCFCFB;
      color: var(--ink);
      font: inherit;
      font-size: 13px;
      font-weight: 450;
      padding: 0 12px;
      outline: none;
    }
    input:focus, select:focus {
      border-color: #B8A69A;
      box-shadow: 0 0 0 3px rgba(186, 77, 15, 0.07);
    }
    input[aria-invalid="true"] {
      border-color: #C2382E;
      box-shadow: 0 0 0 3px rgba(194, 56, 46, 0.08);
    }
    .api-key-field {
      display: grid;
      gap: 7px;
    }
    .api-key-head {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) auto;
      align-items: center;
      gap: 14px;
      padding-left: 11px;
    }
    .api-key-label {
      display: inline-flex;
      align-items: baseline;
      color: var(--muted);
      font-size: 11.5px;
      font-weight: 540;
    }
    .field-error {
      min-height: 16px;
      margin-top: -1px;
      color: #B42318;
      font-size: 11px;
      font-weight: 560;
      line-height: 1.4;
    }
    .field-error:empty {
      display: none;
    }
    .model-config {
      display: grid;
    }
    .model-summary {
      min-height: 42px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      border-radius: 8px;
      background: #F7F8F7;
      padding: 8px 11px;
    }
    .model-summary-copy {
      min-width: 0;
      display: flex;
      align-items: baseline;
      gap: 8px;
    }
    .model-summary-label {
      flex: 0 0 auto;
      color: var(--dim);
      font-size: 11.5px;
      font-weight: 500;
    }
    .model-summary-value {
      min-width: 0;
      overflow: hidden;
      color: #343A40;
      font-size: 11.5px;
      font-weight: 540;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .model-summary-edit,
    .model-editor-done {
      appearance: none;
      min-height: 28px;
      flex: 0 0 auto;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: #6D5547;
      cursor: pointer;
      font: inherit;
      font-size: 10.5px;
      font-weight: 520;
      padding: 0 7px;
      transition: background 150ms ease, color 150ms ease;
    }
    .model-summary-edit:hover,
    .model-editor-done:hover:not(:disabled) {
      background: rgba(186, 77, 15, 0.06);
      color: var(--accent-deep);
    }
    .model-summary-edit:focus-visible,
    .model-editor-done:focus-visible {
      outline: 2px solid rgba(186, 77, 15, 0.36);
      outline-offset: 1px;
    }
    .model-summary-edit {
      width: 28px;
      height: 28px;
      color: #7A818A;
      padding: 0;
    }
    .model-summary-edit:hover {
      background: #ECEEEE;
      color: #50575F;
    }
    .model-summary-edit:focus-visible {
      outline-color: rgba(86, 93, 102, 0.36);
    }
    .model-summary-edit svg {
      width: 14px;
      height: 14px;
      display: block;
      margin: auto;
      fill: none;
      stroke: currentColor;
      stroke-linecap: round;
      stroke-linejoin: round;
      stroke-width: 1.7;
    }
    .model-editor {
      display: grid;
      gap: 7px;
    }
    .model-editor-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .model-editor-head label {
      display: inline-flex;
    }
    .model-editor-done:disabled {
      cursor: not-allowed;
      opacity: 0.4;
    }
	    details {
	      border: 1px solid #e2e0da;
	      border-radius: 8px;
	      background: rgba(255,255,255,0.46);
	      padding: 11px 13px;
    }
    summary {
      color: #656b64;
      cursor: pointer;
      font-size: 12px;
	      font-weight: 600;
	    }
	    .endpoint-panel {
	      border: 0;
	      border-radius: 7px;
	      background: transparent;
	      overflow: hidden;
	    }
	    .endpoint-panel.open {
	      background: transparent;
	      box-shadow: none;
	    }
	    .endpoint-summary {
	      appearance: none;
	      width: 100%;
	      min-height: 42px;
	      display: flex;
	      align-items: center;
	      gap: 9px;
	      border: 0;
	      background: transparent;
	      color: var(--muted);
	      cursor: pointer;
	      font: inherit;
	      font-size: 11.5px;
	      font-weight: 520;
	      padding: 0 4px;
	      text-align: left;
	    }
	    .endpoint-summary::before {
	      content: "";
	      width: 7px;
	      height: 7px;
	      border-right: 2px solid #747a73;
	      border-bottom: 2px solid #747a73;
	      transform: rotate(-45deg);
	      transition: transform 180ms ease, border-color 180ms ease;
	    }
	    .endpoint-panel.open .endpoint-summary::before {
	      border-color: var(--ink);
	      transform: rotate(45deg);
	    }
	    .endpoint-summary:focus-visible {
	      outline: none;
	      box-shadow: inset 0 0 0 3px rgba(186, 77, 15, 0.12);
	    }
	    .endpoint-content {
	      display: grid;
	      grid-template-rows: 0fr;
	      opacity: 0;
	      transform: translateY(-4px);
	      transition: grid-template-rows 220ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity 160ms ease, transform 220ms cubic-bezier(0.2, 0.8, 0.2, 1);
	    }
	    .endpoint-panel.open .endpoint-content {
	      grid-template-rows: 1fr;
	      opacity: 1;
	      transform: translateY(0);
	    }
	    .endpoint-content-clip {
	      min-height: 0;
	      overflow: hidden;
	    }
	    .endpoint-fields {
	      padding: 4px 4px 4px 20px;
	    }
	    .field-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
    .actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 40px;
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .actions > button { flex: 0 0 auto; }
    .submit-status {
      flex: 1 1 auto;
      min-width: 0;
      padding: 0 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      text-align: center;
    }
    button {
      min-height: 36px;
      border: 1px solid transparent;
      cursor: pointer;
      font-size: 13px;
      font-weight: 570;
      padding: 0 15px;
    }
    .secondary {
      background: transparent;
      color: var(--muted);
    }
    .secondary:hover { color: var(--ink); }
    .primary {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      background: var(--primary);
      border-radius: 8px;
      color: #fff;
      box-shadow: none;
      min-width: 112px;
    }
    .primary:hover {
      background: var(--primary-hover);
    }
    .primary:disabled { opacity: 0.55; cursor: not-allowed; }
    .primary.is-loading::before {
      width: 12px;
      height: 12px;
      flex: 0 0 auto;
      border: 2px solid rgba(255, 255, 255, 0.45);
      border-top-color: #fff;
      border-radius: 50%;
      content: "";
      animation: onboarding-submit-spin 700ms linear infinite;
    }
    @keyframes onboarding-submit-spin {
      to { transform: rotate(360deg); }
    }
    .error {
      min-height: 18px;
      color: #b42318;
      font-size: 12px;
      font-weight: 750;
    }
    .error:empty {
      display: none;
    }
    @media (max-width: 680px) {
      main {
        padding: 14px;
        overflow: auto;
      }
      .topbar { width: 100%; }
      .deck { width: 100%; }
      .setup-card { position: relative; min-height: 620px; height: auto; padding: 24px 20px; }
      .provider, .field-pair { grid-template-columns: 1fr; gap: 4px; }
      .provider-feature { grid-template-columns: 1fr; }
      .provider-feature-cta { width: 100%; }
      .provider-promo-copy {
        grid-column: 1 / -1;
        grid-row: 2;
        flex-wrap: wrap;
        white-space: normal;
      }
      .api-key-head {
        grid-template-columns: auto minmax(0, 1fr);
        row-gap: 7px;
      }
      .provider-promo-cta {
        grid-column: 2;
        grid-row: 1;
        justify-self: end;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
      }
      .primary.is-loading::before {
        animation: none !important;
      }
    }
  </style>
</head>
<body>
  <main>
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>OpenSquilla</span></div>
      <label class="language-picker" for="onboardingLocale">
        <span class="sr-only" data-i18n="onboarding.language.label">${ot('onboarding.language.label')}</span>
        <select id="onboardingLocale" aria-label="${ot('onboarding.aria.language')}" data-i18n-aria="onboarding.aria.language">
          ${localeOptionsHtml()}
        </select>
      </label>
    </header>
    <form id="setup-form" class="deck">
      <section class="setup-card active" data-screen="1">
        <header class="card-head">
          <h2 data-i18n="onboarding.step2.heading">${ot('onboarding.step2.heading')}</h2>
          <p data-i18n="onboarding.step2.subtitle">${ot('onboarding.step2.subtitle')}</p>
        </header>
        <div class="card-body">
        <input id="provider" type="hidden" value="tokenrhythm" />
        <input id="routerMode" type="hidden" value="disabled" />
        <input id="modelRoutingMode" type="hidden" value="direct" />
        <div class="provider-select-field">
          <div class="provider-combobox" id="providerCombobox">
            <button class="provider-combobox-toggle" id="providerSelectToggle" type="button" role="combobox" aria-expanded="false" aria-haspopup="listbox" aria-controls="providerSelectPanel" aria-labelledby="providerSelectLabel providerSelectValue">
              <span class="provider-combobox-label" id="providerSelectLabel" data-i18n="onboarding.nav.provider.title">${ot('onboarding.nav.provider.title')}</span>
              <span class="provider-combobox-value" id="providerSelectValue"></span>
              <span class="provider-selected-badges" id="providerSelectedBadges"></span>
              <span class="provider-combobox-chevron" aria-hidden="true"></span>
            </button>
            <div class="provider-select-panel" id="providerSelectPanel" hidden>
              <div class="provider-options" id="providerOptions" role="listbox" aria-labelledby="providerSelectLabel"></div>
            </div>
          </div>
        </div>
        <div class="api-key-field">
          <div class="api-key-head">
            <label class="api-key-label" for="apiKey">
              <span class="field-label-text"><span data-i18n="onboarding.step2.apiKey">${ot('onboarding.step2.apiKey')}</span><span class="required-marker" id="apiKeyRequiredMarker" aria-hidden="true">*</span></span>
            </label>
            <div class="provider-promo-copy">
              <strong data-i18n="onboarding.step2.tokenrhythmTitle">${ot('onboarding.step2.tokenrhythmTitle')}</strong>
              <span data-i18n="onboarding.step2.tokenrhythmRegistration">${ot('onboarding.step2.tokenrhythmRegistration')}</span>
            </div>
            <a class="provider-promo-cta" id="tokenrhythmRegister" href="${TOKENRHYTHM_REGISTER_URL}" target="_blank" rel="noopener noreferrer" data-i18n="onboarding.step2.tokenrhythmCta" data-i18n-aria="onboarding.step2.tokenrhythmCtaExternalLabel" aria-label="${ot('onboarding.step2.tokenrhythmCtaExternalLabel')}">${ot('onboarding.step2.tokenrhythmCta')}</a>
          </div>
          <input id="apiKey" name="apiKey" type="password" autocomplete="off" placeholder="sk-..." aria-describedby="apiKeyError" />
          <span class="field-error" id="apiKeyError" role="alert" aria-live="polite"></span>
        </div>
        <input id="baseUrl" name="baseUrl" type="hidden" />
        <div class="model-config" id="modelConfig">
          <div class="model-summary" id="modelSummary">
            <div class="model-summary-copy">
              <span class="model-summary-label" id="modelSummaryLabel"></span>
              <strong class="model-summary-value" id="modelSummaryValue"></strong>
            </div>
            <button class="model-summary-edit" id="modelEditToggle" type="button"></button>
          </div>
          <div class="model-editor" id="modelEditor" hidden>
            <div class="model-editor-head">
              <label for="model">
                <span class="field-label-text"><span data-i18n="onboarding.step2.directModel">${ot('onboarding.step2.directModel')}</span><span class="required-marker" id="modelRequiredMarker" aria-hidden="true" hidden>*</span></span>
              </label>
              <button class="model-editor-done" id="modelEditDone" type="button"></button>
            </div>
            <input id="model" name="model" autocomplete="off" placeholder="Auto" aria-describedby="modelError" />
            <span class="field-error" id="modelError" role="alert" aria-live="polite"></span>
          </div>
        </div>
        <section class="inline-search-section" aria-labelledby="inlineSearchHeading">
          <button class="inline-search-toggle" id="inlineSearchToggle" type="button" aria-expanded="false" aria-controls="inlineSearchPanel">
            <span class="inline-search-title" id="inlineSearchHeading" data-i18n="onboarding.step5.heading">${ot('onboarding.step5.heading')}</span>
            <span class="inline-search-optional" data-i18n="onboarding.step5.badge">${ot('onboarding.step5.badge')}</span>
          </button>
          <div class="inline-search-panel" id="inlineSearchPanel" hidden>
            <div class="search-provider-list" id="searchProviderGrid" role="radiogroup" aria-label="${ot('onboarding.aria.searchProvider')}" data-i18n-aria="onboarding.aria.searchProvider"></div>
            <input id="searchProvider" type="hidden" value="duckduckgo" />
            <div id="searchKeyParking" hidden>
              <label class="search-key-field" id="searchKeyLabel" for="searchApiKey" hidden>
                <span class="field-label-text"><span data-i18n="onboarding.step5.searchKey">${ot('onboarding.step5.searchKey')}</span><span class="required-marker" aria-hidden="true">*</span></span>
                <input id="searchApiKey" name="searchApiKey" type="password" autocomplete="off" placeholder="SEARCH_API_KEY" aria-describedby="searchApiKeyError" aria-required="true" />
                <span class="field-error" id="searchApiKeyError" role="alert" aria-live="polite"></span>
              </label>
            </div>
          </div>
        </section>
        </div>
        <footer class="actions">
          <button class="secondary" type="button" id="cancel" data-i18n="onboarding.step1.quit">${ot('onboarding.step1.quit')}</button>
          <span class="submit-status" id="submitStatus" role="status" aria-live="polite" aria-atomic="true"></span>
          <button class="primary" type="button" id="finish" data-i18n="onboarding.step5.finish">${ot('onboarding.step5.finish')}</button>
        </footer>
      </section>
      <div class="error" id="error" role="alert" aria-live="assertive" tabindex="-1"></div>
    </form>
  </main>
  <script>
    const desktopMessages = ${inlineScriptJson(DESKTOP_MESSAGES)};
    const onboardingMessageCatalog = ${inlineScriptJson(ONBOARDING_SCRIPT_MESSAGES)};
    const searchNoteCatalog = ${inlineScriptJson(SEARCH_PROVIDER_NOTE_MESSAGES)};
    let activeLocale = ${inlineScriptJson(desktopLocale)};
    let t = messagesFor(activeLocale);
    function messagesFor(locale) {
      return Object.assign({}, onboardingMessageCatalog.en, onboardingMessageCatalog[locale] || {});
    }
    function desktopMessage(locale, key) {
      return (desktopMessages[locale] && desktopMessages[locale][key]) || desktopMessages.en[key] || key;
    }
    function fmt(key, vars) {
      let out = t[key] != null ? String(t[key]) : key;
      if (vars) for (const name of Object.keys(vars)) out = out.split('{' + name + '}').join(String(vars[name]));
      return out;
    }
    const providers = ${inlineScriptJson(PROVIDER_CATALOG)};
    const searchProviders = ${inlineScriptJson(SEARCH_PROVIDER_CATALOG)};
    const routerProfiles = ${inlineScriptJson(ROUTER_PROFILES)};
    const initialProviderPrefill = ${inlineScriptJson(pendingProviderSetup)};
    let searchPaidOpen = false;
    let searchSectionOpen = false;
    let routerTiers = clone(routerProfiles.openrouter);
    let modelEditorOpen = false;
    let submitting = false;
    const SUBMIT_SLOW_FEEDBACK_MS = 8_000;
    let submitSlowTimer = null;
    const setupForm = document.getElementById('setup-form');
    const cardBody = document.querySelector('.card-body');
    const provider = document.getElementById('provider');
    const baseUrl = document.getElementById('baseUrl');
    const model = document.getElementById('model');
    const modelRoutingMode = document.getElementById('modelRoutingMode');
    const routerMode = document.getElementById('routerMode');
    const errorBox = document.getElementById('error');
    const apiKey = document.getElementById('apiKey');
    const apiKeyError = document.getElementById('apiKeyError');
    const apiKeyRequiredMarker = document.getElementById('apiKeyRequiredMarker');
    const modelError = document.getElementById('modelError');
    const modelRequiredMarker = document.getElementById('modelRequiredMarker');
    const modelSummary = document.getElementById('modelSummary');
    const modelSummaryLabel = document.getElementById('modelSummaryLabel');
    const modelSummaryValue = document.getElementById('modelSummaryValue');
    const modelEditToggle = document.getElementById('modelEditToggle');
    const modelEditor = document.getElementById('modelEditor');
    const modelEditDone = document.getElementById('modelEditDone');
    const searchApiKey = document.getElementById('searchApiKey');
    const searchApiKeyError = document.getElementById('searchApiKeyError');
    const finish = document.getElementById('finish');
    const submitStatus = document.getElementById('submitStatus');
    const searchProvider = document.getElementById('searchProvider');
	    const searchProviderGrid = document.getElementById('searchProviderGrid');
	    const searchKeyLabel = document.getElementById('searchKeyLabel');
	    const searchKeyParking = document.getElementById('searchKeyParking');
      const inlineSearchToggle = document.getElementById('inlineSearchToggle');
      const inlineSearchPanel = document.getElementById('inlineSearchPanel');
	    const onboardingLocale = document.getElementById('onboardingLocale');
    const providerCombobox = document.getElementById('providerCombobox');
    const providerSelectToggle = document.getElementById('providerSelectToggle');
    const providerSelectValue = document.getElementById('providerSelectValue');
    const providerSelectedBadges = document.getElementById('providerSelectedBadges');
    const providerSelectPanel = document.getElementById('providerSelectPanel');
    const providerOptions = document.getElementById('providerOptions');
    function clone(value) {
      return JSON.parse(JSON.stringify(value || {}));
    }
    function deepFreeze(value) {
      if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
      Object.values(value).forEach((item) => deepFreeze(item));
      return Object.freeze(value);
    }
    function onboardingPayloadSnapshot() {
      return deepFreeze({
        provider: provider.value,
        apiKey: apiKey.value,
        baseUrl: baseUrl.value,
        model: model.value,
        modelRoutingMode: modelRoutingMode.value,
        routerMode: routerMode.value,
        routerDefaultTier: 'c1',
        routerTiers: clone(routerTiers),
        searchProvider: searchProvider.value,
        searchApiKey: searchApiKey.value,
        locale: activeLocale,
      });
    }
    function currentProvider() {
      return providers.find((item) => item.id === provider.value) || providers[0];
    }
    function defaultModelRoutingModeFor(selected) {
      return selected.routerSupported ? 'squilla_router' : 'direct';
    }
    function syncRouterModeFromModelRouting() {
      routerMode.value = modelRoutingMode.value === 'direct' ? 'disabled' : 'recommended';
    }
    function renderModelField() {
      const value = model.value.trim();
      const showEditor = modelEditorOpen || !value;
      modelSummary.hidden = showEditor;
      modelEditor.hidden = !showEditor;
      modelSummaryLabel.textContent = t.recommendedModel;
      modelSummaryValue.textContent = value || t.noModel;
      modelEditToggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z"></path><path d="m13.5 6.5 3 3"></path></svg>';
      modelEditToggle.setAttribute('aria-label', t.editModel);
      modelEditToggle.setAttribute('title', t.editModel);
      modelEditDone.textContent = t.doneEditingModel;
      modelEditDone.disabled = !value;
    }
	    function profileKeyForMode() {
	      return provider.value;
	    }
	    function syncProviderDefaults(resetRouter) {
	      const selected = currentProvider();
	      apiKeyRequiredMarker.hidden = !selected.requiresApiKey;
	      apiKey.setAttribute('aria-required', String(selected.requiresApiKey));
	      apiKey.disabled = !selected.requiresApiKey;
	      apiKey.placeholder = selected.requiresApiKey ? 'sk-...' : t.noKeyRequired;
	      if (resetRouter) {
	        baseUrl.value = selected.baseUrl || '';
	        model.value = selected.model || '';
          modelEditorOpen = !model.value.trim();
	      } else {
	        if (!baseUrl.value && selected.baseUrl) baseUrl.value = selected.baseUrl;
	        if (!model.value && selected.model) model.value = selected.model;
	      }
	      if (resetRouter) {
	        modelRoutingMode.value = defaultModelRoutingModeFor(selected);
	        syncRouterModeFromModelRouting();
	        routerTiers = clone(routerProfiles[profileKeyForMode()]);
	      }
	      const modelRequired = modelRoutingMode.value === 'direct';
      modelRequiredMarker.hidden = !modelRequired;
      model.setAttribute('aria-required', String(modelRequired));
      renderModelField();
    }
    function selectProvider(nextProvider) {
      const next = nextProvider || 'tokenrhythm';
      // Re-clicking the active provider must preserve any endpoint overrides.
      if (next === provider.value) return;
      provider.value = next;
      apiKey.value = '';
      clearValidationErrors();
      syncProviderDefaults(true);
      renderProviderGrid();
      render();
    }
    function renderProviderGrid() {
      const selected = currentProvider();
      providerSelectValue.textContent = selected.label;
      providerSelectedBadges.innerHTML = '';
      renderProviderOptions();
    }
    function providerBadgesHtml(item) {
      if (!item || item.id !== 'tokenrhythm') return '';
      return '<span class="provider-badge free">' + escapeHtml(t.limitedFreeBadge) + '</span>';
    }
    function providerGroupFor(item) {
      if (item.id === 'tokenrhythm') return 'recommended';
      return item.deployment === 'local' ? 'local' : 'cloud';
    }
    function renderProviderOptions() {
      const groups = [
        { id: 'recommended', label: t.providerGroupRecommended },
        { id: 'cloud', label: t.providerGroupCloud },
        { id: 'local', label: t.providerGroupLocal },
      ];
      const html = groups.map((group) => {
        const items = providers.filter((item) => providerGroupFor(item) === group.id);
        if (!items.length) return '';
        return '<div class="provider-option-group" data-provider-group="' + group.id + '">'
          + '<div class="provider-option-group-label">' + escapeHtml(group.label) + '</div>'
          + items.map((item) => {
            const isSelected = item.id === provider.value;
            return '<button class="provider-option" type="button" role="option" data-provider-option="' + escapeAttr(item.id) + '" aria-selected="' + String(isSelected) + '">'
              + '<span class="provider-option-label">' + escapeHtml(item.label) + '</span>'
              + '<span class="provider-option-badges">' + providerBadgesHtml(item) + '</span>'
              + '<span class="provider-option-check" aria-hidden="true">' + (isSelected ? '✓' : '') + '</span>'
              + '</button>';
          }).join('')
          + '</div>';
      }).join('');
      providerOptions.innerHTML = html;
      providerOptions.querySelectorAll('[data-provider-option]').forEach((option) => {
        option.addEventListener('click', () => {
          selectProvider(option.dataset.providerOption);
          setProviderPickerOpen(false);
          providerSelectToggle.focus();
        });
        option.addEventListener('keydown', (event) => {
          const options = Array.from(providerOptions.querySelectorAll('[data-provider-option]'));
          const index = options.indexOf(option);
          if (event.key === 'ArrowDown' && options[index + 1]) {
            event.preventDefault();
            options[index + 1].focus();
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            (options[index - 1] || providerSelectToggle).focus();
          } else if (event.key === 'Escape') {
            event.preventDefault();
            setProviderPickerOpen(false);
            providerSelectToggle.focus();
          }
        });
      });
    }
    function setProviderPickerOpen(open) {
      providerSelectToggle.setAttribute('aria-expanded', String(open));
      providerSelectPanel.hidden = !open;
      if (open) {
        renderProviderOptions();
        const selectedOption = providerOptions.querySelector('[aria-selected="true"]');
        (selectedOption || providerOptions.querySelector('[data-provider-option]'))?.focus();
      }
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    }
    function escapeAttr(value) {
      return escapeHtml(value);
    }
    function applyLocale(nextLocale) {
      const locale = desktopMessages[nextLocale] ? nextLocale : 'en';
      activeLocale = locale;
      t = messagesFor(locale);
      document.documentElement.lang = locale;
      document.title = desktopMessage(locale, 'onboarding.title');
      document.querySelectorAll('[data-i18n]').forEach((element) => {
        element.textContent = desktopMessage(locale, element.dataset.i18n);
      });
      document.querySelectorAll('[data-i18n-aria]').forEach((element) => {
        element.setAttribute('aria-label', desktopMessage(locale, element.dataset.i18nAria));
      });
      clearValidationErrors();
      renderProviderGrid();
      renderSearchProviderGrid();
      syncProviderDefaults(false);
      renderModelField();
      render();
    }
    function currentSearchProvider() {
      return searchProviders.find((item) => item.providerId === searchProvider.value) || searchProviders[0];
    }
    function searchProviderNote(item) {
      return (searchNoteCatalog[activeLocale] && searchNoteCatalog[activeLocale][item.providerId])
        || (searchNoteCatalog.en && searchNoteCatalog.en[item.providerId])
        || item.note
        || (item.requiresApiKey ? t.requiresApiKey : t.noKeyRequired);
    }
    function renderSearchProviderGrid() {
      searchKeyParking.appendChild(searchKeyLabel);
      searchKeyLabel.hidden = true;
      const renderSearchChoice = (item) => (
        '<div class="search-provider-option" data-search-provider-option="' + escapeAttr(item.providerId) + '">' +
        '<button class="choice' + (item.providerId === searchProvider.value ? ' active' : '') + '" type="button" data-search-provider="' + escapeAttr(item.providerId) + '" aria-pressed="' + String(item.providerId === searchProvider.value) + '">' +
        '<strong>' + escapeHtml(item.label) + '</strong><small>' + escapeHtml(searchProviderNote(item)) + '</small>' +
        '<span class="search-provider-billing ' + (item.requiresApiKey ? 'paid' : 'free') + '">' + escapeHtml(item.requiresApiKey ? t.billingPaid : t.billingFree) + '</span>' +
        '</button></div>'
      );
      const freeProviders = searchProviders.filter((item) => !item.requiresApiKey);
      const paidProviders = searchProviders.filter((item) => item.requiresApiKey);
      searchProviderGrid.innerHTML =
        '<div class="search-provider-group-label">' + escapeHtml(t.searchFreeGroup) + '</div>' +
        freeProviders.map(renderSearchChoice).join('') +
        '<div class="search-paid-disclosure">' +
          '<button class="search-paid-toggle" id="searchPaidToggle" type="button" aria-expanded="' + String(searchPaidOpen) + '" aria-controls="searchPaidPanel">' +
            '<span>' + escapeHtml(t.searchPaidGroup) + '</span><span class="search-paid-count">' + paidProviders.length + '</span>' +
          '</button>' +
          '<div class="search-paid-panel" id="searchPaidPanel"' + (searchPaidOpen ? '' : ' hidden') + '>' +
            paidProviders.map(renderSearchChoice).join('') +
          '</div>' +
        '</div>';
      document.getElementById('searchPaidToggle').addEventListener('click', () => {
        searchPaidOpen = !searchPaidOpen;
        renderSearchProviderGrid();
        render();
      });
      searchProviderGrid.querySelectorAll('[data-search-provider]').forEach((button) => {
        button.addEventListener('click', () => {
          searchProvider.value = button.dataset.searchProvider || 'duckduckgo';
          if (currentSearchProvider().requiresApiKey) searchPaidOpen = true;
          clearFieldError(searchApiKey, searchApiKeyError);
          renderSearchProviderGrid();
          render();
        });
      });
    }
    function syncSearchProviderControls() {
      const selected = currentSearchProvider();
      const selectedButton = searchProviderGrid.querySelector('[data-search-provider="' + selected.providerId + '"]');
      const selectedOption = selectedButton && selectedButton.closest('.search-provider-option');
      if (selected.requiresApiKey && selectedOption) {
        selectedOption.appendChild(searchKeyLabel);
        searchKeyLabel.hidden = false;
      } else {
        searchKeyParking.appendChild(searchKeyLabel);
        searchKeyLabel.hidden = true;
      }
      const input = document.getElementById('searchApiKey');
      if (input) input.placeholder = selected.keyPlaceholder || selected.envKey || 'SEARCH_API_KEY';
    }
    function setSearchSectionOpen(open) {
      searchSectionOpen = Boolean(open);
      inlineSearchToggle.setAttribute('aria-expanded', String(searchSectionOpen));
      inlineSearchPanel.hidden = !searchSectionOpen;
    }
    function render() {
      syncProviderDefaults(false);
      syncSearchProviderControls();
    }
    function clearSubmitSlowTimer() {
      if (submitSlowTimer !== null) clearTimeout(submitSlowTimer);
      submitSlowTimer = null;
    }
    function setSubmitting(next) {
      clearSubmitSlowTimer();
      submitting = Boolean(next);
      if (submitting) setProviderPickerOpen(false);
      setupForm.setAttribute('aria-busy', String(submitting));
      finish.setAttribute('aria-busy', String(submitting));
      finish.disabled = submitting;
      finish.classList.toggle('is-loading', submitting);
      cardBody.inert = submitting;
      onboardingLocale.disabled = submitting;
      if (submitting) {
        finish.textContent = t.savingSetup;
        submitStatus.textContent = desktopMessage(activeLocale, 'boot.profile');
        submitSlowTimer = setTimeout(() => {
          submitSlowTimer = null;
          if (submitting) submitStatus.textContent = t.setupTakingLonger;
        }, SUBMIT_SLOW_FEEDBACK_MS);
      } else {
        finish.textContent = desktopMessage(activeLocale, 'onboarding.step5.finish');
        submitStatus.textContent = '';
      }
    }
    window.addEventListener('pagehide', clearSubmitSlowTimer);
	    onboardingLocale.addEventListener('change', () => {
	      applyLocale(onboardingLocale.value);
	    });
      providerSelectToggle.addEventListener('click', () => {
        setProviderPickerOpen(providerSelectToggle.getAttribute('aria-expanded') !== 'true');
      });
      providerSelectToggle.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          setProviderPickerOpen(true);
        }
      });
      inlineSearchToggle.addEventListener('click', () => {
        setSearchSectionOpen(!searchSectionOpen);
      });
      document.addEventListener('pointerdown', (event) => {
        if (!providerCombobox.contains(event.target)) setProviderPickerOpen(false);
      });
      function clearFieldError(input, output) {
        output.textContent = '';
        input.removeAttribute('aria-invalid');
      }
      function clearValidationErrors() {
        errorBox.textContent = '';
        clearFieldError(apiKey, apiKeyError);
        clearFieldError(model, modelError);
        clearFieldError(searchApiKey, searchApiKeyError);
      }
      function presentValidationIssue(issue) {
        issue.output.textContent = issue.message;
        issue.input.setAttribute('aria-invalid', 'true');
        if (issue.input === model) {
          modelEditorOpen = true;
          renderModelField();
        }
        if (issue.input === searchApiKey) {
          setSearchSectionOpen(true);
        }
        issue.input.focus({ preventScroll: false });
      }
	    function validateStep() {
      const selected = currentProvider();
      const selectedSearch = currentSearchProvider();
      if (selected.requiresApiKey && !apiKey.value.trim()) {
        return { input: apiKey, output: apiKeyError, message: fmt('apiKeyRequired', { label: selected.label }) };
      }
      if (modelRoutingMode.value === 'direct' && !model.value.trim()) {
        return { input: model, output: modelError, message: t.directModelRequiredDirect };
      }
      if (selectedSearch.requiresApiKey && !searchApiKey.value.trim()) {
        return { input: searchApiKey, output: searchApiKeyError, message: fmt('searchApiKeyRequired', { label: selectedSearch.label }) };
      }
      return null;
    }
    [[apiKey, apiKeyError], [model, modelError], [searchApiKey, searchApiKeyError]].forEach(([input, output]) => {
      input.addEventListener('input', () => {
        clearFieldError(input, output);
        if (input === model) renderModelField();
      });
    });
    modelEditToggle.addEventListener('click', () => {
      modelEditorOpen = true;
      renderModelField();
      model.focus({ preventScroll: true });
    });
    modelEditDone.addEventListener('click', () => {
      if (!model.value.trim()) return;
      modelEditorOpen = false;
      renderModelField();
      modelEditToggle.focus({ preventScroll: true });
    });
    document.getElementById('cancel').addEventListener('click', () => {
      window.opensquillaDesktop.cancelOnboarding();
    });
    finish.addEventListener('click', async () => {
      if (submitting) return;
      clearValidationErrors();
      const issue = validateStep();
      if (issue) {
        presentValidationIssue(issue);
        return;
      }
      const payload = onboardingPayloadSnapshot();
      setSubmitting(true);
      let succeeded = false;
      try {
        const result = await window.opensquillaDesktop.saveOnboarding(payload);
        if (!result || result.ok !== true) {
          throw new Error(
            result && result.error
              ? String(result.error)
              : 'OpenSquilla setup could not be saved.',
          );
        }
        succeeded = true;
        clearSubmitSlowTimer();
      } catch (error) {
        errorBox.textContent = error && error.message ? error.message : String(error);
      } finally {
        if (!succeeded) {
          setSubmitting(false);
          errorBox.focus({ preventScroll: false });
        }
      }
    });
    function applyMigrationPrefill(prefill) {
      if (!prefill || typeof prefill !== 'object') return;
      const nextProvider = String(prefill.provider || '').trim().toLowerCase();
      if (nextProvider && nextProvider !== provider.value) {
        provider.value = nextProvider;
        syncProviderDefaults(true);
      }
      if (prefill.baseUrl) baseUrl.value = String(prefill.baseUrl);
      if (prefill.model) model.value = String(prefill.model);
      if (prefill.apiKey) document.getElementById('apiKey').value = String(prefill.apiKey);
      syncProviderDefaults(false);
      modelEditorOpen = !model.value.trim();
      renderModelField();
      renderProviderGrid();
      render();
    }
    renderProviderGrid();
    renderSearchProviderGrid();
    setSearchSectionOpen(false);
    syncProviderDefaults(true);
    applyMigrationPrefill(initialProviderPrefill);
    render();
  </script>
</body>
</html>`
}

function abandonOnboardingFlow(
  flow: OnboardingFlow,
  error: Error,
  closeWindow: boolean,
): void {
  if (!onboardingFlows.abandon(flow)) return
  const reject = flow.reject
  flow.resolve = null
  flow.reject = null
  const window = flow.window
  if (closeWindow && window && !window.isDestroyed()) window.close()
  reject?.(error)
}

async function runOnboarding(): Promise<DesktopConnection> {
  // A detached save retains coordinator ownership until it settles, preventing
  // Retry from opening a replacement flow that the old completion could affect.
  await onboardingFlows.waitForAbandonedSave()
  const pendingProviderSetup = await loadPendingMigrationProviderSetup()
  const existing = await loadDesktopCredential()
  // A saved credential encrypted with the OS keychain that this session cannot
  // read (keychain locked or unavailable) must not be treated as "no credential":
  // silently re-onboarding would re-save the key as plaintext. Surface an
  // actionable error so the user can unlock and retry, or Reset setup.
  if (
    desktopSecretStoragePolicyBackend() === 'safeStorage'
    && desktopSecretStorageBackend() !== 'safeStorage'
    && (
      pendingProviderSetup !== null
      || Boolean(existing?.encryptedApiKey && existing.encryption === 'safeStorage')
    )
  ) {
    throw new Error(
      'OpenSquilla needs the OS keychain to read or safely adopt this credential, '
      + 'but the keychain is currently unavailable. Unlock it and reopen '
      + 'OpenSquilla, or use "Reset setup" to start over.'
    )
  }
  if (!pendingProviderSetup && existing && isConnectionReady(existing)) {
    // Seed the gateway config from the saved credential only when it does not
    // exist yet. Once it exists the Control UI owns it via RPC, so regenerating
    // it from the credential template here would clobber provider/router/channel
    // edits made live in Settings on every boot.
    if (!(await pathExists(desktopConfigPath()))) {
      await writeDesktopConfig(existing)
    }
    return existing
  }

  return new Promise((resolveCredential, rejectCredential) => {
    if (onboardingFlows.active) {
      focusOnboardingWindow()
      rejectCredential(new Error('OpenSquilla setup is already in progress.'))
      return
    }
    const parentWindow = currentMainWindow()
    const window = new BrowserWindow({
      width: 1040,
      height: 820,
      minWidth: 900,
      minHeight: 720,
      title: desktopT('window.onboarding'),
      icon: appIconPath(),
      resizable: true,
      parent: parentWindow ?? undefined,
      modal: Boolean(parentWindow),
      show: false,
      // Match the onboarding page's base so the first frame is not white.
      backgroundColor: '#f5f2eb',
      webPreferences: {
        preload: join(__dirname, 'preload.cjs'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
      },
    })
    const flow: OnboardingFlow = {
      window,
      state: 'editing',
      resolve: resolveCredential,
      reject: rejectCredential,
      savePayload: null,
      savePromise: null,
    }
    if (!onboardingFlows.activate(flow)) {
      window.destroy()
      focusOnboardingWindow()
      rejectCredential(new Error('OpenSquilla setup is already in progress.'))
      return
    }
    onboardingWindow = window
    installEditingContextMenu(window)

    window.webContents.setWindowOpenHandler(({ url }) => {
      if (url === TOKENRHYTHM_REGISTER_URL) {
        void shell.openExternal(TOKENRHYTHM_REGISTER_URL)
      }
      return { action: 'deny' }
    })

    // The wizard is a single data: URL page; block any renderer-initiated
    // top-frame navigation (e.g. a dropped file/link) so it can't replace the
    // onboarding UI — which holds the preload IPC bridge — with a foreign document.
    const guardOnboardingNavigation = (event: Electron.Event, targetUrl: string) => {
      event.preventDefault()
      if (targetUrl === TOKENRHYTHM_REGISTER_URL) {
        void shell.openExternal(TOKENRHYTHM_REGISTER_URL)
      }
    }
    window.webContents.on('will-navigate', guardOnboardingNavigation)
    window.webContents.on('will-redirect', guardOnboardingNavigation)
    // Rebuild the app menu so View → Reload is disabled while onboarding is open.
    createApplicationMenu()

    window.once('ready-to-show', () => {
      if (flow.window !== window || window.isDestroyed() || flow.state === 'abandoned') return
      window.show()
      window.focus()
    })
    window.on('closed', () => {
      if (onboardingWindow === window) onboardingWindow = null
      if (flow.window === window) flow.window = null
      // Re-enable View → Reload now that the wizard is gone.
      createApplicationMenu()
      if (flow.state !== 'completed' && flow.state !== 'abandoned') {
        abandonOnboardingFlow(flow, new Error('OpenSquilla setup was closed.'), false)
      }
    })

    window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(onboardingHtml(
      pendingProviderSetup,
    ))}`).catch((error) => {
      abandonOnboardingFlow(
        flow,
        error instanceof Error ? error : new Error(String(error)),
        true,
      )
    })
  })
}

async function pathExists(path: string): Promise<boolean> {
  try {
    await access(path, constants.F_OK)
    return true
  } catch {
    return false
  }
}

async function pathIsFile(path: string): Promise<boolean> {
  try {
    return (await stat(path)).isFile()
  } catch {
    return false
  }
}

async function assertRepoRoot(): Promise<void> {
  const pyprojectPath = join(repoRoot, 'pyproject.toml')
  const webuiPath = join(repoRoot, 'src', 'opensquilla', 'gateway', 'static', 'dist', 'index.html')
  if (!(await pathExists(pyprojectPath))) {
    throw new Error(`OpenSquilla checkout not found at ${repoRoot}`)
  }
  if (!(await pathExists(webuiPath))) {
    throw new Error(
      `Built Control UI not found at ${webuiPath}. Run "cd opensquilla-webui && npm run build" first.`
    )
  }
}

function packagedRuntimeRoot(): string {
  if (app.isPackaged) return join(process.resourcesPath, 'runtime')
  return join(packageRoot, 'runtime')
}

function pathDelimiter(): string {
  return process.platform === 'win32' ? ';' : ':'
}

function splitPathValue(value?: string): string[] {
  return (value || '').split(pathDelimiter()).filter(Boolean)
}

function desktopChildPath(): string {
  const currentPath = process.env.PATH || process.env.Path || ''
  const currentParts = splitPathValue(currentPath)
  // GUI-launched processes can have a sparse PATH. Keep host directories after
  // the inherited PATH; optional Runtime Packs are resolved inside Gateway per
  // Safe/Full/Guest policy and are never injected statically by Electron.
  const hostFallbackParts = process.platform === 'win32'
    ? [
        process.env.LOCALAPPDATA ? join(process.env.LOCALAPPDATA, 'Programs', 'nodejs') : '',
        process.env.ProgramFiles ? join(process.env.ProgramFiles, 'nodejs') : '',
        process.env['ProgramFiles(x86)'] ? join(process.env['ProgramFiles(x86)'], 'nodejs') : '',
      ]
    : [
        join(app.getPath('home'), '.local', 'bin'),
        join(app.getPath('home'), '.npm-global', 'bin'),
        '/opt/homebrew/bin',
        '/usr/local/bin',
        '/usr/bin',
        '/bin',
        '/usr/sbin',
        '/sbin',
      ]
  const orderedParts = [...currentParts, ...hostFallbackParts]
  const seen = new Set<string>()
  const merged = orderedParts.filter((part) => {
    if (!part || seen.has(part)) return false
    seen.add(part)
    return true
  })
  return merged.join(pathDelimiter())
}

async function resolveGatewayRuntime(): Promise<RuntimeLaunch> {
  const binaryName = process.platform === 'win32' ? 'opensquilla-gateway.exe' : 'opensquilla-gateway'
  const runtimeRoot = join(packagedRuntimeRoot(), 'gateway')
  const onedirBinary = join(runtimeRoot, 'opensquilla-gateway', binaryName)
  const flatBinary = join(runtimeRoot, binaryName)
  const bundledBinary = (await pathIsFile(onedirBinary)) ? onedirBinary : flatBinary
  if (await pathIsFile(bundledBinary)) {
    return {
      command: bundledBinary,
      args: ['gateway', 'run'],
      cwd: dirname(bundledBinary),
      mode: 'bundled',
    }
  }

  await assertRepoRoot()
  return {
    command: 'uv',
    args: ['run', 'opensquilla', 'gateway', 'run'],
    cwd: repoRoot,
    mode: 'dev',
  }
}

const ONBOARDING_PROBE_STDOUT_LIMIT = 256 * 1024
const ONBOARDING_PROBE_TIMEOUT_MS = 40_000

function onboardingProbeString(value: unknown, maxLength: number): string {
  return typeof value === 'string' ? value.trim().slice(0, maxLength) : ''
}

async function probeOnboardingProvider(
  payload: OnboardingProbePayload,
): Promise<OnboardingProbeResult> {
  const providerId = onboardingProbeString(payload?.provider, 120).toLowerCase()
  const selected = PROVIDER_CATALOG.find((entry) => entry.id === providerId)
  if (!selected) {
    return {
      ok: false,
      failureKind: 'invalid_config',
      message: 'Choose a supported provider.',
      latencyMs: 0,
    }
  }
  const model = onboardingProbeString(payload?.model, 500)
  if (!model) {
    return {
      ok: false,
      failureKind: 'invalid_config',
      message: 'Enter a model before verifying the configuration.',
      latencyMs: 0,
    }
  }
  const apiKey = onboardingProbeString(payload?.apiKey, 64 * 1024)
  if (selected.requiresApiKey && !apiKey) {
    return {
      ok: false,
      failureKind: 'auth_invalid',
      message: `Enter the ${selected.label} API key before verifying the configuration.`,
      latencyMs: 0,
    }
  }

  const runtime = await resolveGatewayRuntime()
  const prefix = runtime.args.slice(0, -2)
  const input = JSON.stringify({
    providerId,
    model,
    apiKey,
    baseUrl: onboardingProbeString(payload?.baseUrl, 2_048) || selected.baseUrl,
    timeout: 30,
  })
  const activeProfile = activeDesktopProfile()

  return await new Promise<OnboardingProbeResult>((resolveResult, rejectResult) => {
    const child = spawn(runtime.command, [...prefix, 'models', 'probe-draft'], {
      cwd: runtime.cwd,
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: desktopChildEnvironment(activeProfile, {
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
      }),
    })
    let stdout = ''
    let settled = false
    const timeout = setTimeout(() => {
      child.kill()
      finish(new Error('Configuration verification timed out.'))
    }, ONBOARDING_PROBE_TIMEOUT_MS)
    timeout.unref()

    const finish = (error?: Error, result?: OnboardingProbeResult) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      if (error) rejectResult(error)
      else resolveResult(result as OnboardingProbeResult)
    }

    child.once('error', (error) => finish(error))
    child.stdin.once('error', () => {})
    child.stdin.end(input)
    child.stdout.on('data', (chunk) => {
      if (stdout.length > ONBOARDING_PROBE_STDOUT_LIMIT) return
      stdout += String(chunk)
      if (stdout.length > ONBOARDING_PROBE_STDOUT_LIMIT) {
        child.kill()
        finish(new Error('Configuration verification returned too much data.'))
      }
    })
    // Provider and runtime diagnostics may contain local details. The renderer
    // receives only the parsed, redacted JSON protocol from stdout.
    child.stderr.resume()
    child.once('close', () => {
      if (settled) return
      try {
        const parsed = JSON.parse(stdout) as Record<string, unknown>
        const ok = parsed.ok === true
        finish(undefined, {
          ok,
          failureKind: ok ? '' : onboardingProbeString(parsed.kind, 120) || 'probe_failed',
          message: ok ? '' : onboardingProbeString(parsed.detail, 2_000) || 'Configuration verification failed.',
          latencyMs: Number.isFinite(Number(parsed.latency_ms))
            ? Math.max(0, Math.round(Number(parsed.latency_ms)))
            : 0,
        })
      } catch {
        finish(new Error('Configuration verification did not return a valid result.'))
      }
    })
  })
}

const RECOVERY_PROTOCOL_SCHEMA_VERSION = 1
const RECOVERY_STDOUT_LIMIT = 2 * 1024 * 1024
const RECOVERY_COMMAND_TIMEOUT_MS = 60_000
// Mutating recovery commands fail closed with profile_lock_busy the moment
// another writer holds the profile locks. A short in-CLI wait lets a transient
// writer (an exiting gateway, a cron tick) finish instead of stranding startup
// on the manual recovery page.
const RECOVERY_LOCK_TIMEOUT_SECONDS = 5
const RECOVERY_OUTCOMES = new Set<RecoveryOutcome>([
  'ready',
  'attention',
  'recovery_required',
])

function recoveryRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function parseRecoveryProtocol(value: unknown): RecoveryProtocolResult {
  const record = recoveryRecord(value)
  if (!record) throw new Error('Recovery command returned an invalid protocol object.')
  const outcome = String(record.outcome || '') as RecoveryOutcome
  if (record.schema_version !== RECOVERY_PROTOCOL_SCHEMA_VERSION) {
    throw new Error('Recovery command returned an unsupported protocol schema.')
  }
  if (!RECOVERY_OUTCOMES.has(outcome)) {
    throw new Error('Recovery command returned an invalid outcome.')
  }
  if (typeof record.stable_code !== 'string' || !record.stable_code) {
    throw new Error('Recovery command omitted its stable code.')
  }
  if (typeof record.primary_home !== 'string' || !record.primary_home) {
    throw new Error('Recovery command omitted its primary home.')
  }
  if (record.effective_workspace !== null && typeof record.effective_workspace !== 'string') {
    throw new Error('Recovery command returned an invalid workspace.')
  }
  if (!Array.isArray(record.candidates) || !Array.isArray(record.allowed_actions)) {
    throw new Error('Recovery command returned invalid recovery actions.')
  }
  const candidates = record.candidates.map((value) => {
    const candidate = recoveryRecord(value)
    if (
      !candidate
      || typeof candidate.kind !== 'string'
      || typeof candidate.path !== 'string'
      || typeof candidate.exists !== 'boolean'
      || typeof candidate.valid !== 'boolean'
      || typeof candidate.configured !== 'boolean'
    ) {
      throw new Error('Recovery command returned an invalid workspace candidate.')
    }
    return {
      kind: candidate.kind,
      path: candidate.path,
      exists: candidate.exists,
      valid: candidate.valid,
      configured: candidate.configured,
      ...(typeof candidate.identity === 'string' ? { identity: candidate.identity } : {}),
      ...(typeof candidate.modified_at_ns === 'number'
        ? { modified_at_ns: candidate.modified_at_ns }
        : {}),
    }
  })
  const allowedActions = record.allowed_actions.map((action) => {
    if (typeof action !== 'string') throw new Error('Recovery command returned an invalid action.')
    return action
  })
  if (record.transaction_id !== null && typeof record.transaction_id !== 'string') {
    throw new Error('Recovery command returned an invalid transaction id.')
  }
  if (!Number.isSafeInteger(record.revision) || Number(record.revision) < 0) {
    throw new Error('Recovery command returned an invalid revision.')
  }
  // Older CLIs omit detail; absence and null both mean "no diagnosis".
  if (
    record.detail !== undefined
    && record.detail !== null
    && typeof record.detail !== 'string'
  ) {
    throw new Error('Recovery command returned an invalid detail.')
  }
  return {
    schema_version: RECOVERY_PROTOCOL_SCHEMA_VERSION,
    outcome,
    stable_code: record.stable_code,
    primary_home: record.primary_home,
    effective_workspace: record.effective_workspace as string | null,
    candidates,
    allowed_actions: allowedActions,
    transaction_id: record.transaction_id as string | null,
    revision: Number(record.revision),
    detail: typeof record.detail === 'string' ? record.detail : null,
  }
}

const DESKTOP_PROFILE_CONSOLIDATION_OUTCOMES = new Set<DesktopProfileConsolidationOutcome>([
  'noop',
  'consolidated',
  'blocked',
])
const DESKTOP_CREDENTIAL_ADOPTION_STATUSES = new Set<DesktopCredentialAdoptionStatus>([
  'pending',
  'complete',
  'not_required',
])

function parseDesktopProfileConsolidationProtocol(
  value: unknown,
): DesktopProfileConsolidationResult {
  const record = recoveryRecord(value)
  if (!record || record.schema_version !== 1) {
    throw new Error('Desktop profile consolidation returned an unsupported protocol schema.')
  }
  const outcome = String(record.outcome || '') as DesktopProfileConsolidationOutcome
  if (!DESKTOP_PROFILE_CONSOLIDATION_OUTCOMES.has(outcome)) {
    throw new Error('Desktop profile consolidation returned an invalid outcome.')
  }
  if (typeof record.stable_code !== 'string' || !record.stable_code) {
    throw new Error('Desktop profile consolidation omitted its stable code.')
  }
  if (typeof record.primary_home !== 'string' || !record.primary_home) {
    throw new Error('Desktop profile consolidation omitted the primary home.')
  }
  const credentialAdoptionStatus = String(
    record.credential_adoption_status || '',
  ) as DesktopCredentialAdoptionStatus
  if (!DESKTOP_CREDENTIAL_ADOPTION_STATUSES.has(credentialAdoptionStatus)) {
    throw new Error('Desktop profile consolidation returned an invalid credential adoption status.')
  }
  const nullableStringFields = [
    'configuration_source_recovery_id',
    'configuration_source_credential_path',
    'configuration_source_credential_sha256',
    'backup_path',
    'receipt_path',
  ] as const
  for (const field of nullableStringFields) {
    if (record[field] !== null && typeof record[field] !== 'string') {
      throw new Error(`Desktop profile consolidation returned an invalid ${field}.`)
    }
  }
  const sourceRecoveryId = record.configuration_source_recovery_id as string | null
  const sourceCredentialPath = record.configuration_source_credential_path as string | null
  const sourceCredentialSha256 = record.configuration_source_credential_sha256 as string | null
  const sourceCredentialSize = record.configuration_source_credential_size
  if (
    (sourceRecoveryId !== null && !isRecoveryProfileId(sourceRecoveryId))
    || (sourceCredentialPath !== null && sourceRecoveryId === null)
    || (
      sourceCredentialSha256 !== null
      && !/^[0-9a-f]{64}$/.test(sourceCredentialSha256)
    )
    || (
      sourceCredentialSize !== null
      && (
        !Number.isSafeInteger(sourceCredentialSize)
        || Number(sourceCredentialSize) < 0
      )
    )
  ) {
    throw new Error('Desktop profile consolidation returned an invalid credential source.')
  }
  if (!Array.isArray(record.consumed_recovery_ids)) {
    throw new Error('Desktop profile consolidation returned invalid consumed profile ids.')
  }
  const consumedRecoveryIds = record.consumed_recovery_ids.map((item) => {
    if (!isRecoveryProfileId(item)) {
      throw new Error('Desktop profile consolidation returned an invalid consumed profile id.')
    }
    return item
  })
  if (new Set(consumedRecoveryIds).size !== consumedRecoveryIds.length) {
    throw new Error('Desktop profile consolidation returned duplicate consumed profile ids.')
  }
  if (
    sourceRecoveryId !== null
    && !consumedRecoveryIds.some((item) => (
      item.toLowerCase() === sourceRecoveryId.toLowerCase()
    ))
  ) {
    throw new Error('Desktop profile consolidation returned an unconsumed configuration source.')
  }
  if (!Number.isSafeInteger(record.revision) || Number(record.revision) < 0) {
    throw new Error('Desktop profile consolidation returned an invalid revision.')
  }
  if (
    !Array.isArray(record.errors)
    || record.errors.some((item) => typeof item !== 'string')
  ) {
    throw new Error('Desktop profile consolidation returned invalid errors.')
  }
  const backupPath = record.backup_path as string | null
  const receiptPath = record.receipt_path as string | null
  if (
    (backupPath === null) !== (receiptPath === null)
    || (sourceCredentialPath !== null && backupPath === null)
    || (
      sourceCredentialPath === null
      && (sourceCredentialSha256 !== null || sourceCredentialSize !== null)
    )
    || (
      sourceCredentialPath !== null
      && (sourceCredentialSha256 === null || sourceCredentialSize === null)
    )
    || (
      credentialAdoptionStatus === 'pending'
      && (sourceCredentialPath === null || backupPath === null)
    )
    || (
      credentialAdoptionStatus === 'not_required'
      && sourceCredentialPath !== null
    )
    || (
      outcome === 'consolidated'
      && (
        backupPath === null
        || consumedRecoveryIds.length === 0
        || (
          sourceCredentialPath === null
            ? credentialAdoptionStatus !== 'not_required'
            : credentialAdoptionStatus !== 'pending'
        )
      )
    )
    || (
      outcome === 'blocked'
      && (
        sourceRecoveryId !== null
        || sourceCredentialPath !== null
        || consumedRecoveryIds.length > 0
        || backupPath !== null
        || credentialAdoptionStatus !== 'not_required'
      )
    )
  ) {
    throw new Error('Desktop profile consolidation returned inconsistent outcome metadata.')
  }
  return {
    schema_version: 1,
    outcome,
    stable_code: record.stable_code,
    primary_home: record.primary_home,
    configuration_source_recovery_id: sourceRecoveryId,
    configuration_source_credential_path: sourceCredentialPath,
    configuration_source_credential_sha256: sourceCredentialSha256,
    configuration_source_credential_size: (
      sourceCredentialSize === null ? null : Number(sourceCredentialSize)
    ),
    consumed_recovery_ids: consumedRecoveryIds,
    backup_path: backupPath,
    receipt_path: receiptPath,
    credential_adoption_status: credentialAdoptionStatus,
    revision: Number(record.revision),
    errors: record.errors as string[],
  }
}

function requirePlainConsolidationDirectory(path: string, label: string): string {
  try {
    const info = lstatSync(path)
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('unsafe directory')
    return realpathSync(path)
  } catch {
    throw new Error(`Desktop profile consolidation returned an unsafe ${label}.`)
  }
}

function requirePlainConsolidationFile(path: string, label: string): string {
  try {
    const info = lstatSync(path)
    if (!info.isFile() || info.isSymbolicLink()) throw new Error('unsafe file')
    return realpathSync(path)
  } catch {
    throw new Error(`Desktop profile consolidation returned an unsafe ${label}.`)
  }
}

function validateDesktopProfileConsolidationPaths(
  result: DesktopProfileConsolidationResult,
  primary: DesktopProfilePaths,
): void {
  if (!resolvedPathsEqual(result.primary_home, primary.home)) {
    throw new Error('Desktop profile consolidation returned a different primary home.')
  }
  // Historical receipt metadata is informational once credential adoption is
  // acknowledged. In particular, a user may delete an archived credential or
  // backup after the primary has become authoritative; startup must not depend
  // on that archive forever. A pending noop is different: it is the durable
  // retry record for a crash between consolidation and credential adoption, so
  // its archive must pass the same boundary checks as a fresh consolidation.
  if (
    result.outcome !== 'consolidated'
    && result.credential_adoption_status !== 'pending'
  ) return
  if (result.backup_path === null) return
  if (result.receipt_path === null) {
    throw new Error('Desktop profile consolidation omitted its receipt path.')
  }

  const userData = app.getPath('userData')
  const backups = join(userData, 'backups')
  const consolidationRoot = join(backups, 'profile-consolidation')
  const transactionId = basename(result.backup_path)
  if (
    !isRecoveryProfileId(transactionId)
    || !resolvedPathsEqual(dirname(result.backup_path), consolidationRoot)
    || !resolvedPathsEqual(result.receipt_path, join(result.backup_path, 'receipt.json'))
  ) {
    throw new Error('Desktop profile consolidation returned an unexpected backup path.')
  }

  const userDataReal = requirePlainConsolidationDirectory(userData, 'userData root')
  const backupsReal = requirePlainConsolidationDirectory(backups, 'backup root')
  const consolidationRootReal = requirePlainConsolidationDirectory(
    consolidationRoot,
    'profile consolidation root',
  )
  const backupReal = requirePlainConsolidationDirectory(
    result.backup_path,
    'profile consolidation backup',
  )
  const receiptReal = requirePlainConsolidationFile(
    result.receipt_path,
    'profile consolidation receipt',
  )
  if (
    !resolvedPathsEqual(dirname(backupsReal), userDataReal)
    || !resolvedPathsEqual(dirname(consolidationRootReal), backupsReal)
    || !resolvedPathsEqual(dirname(backupReal), consolidationRootReal)
    || !resolvedPathsEqual(dirname(receiptReal), backupReal)
  ) {
    throw new Error('Desktop profile consolidation backup escaped its trusted root.')
  }
}

async function runDesktopProfileConsolidationCli(
  profile: DesktopProfilePaths,
  commandArgs: string[] = [
    'consolidate-profiles',
    '--user-data', app.getPath('userData'),
    '--primary-home', profile.home,
    '--json',
  ],
): Promise<DesktopProfileConsolidationResult> {
  const runtime = await resolveGatewayRuntime()
  const prefix = runtime.args.slice(0, -2)
  return await new Promise((resolveResult, rejectResult) => {
    const child = spawn(runtime.command, [
      ...prefix,
      'recovery',
      ...commandArgs,
    ], {
      cwd: runtime.cwd,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: desktopChildEnvironment(profile, {
        OPENSQUILLA_RECOVERY_OFFLINE: '1',
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
      }),
    })
    let stdout = ''
    let oversized = false
    let settled = false
    const finish = (
      error?: Error,
      result?: DesktopProfileConsolidationResult,
    ) => {
      if (settled) return
      settled = true
      if (error) rejectResult(error)
      else resolveResult(result as DesktopProfileConsolidationResult)
    }
    child.stdout.on('data', (chunk) => {
      if (oversized) return
      stdout += String(chunk)
      if (stdout.length > RECOVERY_STDOUT_LIMIT) oversized = true
    })
    // The protocol result is the only trusted diagnostic surface. stderr can
    // contain local profile paths and is deliberately drained without exposure.
    child.stderr.resume()
    child.once('error', (error) => finish(
      error instanceof Error ? error : new Error(String(error)),
    ))
    child.once('close', (code) => {
      if (oversized) {
        return finish(new Error('Desktop profile consolidation output exceeded its limit.'))
      }
      let result: DesktopProfileConsolidationResult
      try {
        result = parseDesktopProfileConsolidationProtocol(JSON.parse(stdout))
      } catch (error) {
        if (code !== 0) {
          return finish(new Error(
            `Desktop profile consolidation failed with exit code ${code ?? 'unknown'}.`,
          ))
        }
        return finish(error instanceof Error ? error : new Error(String(error)))
      }
      const expectedExitCode = result.outcome === 'blocked' ? 2 : 0
      if (code !== expectedExitCode) {
        return finish(new Error(
          `Desktop profile consolidation returned ${result.outcome} with exit code ${code ?? 'unknown'}.`,
        ))
      }
      return finish(undefined, result)
    })
  })
}

async function acknowledgeConsolidatedDesktopCredential(
  consolidation: DesktopProfileConsolidationResult,
): Promise<void> {
  if (
    consolidation.credential_adoption_status !== 'pending'
    || consolidation.backup_path === null
    || consolidation.receipt_path === null
  ) {
    throw new Error('Desktop credential adoption acknowledgement was not pending.')
  }
  const primary = primaryDesktopProfile()
  const transactionId = basename(consolidation.backup_path)
  const acknowledged = await runDesktopProfileConsolidationCli(primary, [
    'acknowledge-profile-credential',
    '--user-data', app.getPath('userData'),
    '--primary-home', primary.home,
    '--transaction-id', transactionId,
    '--json',
  ])
  validateDesktopProfileConsolidationPaths(acknowledged, primary)
  const sourceIdsMatch = (
    acknowledged.configuration_source_recovery_id?.toLowerCase()
    === consolidation.configuration_source_recovery_id?.toLowerCase()
  )
  const sourcePathsMatch = (
    acknowledged.configuration_source_credential_path !== null
    && consolidation.configuration_source_credential_path !== null
    && resolvedPathsEqual(
      acknowledged.configuration_source_credential_path,
      consolidation.configuration_source_credential_path,
    )
  )
  const sourceIntegrityMatches = (
    acknowledged.configuration_source_credential_sha256
      === consolidation.configuration_source_credential_sha256
    && acknowledged.configuration_source_credential_size
      === consolidation.configuration_source_credential_size
  )
  const consumedIdsMatch = (
    acknowledged.consumed_recovery_ids.length === consolidation.consumed_recovery_ids.length
    && acknowledged.consumed_recovery_ids.every((item, index) => (
      item.toLowerCase() === consolidation.consumed_recovery_ids[index]?.toLowerCase()
    ))
  )
  if (
    acknowledged.outcome !== 'noop'
    || acknowledged.credential_adoption_status !== 'complete'
    || acknowledged.backup_path === null
    || acknowledged.receipt_path === null
    || !resolvedPathsEqual(acknowledged.backup_path, consolidation.backup_path)
    || !resolvedPathsEqual(acknowledged.receipt_path, consolidation.receipt_path)
    || !sourceIdsMatch
    || !sourcePathsMatch
    || !sourceIntegrityMatches
    || !consumedIdsMatch
    || acknowledged.revision !== consolidation.revision
  ) {
    throw new Error('Desktop credential adoption acknowledgement changed its receipt metadata.')
  }
}

function recoveryFailureResult(home: string, stableCode: string): RecoveryProtocolResult {
  return {
    schema_version: RECOVERY_PROTOCOL_SCHEMA_VERSION,
    outcome: 'recovery_required',
    stable_code: stableCode,
    primary_home: home,
    effective_workspace: null,
    candidates: [],
    allowed_actions: [
      'show-backups',
      'copy-diagnostics',
    ],
    transaction_id: null,
    revision: 0,
    detail: null,
  }
}

async function runRecoveryCli(
  profile: DesktopProfilePaths,
  commandArgs: string[],
  stdinPayload?: string,
  writerReserved = false,
): Promise<RecoveryProtocolResult> {
  const mutating = commandArgs[0] !== 'inspect'
  const kindAwareCommands = new Set(['inspect', 'reconcile', 'choose-workspace'])
  const effectiveArgs = kindAwareCommands.has(commandArgs[0] || '')
    && !commandArgs.includes('--profile-kind')
    ? [...commandArgs, '--profile-kind', 'desktop-primary']
    : commandArgs
  const finishWriter = mutating && !recoveryOperationBusy && !writerReserved
    ? beginDesktopWriterOperation(`recovery ${commandArgs[0] || 'operation'}`)
    : () => {}
  try {
    const runtime = await resolveGatewayRuntime()
    const prefix = runtime.args.slice(0, -2)
    return await new Promise((resolveResult, rejectResult) => {
      const child = spawn(runtime.command, [...prefix, 'recovery', ...effectiveArgs], {
        cwd: runtime.cwd,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: desktopChildEnvironment(profile, {
          OPENSQUILLA_RECOVERY_OFFLINE: '1',
          PYTHONUNBUFFERED: '1',
          PYTHONUTF8: '1',
          PYTHONIOENCODING: 'utf-8:replace',
        }),
      })
      let stdout = ''
      let oversized = false
      let settled = false
      let timeout: NodeJS.Timeout | null = null
      const finish = (error?: Error, result?: RecoveryProtocolResult) => {
        if (settled) return
        settled = true
        if (timeout) clearTimeout(timeout)
        if (error) rejectResult(error)
        else resolveResult(result as RecoveryProtocolResult)
      }
      if (!mutating) {
        timeout = setTimeout(() => {
          child.kill()
          finish(new Error('Recovery command timed out.'))
        }, RECOVERY_COMMAND_TIMEOUT_MS)
        timeout.unref()
      }
      child.stdin.once('error', () => {})
      child.stdin.end(stdinPayload ?? '')
      child.stdout.on('data', (chunk) => {
        if (oversized) return
        stdout += String(chunk)
        if (stdout.length > RECOVERY_STDOUT_LIMIT) {
          oversized = true
          if (!mutating) child.kill()
        }
      })
      // The renderer receives only parsed protocol JSON. stderr is drained but
      // never copied into diagnostics because it may contain local details.
      child.stderr.resume()
      child.once('error', (error) => finish(
        error instanceof Error ? error : new Error(String(error)),
      ))
      child.once('close', (code) => {
        if (oversized) return finish(new Error('Recovery command output exceeded its limit.'))
        try {
          return finish(undefined, parseRecoveryProtocol(JSON.parse(stdout)))
        } catch (error) {
          if (code !== 0) {
            return finish(new Error(`Recovery command failed with exit code ${code ?? 'unknown'}.`))
          }
          return finish(error instanceof Error ? error : new Error(String(error)))
        }
      })
    })
  } finally {
    finishWriter()
  }
}

async function inspectDesktopProfile(profile: DesktopProfilePaths): Promise<RecoveryProtocolResult> {
  try {
    return await runRecoveryCli(profile, ['inspect', '--home', profile.home, '--json'])
  } catch (error) {
    desktopLog('recovery_inspect_failed', {
      profileKind: profile.kind,
      error: error instanceof Error ? error.message : 'unknown error',
    })
    return recoveryFailureResult(
      profile.home,
      'desktop_recovery_inspect_failed',
    )
  }
}

async function recoverInspectedProfileTransaction(
  profile: DesktopProfilePaths,
  inspection: RecoveryProtocolResult,
): Promise<RecoveryProtocolResult> {
  if (
    !inspection.allowed_actions.includes('recover-transaction')
    || !inspection.transaction_id
  ) {
    throw new Error('The interrupted profile operation cannot be recovered automatically.')
  }
  return await runRecoveryCli(profile, [
    'recover-transaction',
    '--home', profile.home,
    '--transaction-id', inspection.transaction_id,
    '--expected-revision', String(inspection.revision),
    '--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS),
    '--json',
  ])
}

async function readVerifiedConsolidatedCredential(
  path: string,
  expectedRealPath: string,
  expectedSha256: string,
  expectedSize: number,
): Promise<string> {
  const handle = await open(path, 'r')
  let raw: Buffer
  try {
    const before = await handle.stat()
    if (!before.isFile() || before.size !== expectedSize) {
      throw new Error('The archived Desktop credential no longer matches its receipt.')
    }
    raw = await handle.readFile()
    const after = await handle.stat()
    if (
      before.dev !== after.dev
      || before.ino !== after.ino
      || before.mode !== after.mode
      || before.size !== after.size
      || before.mtimeMs !== after.mtimeMs
    ) {
      throw new Error('The archived Desktop credential changed while it was being read.')
    }
  } finally {
    await handle.close()
  }
  const currentRealPath = requirePlainConsolidationFile(
    path,
    'archived Desktop credential',
  )
  const digest = createHash('sha256').update(raw).digest('hex')
  if (
    !resolvedPathsEqual(currentRealPath, expectedRealPath)
    || raw.length !== expectedSize
    || digest !== expectedSha256
  ) {
    throw new Error('The archived Desktop credential no longer matches its receipt.')
  }
  return raw.toString('utf8')
}

async function adoptConsolidatedDesktopCredential(
  consolidation: DesktopProfileConsolidationResult,
): Promise<void> {
  // A fresh consolidation and a noop returned from an explicitly pending
  // receipt are both eligible. Completed/no-op receipts are never replayed, so
  // deleting a primary credential later cannot resurrect an archived secret.
  if (consolidation.credential_adoption_status !== 'pending') return
  const sourceRecoveryId = consolidation.configuration_source_recovery_id
  const sourceCredentialPath = consolidation.configuration_source_credential_path
  const sourceCredentialSha256 = consolidation.configuration_source_credential_sha256
  const sourceCredentialSize = consolidation.configuration_source_credential_size
  if (
    sourceRecoveryId === null
    || sourceCredentialPath === null
    || sourceCredentialSha256 === null
    || sourceCredentialSize === null
    || consolidation.backup_path === null
  ) {
    throw new Error('Desktop profile consolidation returned an incomplete credential source.')
  }

  const expectedSourcePath = join(
    consolidation.backup_path,
    'recovery-profiles',
    sourceRecoveryId,
    'desktop-credential.json',
  )
  if (!resolvedPathsEqual(sourceCredentialPath, expectedSourcePath)) {
    throw new Error('Desktop profile consolidation returned an unexpected credential path.')
  }
  const recoveryContainer = join(consolidation.backup_path, 'recovery-profiles')
  const recoveryRoot = join(recoveryContainer, sourceRecoveryId)
  const backupReal = requirePlainConsolidationDirectory(
    consolidation.backup_path,
    'profile consolidation backup',
  )
  const recoveryContainerReal = requirePlainConsolidationDirectory(
    recoveryContainer,
    'archived recovery container',
  )
  const recoveryRootReal = requirePlainConsolidationDirectory(
    recoveryRoot,
    'archived recovery profile',
  )
  const sourceReal = requirePlainConsolidationFile(
    sourceCredentialPath,
    'archived Desktop credential',
  )
  if (
    !resolvedPathsEqual(dirname(recoveryContainerReal), backupReal)
    || !resolvedPathsEqual(dirname(recoveryRootReal), recoveryContainerReal)
    || !resolvedPathsEqual(dirname(sourceReal), recoveryRootReal)
    || !resolvedPathsEqual(sourceReal, expectedSourcePath)
  ) {
    throw new Error('The consolidated Desktop credential changed before it could be adopted.')
  }
  // Bind credential adoption to the exact bytes recorded by the offline
  // consolidation receipt. Parsing/decryption errors in those verified bytes
  // can fall back to onboarding; an integrity mismatch must remain pending and
  // block adoption instead of silently acknowledging a different secret.
  const sourceCredential = await readVerifiedConsolidatedCredential(
    sourceCredentialPath,
    sourceReal,
    sourceCredentialSha256,
    sourceCredentialSize,
  )

  const finishWriter = beginDesktopWriterOperation('adopt consolidated Desktop credential')
  try {
    const primary = primaryDesktopProfile()
    const currentCredential = await readOptionalDesktopText(primary.credentialPath)
    let disposition: 'adopted' | 'primary_exists' | 'source_unusable' | null = null
    if (
      currentCredential !== null
      && desktopCredentialHasUserConfiguration(currentCredential)
    ) {
      // A primary credential that appeared while consolidation ran is
      // authoritative; path validation above still runs before we acknowledge
      // the historical source.
      disposition = 'primary_exists'
    } else {
      let credential: DesktopConnection | null = null
      let credentialPhase: 'parse' | 'decrypt' = 'parse'
      try {
        const raw = sourceCredential
        const parsed = recoveryRecord(JSON.parse(raw))
        if (!parsed) throw new Error('credential is not an object')
        const stringFields = [
          'provider',
          'model',
          'baseUrl',
          'apiKeyEnv',
          'encryptedApiKey',
          'modelRoutingMode',
          'routerMode',
          'routerDefaultTier',
          'searchProvider',
          'searchApiKeyEnv',
          'encryptedSearchApiKey',
          'encryption',
          'configAuthority',
          'importTransactionId',
          'createdAt',
          'updatedAt',
        ] as const
        if (stringFields.some((field) => (
          Object.prototype.hasOwnProperty.call(parsed, field)
          && typeof parsed[field] !== 'string'
        ))) {
          throw new Error('credential contains an invalid string field')
        }
        if (
          Object.prototype.hasOwnProperty.call(parsed, 'disableNetworkObservability')
          && typeof parsed.disableNetworkObservability !== 'boolean'
        ) {
          throw new Error('credential contains an invalid boolean field')
        }
        if (
          Object.prototype.hasOwnProperty.call(parsed, 'routerTiers')
          && !recoveryRecord(parsed.routerTiers)
        ) {
          throw new Error('credential contains invalid router tiers')
        }
        const candidateCredential = normalizeDesktopCredential(
          parsed as Partial<DesktopConnection>,
        )
        credentialPhase = 'decrypt'
        // Validate OS-keychain ciphertext before publishing it at the primary path.
        // Unusable historical secrets are skipped so normal onboarding can collect
        // a fresh credential instead of permanently blocking every startup.
        if (
          candidateCredential.encryptedApiKey
          && !decryptApiKey(candidateCredential)
        ) {
          throw new Error('provider credential decrypted to an empty value')
        }
        if (
          candidateCredential.encryptedSearchApiKey
          && !decryptSearchApiKey(candidateCredential)
        ) {
          throw new Error('search credential decrypted to an empty value')
        }
        // Publish eligibility is assigned only after both safeStorage/plaintext
        // validations complete. A decryption exception must leave this null so
        // the historical source is skipped rather than copied into primary.
        credential = candidateCredential
      } catch {
        const stableCode = credentialPhase === 'parse'
          ? 'archived_credential_invalid'
          : 'archived_credential_decryption_failed'
        desktopLog('desktop_profile_consolidation_credential_skipped', {
          sourceRecoveryId,
          stableCode,
        })
        disposition = 'source_unusable'
      }

      if (credential !== null) {
        const expectedConfig = await readOptionalDesktopText(join(primary.home, 'config.toml'))
        if (expectedConfig === null) {
          // A credential-only legacy recovery has no profile config authority
          // to preserve. Generate the canonical primary config and publish it
          // with the credential through the existing paired settings
          // transaction, so a crash cannot expose only one half.
          credential = normalizeDesktopCredential({
            ...credential,
            configAuthority: 'generated',
            importTransactionId: '',
          })
          await applyDesktopSettingsPair(
            primary,
            credential,
            JSON.stringify(credential, null, 2),
            currentCredential,
            true,
          )
        } else {
          const inspection = await inspectDesktopProfile(primary)
          if (inspection.outcome === 'recovery_required' || !inspection.transaction_id) {
            throw new Error(
              `The consolidated primary config is not ready for credential adoption (${inspection.stable_code}).`,
            )
          }
          const result = await runRecoveryCli(
            primary,
            [
              'apply-settings',
              '--home', primary.home,
              '--transaction-id', inspection.transaction_id,
              '--expected-revision', String(inspection.revision),
              '--json',
            ],
            JSON.stringify({
              expected_config: expectedConfig,
              config: expectedConfig,
              expected_credential: currentCredential,
              credential: JSON.stringify(credential, null, 2),
            }),
            true,
          )
          if (result.outcome === 'recovery_required') {
            throw new Error(
              `The consolidated Desktop credential was not adopted (${result.stable_code}).`,
            )
          }
        }
        // Force the normal loader to validate the bytes at their final primary path.
        if (!await loadDesktopCredential()) {
          throw new Error('The consolidated Desktop credential was not published.')
        }
        desktopLog('desktop_profile_consolidation_credential_adopted', {
          sourceRecoveryId,
        })
        disposition = 'adopted'
      }
    }

    if (disposition === null) {
      throw new Error('Desktop credential adoption did not reach a terminal disposition.')
    }
    await acknowledgeConsolidatedDesktopCredential(consolidation)
    desktopLog('desktop_profile_consolidation_credential_acknowledged', {
      sourceRecoveryId,
      disposition,
    })
  } finally {
    finishWriter()
  }
}

let desktopProfilesConsolidatedThisProcess = false
let desktopProfileConsolidationPromise: Promise<DesktopProfileConsolidationResult | null> | null = null
let pendingDesktopCredentialConsolidation: DesktopProfileConsolidationResult | null = null
let desktopProfileConsolidationMaintenance: DesktopProfileConsolidationMaintenance | null = null
let desktopProfileConsolidationFailureDetail = ''

// A blocked fan-in is maintenance, not a startup verdict. The independent
// primary inspector decides whether the product can open. This flag only stops
// window reopens from repeating the same maintenance command in one process; an
// explicit in-app repair clears it for one safe retry.
let desktopProfileConsolidationDeferredThisProcess = false

async function consolidateLegacyRecoveryProfilesBeforeStartup(
): Promise<DesktopProfileConsolidationResult | null> {
  if (desktopProfileConsolidationDeferredThisProcess) return null
  if (desktopProfilesConsolidatedThisProcess) return null
  if (desktopProfileConsolidationPromise) return await desktopProfileConsolidationPromise

  desktopProfileConsolidationPromise = (async () => {
    const exclusive = desktopWriters.tryBeginExclusive('consolidate legacy Desktop profiles')
    if (!exclusive) {
      throw new Error('OpenSquilla is finishing another profile operation. Try startup again.')
    }
    try {
      await waitForDesktopWriterOperations(1)
      const primary = primaryDesktopProfile()
      const recoveryProfiles = legacyRecoveryProfiles()
      // An Electron crash can leave a verified Gateway serving any historical
      // profile. Stop only instances proven by the owner record + HMAC challenge
      // before the offline fan-in takes source and target locks.
      for (const profile of [...recoveryProfiles, primary]) {
        await recoverVerifiedOrphanGatewayBeforeSpawn(profile)
      }
      const result = await runDesktopProfileConsolidationCli(primary)
      validateDesktopProfileConsolidationPaths(result, primary)
      desktopLog('desktop_profile_consolidation_completed', {
        outcome: result.outcome,
        stableCode: result.stable_code,
        recoveryProfileCount: recoveryProfiles.length,
        consumedRecoveryProfileCount: result.consumed_recovery_ids.length,
      })
      if (result.outcome === 'blocked') {
        return result
      }
      desktopProfileConsolidationMaintenance = null
      desktopProfileConsolidationFailureDetail = ''
      pendingDesktopCredentialConsolidation = (
        result.credential_adoption_status === 'pending'
      )
        ? result
        : null
      desktopProfilesConsolidatedThisProcess = true
      return null
    } finally {
      exclusive.finish()
      desktopWriters.reopen(exclusive.admissionToken)
    }
  })().finally(() => {
    desktopProfileConsolidationPromise = null
  })
  return await desktopProfileConsolidationPromise
}

function deferProfileConsolidationMaintenance(
  result: DesktopProfileConsolidationResult,
): void {
  desktopProfileConsolidationDeferredThisProcess = true
  desktopProfileConsolidationFailureDetail = (
    result.errors.find((value) => value.trim())?.trim() ?? ''
  )
  desktopProfileConsolidationMaintenance = {
    kind: 'profile-consolidation',
    stable_code: result.stable_code,
    retryable: true,
    recovery_profile_count: legacyRecoveryProfiles().length,
  }
  desktopLog('desktop_profile_consolidation_deferred', {
    stableCode: result.stable_code,
    detail: desktopProfileConsolidationFailureDetail,
    recoveryProfileCount: desktopProfileConsolidationMaintenance.recovery_profile_count,
  })
}

function recoveryStateSnapshot(): DesktopRecoveryViewState {
  return {
    inspection: recoveryInspection,
    maintenance: desktopProfileConsolidationMaintenance,
    blocked: recoveryInspection?.outcome === 'recovery_required',
    busy: recoveryOperationBusy,
    error: recoveryOperationError,
  }
}

function publishRecoveryState(): void {
  mainWindow?.webContents.send('desktop:recovery:state-changed', recoveryStateSnapshot())
}

function sanitizedRecoveryDiagnostics(): string {
  const report = recoveryInspection
  const redactPath = (value: string | null): string | null => {
    if (!value) return value
    const userData = app.getPath('userData')
    const home = homedir()
    if (value === userData || value.startsWith(`${userData}/`) || value.startsWith(`${userData}\\`)) {
      return `<USER_DATA>${value.slice(userData.length)}`
    }
    if (value === home || value.startsWith(`${home}/`) || value.startsWith(`${home}\\`)) {
      return `<HOME>${value.slice(home.length)}`
    }
    return '<EXTERNAL_PATH>'
  }
  const redactText = (value: string | undefined): string | null => {
    if (!value) return null
    return value
      .split(app.getPath('userData')).join('<USER_DATA>')
      .split(homedir()).join('<HOME>')
  }
  return JSON.stringify({
    schema_version: RECOVERY_PROTOCOL_SCHEMA_VERSION,
    app_version: app.getVersion(),
    platform: process.platform,
    profile_kind: 'primary',
    outcome: report?.outcome ?? 'recovery_required',
    stable_code: report?.stable_code ?? 'desktop_recovery_state_unavailable',
    maintenance: desktopProfileConsolidationMaintenance
      ? {
          ...desktopProfileConsolidationMaintenance,
          detail: redactText(desktopProfileConsolidationFailureDetail),
        }
      : null,
    primary_home: redactPath(report?.primary_home ?? primaryDesktopProfile().home),
    effective_workspace: redactPath(report?.effective_workspace ?? null),
    candidates: (report?.candidates ?? []).map((candidate) => ({
      kind: candidate.kind,
      path: redactPath(candidate.path),
      exists: candidate.exists,
      valid: candidate.valid,
      configured: candidate.configured,
    })),
    allowed_actions: report?.allowed_actions ?? [],
    transaction_id: report?.transaction_id ?? null,
    revision: report?.revision ?? 0,
  }, null, 2)
}

const GATEWAY_PORT_FIRST = 18791
const GATEWAY_PORT_LAST = 18830
let gatewayPortCursor = GATEWAY_PORT_FIRST

function explicitGatewayPort(): number | null {
  const envPort = Number(process.env.OPENSQUILLA_DESKTOP_GATEWAY_PORT || '')
  return Number.isInteger(envPort) && envPort > 0 ? envPort : null
}

function hasExplicitGatewayPort(): boolean {
  return explicitGatewayPort() !== null
}

function nextGatewayPortAfter(port: number): number {
  return port >= GATEWAY_PORT_LAST ? GATEWAY_PORT_FIRST : port + 1
}

function isPortBindable(port: number): Promise<boolean> {
  return new Promise((resolveBindable) => {
    const server = net.createServer()
    let settled = false
    const settle = (bindable: boolean) => {
      if (settled) return
      settled = true
      server.removeAllListeners()
      if (server.listening) {
        server.close(() => resolveBindable(bindable))
      } else {
        resolveBindable(bindable)
      }
    }
    server.once('error', () => settle(false))
    server.once('listening', () => settle(true))
    server.listen({ host: '127.0.0.1', port, exclusive: true })
  })
}

async function findGatewayPort(): Promise<number> {
  const envPort = explicitGatewayPort()
  if (envPort !== null) return envPort

  const portCount = GATEWAY_PORT_LAST - GATEWAY_PORT_FIRST + 1
  const startPort = Math.min(Math.max(gatewayPortCursor, GATEWAY_PORT_FIRST), GATEWAY_PORT_LAST)
  for (let offset = 0; offset < portCount; offset += 1) {
    const port = GATEWAY_PORT_FIRST + ((startPort - GATEWAY_PORT_FIRST + offset) % portCount)
    if (await isPortBindable(port)) {
      gatewayPortCursor = nextGatewayPortAfter(port)
      return port
    }
  }
  throw new Error('No free OpenSquilla desktop gateway port found in 18791-18830.')
}

async function healthCheck(url: string, timeoutMs = 1000): Promise<boolean> {
  try {
    const boundedTimeoutMs = Math.max(1, Math.min(1000, Math.floor(timeoutMs)))
    const response = await fetch(`${url}/healthz`, {
      signal: AbortSignal.timeout(boundedTimeoutMs),
    })
    if (!response.ok) return false
    const payload = await response.json().catch(() => null)
    return Boolean(payload && payload.ok === true)
  } catch {
    return false
  }
}

const GATEWAY_OUTPUT_TAIL_MAX_CHARS = 12_000
const NEWER_CONFIG_DIAGNOSTIC_FIELDS = [
  'llm_ensemble',
  'privacy',
  'sandbox.auto_setup',
  'llm_profiles',
] as const

function appendGatewayOutputTail(tail: string, chunk: Buffer | string): string {
  const next = tail + String(chunk)
  return next.length > GATEWAY_OUTPUT_TAIL_MAX_CHARS ? next.slice(-GATEWAY_OUTPUT_TAIL_MAX_CHARS) : next
}

function gatewayExitLooksLikeNewerConfig(output: string): boolean {
  const normalized = output.toLowerCase()
  const hasValidationSignal = (
    normalized.includes('validationerror') ||
    normalized.includes('extra_forbidden') ||
    normalized.includes('extra inputs are not permitted')
  )
  return hasValidationSignal && NEWER_CONFIG_DIAGNOSTIC_FIELDS.some((field) => normalized.includes(field))
}

function gatewayExitLooksLikePortInUse(output: string): boolean {
  return /OPENSQUILLA_GATEWAY_PORT_IN_USE/i.test(output)
    || /gateway could not start:.*is already in use/i.test(output)
    || /gateway port is already in use/i.test(output)
    || /:\d+\s+is already in use/i.test(output)
}

function gatewayExitLooksLikeProfileInUse(output: string): boolean {
  return /OPENSQUILLA_PROFILE_IN_USE/i.test(output)
}

function desktopGatewayStillRunningMessage(): string {
  return (
    'OPENSQUILLA_PROFILE_IN_USE: A previous Desktop Gateway has not exited. ' +
    'Wait for it to finish, or quit every OpenSquilla app or terminal using this profile, ' +
    'then try again. If it will not exit, restart the computer. ' +
    'Do not delete profile lock files.'
  )
}

function classifyGatewayExitMessage(message: string, outputTail: string): string {
  if (gatewayExitLooksLikeProfileInUse(outputTail)) {
    return (
      message +
      '\n\nAnother OpenSquilla runtime is still using this profile. ' +
      'Quit every OpenSquilla app or terminal using it, then try again. ' +
      'If an older process will not exit, restart the computer. Do not delete profile lock files.'
    )
  }
  if (!gatewayExitLooksLikeNewerConfig(outputTail)) return message
  return (
    message +
    '\n\nOpenSquilla could not read this config because it contains settings written by a newer OpenSquilla version. ' +
    `Reopen the newer OpenSquilla version that created it, or reset the desktop config before running this version (${app.getVersion()}). ` +
    'Use Reveal log for details.'
  )
}

async function waitForGateway(url: string, earlyExitMessage?: () => string | null): Promise<void> {
  const result = await waitForGatewayReadiness({
    probe: (remainingMs) => healthCheck(url, remainingMs),
    exitMessage: earlyExitMessage,
    primaryTimeoutMs: 45_000,
    lateGraceMs: 15_000,
    pollIntervalMs: 500,
  })
  if (result.status === 'ready') {
    if (result.late) {
      desktopLog('gateway_health_ready_after_primary_deadline', { port: gatewayState.port })
    }
    return
  }
  if (result.status === 'exited') throw new Error(result.message)
  throw new Error(`Gateway did not become healthy at ${url}`)
}

async function waitForControlUi(url: string, earlyExitMessage?: () => string | null): Promise<void> {
  const startedAt = Date.now()
  while (Date.now() - startedAt < 45_000) {
    const earlyExit = earlyExitMessage?.()
    if (earlyExit) throw new Error(earlyExit)
    try {
      const response = await fetch(`${url}/control/`, { signal: AbortSignal.timeout(1500) })
      if (response.ok) return
    } catch {
      // The ASGI socket can become healthy just before static routes are ready.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 500))
  }
  const earlyExit = earlyExitMessage?.()
  if (earlyExit) throw new Error(earlyExit)
  throw new Error(`Control UI did not become reachable at ${url}/control/`)
}

function hasGatewayProcessExited(process: ChildProcessWithoutNullStreams | null): boolean {
  return Boolean(process && (process.exitCode !== null || process.signalCode !== null))
}

function trackStoppingGatewayProcess(child: ChildProcessWithoutNullStreams): void {
  if (hasGatewayProcessExited(child) || gatewayStoppingProcesses.has(child)) return
  gatewayStoppingProcesses.add(child)
  child.once('exit', () => {
    gatewayStoppingProcesses.delete(child)
    if (updateGatewayShutdownProcess === child) updateGatewayShutdownProcess = null
  })
}

function liveLifecycleOwnedGatewayProcesses(): ChildProcessWithoutNullStreams[] {
  const children = new Set(gatewayStoppingProcesses)
  if (gatewayProcess && gatewayState.owned) children.add(gatewayProcess)
  if (updateGatewayShutdownProcess) children.add(updateGatewayShutdownProcess)
  return [...children].filter((child) => !hasGatewayProcessExited(child))
}

async function reuseHealthyGatewayState(
  isCurrent: () => boolean,
): Promise<GatewayState | null> {
  if (gatewayProfileKey !== desktopProfileKey()) return null
  if (!gatewayState.url) return null
  if (gatewayState.status !== 'ready' && !gatewayProcess) return null
  // A live owned child must pass the exact-child and profile checks in
  // resumeOwnedGatewayStartup; a healthy port alone is not ownership proof.
  if (gatewayProcess && gatewayState.owned) return null

  const healthy = await healthCheck(gatewayState.url)
  if (!isCurrent()) return null
  if (healthy) {
    gatewayState.status = 'ready'
    gatewayState.error = undefined
    sendBootStatus('control')
    return gatewayState
  }

  if (gatewayProcess && gatewayState.owned && hasGatewayProcessExited(gatewayProcess)) {
    gatewayProcess = null
    gatewayState.status = 'stopped'
  }
  return null
}

async function verifyOwnedGatewayLaunch(
  child: ChildProcessWithoutNullStreams,
): Promise<boolean> {
  const context = gatewayProcessOwnershipContexts.get(child)
  if (!context) return false
  return await verifyDesktopGatewayLaunchOwnership(context.ownershipDir, {
    instanceNonce: context.nonce,
    profileFingerprint: context.profileFingerprint,
    port: context.port,
  })
}

async function discardUnverifiedOwnedGatewayChild(
  child: ChildProcessWithoutNullStreams,
): Promise<boolean> {
  if (gatewayProcess !== child) return false
  trackStoppingGatewayProcess(child)
  gatewayProcess = null
  gatewayState.status = 'stopped'
  gatewayState.error = undefined
  // Do not issue any URL-based shutdown request: the listener just failed the
  // exact launch proof. Signal only the ChildProcess tree created by Electron.
  hardTerminateGatewayProcess(child)
  const exited = await waitForGatewayProcessExit(child, 10_000)
  if (!exited) {
    // Restore the exact handle as current authority so the caller can surface
    // the failed stop and a later retry can join it. The stopping-set admission
    // gate prevents a successor from being spawned while this child is live.
    if (!gatewayProcess) gatewayProcess = child
    throw new Error(desktopGatewayStillRunningMessage())
  }
  return true
}

async function resumeOwnedGatewayStartup(
  isCurrent: () => boolean,
): Promise<GatewayState | null> {
  if (!isCurrent()) throw new Error('Desktop startup was superseded by a newer lifecycle operation.')
  const child = gatewayProcess
  if (
    !child
    || !gatewayState.owned
    || !gatewayState.url
    || gatewayProfileKey !== desktopProfileKey()
    || hasGatewayProcessExited(child)
  ) return null

  const url = gatewayState.url
  const childExitMessage = (): string | null => {
    if (gatewayProcess === child && !hasGatewayProcessExited(child)) return null
    return gatewayState.error || 'gateway exited before becoming healthy'
  }
  gatewayState.status = 'starting'
  gatewayState.error = undefined
  sendBootStatus('gateway-health')
  await waitForGateway(url, childExitMessage)
  if (!isCurrent()) throw new Error('Desktop startup was superseded during health verification.')
  await waitForControlUi(url, childExitMessage)

  // Health alone never grants ownership. The same exact child and profile that
  // entered this resume path must still be current after both asynchronous
  // probes, otherwise a concurrent restart or profile switch won the race.
  if (
    gatewayProcess !== child
    || hasGatewayProcessExited(child)
    || gatewayProfileKey !== desktopProfileKey()
    || !isCurrent()
  ) {
    throw new Error(childExitMessage() || 'Desktop gateway changed while startup resumed.')
  }
  if (!await verifyOwnedGatewayLaunch(child)) {
    if (!await discardUnverifiedOwnedGatewayChild(child)) {
      throw new Error('Desktop gateway changed during ownership verification.')
    }
    return null
  }
  if (
    gatewayProcess !== child
    || hasGatewayProcessExited(child)
    || gatewayProfileKey !== desktopProfileKey()
    || !isCurrent()
  ) {
    throw new Error(childExitMessage() || 'Desktop gateway changed during ownership verification.')
  }

  gatewayState.status = 'ready'
  gatewayState.error = undefined
  sendBootStatus('control')
  return gatewayState
}

const VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS = 80_000
const desktopGatewayOwnershipVerification = new DesktopGatewayOwnershipVerificationCoordinator({
  onPidRecycled: (record) => {
    desktopLog('gateway_ownership_pid_recycled', { pid: record.pid, port: record.port })
  },
})

function verifiedOrphanGatewayError(detail: string): Error {
  return new Error(
    'OPENSQUILLA_PROFILE_IN_USE: ' + detail + ' ' +
    'The existing Gateway was left by an earlier Desktop process. ' +
    'Do not delete profile lock files; quit that Gateway and try again.',
  )
}

async function verifyDesktopGatewayOwnershipWhenReady(
  ownershipDir: string,
  record: DesktopGatewayOwnershipRecord,
): Promise<boolean> {
  return await desktopGatewayOwnershipVerification.verifyWhenReady(ownershipDir, record)
}

/**
 * A fresh Electron process has no ChildProcess handle for a Gateway left by a
 * crashed predecessor. Recover only when the profile-scoped record and the
 * loopback HMAC challenge both identify that exact Desktop instance. A health
 * check, occupied port, or PID by itself never grants stop authority.
 */
async function recoverVerifiedOrphanGatewayBeforeSpawn(
  profile = activeDesktopProfile(),
): Promise<void> {
  const ownershipDir = desktopGatewayOwnershipDir(profile)
  const loaded = loadDesktopGatewayOwnershipRecord(ownershipDir)
  if (loaded.status !== 'valid') {
    if (loaded.status === 'invalid') {
      desktopLog('gateway_ownership_record_untrusted')
    }
    return
  }

  const record: DesktopGatewayOwnershipRecord = loaded.record
  await desktopGatewayOwnershipVerification.runRecovery(ownershipDir, record, async (current) => {
    if (current.profile_fingerprint !== desktopProfileFingerprint(profile.home)) {
      desktopLog('gateway_ownership_profile_mismatch', {
        pid: current.pid,
        port: current.port,
      })
      return
    }
    if (!await verifyDesktopGatewayOwnershipWhenReady(ownershipDir, current)) {
      // A stale record after SIGKILL is harmless: the OS has already released the
      // profile lock and the next admitted Gateway will replace the record. Do
      // not unlink it or infer authority over whatever now owns the PID/port.
      desktopLog('gateway_ownership_not_verified', { pid: current.pid, port: current.port })
      return
    }

    desktopLog('gateway_orphan_verified', {
      pid: current.pid,
      port: current.port,
      version: current.version,
    })
    const accepted = await requestVerifiedDesktopGatewayShutdown(current)
    if (!accepted) {
      // The verified process may have completed shutdown between the challenge
      // and the authenticated request. The ownership record is removed only
      // after its writer leases are released, so that disappearance is sufficient.
      if (loadDesktopGatewayOwnershipRecord(ownershipDir).status === 'missing') return
      throw verifiedOrphanGatewayError('The verified Gateway rejected the shutdown request.')
    }
    const released = await waitForDesktopGatewayOwnershipRelease(ownershipDir, current, {
      timeoutMs: VERIFIED_ORPHAN_GATEWAY_RELEASE_TIMEOUT_MS,
    })
    desktopLog('gateway_orphan_shutdown_complete', {
      pid: current.pid,
      port: current.port,
      released,
    })
    if (!released) {
      throw verifiedOrphanGatewayError('The verified Gateway did not finish shutting down.')
    }
  })
}

async function startGateway(): Promise<GatewayState> {
  const startupRevision = desktopOpenFlowRevision
  const startupProfileKey = desktopProfileKey()
  const isCurrent = () => desktopOpenAuthorityIsCurrent(startupRevision, startupProfileKey)
  const reusableGateway = forceOnboardingOnNextStartup
    ? null
    : await reuseHealthyGatewayState(isCurrent)
  if (reusableGateway) return reusableGateway
  if (!isCurrent()) throw new Error('Desktop startup was superseded by a newer open request.')
  artifactPreviewLeaseBroker.clear()

  assertSupportedMacInstallLocation()

  // Retry, recovery, update, or quit may already have moved a child out of the
  // current slot while its graceful shutdown still owns the profile lock. A
  // separate activate/second-instance open flow must not bypass that drain and
  // publish a replacement. Retry remains the explicit join operation; ordinary
  // resume entry points fail closed until the tracked child has exited.
  const alreadyStoppingGateways = liveLifecycleOwnedGatewayProcesses().filter(
    (child) => child !== gatewayProcess,
  )
  if (alreadyStoppingGateways.length > 0) {
    desktopLog('gateway_spawn_blocked_by_stopping_processes', {
      pids: alreadyStoppingGateways.map((child) => child.pid),
    })
    throw new Error(desktopGatewayStillRunningMessage())
  }

  const resumedGateway = forceOnboardingOnNextStartup
    ? null
    : await resumeOwnedGatewayStartup(isCurrent)
  if (resumedGateway) return resumedGateway

  if (gatewayProcess && gatewayState.owned) {
    if (hasGatewayProcessExited(gatewayProcess)) {
      gatewayProcess = null
    } else {
      // Wait for the old child to actually exit before spawning a replacement.
      // stopGateway() only initiates termination; the gateway drains for several
      // seconds and holds gateway.pid.lock until it exits, so respawning
      // immediately makes the new gateway abort on the held lock and the restart
      // fails with an unclassified error.
      const previousChild = gatewayProcess
      stopGateway()
      const exited = await waitForGatewayProcessExit(previousChild)
      if (!exited) {
        throw new Error(desktopGatewayStillRunningMessage())
      }
    }
    gatewayState.status = 'stopped'
    gatewayState.error = undefined
  }

  const activeProfile = activeDesktopProfile()
  const overrideUrl = process.env.OPENSQUILLA_DESKTOP_GATEWAY_URL
  if (overrideUrl) {
    if (!isCurrent()) throw new Error('Desktop startup was superseded before Gateway override validation.')
    sendBootStatus('gateway-health')
    gatewayState.url = overrideUrl.replace(/\/$/, '')
    gatewayState.port = Number(new URL(gatewayState.url).port || 0)
    gatewayState.owned = false
    gatewayProfileKey = desktopProfileKey(activeProfile)
    const overrideReady = await healthCheck(gatewayState.url)
    if (!isCurrent()) throw new Error('Desktop startup was superseded during Gateway override validation.')
    gatewayState.status = overrideReady ? 'ready' : 'error'
    if (gatewayState.status !== 'ready') {
      throw new Error(`Configured gateway is not healthy: ${gatewayState.url}`)
    }
    return gatewayState
  }

  sendBootStatus('gateway-health')
  await recoverVerifiedOrphanGatewayBeforeSpawn()
  if (!isCurrent()) throw new Error('Desktop startup was superseded during Gateway recovery.')

  sendBootStatus('profile')
  const connection = await runOnboarding()
  if (!isCurrent()) throw new Error('Desktop startup was superseded during profile setup.')
  forceOnboardingOnNextStartup = false
  const apiKey = decryptApiKey(connection)
  // Keyless providers (e.g. Ollama) ship requiresApiKey=false and are accepted
  // by onboarding without a key, so only treat a missing key as fatal when the
  // provider actually needs one — otherwise every keyless credential wedges boot.
  if (providerDefaults(connection.provider).requiresApiKey && !apiKey) {
    throw new Error('Saved desktop API key could not be read.')
  }
  const searchApiKey = decryptSearchApiKey(connection)
  // Config is seeded (when missing) inside runOnboarding / the onboarding save,
  // and is otherwise the RPC-owned source of truth — so it is intentionally NOT
  // regenerated here on every boot.

  sendBootStatus('gateway-start')
  const runtime = await resolveGatewayRuntime()

  const port = await findGatewayPort()
  // This is the final await before spawn. Update, quit, cleanup, and recovery
  // close writer/lifecycle admission before draining current children; an
  // in-flight start that had not published its child must not appear after an
  // empty stop/join snapshot and race the installer or a profile write.
  const liveOwnedGatewayCount = liveLifecycleOwnedGatewayProcesses().length
  if (!lifecycleAllowsProcessSpawn(
    isQuitting,
    desktopWriters.closed,
    liveOwnedGatewayCount,
  )) {
    if (liveOwnedGatewayCount > 0) {
      throw new Error(desktopGatewayStillRunningMessage())
    }
    throw new Error('Gateway startup was cancelled by an active lifecycle or profile operation.')
  }
  if (!isCurrent()) throw new Error('Desktop startup was superseded before Gateway spawn.')
  const url = `http://127.0.0.1:${port}`
  const logDir = desktopLogsDir()
  mkdirSync(logDir, { recursive: true })
  const logPath = join(logDir, 'gateway.log')
  const logStream = createWriteStream(logPath, { flags: 'a' })
  // Node can emit both 'error' and 'exit' for one child, and each handler closes
  // the stream — so guard writes/close to be idempotent, and swallow any late
  // write-after-end/EPIPE rather than letting it crash the main process.
  let logStreamClosed = false
  logStream.on('error', () => {})
  const writeLogLine = (text: string) => {
    if (!logStreamClosed) logStream.write(text)
  }
  const closeLogStream = () => {
    if (logStreamClosed) return
    logStreamClosed = true
    logStream.end()
  }

  gatewayState.url = url
  gatewayState.port = port
  gatewayState.owned = true
  gatewayState.status = 'starting'
  gatewayState.logPath = logPath

  const childPath = desktopChildPath()
  const gatewayInstanceNonce = createDesktopGatewayInstanceNonce()
  const gatewayOwnershipDir = desktopGatewayOwnershipDir(activeProfile)
  const gatewayProfileFingerprint = desktopProfileFingerprint(activeProfile.home)
  const childEnv = desktopChildEnvironment(activeProfile, {
    PATH: childPath,
    ...(process.platform === 'win32' ? { Path: childPath } : {}),
    ...(connection.apiKeyEnv && apiKey ? { [connection.apiKeyEnv]: apiKey } : {}),
    ...(connection.searchApiKeyEnv && searchApiKey ? { [connection.searchApiKeyEnv]: searchApiKey } : {}),
    OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE: gatewayInstanceNonce,
    OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR: gatewayOwnershipDir,
    // desktopChildEnvironment pins OPENSQUILLA_STATE_DIR to H. RC4's Python
    // recovery engine has already validated/reconciled the historical nested
    // layout before this writer is admitted.
    ...(connection.disableNetworkObservability ? { OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: '1' } : {}),
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8:replace',
  })

  const child = spawn(
    runtime.command,
    [...runtime.args, '--port', String(port), '--listen', '127.0.0.1', '--config', desktopConfigPath()],
    {
      cwd: runtime.cwd,
      env: childEnv,
      detached: runtime.mode === 'dev' && process.platform !== 'win32',
      // The bundled gateway is a console-subsystem binary; without this Windows
      // allocates a stray console window whose closure would kill the gateway.
      windowsHide: true,
    }
  )
  gatewayProcess = child
  gatewayProcessOwnershipContexts.set(child, {
    nonce: gatewayInstanceNonce,
    ownershipDir: gatewayOwnershipDir,
    profileFingerprint: gatewayProfileFingerprint,
    port,
  })
  gatewayProfileKey = desktopProfileKey(activeProfile)
  desktopLog('gateway_spawned', {
    profileKind: activeProfile.kind,
    pid: child.pid,
    port,
  })
  if (runtime.mode === 'dev') gatewayProcessTreeChildren.add(child)

  let gatewayOutputTail = ''
  let childExitMessage: string | null = null
  const rememberGatewayOutput = (chunk: Buffer | string) => {
    gatewayOutputTail = appendGatewayOutputTail(gatewayOutputTail, chunk)
  }
  child.stdout.on('data', rememberGatewayOutput)
  child.stderr.on('data', rememberGatewayOutput)
  child.stdout.pipe(logStream, { end: false })
  child.stderr.pipe(logStream, { end: false })
  // Classify startup failures only after stdio has closed. Node may emit
  // 'exit' before the final stdout/stderr chunks, which can otherwise drop the
  // stable OPENSQUILLA_PROFILE_IN_USE marker printed immediately before exit.
  child.once('close', (code, signal) => {
    const message = `gateway exited code=${code ?? 'null'} signal=${signal ?? 'null'}`
    const portConflictExit = gatewayExitLooksLikePortInUse(gatewayOutputTail)
    const exitMessage = portConflictExit ? `${message}\nGateway port is already in use.` : message
    const classifiedMessage = classifyGatewayExitMessage(exitMessage, gatewayOutputTail)
    const isCurrentGateway = gatewayProcess === child
    if (isCurrentGateway) gatewayProcess = null
    writeLogLine(`\n[desktop] ${message}\n`)
    // Release the append fd; without this every (re)start leaks one open handle
    // to gateway.log for the lifetime of the main process.
    closeLogStream()
    if (!isCurrentGateway) return
    if (isQuitting) {
      gatewayState.status = 'stopped'
      return
    }
    gatewayState.status = 'error'
    gatewayState.error = classifiedMessage
    childExitMessage = classifiedMessage
    if (portConflictExit && !hasExplicitGatewayPort()) {
      gatewayState.status = 'stopped'
      gatewayState.error = undefined
      return
    }
    sendBootError(gatewayState.error)
    // After boot the window is on the gateway-served Control UI, which never
    // listens for boot:error. Restore the boot splash so the crash message and
    // the Retry/Reset recovery affordances are visible instead of a dead origin.
    void restoreMainWindowToBootPage()
  })

  // A failed spawn (uv missing in dev, non-executable bundled binary) emits
  // 'error' and never 'exit'; without a listener Node rethrows it as an uncaught
  // main-process exception (raw Electron crash dialog) and the boot wait hangs.
  child.once('error', (err) => {
    const message = `gateway failed to start: ${err instanceof Error ? err.message : String(err)}`
    const isCurrentGateway = gatewayProcess === child
    if (isCurrentGateway) gatewayProcess = null
    closeLogStream()
    if (!isCurrentGateway) return
    childExitMessage = message
    if (isQuitting) {
      gatewayState.status = 'stopped'
      return
    }
    gatewayState.status = 'error'
    gatewayState.error = message
    sendBootError(message)
  })

  sendBootStatus('gateway-health')
  await waitForGateway(url, () => childExitMessage)
  await waitForControlUi(url, () => childExitMessage)
  // Guard against adopting a foreign gateway that won the probe→bind race: if our
  // spawned child has already exited, it lost the exclusive bind and the healthy
  // endpoint belongs to someone else (e.g. a CLI `opensquilla gateway run` on the
  // same port). Surface it as a port conflict so recovery advances to the next
  // port instead of silently attaching the window to the wrong profile.
  if (hasGatewayProcessExited(child) || gatewayProcess !== child || !isCurrent()) {
    throw new Error(childExitMessage
      || 'OPENSQUILLA_GATEWAY_PORT_IN_USE: desktop gateway did not keep the port bind.')
  }
  if (!await verifyOwnedGatewayLaunch(child)) {
    // Never send a shutdown request to the unverified listener. Terminate only
    // the exact child handle we spawned, then let port recovery choose another
    // loopback port once that child has released its profile writer lease.
    await discardUnverifiedOwnedGatewayChild(child)
    throw new Error(
      'OPENSQUILLA_GATEWAY_PORT_IN_USE: Gateway port is already in use by an unverified listener.',
    )
  }
  if (hasGatewayProcessExited(child) || gatewayProcess !== child || !isCurrent()) {
    throw new Error(childExitMessage
      || 'OPENSQUILLA_GATEWAY_PORT_IN_USE: desktop gateway changed during ownership verification.')
  }
  sendBootStatus('control')
  gatewayState.status = 'ready'
  return gatewayState
}

async function startGatewayWithPortRecovery(): Promise<GatewayState> {
  // Begin each fresh recovery sequence at the first port so a previously-used
  // port that is now free is reused. The cursor still advances within this loop
  // to skip a port whose bind lost a post-probe race, but it must not persist
  // across separate starts — otherwise every in-session restart hops to a new
  // 127.0.0.1:<port> origin and silently drops the Control UI's per-origin state.
  if (!hasExplicitGatewayPort()) gatewayPortCursor = GATEWAY_PORT_FIRST
  const maxAttempts = hasExplicitGatewayPort() ? 1 : GATEWAY_PORT_LAST - GATEWAY_PORT_FIRST + 1
  let lastError: unknown = null
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      return await startGateway()
    } catch (err) {
      lastError = err
      const message = err instanceof Error ? err.message : String(err)
      if (hasExplicitGatewayPort() || !gatewayExitLooksLikePortInUse(message)) throw err
      desktopLog('gateway_port_retry', { attempt: attempt + 1 })
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError || 'Gateway port retry exhausted.'))
}

async function loadControlUi(window: BrowserWindow, gatewayUrl: string): Promise<void> {
  const url = `${gatewayUrl}/control/chat/new`
  let lastError: Error | null = null
  for (let attempt = 1; attempt <= 10; attempt += 1) {
    try {
      await window.loadURL(url)
      return
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error))
      await new Promise((resolveWait) => setTimeout(resolveWait, 500))
    }
  }
  throw lastError ?? new Error(`Failed to load ${url}`)
}

// The main window is only ever meant to sit on the gateway-served Control UI
// origin (its /control paths). Anything else — a dropped file:// document, an
// off-origin redirect — is a foreign navigation and must be blocked. The boot
// splash is loaded programmatically (loadFile), which does not go through the
// navigation guard, so it needs no allow-entry here.
function isAllowedMainWindowNavigation(targetUrl: string): boolean {
  if (!gatewayState.url) return false
  try {
    const target = new URL(targetUrl)
    const gateway = new URL(gatewayState.url)
    return (
      target.origin === gateway.origin
      && (target.pathname === '/control' || target.pathname.startsWith('/control/'))
    )
  } catch {
    return false
  }
}

function isCurrentWindowAtControlUi(window: BrowserWindow, gatewayUrl: string): boolean {
  const currentUrl = window.webContents.getURL()
  if (!currentUrl) return false

  try {
    const current = new URL(currentUrl)
    const gateway = new URL(gatewayUrl)
    return (
      current.origin === gateway.origin
      && (current.pathname === '/control' || current.pathname.startsWith('/control/'))
    )
  } catch {
    return false
  }
}

async function createMainWindow(): Promise<BrowserWindow> {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow

  const window = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    title: 'OpenSquilla',
    icon: appIconPath(),
    show: false,
    // Paint the window in the app's base color from the first frame so launch
    // never flashes white before the splash/app paints. The app theme defaults
    // to 'system', so match the Control UI's canonical light/dark canvas.
    backgroundColor: desktopWindowBackgroundColor(),
    webPreferences: {
      preload: join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  mainWindow = window
  installDesktopZoomShortcuts(
    window.webContents,
    window.webContents,
    () => nativeWorkbenchSurfaces.refreshBounds(window),
  )
  installEditingContextMenu(window)

  const rendererConsoleLogLimiter = new RendererConsoleLogLimiter()
  let rendererUnresponsiveAt: number | null = null
  const flushRendererConsoleSuppression = (): void => {
    for (const entry of rendererConsoleLogLimiter.flush()) {
      desktopLog(entry.event, entry.detail)
    }
  }

  // Forward renderer console errors to desktop.log. The Control UI runs
  // in the renderer, so a purely front-end failure (a thrown error, an unhandled
  // rejection) otherwise leaves no trace: it
  // never reaches the gateway log, and DevTools is disabled on Windows. Persisting
  // error messages makes those front-end problems diagnosable from a user's log
  // folder without a reproduction. Only the trusted main frame is accepted: an
  // artifact or other child frame must not be able to write to the lifecycle log.
  window.webContents.on('console-message', (details) => {
    if (details.frame !== window.webContents.mainFrame) return
    const entry = buildRendererConsoleLogEntry({
      level: details.level,
      message: details.message,
      sourceId: details.sourceId,
      lineNumber: details.lineNumber,
    }, { homeDir: app.getPath('home') })
    if (!entry) return
    for (const accepted of rendererConsoleLogLimiter.accept(entry)) {
      desktopLog(accepted.event, accepted.detail)
    }
  })

  // Record renderer crashes. A gone render process is the most opaque
  // failure of all — the whole UI freezes with nothing in any log — so stamping
  // the reason and exit code gives a first, always-present breadcrumb.
  window.webContents.on('render-process-gone', (_event, details) => {
    flushRendererConsoleSuppression()
    const entry = buildRendererGoneLogEntry({
      reason: details.reason,
      exitCode: details.exitCode,
    })
    desktopLog(entry.event, entry.detail)
  })

  window.webContents.on('unresponsive', () => {
    flushRendererConsoleSuppression()
    if (rendererUnresponsiveAt !== null) return
    rendererUnresponsiveAt = Date.now()
    const entry = buildRendererStateLogEntry('unresponsive')
    desktopLog(entry.event, entry.detail)
  })

  window.webContents.on('responsive', () => {
    if (rendererUnresponsiveAt === null) return
    const durationMs = Date.now() - rendererUnresponsiveAt
    rendererUnresponsiveAt = null
    const entry = buildRendererStateLogEntry('responsive', durationMs)
    desktopLog(entry.event, entry.detail)
  })

  window.webContents.setWindowOpenHandler(({ url }) => {
    // Forward real outbound links to the system browser; deny everything else.
    // Empty/blob/data popups (e.g. the web "open in new tab" artifact pattern)
    // must NOT reach shell.openExternal — they would be no-ops, and the renderer
    // opens artifacts through the desktop:artifact:open IPC instead.
    if (/^https?:\/\//i.test(url) || url.startsWith('mailto:')) {
      void shell.openExternal(url)
    }
    return { action: 'deny' }
  })

  // Top-frame navigation guard. Programmatic loadFile/loadURL from the main
  // process do NOT emit these, so this only blocks renderer-initiated top-frame
  // navigations: a dropped file/link (Chromium's default drop action) or an
  // in-content redirect that would replace the Control UI with a foreign document
  // while keeping the full opensquillaDesktop IPC bridge. SPA route changes use
  // history.pushState and are unaffected.
  const guardMainWindowNavigation = (event: Electron.Event, targetUrl: string) => {
    if (isAllowedMainWindowNavigation(targetUrl)) return
    event.preventDefault()
    if (/^https?:\/\//i.test(targetUrl) || targetUrl.startsWith('mailto:')) {
      void shell.openExternal(targetUrl)
    }
  }
  window.webContents.on('will-navigate', guardMainWindowNavigation)
  window.webContents.on('will-redirect', guardMainWindowNavigation)
  window.webContents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => {
    // Native child views sit above the renderer DOM. A full document change
    // must remove them before boot/recovery/another Control UI can become
    // visible; same-document SPA navigation keeps the Workbench lifecycle in
    // Vue and is intentionally left alone.
    if (isMainFrame && !isInPlace) void nativeWorkbenchSurfaces.destroyAll()
  })

  window.on('close', (event) => handleMainWindowClose(window, event))
  if (process.platform === 'win32') {
    const commitSystemSessionEnd = () => {
      if (!systemSessionEnding) windowsSessionEndPreviousPhase = appExitPhase
      systemSessionEnding = true
      setAppExitPhase('committed', 'windows session ending')
    }
    window.on('query-session-end', () => {
      commitSystemSessionEnd()
      if (windowsSessionEndResetTimer) clearTimeout(windowsSessionEndResetTimer)
      // Windows can abandon a shutdown/logout because another application
      // vetoed it, but Electron exposes no matching cancellation event. Do not
      // leave this instance permanently committed and unrevealable.
      windowsSessionEndResetTimer = setTimeout(() => {
        windowsSessionEndResetTimer = null
        if (!systemSessionEnding) return
        const previousPhase = windowsSessionEndPreviousPhase ?? 'running'
        windowsSessionEndPreviousPhase = null
        systemSessionEnding = false
        if (appExitPhase === 'committed') {
          setAppExitPhase(previousPhase, 'Windows session end was not committed')
        }
      }, 15_000)
      windowsSessionEndResetTimer.unref()
    })
    window.on('session-end', () => {
      if (windowsSessionEndResetTimer) clearTimeout(windowsSessionEndResetTimer)
      windowsSessionEndResetTimer = null
      windowsSessionEndPreviousPhase = null
      commitSystemSessionEnd()
      // Electron does not emit app.before-quit for Windows shutdown, restart,
      // or logout. Release the tray and exact owned child from this
      // BrowserWindow event, which is guaranteed for that lifecycle.
      isQuitting = true
      destroyWindowsTray()
      stopGateway()
    })
  }

  window.once('ready-to-show', () => {
    if (!window.isDestroyed()) window.show()
  })

  window.once('closed', () => {
    if (mainWindow === window) mainWindow = null
  })

  await window.loadFile(bootPagePath())
  return window
}

function currentMainWindow(): BrowserWindow | null {
  return mainWindow && !mainWindow.isDestroyed() ? mainWindow : null
}

function ensureGatewayStarted(): Promise<GatewayState> {
  if (!gatewayStartPromise) {
    sendBootStatus('profile')
    gatewayStartPromise = startGatewayWithPortRecovery().finally(() => {
      gatewayStartPromise = null
    })
  }
  return gatewayStartPromise
}

async function loadControlUiIntoCurrentWindow(
  gatewayUrl: string,
  isCurrent: () => boolean,
): Promise<boolean> {
  if (!isCurrent()) return false
  const window = currentMainWindow()
  if (!window) return false

  sendBootStatus('control')
  if (isCurrentWindowAtControlUi(window, gatewayUrl)) {
    if (!isCurrent()) return false
    sendBootStatus('ready')
    return true
  }

  try {
    await loadControlUi(window, gatewayUrl)
  } catch (error) {
    if (window.isDestroyed()) return false
    throw error
  }
  if (!isCurrent()) return false
  sendBootStatus('ready')
  return true
}

// Bring the main window back to the boot splash when a gateway failure happens
// while the window is showing the gateway-served Control UI. The splash owns the
// boot:error listener plus the Retry/Reset recovery buttons, so this is what
// turns an otherwise-dead Control UI origin back into a recoverable state.
async function restoreMainWindowToBootPage(): Promise<void> {
  const window = currentMainWindow()
  if (!window) return
  // Already on the boot splash (initial boot); its own onBootError handler will
  // render the error. Only navigate back when the window left for the Control UI.
  if (window.webContents.getURL().startsWith('file:')) return
  try {
    await window.loadFile(bootPagePath())
  } catch {
    // Best-effort: the diagnostic log and gatewayState still record the failure.
  }
}

async function stopOwnedGatewayAndWait(): Promise<void> {
  const exited = await stopAndJoinAllLifecycleOwnedGateways()
  if (!exited) throw new Error('The Desktop gateway did not stop before the recovery operation.')
  updateGatewayShutdownProcess = null
  clearReusableGatewayState()
}

async function inspectActiveProfileBeforeStartup(): Promise<boolean> {
  const consolidationFailure = await consolidateLegacyRecoveryProfilesBeforeStartup()
  const active = activeDesktopProfile()
  // On a hard Electron crash, the Python Gateway can remain healthy and keep
  // the profile writer lease. Prove and stop that exact prior Desktop instance
  // before profile inspection; otherwise the inspector reports profile_lock_busy
  // and strands startup on the manual recovery screen before startGateway() can
  // run. Never apply this to a developer override or this process's own child.
  const overrideUrl = process.env.OPENSQUILLA_DESKTOP_GATEWAY_URL
  if (!overrideUrl && liveLifecycleOwnedGatewayProcesses().length === 0) {
    await recoverVerifiedOrphanGatewayBeforeSpawn(active)
  }
  recoveryOperationError = null
  let inspection = await inspectDesktopProfile(active)
  // Findings below are warnings (`attention`), not startup blockers: the
  // repair action advertised by the inspector is run automatically and
  // startup continues. Only a failed automatic repair, a config authored by
  // a newer build, or an elevated-Windows unsafe path reaches the manual
  // recovery page.
  if (
    inspection.allowed_actions.includes('abandon-cleanup')
    && inspection.stable_code === 'cleanup_transaction_incomplete'
    && inspection.transaction_id
  ) {
    if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
    try {
      // Abandoning archives only the cleanup journal record; every surviving
      // file is preserved, so no confirmation is needed before startup
      // continues on the remaining profile.
      inspection = await runRecoveryCli(active, [
        'abandon-cleanup',
        '--user-data', app.getPath('userData'),
        '--home', active.home,
        '--profile-kind', 'desktop-primary',
        '--transaction-id', inspection.transaction_id,
        '--expected-revision', String(inspection.revision),
        '--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS),
        '--json',
      ])
    } catch (error) {
      desktopLog('cleanup_auto_abandon_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'cleanup_auto_abandon_failed')
    }
  }

  if (inspection.allowed_actions.includes('recover-settings')) {
    if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
    try {
      inspection = await runRecoveryCli(active, [
        'recover-settings', '--home', active.home,
        '--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS), '--json',
      ])
    } catch (error) {
      desktopLog('settings_transaction_recovery_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'settings_transaction_recovery_failed')
    }
  }

  if (
    inspection.allowed_actions.includes('recover-transaction')
    && inspection.transaction_id
  ) {
    if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
    try {
      inspection = await recoverInspectedProfileTransaction(active, inspection)
    } catch (error) {
      desktopLog('profile_transaction_auto_recovery_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'profile_transaction_auto_recovery_failed')
    }
  }

  if (inspection.allowed_actions.includes('recover-config')) {
    if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
    try {
      // The CLI preserves the corrupt config.toml beside itself before it
      // restores the newest valid backup (or minimal defaults), so the
      // automatic path never destroys evidence.
      inspection = await runRecoveryCli(active, [
        'recover-config', '--home', active.home,
        '--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS), '--json',
      ])
    } catch (error) {
      desktopLog('config_auto_recovery_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'config_auto_recovery_failed')
    }
  }

  const provenReconcile = inspection.outcome !== 'recovery_required'
    && inspection.allowed_actions.includes('reconcile')
  const safeLayoutFinalize = inspection.outcome === 'ready'
    && inspection.allowed_actions.includes('finalize-layout')
  if (provenReconcile || safeLayoutFinalize) {
    if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
    try {
      inspection = await runRecoveryCli(active, [
        'reconcile', '--home', active.home,
        '--lock-timeout', String(RECOVERY_LOCK_TIMEOUT_SECONDS), '--json',
      ])
    } catch (error) {
      desktopLog('recovery_reconcile_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'desktop_recovery_reconcile_failed')
    }
  }

  if (inspection.outcome !== 'recovery_required') {
    try {
      // A whole-profile transaction may have committed immediately before the
      // Electron process stopped. The narrow layout receipt is the authority
      // for finishing provider credential reconciliation.
      await recoverPendingMigrationReconciliation()
      inspection = await inspectDesktopProfile(active)
    } catch (error) {
      desktopLog('migration_reconciliation_startup_failed', {
        error: error instanceof Error ? error.message : 'unknown error',
      })
      inspection = recoveryFailureResult(active.home, 'migration_reconciliation_failed')
    }
  }

  if (
    inspection.outcome !== 'recovery_required'
    && pendingDesktopCredentialConsolidation
  ) {
    const pending = pendingDesktopCredentialConsolidation
    await adoptConsolidatedDesktopCredential(pending)
    pendingDesktopCredentialConsolidation = null
    // apply-settings advances the recovery revision even though it preserves
    // config.toml bytes. Refresh the authoritative primary state before the
    // boot page or any later repair action consumes it.
    inspection = await inspectDesktopProfile(active)
  }

  if (consolidationFailure) {
    if (inspection.outcome === 'recovery_required') {
      desktopLog('desktop_profile_consolidation_primary_blocked', {
        consolidationStableCode: consolidationFailure.stable_code,
        primaryStableCode: inspection.stable_code,
      })
    } else {
      deferProfileConsolidationMaintenance(consolidationFailure)
    }
  }

  recoveryInspection = inspection
  primaryRecoveryInspection = inspection
  publishRecoveryState()
  createApplicationMenu()
  if (inspection.outcome !== 'recovery_required') return true

  if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
  bootError = null
  await restoreMainWindowToBootPage()
  publishRecoveryState()
  return false
}

async function openOrResumeDesktopApp(): Promise<void> {
  invalidateDesktopOpenFlow()
  if (desktopOpenFlowPromise) return await desktopOpenFlowPromise

  desktopOpenFlowPromise = (async () => {
    while (!isQuitting) {
      const revision = desktopOpenFlowRevision
      const requestedProfileKey = desktopProfileKey()
      let operationIsCurrent = () => (
        desktopOpenAuthorityIsCurrent(revision, requestedProfileKey)
      )
      await createMainWindow()
      focusMainWindow()

      try {
        if (await inspectActiveProfileBeforeStartup()) {
          if (revision === desktopOpenFlowRevision && requestedProfileKey === desktopProfileKey()) {
            const reusableGateway = forceOnboardingOnNextStartup
              ? null
              : await reuseHealthyGatewayState(
                operationIsCurrent,
              )
            if (!operationIsCurrent()) {
              throw new Error('Desktop startup was superseded by a newer open request.')
            }
            const gateway = reusableGateway ?? await ensureGatewayStarted()
            const gatewayUrl = gateway.url
            const expectedOwned = gateway.owned
            const expectedChild = expectedOwned ? gatewayProcess : null
            operationIsCurrent = () => (
              desktopOpenAuthorityIsCurrent(revision, requestedProfileKey)
              && gatewayState.url === gatewayUrl
              && gatewayState.owned === expectedOwned
              && (
                !expectedOwned
                || (
                  expectedChild !== null
                  && gatewayProcess === expectedChild
                  && !hasGatewayProcessExited(expectedChild)
                )
              )
            )
            if (operationIsCurrent()) {
              await loadControlUiIntoCurrentWindow(
                gatewayUrl,
                operationIsCurrent,
              )
            }
          }
        }
      } catch (error) {
        const authoritative = operationIsCurrent()
        if (authoritative) {
          if (gatewayState.status !== 'ready') {
            gatewayState.status = 'error'
            gatewayState.error = error instanceof Error ? error.message : String(error)
          }
          desktopLog('desktop_open_failed', {
            profileKind: 'primary',
            gatewayPid: gatewayProcess?.pid,
            gatewayStatus: gatewayState.status,
            error: error instanceof Error ? error.message : String(error),
          })
          if (currentMainWindow()) sendBootError(error)
        }
      }
      if (desktopOpenAuthorityIsCurrent(revision, requestedProfileKey)) return
      if (gatewayProfileKey && gatewayProfileKey !== desktopProfileKey()) {
        if (gatewayProcess && gatewayState.owned) await stopOwnedGatewayAndWait()
        else clearReusableGatewayState()
      }
    }
  })().finally(() => {
    desktopOpenFlowPromise = null
  })
  await desktopOpenFlowPromise
}

// SIGKILL deadline for the owned gateway child. The Python gateway drains
// in-flight agent turns and background completions on shutdown (up to two
// graceful phases plus teardown — see gateway_shutdown_deadline()), so the
// force-kill must exceed that worst case or the drain is cut off mid-write.
// Keep in sync with the default OPENSQUILLA_GATEWAY_GRACEFUL_TIMEOUT (30s).
const GATEWAY_SHUTDOWN_KILL_AFTER_MS = 75_000
// Short SIGKILL backstop after a hard terminate (TerminateProcess / SIGTERM)
// when the graceful path was skipped or already overran its deadline.
const GATEWAY_HARD_KILL_BACKSTOP_MS = 5_000
const UPDATE_GATEWAY_EXIT_TIMEOUT_MS = GATEWAY_SHUTDOWN_KILL_AFTER_MS + GATEWAY_HARD_KILL_BACKSTOP_MS

// Ask the gateway to shut down gracefully over its owner-only HTTP endpoint,
// which runs the full GatewayServer.close() drain before exiting. The desktop
// child is loopback (no-auth owner), so no token is needed. Best-effort:
// returns false if the gateway is unreachable or rejects the request.
async function requestGatewayShutdown(url: string): Promise<boolean> {
  if (!url) return false
  try {
    const response = await fetch(`${url}/api/system/shutdown`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      signal: AbortSignal.timeout(2000),
    })
    return response.ok
  } catch {
    return false
  }
}

// Prefer the instance-bound Desktop protocol so token-authenticated profiles can
// still drain without exposing their API token to the Electron shell. The
// nonce, PID, profile fingerprint, and port must all match the child we spawned;
// a stale record or an unrelated healthy listener never grants stop authority.
async function requestOwnedGatewayShutdown(
  child: ChildProcessWithoutNullStreams,
  url: string,
): Promise<boolean> {
  const context = gatewayProcessOwnershipContexts.get(child)
  const loaded = context
    ? loadDesktopGatewayOwnershipRecord(context.ownershipDir)
    : { status: 'missing' as const, record: null }
  if (
    context
    && loaded.status === 'valid'
    && desktopGatewayOwnershipMatchesLaunch(loaded.record, {
      instanceNonce: context.nonce,
      profileFingerprint: context.profileFingerprint,
      port: context.port,
    })
  ) {
    if (await requestVerifiedDesktopGatewayShutdown(loaded.record)) return true
  }
  // A known launch context that cannot prove its listener must never send an
  // unauthenticated shutdown request to that URL. The caller will signal only
  // the exact ChildProcess handle. Keep the legacy URL path solely for handles
  // created before this Electron process could attach a launch context.
  if (context) return false
  return await requestGatewayShutdown(url)
}

// Fetch a diagnostics bundle from the child gateway (loopback owner, no token
// needed — same auth posture as requestGatewayShutdown) and save it where the
// user chooses. Falls back to opening the logs folder when no gateway is up.
async function downloadDiagnostics(): Promise<void> {
  desktopLog('diagnostics_download_requested')
  const url = gatewayState.url
  if (!url) {
    await shell.openPath(join(app.getPath('userData'), 'logs')).catch(() => null)
    return
  }
  try {
    const response = await fetch(`${url}/api/v1/diagnostics/bundle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
      signal: AbortSignal.timeout(60000),
    })
    if (!response.ok) {
      desktopLog('diagnostics_download_failed', { status: response.status })
      await shell.openPath(join(app.getPath('userData'), 'logs')).catch(() => null)
      return
    }
    // Read the body before showing the modal save dialog: the 60s abort signal
    // keeps counting while the dialog is open, and a slow deliberation would
    // otherwise abort the already-successful response.
    const bytes = Buffer.from(await response.arrayBuffer())
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    const win = currentMainWindow()
    const defaultPath = join(
      app.getPath('downloads'),
      `opensquilla-bundle-${stamp}.zip`,
    )
    const saveOptions: Electron.SaveDialogOptions = {
      defaultPath,
      filters: [{ name: 'Zip archive', extensions: ['zip'] }],
    }
    const result = win
      ? await dialog.showSaveDialog(win, saveOptions)
      : await dialog.showSaveDialog(saveOptions)
    if (result.canceled || !result.filePath) return
    await writeFile(result.filePath, bytes)
    desktopLog('diagnostics_download_saved', { bytes: bytes.length })
    await shell.showItemInFolder(result.filePath)
  } catch (err) {
    desktopLog('diagnostics_download_failed', { error: String(err) })
    await shell.openPath(join(app.getPath('userData'), 'logs')).catch(() => null)
  }
}

async function clearKnownOwnedGatewayPidFile(): Promise<void> {
  // Leave gateway.pid.lock in place. The persistent lock path is the authority
  // shared by all contenders; deleting it can create split-brain locks.
  await rm(join(desktopStateDir(), 'gateway.pid'), { force: true }).catch(() => null)
}

function hardTerminateGatewayProcess(
  child: ChildProcessWithoutNullStreams,
  backstopMs = GATEWAY_HARD_KILL_BACKSTOP_MS,
): void {
  if (hasGatewayProcessExited(child)) return
  terminateGatewayProcess(child, 'SIGTERM')
  if (process.platform === 'win32') void clearKnownOwnedGatewayPidFile()
  setTimeout(() => {
    if (!hasGatewayProcessExited(child)) {
      terminateGatewayProcess(child, 'SIGKILL')
      if (process.platform === 'win32') void clearKnownOwnedGatewayPidFile()
    }
  }, backstopMs).unref()
}

function terminateGatewayProcess(
  child: ChildProcessWithoutNullStreams,
  signal: NodeJS.Signals,
): void {
  const pid = child.pid
  if (pid && gatewayProcessTreeChildren.has(child)) {
    if (process.platform === 'win32') {
      const result = spawnSync('taskkill', ['/pid', String(pid), '/t', '/f'], {
        stdio: 'ignore',
        windowsHide: true,
      })
      if (result.status === 0) return
    } else {
      try {
        process.kill(-pid, signal)
        return
      } catch {
        // Fall back to signaling the direct child below.
      }
    }
  }
  child.kill(signal)
}

function stopGateway(): void {
  artifactPreviewLeaseBroker.clear()
  if (!gatewayProcess || !gatewayState.owned) return
  const child = gatewayProcess
  const url = gatewayState.url
  trackStoppingGatewayProcess(child)
  gatewayProcess = null

  const hardTerminate = () => {
    hardTerminateGatewayProcess(child)
  }

  // The Windows HTTP graceful path is async (fetch + timers) and only works
  // while the main process stays alive to drive it. On app quit (isQuitting) the
  // process is about to exit, so that fire-and-forget work would race teardown
  // and orphan the child — leaving it holding the listen port + PID lock and
  // breaking the next launch. So only take the graceful path when NOT quitting;
  // on quit, fall through to a synchronous TerminateProcess.
  if (process.platform === 'win32' && (!isQuitting || allowGracefulShutdownWhileQuitting)) {
    // Windows has no real SIGTERM — child.kill('SIGTERM') maps to an immediate
    // TerminateProcess that skips the drain. Trigger the HTTP graceful path,
    // wait for the child to exit on its own, and only force-terminate if it
    // overruns the deadline or the gateway never accepted the request.
    let exited = false
    child.once('exit', () => {
      exited = true
    })
    void requestOwnedGatewayShutdown(child, url).then((accepted) => {
      if (!accepted && !exited) hardTerminate()
    })
    setTimeout(() => {
      if (!exited) hardTerminate()
    }, GATEWAY_SHUTDOWN_KILL_AFTER_MS).unref()
    return
  }

  // POSIX: SIGTERM triggers the gateway's graceful drain directly (the detached
  // child drains and exits on its own after the main process is gone).
  // Windows-on-quit: SIGTERM maps to a synchronous TerminateProcess, killing the
  // child before the main process exits — no drain, but no orphan either.
  hardTerminateGatewayProcess(child, GATEWAY_SHUTDOWN_KILL_AFTER_MS)
}

// ── Desktop updates ──────────────────────────────────────────────────────────
// macOS release builds are Developer-ID signed + notarized and ship the zip +
// latest-mac.yml feed that Squirrel.Mac consumes, so in-place auto-update is
// safe. Windows builds are currently unsigned, so the desktop shell discovers
// the release but opens its exact versioned NSIS installer for an explicit
// manual install. OPENSQUILLA_DESKTOP_ENABLE_WIN_UPDATE=1 opts in to native
// Windows updating for local tests only; OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE
// disables all shell-managed discovery.
const { autoUpdater } = electronUpdater

let autoUpdaterReady = false
let updateDownloadInProgress = false
let manualInstallerActionInProgress = false
let updateApplying = false
// A user/system quit that arrives while an update is still draining writers or
// the gateway is deferred until that phase either fails safely or reaches the
// updater-owned handoff. Only quitAndInstall may set handoff ready.
let updateInstallHandoffReady = false
let quitRequestedDuringUpdateDrain = false
let downloadedUpdateVersion: string | null = null
let verifiedManualInstallerPath: string | null = null
let updateGatewayShutdownProcess: ChildProcessWithoutNullStreams | null = null
let mockDownloadedUpdate = false
let mockUpdatePromptActive = false
let mockUpdateDialogResponses: number[] | null = null

const MOCK_UPDATE_VERSION_ENV = 'OPENSQUILLA_DESKTOP_MOCK_UPDATE_VERSION'
const MOCK_UPDATE_DIALOG_RESPONSES_ENV = 'OPENSQUILLA_DESKTOP_MOCK_UPDATE_DIALOG_RESPONSES'

type DesktopUpdateStatus =
  | 'idle'
  | 'checking'
  | 'available'
  | 'downloading'
  | 'downloaded'
  | 'not-available'
  | 'error'
  | 'applying'

type DesktopUpdateInstallMode = 'native' | 'manual' | 'unsupported'
type DesktopUpdateErrorCode =
  | 'source_unreachable'
  | 'manifest_invalid'
  | 'checksum_unavailable'
  | 'integrity_failed'
  | 'download_failed'
  | 'install_failed'
  | null

interface DesktopUpdateState {
  status: DesktopUpdateStatus
  currentVersion: string
  latestVersion: string | null
  progress: number | null
  checkedAt: string | null
  error: string | null
  errorCode: DesktopUpdateErrorCode
  snoozedUntil: string | null
  canCheck: boolean
  canNativeInstall: boolean
  installMode: DesktopUpdateInstallMode
  releaseUrl: string | null
  source: DesktopUpdateSource | null
  fallbackUsed: boolean
}

interface DesktopUpdateFailureFallback {
  state: DesktopUpdateState
  candidate: DesktopUpdateCandidate | null
}

interface NativeUpdateReady {
  tag: string
  version: string
  source: DesktopUpdateSource
}

interface DesktopUpdatePersistedState {
  snoozedVersion?: string
  snoozedUntil?: string
  lastSuccessfulSource?: DesktopUpdateSource
}

const UPDATE_SNOOZE_MS = 24 * 60 * 60 * 1000
const UPDATE_CHECK_INITIAL_DELAY_MS = 12_000
const MOCK_UPDATE_CHECK_INITIAL_DELAY_MS = 1_000
const UPDATE_CHECK_REPEAT_DELAY_MS = 24 * 60 * 60 * 1000
const UPDATE_CHECKSUM_MAX_BYTES = 1024 * 1024
const UPDATE_INSTALLER_MAX_BYTES = 4 * 1024 * 1024 * 1024
const UPDATE_INSTALLER_DOWNLOAD_TIMEOUT_MS = 60 * 60 * 1000

let desktopUpdateStatus: DesktopUpdateStatus = 'idle'
let desktopUpdateLatestVersion: string | null = null
let desktopUpdateProgress: number | null = null
let desktopUpdateCheckedAt: string | null = null
let desktopUpdateError: string | null = null
let desktopUpdateErrorCode: DesktopUpdateErrorCode = null
let desktopUpdateReleaseUrl: string | null = null
let desktopUpdateSource: DesktopUpdateSource | null = null
let desktopUpdateFallbackUsed = false
let desktopUpdateCandidate: DesktopUpdateCandidate | null = null
let nativeUpdateReady: NativeUpdateReady | null = null
let lastSuccessfulUpdateSource: DesktopUpdateSource | null = null
let desktopUpdateSnoozedVersion: string | null = null
let desktopUpdateSnoozedUntil: string | null = null
let desktopUpdatePersistenceLoaded = false
let desktopUpdatePersistenceWrite: Promise<void> = Promise.resolve()

const NETWORK_OBSERVABILITY_DISABLE_ENV_KEYS = [
  'OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY',
  'OPENSQUILLA_TELEMETRY_DISABLED',
  'OPENSQUILLA_UPDATE_CHECK_DISABLED',
] as const

// mtime-keyed caches so the update-state publish path (which runs on every
// download-progress tick) does not re-read + JSON.parse the credential and
// re-scan config.toml on every call. Invalidated automatically when the file
// changes — atomicWriteFile's rename bumps mtime, and deletion falls through the
// existsSync guard.
let persistedNetObsCache: { mtime: number; value: boolean } | null = null
let configNetObsCache: { mtime: number; value: boolean | null } | null = null

function desktopPersistedNetworkObservabilityDisabled(): boolean {
  try {
    const path = credentialPath()
    if (!existsSync(path)) return false
    const mtime = statSync(path).mtimeMs
    if (persistedNetObsCache && persistedNetObsCache.mtime === mtime) return persistedNetObsCache.value
    const raw = readFileSync(path, 'utf8')
    const value = normalizeDesktopCredential(JSON.parse(raw) as Partial<DesktopConnection>).disableNetworkObservability
    persistedNetObsCache = { mtime, value }
    return value
  } catch {
    return true
  }
}

function parseDesktopNetworkObservabilityPrivacyConfig(raw: string): boolean | null {
  let inPrivacySection = false
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const section = line.match(/^\[([^\]]+)\]$/)
    if (section) {
      inPrivacySection = section[1]?.trim() === 'privacy'
      continue
    }
    if (!inPrivacySection) continue
    const setting = line.match(/^disable_network_observability\s*=\s*(.*)$/i)
    if (!setting) continue
    const value = String(setting[1] ?? '').split('#', 1)[0].trim().toLowerCase()
    if (value === 'true') return true
    if (value === 'false') return false
    return true
  }
  return null
}

function readDesktopConfigNetworkObservabilitySetting(): boolean | null {
  try {
    const path = desktopConfigPath()
    if (!existsSync(path)) return null
    const mtime = statSync(path).mtimeMs
    if (configNetObsCache && configNetObsCache.mtime === mtime) return configNetObsCache.value
    const raw = readFileSync(path, 'utf8')
    const value = parseDesktopNetworkObservabilityPrivacyConfig(raw)
    configNetObsCache = { mtime, value }
    return value
  } catch {
    return true
  }
}

function desktopConfigNetworkObservabilityDisabled(): boolean {
  return readDesktopConfigNetworkObservabilitySetting() ?? false
}

function desktopNetworkObservabilityDisabled(): boolean {
  if (NETWORK_OBSERVABILITY_DISABLE_ENV_KEYS.some((key) => truthyEnv(process.env[key]))) return true
  return desktopPersistedNetworkObservabilityDisabled() || desktopConfigNetworkObservabilityDisabled()
}

function mockUpdateVersion(): string | null {
  if (app.isPackaged) return null
  const version = (process.env[MOCK_UPDATE_VERSION_ENV] || '').trim()
  return version || null
}

function desktopUpdateMenuEnabled(): boolean {
  return desktopUpdateManaged() || mockUpdateVersion() !== null
}

function desktopUpdateManaged(): boolean {
  if (!app.isPackaged) return false
  if (desktopNetworkObservabilityDisabled()) return false
  if (process.env.OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE === '1') return false
  return process.platform === 'darwin' || process.platform === 'win32'
}

function autoUpdateSupported(): boolean {
  if (desktopNetworkObservabilityDisabled()) return false
  if (!desktopUpdateManaged()) return false
  if (process.platform === 'darwin') return true
  if (process.platform === 'win32' && process.env.OPENSQUILLA_DESKTOP_ENABLE_WIN_UPDATE === '1') {
    return true
  }
  return false
}

function nativeAutoUpdateEnabled(): boolean {
  return mockUpdateVersion() !== null || (autoUpdateSupported() && macUpdateLocationOk())
}

function desktopUpdateInstallMode(): DesktopUpdateInstallMode {
  if (nativeAutoUpdateEnabled()) return 'native'
  if (desktopUpdateManaged() && process.platform === 'win32') return 'manual'
  return 'unsupported'
}

function desktopUpdateStatePath(): string {
  // Update availability is application-global and may be read before profile
  // inspection, so it must never resolve through an untrusted selected H.
  return join(app.getPath('userData'), 'desktop-update.json')
}

function loadDesktopUpdatePersistence(): void {
  if (desktopUpdatePersistenceLoaded) return
  desktopUpdatePersistenceLoaded = true
  try {
    const path = desktopUpdateStatePath()
    if (!existsSync(path)) return
    const parsed = JSON.parse(readFileSync(path, 'utf8')) as DesktopUpdatePersistedState
    if (parsed.lastSuccessfulSource === 'oss' || parsed.lastSuccessfulSource === 'github') {
      lastSuccessfulUpdateSource = parsed.lastSuccessfulSource
    }
    const snoozedVersion = String(parsed.snoozedVersion || '').trim()
    const snoozedUntil = String(parsed.snoozedUntil || '').trim()
    if (!snoozedVersion || !snoozedUntil) return
    if (Number.isNaN(Date.parse(snoozedUntil)) || Date.parse(snoozedUntil) <= Date.now()) return
    desktopUpdateSnoozedVersion = snoozedVersion
    desktopUpdateSnoozedUntil = snoozedUntil
  } catch {
    desktopUpdateSnoozedVersion = null
    desktopUpdateSnoozedUntil = null
  }
}

function persistDesktopUpdateState(): Promise<void> {
  desktopUpdatePersistenceWrite = desktopUpdatePersistenceWrite.then(async () => {
    try {
      mkdirSync(app.getPath('userData'), { recursive: true })
      await atomicWriteFile(
        desktopUpdateStatePath(),
        JSON.stringify(
          {
            snoozedVersion: desktopUpdateSnoozedVersion || undefined,
            snoozedUntil: desktopUpdateSnoozedUntil || undefined,
            lastSuccessfulSource: lastSuccessfulUpdateSource || undefined,
          },
          null,
          2,
        ),
        0o600,
      )
    } catch (err) {
      console.warn('[updater] failed to persist update state', err)
    }
  })
  return desktopUpdatePersistenceWrite
}

function activeDesktopUpdateSnoozeFor(version: string | null): string | null {
  loadDesktopUpdatePersistence()
  if (!version || !desktopUpdateSnoozedVersion || !desktopUpdateSnoozedUntil) return null
  if (desktopUpdateSnoozedVersion !== version) return null
  if (Date.parse(desktopUpdateSnoozedUntil) <= Date.now()) {
    desktopUpdateSnoozedVersion = null
    desktopUpdateSnoozedUntil = null
    void persistDesktopUpdateState()
    return null
  }
  return desktopUpdateSnoozedUntil
}

function clearDesktopUpdateSnoozeIfVersionChanged(version: string | null): void {
  loadDesktopUpdatePersistence()
  if (!version || !desktopUpdateSnoozedVersion || desktopUpdateSnoozedVersion === version) return
  desktopUpdateSnoozedVersion = null
  desktopUpdateSnoozedUntil = null
  void persistDesktopUpdateState()
}

function desktopUpdateSnapshot(): DesktopUpdateState {
  const latestVersion = desktopUpdateLatestVersion || downloadedUpdateVersion
  const installMode = desktopUpdateInstallMode()
  return {
    status: desktopUpdateStatus,
    currentVersion: app.getVersion(),
    latestVersion,
    progress: desktopUpdateProgress,
    checkedAt: desktopUpdateCheckedAt,
    error: desktopUpdateError,
    errorCode: desktopUpdateErrorCode,
    snoozedUntil: activeDesktopUpdateSnoozeFor(latestVersion),
    canCheck: desktopUpdateManaged() || mockUpdateVersion() !== null,
    canNativeInstall: installMode === 'native',
    installMode,
    releaseUrl: desktopUpdateReleaseUrl,
    source: desktopUpdateSource,
    fallbackUsed: desktopUpdateFallbackUsed,
  }
}

function publishDesktopUpdateState(): DesktopUpdateState {
  const state = desktopUpdateSnapshot()
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send('desktop:update:state-changed', state)
  }
  return state
}

function setDesktopUpdateState(patch: Partial<DesktopUpdateState>): DesktopUpdateState {
  if (patch.status !== undefined) desktopUpdateStatus = patch.status
  if ('latestVersion' in patch) desktopUpdateLatestVersion = patch.latestVersion ?? null
  if ('progress' in patch) desktopUpdateProgress = patch.progress ?? null
  if ('checkedAt' in patch) desktopUpdateCheckedAt = patch.checkedAt ?? null
  if ('error' in patch) {
    desktopUpdateError = patch.error ?? null
    if (patch.error == null && !('errorCode' in patch)) desktopUpdateErrorCode = null
  }
  if ('errorCode' in patch) desktopUpdateErrorCode = patch.errorCode ?? null
  if ('releaseUrl' in patch) desktopUpdateReleaseUrl = patch.releaseUrl ?? null
  if ('source' in patch) desktopUpdateSource = patch.source ?? null
  if ('fallbackUsed' in patch) desktopUpdateFallbackUsed = patch.fallbackUsed ?? false
  clearDesktopUpdateSnoozeIfVersionChanged(desktopUpdateLatestVersion || downloadedUpdateVersion)
  return publishDesktopUpdateState()
}

async function dismissDesktopUpdate(): Promise<DesktopUpdateState> {
  const latestVersion = desktopUpdateLatestVersion || downloadedUpdateVersion
  if (!latestVersion && desktopUpdateStatus === 'error') {
    return setDesktopUpdateState({
      status: 'idle',
      progress: null,
      error: null,
      errorCode: null,
      releaseUrl: null,
      source: null,
      fallbackUsed: false,
    })
  }
  if (latestVersion) {
    desktopUpdateSnoozedVersion = latestVersion
    desktopUpdateSnoozedUntil = new Date(Date.now() + UPDATE_SNOOZE_MS).toISOString()
    await persistDesktopUpdateState()
  }
  return publishDesktopUpdateState()
}

// macOS Squirrel cannot swap an app that runs from a read-only/translocated
// location (a mounted DMG, ~/Downloads). The app must live in /Applications.
function macUpdateLocationOk(): boolean {
  if (process.platform !== 'darwin') return true
  try {
    return app.isInApplicationsFolder()
  } catch {
    return true
  }
}

function desktopTv(key: string, version: string): string {
  return desktopT(key).replace('{version}', version)
}

function nextMockUpdateDialogResponse(): number | null {
  if (mockUpdateVersion() === null) return null
  if (mockUpdateDialogResponses === null) {
    const raw = (process.env[MOCK_UPDATE_DIALOG_RESPONSES_ENV] || '').trim()
    mockUpdateDialogResponses = raw
      ? raw.split(',').map((part) => Number(part.trim()))
      : []
  }
  const response = mockUpdateDialogResponses.shift()
  if (!Number.isInteger(response)) return null
  return Number(response)
}

function showUpdateDialog(
  options: Electron.MessageBoxOptions,
): Promise<Electron.MessageBoxReturnValue> {
  const mockResponse = nextMockUpdateDialogResponse()
  if (mockResponse !== null) {
    console.log(`[mock-updater] ${String(options.title || options.message || 'dialog')} response=${mockResponse}`)
    return Promise.resolve({ response: mockResponse, checkboxChecked: false })
  }
  const win = currentMainWindow()
  return win ? dialog.showMessageBox(win, options) : dialog.showMessageBox(options)
}

function classifyDesktopUpdateError(err: unknown): Exclude<DesktopUpdateErrorCode, null> {
  if (err instanceof UpdateChannelError) {
    if (err.code === 'manifest_invalid' || err.code === 'current_version_invalid') return 'manifest_invalid'
    if (err.code === 'checksum_unavailable') return 'checksum_unavailable'
    if (err.code === 'integrity_failed') return 'integrity_failed'
    if (err.code === 'download_failed') return 'download_failed'
    if (err.code === 'install_failed') return 'install_failed'
    return 'source_unreachable'
  }
  return updateDownloadInProgress ? 'download_failed' : 'source_unreachable'
}

function desktopUpdateErrorMessage(code: Exclude<DesktopUpdateErrorCode, null>): string {
  if (code === 'manifest_invalid') return desktopT('update.manifestInvalid')
  if (code === 'checksum_unavailable') return desktopT('update.checksumUnavailable')
  if (code === 'integrity_failed') return desktopT('update.integrityFailed')
  if (code === 'download_failed') return desktopT('update.downloadFailed')
  if (code === 'install_failed') return desktopT('update.installFailed')
  return desktopT('update.sourceUnavailable')
}

function showUpdateError(
  err: unknown,
  silentFallback: DesktopUpdateFailureFallback | null = null,
): void {
  const shouldNotify = desktopUpdateCheckScheduler.consumeManualRequest() || updateDownloadInProgress
  const errorCode = classifyDesktopUpdateError(err)
  updateDownloadInProgress = false
  if (!shouldNotify) {
    if (silentFallback && !['checking', 'downloading', 'applying'].includes(silentFallback.state.status)) {
      desktopUpdateCandidate = silentFallback.candidate
      setDesktopUpdateState({
        status: silentFallback.state.status,
        latestVersion: silentFallback.state.latestVersion,
        progress: silentFallback.state.progress,
        checkedAt: silentFallback.state.checkedAt,
        error: silentFallback.state.error,
        errorCode: silentFallback.state.errorCode,
        releaseUrl: silentFallback.state.releaseUrl,
        source: silentFallback.state.source,
        fallbackUsed: silentFallback.state.fallbackUsed,
      })
      return
    }
    setDesktopUpdateState({
      status: downloadedUpdateVersion ? 'downloaded' : 'idle',
      latestVersion: downloadedUpdateVersion,
      progress: downloadedUpdateVersion ? 100 : null,
      checkedAt: new Date().toISOString(),
      error: null,
      errorCode: null,
    })
    return
  }
  setDesktopUpdateState({
    status: 'error',
    progress: null,
    checkedAt: new Date().toISOString(),
    error: desktopUpdateErrorMessage(errorCode),
    errorCode,
  })
}

async function runMockUpdateFlow(version: string): Promise<void> {
  if (mockUpdatePromptActive) return
  mockUpdatePromptActive = true
  try {
    if (downloadedUpdateVersion === version) {
      setDesktopUpdateState({
        status: 'downloaded',
        latestVersion: version,
        progress: 100,
        checkedAt: new Date().toISOString(),
        error: null,
      })
    } else {
      setDesktopUpdateState({
        status: 'available',
        latestVersion: version,
        progress: null,
        checkedAt: new Date().toISOString(),
        error: null,
      })
    }
  } finally {
    mockUpdatePromptActive = false
    updateDownloadInProgress = false
  }
}

async function downloadDesktopUpdate(): Promise<DesktopUpdateState> {
  if (
    desktopUpdateInstallMode() === 'manual'
    && desktopUpdateStatus === 'downloaded'
    && verifiedManualInstallerPath
  ) {
    try {
      shell.showItemInFolder(verifiedManualInstallerPath)
    } catch (err) {
      console.error('[updater] failed to reveal verified manual installer', err)
      return setDesktopUpdateState({
        status: 'error',
        progress: null,
        error: desktopUpdateErrorMessage('install_failed'),
        errorCode: 'install_failed',
      })
    }
    return desktopUpdateSnapshot()
  }
  if (
    updateDownloadInProgress
    || manualInstallerActionInProgress
    || updateApplying
    || desktopUpdateStatus === 'downloaded'
  ) {
    return desktopUpdateSnapshot()
  }

  const mockVersion = mockUpdateVersion()
  if (mockVersion !== null) {
    const version = desktopUpdateLatestVersion || mockVersion
    updateDownloadInProgress = true
    setDesktopUpdateState({
      status: 'downloading',
      latestVersion: version,
      progress: 0,
      error: null,
    })
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 100)
    })
    updateDownloadInProgress = false
    downloadedUpdateVersion = version
    mockDownloadedUpdate = true
    createApplicationMenu()
    return setDesktopUpdateState({
      status: 'downloaded',
      latestVersion: version,
      progress: 100,
      error: null,
    })
  }

  if (desktopUpdateInstallMode() === 'manual') {
    manualInstallerActionInProgress = true
    try {
      if (desktopUpdateStatus === 'checking') await checkForUpdates(true)
      if (!desktopUpdateCandidate) await checkForUpdates(true)
      const candidate = desktopUpdateCandidate
      if (!candidate || desktopUpdateStatus !== 'available') return desktopUpdateSnapshot()
      updateDownloadInProgress = true
      verifiedManualInstallerPath = null

      let chosen: { source: DesktopUpdateSource; fallbackUsed: boolean }
      try {
        chosen = await chooseDesktopUpdateSource(candidate, candidate.installer)
      } catch (err) {
        console.error('[updater] manual installer sources are unreachable', err)
        return setDesktopUpdateState({
          status: 'error',
          progress: null,
          checkedAt: new Date().toISOString(),
          error: desktopUpdateErrorMessage('source_unreachable'),
          errorCode: 'source_unreachable',
        })
      }

      const installerUrl = updateAssetUrl(candidate, chosen.source)
      setDesktopUpdateState({
        status: 'downloading',
        latestVersion: candidate.version,
        progress: 0,
        releaseUrl: installerUrl,
        source: chosen.source,
        fallbackUsed: chosen.fallbackUsed,
        error: null,
        errorCode: null,
      })
      try {
        const expectedSha256 = await fetchCanonicalWindowsInstallerDigest(candidate)
        const verified = await downloadVerifiedWindowsInstallerWithFallback(
          candidate,
          chosen,
          expectedSha256,
        )
        verifiedManualInstallerPath = verified.path
        rememberSuccessfulUpdateSource(verified.source)
        setDesktopUpdateState({
          status: 'downloaded',
          latestVersion: candidate.version,
          progress: 100,
          checkedAt: new Date().toISOString(),
          releaseUrl: updateAssetUrl(candidate, verified.source),
          source: verified.source,
          fallbackUsed: verified.fallbackUsed,
          error: null,
          errorCode: null,
        })
        try {
          shell.showItemInFolder(verified.path)
        } catch (err) {
          throw new UpdateChannelError(
            'install_failed',
            `The verified installer could not be shown: ${String(err instanceof Error ? err.message : err)}`,
          )
        }
      } catch (err) {
        console.error('[updater] failed to prepare verified manual installer', err)
        showUpdateError(err)
        return desktopUpdateSnapshot()
      }
      return desktopUpdateSnapshot()
    } finally {
      updateDownloadInProgress = false
      manualInstallerActionInProgress = false
    }
  }

  if (!autoUpdateSupported()) return desktopUpdateSnapshot()
  if (!macUpdateLocationOk()) {
    return setDesktopUpdateState({
      status: 'error',
      progress: null,
      error: desktopT('update.moveToApplications'),
      errorCode: null,
    })
  }

  initAutoUpdater()
  let candidate = desktopUpdateCandidate
  if (!candidate || !nativeUpdateReadyFor(candidate)) {
    await checkForUpdates(true)
    candidate = desktopUpdateCandidate
  }
  if (!candidate || !nativeUpdateReadyFor(candidate)) return desktopUpdateSnapshot()

  updateDownloadInProgress = true
  setDesktopUpdateState({
    status: 'downloading',
    latestVersion: candidate.version,
    progress: 0,
    error: null,
  })
  try {
    await downloadNativeDesktopUpdateWithFallback()
  } catch (err) {
    console.error('[updater] download failed', err)
    showUpdateError(err)
  }
  return desktopUpdateSnapshot()
}

function initAutoUpdater(): void {
  if (autoUpdaterReady || !autoUpdateSupported()) return
  autoUpdaterReady = true

  // Consent-based: the bundled gateway + ML runtime make updates large, so we
  // never download without asking. We also keep installation on the explicit
  // restart path so applyDownloadedUpdate() can drain the owned gateway first.
  autoUpdater.autoDownload = false
  autoUpdater.autoInstallOnAppQuit = false
  // electron-updater generates a persistent per-install staging UUID even
  // when no staged rollout is configured. Override the outbound header with a
  // fixed, non-user-specific value; OpenSquilla channels do not use rollout
  // bucketing and update checks should not add a cross-request identifier.
  autoUpdater.requestHeaders = {
    'x-user-staging-id': '00000000-0000-4000-8000-000000000000',
  }
  autoUpdater.logger = {
    info: (m: unknown) => console.log('[updater]', m),
    warn: (m: unknown) => console.warn('[updater]', m),
    error: (m: unknown) => console.error('[updater]', m),
    debug: () => {},
  }

  autoUpdater.on('update-available', (info) => {
    console.log('[updater] provider reports update available', String(info?.version ?? ''))
  })

  autoUpdater.on('update-not-available', (info) => {
    console.log('[updater] provider reports no update', String(info?.version ?? ''))
  })

  autoUpdater.on('download-progress', (progress) => {
    const percent = Number(progress?.percent)
    setDesktopUpdateState({
      status: 'downloading',
      progress: Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null,
      error: null,
    })
  })

  autoUpdater.on('update-downloaded', (info) => {
    updateDownloadInProgress = false
    const version = String(info?.version ?? '')
    downloadedUpdateVersion = version
    mockDownloadedUpdate = false
    createApplicationMenu()
    setDesktopUpdateState({
      status: 'downloaded',
      latestVersion: version || null,
      progress: 100,
      checkedAt: new Date().toISOString(),
      error: null,
    })
  })

  autoUpdater.on('error', (err) => {
    console.error('[updater] error', err)
    // electron-updater also rejects the active check/download promise. The
    // promise owner performs source fallback and publishes at most one final
    // error after both sources have failed.
  })
}

// ── Static release-channel discovery and regional source selection ─────────
// GitHub Release remains the source of truth, but clients discover the moving
// stable / same-base preview channel from a small OSS JSON manifest. Once the
// strict tag is known, metadata and installers can come from either the OSS
// version directory or the matching GitHub Release without using the anonymous
// GitHub Releases API.

interface ResolvedDesktopUpdate {
  candidate: DesktopUpdateCandidate
  source: DesktopUpdateSource
  fallbackUsed: boolean
}

function desktopUpdatePlatform(): DesktopUpdatePlatform | null {
  if (process.platform === 'darwin' && process.arch === 'arm64') return 'darwin-arm64'
  if (process.platform === 'win32' && process.arch === 'x64') return 'win32-x64'
  return null
}

function desktopUpdateLocaleTags(): string[] {
  const preferred = typeof app.getPreferredSystemLanguages === 'function'
    ? app.getPreferredSystemLanguages()
    : []
  return [...preferred, app.getLocale()]
}

async function fetchDesktopUpdateChannelFromRoot(root?: string): Promise<unknown> {
  const url = updateChannelManifestUrl(app.getVersion(), root)
  if (!url) {
    throw new UpdateChannelError('manifest_invalid', 'The installed version has no supported update channel.')
  }
  let response: Response
  try {
    response = await fetch(url, {
      headers: { Accept: 'application/json', 'User-Agent': 'OpenSquilla-Desktop' },
      signal: AbortSignal.timeout(8000),
      cache: 'no-store',
    })
  } catch (err) {
    throw new UpdateChannelError(
      'source_unreachable',
      `The update channel is temporarily unreachable: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  if (!response.ok) {
    throw new UpdateChannelError('source_unreachable', 'The update channel is temporarily unavailable.')
  }
  try {
    return await response.json()
  } catch {
    throw new UpdateChannelError('manifest_invalid', 'The update channel returned invalid JSON.')
  }
}

async function fetchDesktopUpdateChannelFromGithubReleases(): Promise<unknown> {
  let response: Response
  try {
    response = await fetch(UPDATE_GITHUB_RELEASES_API_URL, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'OpenSquilla-Desktop' },
      signal: AbortSignal.timeout(8000),
      cache: 'no-store',
    })
  } catch (err) {
    throw new UpdateChannelError(
      'source_unreachable',
      `The GitHub release inventory is temporarily unreachable: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new UpdateChannelError('source_unreachable', 'The GitHub release inventory is temporarily unavailable.')
  }
  let inventory: unknown
  try {
    inventory = await response.json()
  } catch {
    throw new UpdateChannelError('manifest_invalid', 'The GitHub release inventory returned invalid JSON.')
  }
  const manifest = updateChannelManifestFromReleaseInventory(app.getVersion(), inventory)
  if (!manifest) {
    throw new UpdateChannelError(
      'source_unreachable',
      'The GitHub release inventory has no release for this update channel.',
    )
  }
  return manifest
}

async function fetchDesktopUpdateChannel(): Promise<unknown> {
  if (!updateChannelPathForVersion(app.getVersion())) {
    throw new UpdateChannelError('manifest_invalid', 'The installed version has no supported update channel.')
  }
  const rootOverride = (process.env.OPENSQUILLA_DESKTOP_UPDATE_CHANNEL_ROOT || '').trim()
  if (rootOverride) return await fetchDesktopUpdateChannelFromRoot(rootOverride)
  loadDesktopUpdatePersistence()
  // Discovery itself is dual-sourced: the mirrored channel manifest and the
  // GitHub release inventory are tried in the same order as asset downloads,
  // so an unreachable mirror cannot disable update checks outright.
  const order = orderedUpdateSources(
    desktopUpdateLocaleTags(),
    lastSuccessfulUpdateSource,
    process.env.OPENSQUILLA_DESKTOP_UPDATE_SOURCE,
  )
  let lastError: unknown = null
  for (const source of order) {
    try {
      return source === 'oss'
        ? await fetchDesktopUpdateChannelFromRoot()
        : await fetchDesktopUpdateChannelFromGithubReleases()
    } catch (err) {
      lastError = err
      desktopLog('update_channel_discovery_failed', {
        source,
        error: String(err instanceof Error ? err.message : err),
      })
    }
  }
  if (lastError instanceof UpdateChannelError) throw lastError
  throw new UpdateChannelError('source_unreachable', 'No desktop update discovery source is reachable.')
}

async function probeDesktopUpdateSource(
  candidate: DesktopUpdateCandidate,
  source: DesktopUpdateSource,
  asset = candidate.feed,
): Promise<void> {
  const url = updateAssetUrl(candidate, source, asset)
  let response: Response
  try {
    response = await fetch(url, {
      headers: { Accept: 'application/octet-stream', Range: 'bytes=0-0', 'User-Agent': 'OpenSquilla-Desktop' },
      signal: AbortSignal.timeout(5000),
      cache: 'no-store',
    })
  } catch (err) {
    throw new UpdateChannelError(
      'source_unreachable',
      `${source} update source is unreachable: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  if (!response.ok) {
    throw new UpdateChannelError('source_unreachable', `${source} update source is unavailable.`)
  }
  await response.body?.cancel().catch(() => {})
}

async function chooseDesktopUpdateSource(
  candidate: DesktopUpdateCandidate,
  asset = candidate.feed,
): Promise<{ source: DesktopUpdateSource; fallbackUsed: boolean }> {
  loadDesktopUpdatePersistence()
  const order = orderedUpdateSources(
    desktopUpdateLocaleTags(),
    lastSuccessfulUpdateSource,
    process.env.OPENSQUILLA_DESKTOP_UPDATE_SOURCE,
  )
  let lastError: unknown = null
  for (let index = 0; index < order.length; index += 1) {
    const source = order[index]
    try {
      await probeDesktopUpdateSource(candidate, source, asset)
      return { source, fallbackUsed: index > 0 }
    } catch (err) {
      lastError = err
      desktopLog('update_source_probe_failed', {
        source,
        error: String(err instanceof Error ? err.message : err),
      })
    }
  }
  if (lastError instanceof UpdateChannelError) throw lastError
  throw new UpdateChannelError('source_unreachable', 'No desktop update source is reachable.')
}

function rememberSuccessfulUpdateSource(source: DesktopUpdateSource): void {
  if (lastSuccessfulUpdateSource === source) return
  lastSuccessfulUpdateSource = source
  void persistDesktopUpdateState()
}

// The OSS mirror is the primary checksum source; the canonical GitHub Release
// copy is the fail-over so a missing or unreadable mirror object cannot block
// verified updates. Each source gets one initial fetch plus two retries before
// the next source is tried.
const DESKTOP_UPDATE_CHECKSUM_SOURCES: readonly DesktopUpdateSource[] = ['oss', 'github']
const UPDATE_CHECKSUM_FETCH_ATTEMPTS = 3
const UPDATE_CHECKSUM_RETRY_DELAY_MS = 500

async function fetchWindowsInstallerDigestFromSource(
  candidate: DesktopUpdateCandidate,
  source: DesktopUpdateSource,
): Promise<string> {
  const checksumUrl = updateAssetUrl(candidate, source, 'SHA256SUMS')
  let response: Response
  try {
    response = await fetch(checksumUrl, {
      headers: { Accept: 'text/plain', 'User-Agent': 'OpenSquilla-Desktop' },
      signal: AbortSignal.timeout(10_000),
      cache: 'no-store',
    })
  } catch (err) {
    throw new UpdateChannelError(
      'checksum_unavailable',
      `The ${source} checksum is unreachable: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new UpdateChannelError(
      'checksum_unavailable',
      `The ${source} checksum returned HTTP ${response.status}.`,
    )
  }
  try {
    const contents = await readResponseTextWithLimit(response, UPDATE_CHECKSUM_MAX_BYTES)
    return parseSha256SumsForAsset(contents, candidate.installer)
  } catch (err) {
    if (err instanceof UpdateChannelError) throw err
    throw new UpdateChannelError(
      'integrity_failed',
      `The ${source} checksum could not be parsed: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
}

async function fetchWindowsInstallerDigestWithRetries(
  candidate: DesktopUpdateCandidate,
  source: DesktopUpdateSource,
): Promise<string> {
  let lastError: unknown = null
  for (let attempt = 1; attempt <= UPDATE_CHECKSUM_FETCH_ATTEMPTS; attempt += 1) {
    try {
      return await fetchWindowsInstallerDigestFromSource(candidate, source)
    } catch (err) {
      // A malformed SHA256SUMS is deterministic: refetching the same bytes
      // cannot succeed, so skip the remaining attempts for this source.
      if (err instanceof UpdateChannelError && err.code === 'integrity_failed') throw err
      lastError = err
      if (attempt < UPDATE_CHECKSUM_FETCH_ATTEMPTS) {
        desktopLog('update_checksum_fetch_retry', {
          source,
          attempt,
          error: String(err instanceof Error ? err.message : err),
        })
        await new Promise((resolveWait) =>
          setTimeout(resolveWait, UPDATE_CHECKSUM_RETRY_DELAY_MS * attempt),
        )
      }
    }
  }
  throw lastError
}

async function fetchCanonicalWindowsInstallerDigest(
  candidate: DesktopUpdateCandidate,
): Promise<string> {
  let lastError: unknown = null
  for (const source of DESKTOP_UPDATE_CHECKSUM_SOURCES) {
    try {
      return await fetchWindowsInstallerDigestWithRetries(candidate, source)
    } catch (err) {
      lastError = err
      desktopLog('update_checksum_fetch_failed', {
        source,
        error: String(err instanceof Error ? err.message : err),
      })
    }
  }
  if (lastError instanceof UpdateChannelError) throw lastError
  throw new UpdateChannelError('checksum_unavailable', 'No checksum source is reachable.')
}

async function downloadVerifiedWindowsInstaller(
  candidate: DesktopUpdateCandidate,
  source: DesktopUpdateSource,
  expectedSha256: string,
): Promise<string> {
  const installerUrl = updateAssetUrl(candidate, source)
  let response: Response
  try {
    response = await fetch(installerUrl, {
      headers: { Accept: 'application/octet-stream', 'User-Agent': 'OpenSquilla-Desktop' },
      signal: AbortSignal.timeout(UPDATE_INSTALLER_DOWNLOAD_TIMEOUT_MS),
      cache: 'no-store',
    })
  } catch (err) {
    throw new UpdateChannelError(
      'download_failed',
      `The installer download failed: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new UpdateChannelError('download_failed', `The installer returned HTTP ${response.status}.`)
  }

  const destinationPath = join(
    app.getPath('userData'),
    'update-downloads',
    candidate.installer,
  )
  let lastProgress = -1
  let reportedUnknownLength = false
  try {
    const result = await streamResponseToVerifiedFile(
      response,
      destinationPath,
      expectedSha256,
      {
        maxBytes: UPDATE_INSTALLER_MAX_BYTES,
        onProgress(receivedBytes, totalBytes) {
          if (totalBytes === null || totalBytes <= 0) {
            if (reportedUnknownLength) return
            reportedUnknownLength = true
            setDesktopUpdateState({ status: 'downloading', progress: null, error: null })
            return
          }
          const progress = Math.max(0, Math.min(100, Math.floor((receivedBytes / totalBytes) * 100)))
          if (progress === lastProgress) return
          lastProgress = progress
          setDesktopUpdateState({ status: 'downloading', progress, error: null })
        },
      },
    )
    return result.path
  } catch (err) {
    if (err instanceof UpdateChannelError) throw err
    throw new UpdateChannelError(
      'download_failed',
      `The installer download could not be saved: ${String(err instanceof Error ? err.message : err)}`,
    )
  }
}

interface VerifiedManualInstaller {
  path: string
  source: DesktopUpdateSource
  fallbackUsed: boolean
}

async function downloadVerifiedWindowsInstallerWithFallback(
  candidate: DesktopUpdateCandidate,
  chosen: { source: DesktopUpdateSource; fallbackUsed: boolean },
  expectedSha256: string,
): Promise<VerifiedManualInstaller> {
  const attempts: DesktopUpdateSource[] = [
    chosen.source,
    alternateDesktopUpdateSource(chosen.source),
  ]
  let lastError: unknown = null
  let integrityError: UpdateChannelError | null = null
  for (let index = 0; index < attempts.length; index += 1) {
    const source = attempts[index]
    const fallbackUsed = chosen.fallbackUsed || index > 0
    setDesktopUpdateState({
      status: 'downloading',
      progress: 0,
      releaseUrl: updateAssetUrl(candidate, source),
      source,
      fallbackUsed,
      error: null,
      errorCode: null,
    })
    try {
      const path = await downloadVerifiedWindowsInstaller(candidate, source, expectedSha256)
      return { path, source, fallbackUsed }
    } catch (err) {
      lastError = err
      if (err instanceof UpdateChannelError && err.code === 'integrity_failed') {
        integrityError = err
      }
      const retryable = err instanceof UpdateChannelError
        && (err.code === 'download_failed' || err.code === 'integrity_failed')
      desktopLog('update_manual_download_failed', {
        source,
        retrying: retryable && index + 1 < attempts.length,
        error: String(err instanceof Error ? err.message : err),
      })
      if (!retryable) throw err
    }
  }
  if (integrityError) throw integrityError
  if (lastError instanceof Error) throw lastError
  throw new UpdateChannelError('download_failed', 'No verified Windows installer source is reachable.')
}

function alternateDesktopUpdateSource(source: DesktopUpdateSource): DesktopUpdateSource {
  return source === 'oss' ? 'github' : 'oss'
}

function nativeUpdateReadyFor(candidate: DesktopUpdateCandidate): boolean {
  return nativeUpdateReady?.tag === candidate.tag
    && nativeUpdateReady.version === candidate.version
    && nativeUpdateReady.source === desktopUpdateSource
}

async function resolveDesktopUpdate(): Promise<ResolvedDesktopUpdate | null> {
  const platform = desktopUpdatePlatform()
  if (!platform) {
    throw new UpdateChannelError('manifest_invalid', 'This desktop architecture has no update feed.')
  }
  const manifest = await fetchDesktopUpdateChannel()
  const candidate = candidateFromUpdateChannel(app.getVersion(), manifest, platform)
  if (!candidate) return null
  const chosen = await chooseDesktopUpdateSource(candidate)
  return { candidate, ...chosen }
}

function configureDesktopUpdateFeed(resolved: ResolvedDesktopUpdate): void {
  autoUpdater.allowDowngrade = false
  autoUpdater.setFeedURL({
    provider: 'generic',
    url: updateFeedBaseUrl(resolved.candidate, resolved.source),
    channel: 'latest',
  })
  // The strict manifest resolver already proved a same-base RC is a forward
  // move. electron-updater compares identifiers such as rc9/rc10 lexically, so
  // allow its apparent "downgrade" only for that validated prerelease path.
  const current = parseOpenSquillaReleaseTag(app.getVersion())
  autoUpdater.allowDowngrade = current?.rc !== null && current?.rc !== undefined
  desktopLog('update_feed_resolved', {
    tag: resolved.candidate.tag,
    version: resolved.candidate.version,
    source: resolved.source,
    fallbackUsed: resolved.fallbackUsed,
  })
}

async function checkNativeDesktopUpdate(resolved: ResolvedDesktopUpdate): Promise<void> {
  nativeUpdateReady = null
  const attempts: ResolvedDesktopUpdate[] = [
    resolved,
    {
      candidate: resolved.candidate,
      source: alternateDesktopUpdateSource(resolved.source),
      fallbackUsed: true,
    },
  ]
  let lastError: unknown = null
  for (let index = 0; index < attempts.length; index += 1) {
    const attempt = attempts[index]
    try {
      if (index > 0) await probeDesktopUpdateSource(attempt.candidate, attempt.source)
      configureDesktopUpdateFeed(attempt)
      const result = await autoUpdater.checkForUpdates()
      const feedVersion = String(result?.updateInfo?.version ?? '').trim()
      if (result?.isUpdateAvailable !== true || feedVersion !== attempt.candidate.version) {
        throw new UpdateChannelError(
          'manifest_invalid',
          `Update feed did not offer the expected version ${attempt.candidate.version} (received ${feedVersion || '<missing>'}).`,
        )
      }
      rememberSuccessfulUpdateSource(attempt.source)
      desktopUpdateCandidate = attempt.candidate
      nativeUpdateReady = {
        tag: attempt.candidate.tag,
        version: attempt.candidate.version,
        source: attempt.source,
      }
      setDesktopUpdateState({
        status: 'available',
        latestVersion: attempt.candidate.version,
        progress: null,
        checkedAt: new Date().toISOString(),
        releaseUrl: attempt.candidate.releaseUrl,
        source: attempt.source,
        fallbackUsed: resolved.fallbackUsed || attempt.fallbackUsed,
        error: null,
        errorCode: null,
      })
      return
    } catch (err) {
      lastError = err
      desktopLog('update_native_check_failed', {
        source: attempt.source,
        error: String(err instanceof Error ? err.message : err),
      })
    }
  }
  throw lastError ?? new UpdateChannelError('source_unreachable', 'No desktop update source is reachable.')
}

async function downloadNativeDesktopUpdateWithFallback(): Promise<void> {
  const readyCandidate = desktopUpdateCandidate
  if (!readyCandidate || !nativeUpdateReadyFor(readyCandidate)) {
    throw new UpdateChannelError('manifest_invalid', 'The selected native update feed is not verified.')
  }
  try {
    await autoUpdater.downloadUpdate()
    return
  } catch (firstError) {
    const candidate = desktopUpdateCandidate
    const currentSource = desktopUpdateSource
    if (!candidate || !currentSource) throw firstError
    nativeUpdateReady = null

    const fallback: ResolvedDesktopUpdate = {
      candidate,
      source: alternateDesktopUpdateSource(currentSource),
      fallbackUsed: true,
    }
    desktopLog('update_native_download_retry', {
      from: currentSource,
      to: fallback.source,
      error: String(firstError instanceof Error ? firstError.message : firstError),
    })
    await probeDesktopUpdateSource(candidate, fallback.source)
    configureDesktopUpdateFeed(fallback)
    const result = await autoUpdater.checkForUpdates()
    const feedVersion = String(result?.updateInfo?.version ?? '').trim()
    if (result?.isUpdateAvailable !== true || feedVersion !== candidate.version) {
      throw new UpdateChannelError(
        'manifest_invalid',
        `Fallback update feed did not offer ${candidate.version} (received ${feedVersion || '<missing>'}).`,
      )
    }
    rememberSuccessfulUpdateSource(fallback.source)
    nativeUpdateReady = {
      tag: candidate.tag,
      version: candidate.version,
      source: fallback.source,
    }
    updateDownloadInProgress = true
    setDesktopUpdateState({
      status: 'downloading',
      latestVersion: candidate.version,
      progress: 0,
      source: fallback.source,
      fallbackUsed: true,
      releaseUrl: candidate.releaseUrl,
      error: null,
      errorCode: null,
    })
    await autoUpdater.downloadUpdate()
  }
}

function desktopUpdateCheckAllowed(): boolean {
  return isUpdateCheckAllowed({
    downloading: updateDownloadInProgress || (manualInstallerActionInProgress && desktopUpdateCandidate !== null),
    applying: updateApplying,
    downloaded: downloadedUpdateVersion !== null || desktopUpdateStatus === 'downloaded',
  })
}

async function runDesktopUpdateCheck(): Promise<void> {
  // Keep this defensive guard even though the scheduler checks the same state:
  // download/apply events can change it between admission and execution.
  if (!desktopUpdateCheckAllowed()) return

  const mockVersion = mockUpdateVersion()
  if (mockVersion !== null) {
    setDesktopUpdateState({
      status: 'checking',
      latestVersion: desktopUpdateLatestVersion || mockVersion,
      progress: null,
      checkedAt: new Date().toISOString(),
      error: null,
    })
    await runMockUpdateFlow(mockVersion)
    return
  }

  if (!desktopUpdateManaged()) {
    if (desktopUpdateCheckScheduler.manualRequestPending) {
      setDesktopUpdateState({
        status: 'error',
        progress: null,
        checkedAt: new Date().toISOString(),
        error: desktopT('update.errorTitle'),
        errorCode: 'source_unreachable',
      })
    }
    return
  }

  // Guide the user to /Applications first, otherwise the in-place swap fails.
  if (process.platform === 'darwin' && !macUpdateLocationOk()) {
    setDesktopUpdateState({
      status: 'error',
      progress: null,
      checkedAt: new Date().toISOString(),
      error: desktopT('update.moveToApplications'),
      errorCode: null,
    })
    return
  }

  if (nativeAutoUpdateEnabled()) initAutoUpdater()
  const failureFallback: DesktopUpdateFailureFallback = {
    state: desktopUpdateSnapshot(),
    candidate: desktopUpdateCandidate,
  }
  if (desktopUpdateInstallMode() === 'native') nativeUpdateReady = null
  setDesktopUpdateState({
    status: 'checking',
    progress: null,
    checkedAt: new Date().toISOString(),
    error: null,
    errorCode: null,
  })
  try {
    const resolved = await resolveDesktopUpdate()
    if (!resolved) {
      desktopUpdateCandidate = null
      nativeUpdateReady = null
      verifiedManualInstallerPath = null
      setDesktopUpdateState({
        status: 'not-available',
        latestVersion: app.getVersion(),
        progress: null,
        checkedAt: new Date().toISOString(),
        error: null,
        errorCode: null,
        releaseUrl: null,
        source: null,
        fallbackUsed: false,
      })
      return
    }
    const manualInstall = desktopUpdateInstallMode() === 'manual'
    if (manualInstall) {
      verifiedManualInstallerPath = null
      desktopUpdateCandidate = resolved.candidate
      setDesktopUpdateState({
        status: 'available',
        latestVersion: resolved.candidate.version,
        progress: null,
        checkedAt: new Date().toISOString(),
        releaseUrl: updateAssetUrl(resolved.candidate, resolved.source),
        source: resolved.source,
        fallbackUsed: resolved.fallbackUsed,
        error: null,
        errorCode: null,
      })
      return
    }
    await checkNativeDesktopUpdate(resolved)
  } catch (err) {
    console.error('[updater] checkForUpdates failed', err)
    showUpdateError(err, failureFallback)
  }
}

const desktopUpdateCheckScheduler = new UpdateCheckScheduler({
  runCheck: runDesktopUpdateCheck,
  canCheck: desktopUpdateCheckAllowed,
  repeatDelayMs: UPDATE_CHECK_REPEAT_DELAY_MS,
})

function checkForUpdates(manual: boolean): Promise<void> {
  return desktopUpdateCheckScheduler.request(manual)
}

async function waitForGatewayProcessExit(
  child: ChildProcessWithoutNullStreams,
  timeoutMs = UPDATE_GATEWAY_EXIT_TIMEOUT_MS,
): Promise<boolean> {
  if (hasGatewayProcessExited(child)) return true
  return new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (exited: boolean) => {
      if (settled) return
      settled = true
      resolve(exited)
    }
    child.once('exit', () => finish(true))
    setTimeout(() => {
      finish(hasGatewayProcessExited(child))
    }, timeoutMs).unref()
  })
}

async function stopAndJoinAllLifecycleOwnedGateways(
  stopCurrentProcess: (child: ChildProcessWithoutNullStreams) => void = () => stopGateway(),
): Promise<boolean> {
  return await stopAndJoinLifecycleProcesses({
    currentProcess: () => (
      gatewayProcess && gatewayState.owned && !hasGatewayProcessExited(gatewayProcess)
        ? gatewayProcess
        : null
    ),
    stopCurrentProcess,
    liveProcesses: liveLifecycleOwnedGatewayProcesses,
    waitForExit: (child) => waitForGatewayProcessExit(child),
  })
}

function restoreDownloadedUpdateRetryState(
  pendingVersion: string | null,
  writerAdmissionToken: symbol | null = null,
): boolean {
  if (writerAdmissionToken) desktopWriters.reopen(writerAdmissionToken)
  downloadedUpdateVersion = pendingVersion
  updateApplying = false
  updateInstallHandoffReady = false
  isQuitting = false
  setAppExitPhase('running', 'update handoff did not commit')
  createWindowsTray()
  createApplicationMenu()
  setDesktopUpdateState({
    status: pendingVersion ? 'downloaded' : 'error',
    latestVersion: pendingVersion,
    progress: pendingVersion ? 100 : null,
  })
  if (!quitRequestedDuringUpdateDrain) return false
  quitRequestedDuringUpdateDrain = false
  setImmediate(() => app.quit())
  return true
}

// Stop the owned gateway child and WAIT for it to exit before handing control to
// the installer. The gateway holds the listen port + a PID lock and (on Windows)
// open file handles under resources/runtime that the installer must overwrite —
// orphaning it breaks the next launch. Mirrors the uninstall quiesce path.
async function applyDownloadedUpdate(): Promise<void> {
  if (updateApplying) return
  if (isQuitting || desktopWriters.closed) return
  if (!mockDownloadedUpdate && !downloadedUpdateVersion) return

  if (mockDownloadedUpdate) {
    const version = downloadedUpdateVersion || mockUpdateVersion() || app.getVersion()
    updateApplying = true
    setDesktopUpdateState({
      status: 'applying',
      latestVersion: version,
      progress: 100,
      error: null,
    })
    try {
      await showUpdateDialog({
        type: 'info',
        buttons: ['OK'],
        title: desktopT('update.mockInstallTitle'),
        message: desktopT('update.mockInstallTitle'),
        detail: desktopTv('update.mockInstallDetail', version),
      })
    } finally {
      downloadedUpdateVersion = version
      updateApplying = false
      createApplicationMenu()
      setDesktopUpdateState({
        status: 'downloaded',
        latestVersion: version,
        progress: 100,
        error: null,
      })
      if (quitRequestedDuringUpdateDrain) {
        quitRequestedDuringUpdateDrain = false
        setImmediate(() => app.quit())
      }
    }
    return
  }
  const pendingVersion = downloadedUpdateVersion
  updateApplying = true
  setAppExitPhase('deferred', 'preparing downloaded update')
  // Never interrupt a reconcile/workspace/settings transaction after it has
  // been admitted. Close new writers and wait for existing operations to
  // finish naturally before the installer handoff.
  const updateWriterAdmission = desktopWriters.close('apply downloaded update')
  await waitForDesktopWriterOperations()
  downloadedUpdateVersion = null
  createApplicationMenu()
  setDesktopUpdateState({
    status: 'applying',
    latestVersion: pendingVersion,
    progress: 100,
    error: null,
  })
  isQuitting = true
  setAppExitPhase('draining', 'stopping Gateway for downloaded update')
  const exited = await stopAndJoinAllLifecycleOwnedGateways((child) => {
    updateGatewayShutdownProcess = child
    // We stay alive and await the exit below, so let the gateway take its
    // Windows HTTP graceful drain instead of an immediate TerminateProcess.
    allowGracefulShutdownWhileQuitting = true
    try {
      stopGateway()
    } finally {
      allowGracefulShutdownWhileQuitting = false
    }
  })
  // Re-read the shared ownership set immediately before handoff. There is no
  // await between this check and quitAndInstall, so a child already stopping
  // for Retry/recovery cannot be skipped by the installer lifecycle.
  if (!exited || liveLifecycleOwnedGatewayProcesses().length > 0) {
    const quitResumed = restoreDownloadedUpdateRetryState(
      pendingVersion,
      updateWriterAdmission,
    )
    if (!quitResumed) {
      void showUpdateDialog({
        type: 'error',
        buttons: ['OK'],
        title: desktopT('update.errorTitle'),
        message: desktopT('update.errorTitle'),
        detail: desktopT('update.gatewayShutdownTimeout'),
      })
    }
    return
  }
  updateGatewayShutdownProcess = null
  // isSilent=false (show the platform installer UI where applicable),
  // isForceRunAfter=true (relaunch after install).
  try {
    updateInstallHandoffReady = true
    setAppExitPhase('committed', 'handing off to desktop updater')
    autoUpdater.quitAndInstall(false, true)
  } catch (err) {
    const quitResumed = restoreDownloadedUpdateRetryState(
      pendingVersion,
      updateWriterAdmission,
    )
    if (!quitResumed) {
      void showUpdateDialog({
        type: 'error',
        buttons: ['OK'],
        title: desktopT('update.errorTitle'),
        message: desktopT('update.errorTitle'),
        detail: String(err instanceof Error ? err.message : err ?? ''),
      })
    }
    // The owned gateway was stopped for the (now-failed) handoff and its exit was
    // swallowed as intentional (isQuitting was true). restoreDownloadedUpdateRetryState
    // cleared isQuitting, so bring the runtime back up instead of leaving the
    // window stranded on the dead gateway's Control UI.
    if (!quitResumed) void openOrResumeDesktopApp()
  }
}

// Lets the gateway-served Control UI know whether this desktop owns update
// discovery. Discovery ownership and native installation are deliberately separate:
// unsigned Windows builds use the managed exact-installer flow, while macOS
// can install the verified archive in place.
ipcMain.handle('desktop:update:managed', () => desktopUpdateManaged() || mockUpdateVersion() !== null)
ipcMain.handle('desktop:update:supported', () => nativeAutoUpdateEnabled())
ipcMain.handle('desktop:update:state', () => desktopUpdateSnapshot())
ipcMain.handle('desktop:update:check', async () => {
  await checkForUpdates(true)
  return desktopUpdateSnapshot()
})
ipcMain.handle('desktop:update:download', async () => downloadDesktopUpdate())
ipcMain.handle('desktop:update:relaunch', async () => {
  await applyDownloadedUpdate()
  return desktopUpdateSnapshot()
})
ipcMain.handle('desktop:update:dismiss', async () => dismissDesktopUpdate())
ipcMain.handle('desktop:os-locale', () => desktopLocale)
ipcMain.handle('desktop:theme:set', (_event, payload: unknown) => (
  applyDesktopNativeTheme(normalizeDesktopNativeThemeSource(payload))
))
ipcMain.handle('gateway:status', () => ({ ...gatewayState }))
ipcMain.handle('gateway:cli-invocation', async () => {
  const runtime = await resolveGatewayRuntime()
  return buildCliInvocation({
    platform: process.platform,
    mode: runtime.mode,
    binaryPath: runtime.mode === 'bundled' ? runtime.command : undefined,
    repoRoot: runtime.mode === 'dev' ? repoRoot : undefined,
    stateDir: desktopHome(),
    configPath: desktopConfigPath(),
  })
})
ipcMain.handle('gateway:reveal-log', async () => {
  if (gatewayState.logPath) {
    await shell.showItemInFolder(gatewayState.logPath)
    return true
  }
  // Startup can fail before the gateway log path is assigned (e.g. onboarding or
  // port selection error), so Reveal log would otherwise be a dead button on the
  // error panel. Fall back to the always-present desktop lifecycle log.
  const desktopLogPath = join(app.getPath('userData'), 'logs', 'desktop.log')
  if (existsSync(desktopLogPath)) {
    await shell.showItemInFolder(desktopLogPath)
    return true
  }
  await shell.openPath(join(app.getPath('userData'), 'logs')).catch(() => null)
  return false
})
ipcMain.handle('desktop:settings:get', async () => loadDesktopSettings())
ipcMain.handle('desktop:settings:save', async (_event, payload: DesktopSettingsPayload) => saveDesktopSettings(payload))
ipcMain.handle('desktop:settings:reset', async (event) => {
  if (!trustedRecoveryIpc(event)) throw new Error('Untrusted Desktop reset request.')
  return await resetDesktopSettingsThroughCleanup()
})
ipcMain.handle('desktop:preferences:get', (event) => {
  if (!trustedMainWindowControlIpc(event)) throw new Error('Untrusted Desktop preferences request.')
  return desktopPreferencesSnapshot()
})
ipcMain.handle('desktop:preferences:save', async (event, payload: DesktopPreferencesPayload) => {
  if (!trustedMainWindowControlIpc(event)) throw new Error('Untrusted Desktop preferences request.')
  return await saveDesktopPreferences(payload)
})
ipcMain.handle('desktop:sandbox:unavailable', async (event, payload: unknown) => {
  if (!trustedMainWindowControlIpc(event)) {
    throw new Error('Untrusted sandbox availability report.')
  }
  return await reportSandboxUnavailable(payload)
})
ipcMain.handle('desktop:artifact:open', async (_event, payload: ArtifactOpenRequest) => openArtifactWithDefaultApp(payload))
ipcMain.handle('desktop:workspace:choose-directory', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) return null
  const choice = await dialog.showOpenDialog(
    currentMainWindow()!,
    projectDirectoryDialogOptions(process.platform, payload),
  )
  if (choice.canceled || choice.filePaths.length !== 1) return null
  return { path: resolve(choice.filePaths[0]!) }
})
ipcMain.handle('desktop:workbench:capabilities', (event) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  return process.env.OPENSQUILLA_PREVIEW_FORCE_OFFLINE === '1'
    ? { ...NATIVE_WORKBENCH_CAPABILITIES, modes: ['offline'] as const }
    : NATIVE_WORKBENCH_CAPABILITIES
})
ipcMain.handle('desktop:workbench:preview-lease:create', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  return await artifactPreviewLeaseBroker.create(payload)
})
ipcMain.handle('desktop:workbench:preview-lease:renew', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  return await artifactPreviewLeaseBroker.renew(payload)
})
ipcMain.handle('desktop:workbench:preview-lease:revoke', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  return await artifactPreviewLeaseBroker.revoke(payload)
})
ipcMain.handle('desktop:workbench:surface:create', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    const request = parseNativeWorkbenchCreateRequest(payload)
    if (
      request.kind === 'artifact-preview'
      && !artifactPreviewLeaseBroker.authorizesSurface(request.payload)
    ) {
      return {
        ok: false,
        message: 'The artifact preview lease is not authorized by this Desktop Gateway.',
      }
    }
    return await nativeWorkbenchSurfaces.createSurface(request)
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})
ipcMain.handle('desktop:workbench:surface:navigate', async (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    return await nativeWorkbenchSurfaces.navigateSurface(
      parseNativeWorkbenchNavigationRequest(payload),
    )
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})
ipcMain.handle('desktop:workbench:permission:respond', (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    return nativeWorkbenchSurfaces.respondToPermission(
      parseNativeWorkbenchPermissionResponse(payload),
    )
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})
ipcMain.handle('desktop:workbench:surface:set-rect', (event, payload: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    return nativeWorkbenchSurfaces.setSurfaceRect(
      parseNativeWorkbenchSurfaceRectRequest(payload),
    )
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})
ipcMain.handle('desktop:workbench:surface:activate', (event, surfaceId: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    return nativeWorkbenchSurfaces.activateSurface(parseNativeWorkbenchSurfaceId(surfaceId))
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})
ipcMain.handle('desktop:workbench:surface:destroy', async (event, surfaceId: unknown) => {
  if (!trustedControlUiIpc(event)) throw new Error('Untrusted native Workbench request.')
  try {
    return await nativeWorkbenchSurfaces.destroySurface(
      parseNativeWorkbenchSurfaceId(surfaceId),
    )
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
})

// ── Desktop data cleanup ───────────────────────────────────────────────────
// Python owns inventory, locking, CAS, no-follow deletion, and partial recovery.
// Electron owns only trusted IPC, gateway/writer quiescence, explicit native
// confirmation, and post-operation lifecycle. Renderer payloads never provide a
// path, profile selector, transaction id, or revision.
const DESKTOP_CLEANUP_STDOUT_LIMIT = 2 * 1024 * 1024
const DESKTOP_CLEANUP_INSPECT_TIMEOUT_MS = 30_000
const DESKTOP_CLEANUP_APPROVAL_LIMIT = 512 * 1024
const DELETE_ALL_CONFIRMATION = 'DELETE ALL OPENSQUILLA DATA'

function currentDesktopCleanupSelection(mode: DesktopCleanupMode): DesktopCleanupSelection {
  return {
    mode,
    profileKey: desktopProfileKey(),
  }
}

async function runDesktopCleanupCli(
  profile: DesktopProfilePaths,
  command: 'cleanup-inspect' | 'cleanup-apply',
  args: string[],
): Promise<DesktopCleanupReport> {
  const runtime = await resolveGatewayRuntime()
  const prefix = runtime.args.slice(0, -2)
  return await new Promise((resolveResult, rejectResult) => {
    const child = spawn(runtime.command, [...prefix, 'recovery', command, ...args], {
      cwd: runtime.cwd,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: desktopChildEnvironment(profile, {
        OPENSQUILLA_RECOVERY_OFFLINE: '1',
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
      }),
    })
    let stdout = ''
    let oversized = false
    let settled = false
    let inspectTimeout: NodeJS.Timeout | null = null
    const finish = (error?: Error, report?: DesktopCleanupReport) => {
      if (settled) return
      settled = true
      if (inspectTimeout) clearTimeout(inspectTimeout)
      if (error) rejectResult(error)
      else resolveResult(report as DesktopCleanupReport)
    }
    if (command === 'cleanup-inspect') {
      inspectTimeout = setTimeout(() => {
        child.kill()
        finish(new Error('Cleanup inventory inspection timed out.'))
      }, DESKTOP_CLEANUP_INSPECT_TIMEOUT_MS)
      inspectTimeout.unref()
    }
    child.stdout.on('data', (chunk) => {
      if (oversized) return
      stdout += String(chunk)
      if (stdout.length > DESKTOP_CLEANUP_STDOUT_LIMIT) {
        oversized = true
        if (command === 'cleanup-inspect') child.kill()
      }
    })
    // Never copy stderr into a renderer response or diagnostic: filesystem and
    // environment details can be sensitive. The stable JSON code is sufficient.
    child.stderr.resume()
    child.once('error', (error) => finish(
      error instanceof Error ? error : new Error(String(error)),
    ))
    child.once('close', (code) => {
      if (oversized) return finish(new Error('Cleanup command output exceeded its limit.'))
      try {
        return finish(undefined, parseDesktopCleanupProtocol(JSON.parse(stdout)))
      } catch (error) {
        if (code !== 0) {
          return finish(new Error(`Cleanup command failed with exit code ${code ?? 'unknown'}.`))
        }
        return finish(error instanceof Error ? error : new Error(String(error)))
      }
    })
  })
}

async function inspectDesktopCleanup(mode: DesktopCleanupMode): Promise<{
  ok: boolean
  previewId: string | null
  report: DesktopCleanupReport
  profile: { kind: 'primary'; recoveryId: null }
}> {
  desktopCleanupPreviews.clear()
  const profile = activeDesktopProfile()
  const selection = currentDesktopCleanupSelection(mode)
  const report = await runDesktopCleanupCli(profile, 'cleanup-inspect', [
    '--user-data', app.getPath('userData'),
    ...cleanupSelectorArgs(selection),
    '--json',
  ])
  if (report.mode !== mode) throw new Error('Cleanup inventory did not match the requested mode.')
  const preview = report.outcome === 'ready'
    ? desktopCleanupPreviews.create(report, selection)
    : null
  return {
    ok: report.outcome === 'ready',
    previewId: preview?.id ?? null,
    report,
    profile: { kind: 'primary', recoveryId: null },
  }
}

function cleanupInventoryDetail(report: DesktopCleanupReport): string {
  const paths = report.items
    .filter((item) => item.exists)
    .slice(0, 8)
    .map((item) => item.path)
  const remaining = report.items.filter((item) => item.exists).length - paths.length
  return [
    ...paths,
    ...(remaining > 0 ? [`+${remaining} ${desktopT('cleanup.moreItems')}`] : []),
  ].join('\n')
}

async function confirmDesktopCleanup(report: DesktopCleanupReport): Promise<boolean> {
  if (report.mode === 'reset-current-settings') return true
  const deleteAll = report.mode === 'delete-all-user-data'
  const options: Electron.MessageBoxOptions = {
    type: 'warning',
    buttons: [desktopT('cleanup.cancel'), desktopT(
      deleteAll ? 'cleanup.deleteAllConfirm' : 'cleanup.deleteProfileConfirm',
    )],
    defaultId: 0,
    cancelId: 0,
    noLink: true,
    title: desktopT(deleteAll ? 'cleanup.deleteAllTitle' : 'cleanup.deleteProfileTitle'),
    message: desktopT(deleteAll ? 'cleanup.deleteAllMessage' : 'cleanup.deleteProfileMessage'),
    detail: cleanupInventoryDetail(report),
  }
  const window = currentMainWindow()
  const choice = window
    ? await dialog.showMessageBox(window, options)
    : await dialog.showMessageBox(options)
  return choice.response === 1
}

async function spawnDeleteAllAfterElectronExit(
  profile: DesktopProfilePaths,
  selection: DesktopCleanupSelection,
  approvedScopeFingerprint: string,
  approvedReport: DesktopCleanupReport,
): Promise<void> {
  const runtime = await resolveGatewayRuntime()
  const prefix = runtime.args.slice(0, -2)
  const args = [
    ...prefix,
    'recovery', 'cleanup-apply',
    '--user-data', app.getPath('userData'),
    ...cleanupSelectorArgs(selection),
    // The child waits for this process's stdin pipe to close, performs a fresh
    // post-exit inventory, and applies that inventory. This avoids Windows open
    // handles and Chromium writes invalidating the live-process preview.
    '--after-parent-exit',
    '--transaction-id', 'post-exit-reinspect',
    '--expected-revision', '0',
    '--expected-scope-fingerprint', approvedScopeFingerprint,
    '--confirm-user-data', app.getPath('userData'),
    '--json',
  ]
  const approval = `${JSON.stringify({
    schema_version: 1,
    scope_fingerprint: approvedScopeFingerprint,
    items: approvedReport.items.map((item) => ({ kind: item.kind, path: item.path })),
  })}\n`
  if (Buffer.byteLength(approval, 'utf8') > DESKTOP_CLEANUP_APPROVAL_LIMIT) {
    throw new Error('The confirmed cleanup inventory is too large for the exit handoff.')
  }
  await new Promise<void>((resolveSpawn, rejectSpawn) => {
    const helper = spawn(runtime.command, args, {
      cwd: runtime.cwd,
      windowsHide: true,
      detached: true,
      stdio: ['pipe', 'ignore', 'ignore'],
      env: desktopChildEnvironment(profile, {
        OPENSQUILLA_RECOVERY_OFFLINE: '1',
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
      }),
    })
    let settled = false
    const finish = (error?: Error) => {
      if (settled) return
      settled = true
      if (error) rejectSpawn(error)
      else resolveSpawn()
    }
    helper.stdin.on('error', (error) => finish(
      error instanceof Error ? error : new Error(String(error)),
    ))
    helper.once('error', (error) => finish(
      error instanceof Error ? error : new Error(String(error)),
    ))
    helper.once('spawn', () => {
      helper.once('exit', () => {
        if (pendingDeleteAllHelper === helper) pendingDeleteAllHelper = null
      })
      helper.stdin.write(approval, 'utf8', (error) => {
        if (error) return finish(error)
        // Deliberately keep stdin open. The helper reads this bounded approval
        // and then waits for EOF, which is emitted only when Electron exits and
        // releases its Chromium/userData handles.
        pendingDeleteAllHelper = helper
        helper.unref()
        ;(helper.stdin as NodeJS.WritableStream & { unref?: () => void }).unref?.()
        finish()
      })
    })
  })
}

async function restoreAfterIncompleteCleanup(
  profile: DesktopProfilePaths,
  preserveControlUi = false,
): Promise<void> {
  isQuitting = false
  clearReusableGatewayState()
  const inspection = await inspectDesktopProfile(profile)
  recoveryInspection = inspection
  primaryRecoveryInspection = inspection
  bootError = null
  if (!preserveControlUi || inspection.outcome === 'recovery_required') {
    await restoreMainWindowToBootPage()
  }
  publishRecoveryState()
  if (inspection.outcome !== 'recovery_required') void openOrResumeDesktopApp()
}

ipcMain.handle('desktop:cleanup:inspect', async (event, payload?: { mode?: unknown }) => {
  if (!trustedRecoveryIpc(event)) throw new Error('Untrusted Desktop cleanup request.')
  if (desktopCleanupBusy) throw new Error('Another Desktop cleanup operation is running.')
  const mode = parseDesktopCleanupMode(payload?.mode)
  if (!mode) throw new Error('Choose a supported Desktop cleanup action.')
  return await inspectDesktopCleanup(mode)
})

ipcMain.handle('desktop:cleanup:discard', (event, payload?: { previewId?: unknown }) => {
  if (!trustedRecoveryIpc(event)) throw new Error('Untrusted Desktop cleanup request.')
  return desktopCleanupPreviews.discard(payload?.previewId, desktopProfileKey())
})

async function applyApprovedDesktopCleanup(
  preview: TrustedDesktopCleanupPreview,
  approval: { acknowledged: boolean; confirmation: string },
) {
  const active = activeDesktopProfile()
  const report = preview.report
  if (
    report.mode !== 'reset-current-settings'
    && approval.acknowledged !== true
  ) return { ok: false, detail: 'Confirm that you reviewed every listed location.' }
  if (
    report.mode === 'delete-all-user-data'
    && approval.confirmation !== DELETE_ALL_CONFIRMATION
  ) return { ok: false, detail: `Type ${DELETE_ALL_CONFIRMATION} to confirm.` }
  if (!await confirmDesktopCleanup(report)) {
    const replacement = await inspectDesktopCleanup(report.mode).catch(() => null)
    return {
      ...(replacement || {}),
      ok: false,
      aborted: true,
      detail: 'cancelled',
    }
  }

  const exclusive = desktopWriters.tryBeginExclusive('Desktop data cleanup')
  if (!exclusive) {
    return { ok: false, detail: 'OpenSquilla is finishing another profile operation. Try again.' }
  }
  desktopCleanupBusy = true
  let shouldQuit = false
  try {
    await waitForDesktopWriterOperations(1)
    if (desktopProfileKey(active) !== desktopProfileKey()) {
      return { ok: false, detail: 'The active profile changed. Review the cleanup inventory again.' }
    }
    if ((!gatewayProcess || !gatewayState.owned) && gatewayState.url && await healthCheck(gatewayState.url)) {
      return { ok: false, detail: 'A gateway is still serving this profile; stop it and retry.' }
    }
    isQuitting = true
    allowGracefulShutdownWhileQuitting = true
    try {
      await stopOwnedGatewayAndWait()
    } finally {
      allowGracefulShutdownWhileQuitting = false
    }

    // Gateway shutdown legitimately changes state/log metadata, so the old CAS
    // revision cannot be applied directly. Reinspect after every writer is gone
    // and accept only the exact same mode + bounded kind/path scope the user saw.
    // The refreshed transaction/revision then becomes the apply authority.
    const refreshed = await runDesktopCleanupCli(active, 'cleanup-inspect', [
      '--user-data', app.getPath('userData'),
      ...cleanupSelectorArgs(preview.selection),
      '--json',
    ])
    if (
      refreshed.outcome !== 'ready'
      || !sameDesktopCleanupScope(report, refreshed, app.getPath('userData'))
    ) {
      const replacement = refreshed.outcome === 'ready'
        && desktopCleanupScopeIsContained(refreshed, app.getPath('userData'))
        ? desktopCleanupPreviews.create(refreshed, preview.selection)
        : null
      await restoreAfterIncompleteCleanup(active, true)
      return {
        ok: false,
        previewId: replacement?.id ?? null,
        report: refreshed,
        profile: { kind: 'primary', recoveryId: null },
        detail: 'The cleanup locations changed while the local runtime stopped. Review them again.',
      }
    }

    if (report.mode === 'delete-all-user-data') {
      await spawnDeleteAllAfterElectronExit(
        active,
        preview.selection,
        refreshed.scope_fingerprint,
        report,
      )
      shouldQuit = true
      return { ok: true, scheduled: true, report }
    }

    const result = await runDesktopCleanupCli(active, 'cleanup-apply', [
      '--user-data', app.getPath('userData'),
      ...cleanupSelectorArgs(preview.selection),
      '--transaction-id', refreshed.transaction_id,
      '--expected-revision', String(refreshed.revision),
      '--confirm-user-data', app.getPath('userData'),
      '--json',
    ])
    if (result.outcome === 'complete') {
      if (report.mode === 'reset-current-settings') {
        transientPendingMigrationProviderSetup = null
        forceOnboardingOnNextStartup = true
        isQuitting = false
        clearReusableGatewayState()
        bootError = null
        setImmediate(() => {
          void currentMainWindow()?.loadFile(bootPagePath()).then(() => openOrResumeDesktopApp())
        })
      } else {
        shouldQuit = true
      }
      return { ok: true, report: result }
    }
    await restoreAfterIncompleteCleanup(active)
    return {
      ok: false,
      partial: result.outcome === 'partial',
      report: result,
      detail: result.stable_code,
    }
  } catch (error) {
    await restoreAfterIncompleteCleanup(active).catch(() => null)
    return { ok: false, detail: error instanceof Error ? error.message : String(error) }
  } finally {
    exclusive.finish()
    // app.exit bypasses quit handlers that write desktop.log. Once a profile is
    // deleted, Electron must not reopen writer admission or recreate any path
    // inside the confirmed scope. Only a failed/cancelled operation may reopen.
    if (shouldQuit) {
      // Do not call setAppExitPhase here: its durable lifecycle log would
      // recreate userData/logs after an approved delete operation removed it.
      appExitPhase = 'committed'
      destroyWindowsTray()
      app.exit(0)
    } else {
      desktopWriters.reopen(exclusive.admissionToken)
      desktopCleanupBusy = false
    }
  }
}

async function resetDesktopSettingsThroughCleanup() {
  if (desktopCleanupBusy) return { ok: false, detail: 'Another cleanup operation is running.' }
  const inspection = await inspectDesktopCleanup('reset-current-settings')
  if (!inspection.ok || !inspection.previewId) {
    return { ok: false, report: inspection.report, detail: inspection.report.stable_code }
  }
  const preview = desktopCleanupPreviews.consume(
    inspection.previewId,
    desktopProfileKey(activeDesktopProfile()),
  )
  if (!preview) return { ok: false, detail: 'The cleanup inventory changed. Try again.' }
  return await applyApprovedDesktopCleanup(preview, {
    acknowledged: true,
    confirmation: '',
  })
}

ipcMain.handle('desktop:cleanup:apply', async (event, payload?: {
  previewId?: unknown
  acknowledged?: unknown
  confirmation?: unknown
}) => {
  if (!trustedRecoveryIpc(event)) throw new Error('Untrusted Desktop cleanup request.')
  if (desktopCleanupBusy) return { ok: false, detail: 'Another cleanup operation is running.' }
  const active = activeDesktopProfile()
  const preview = desktopCleanupPreviews.consume(payload?.previewId, desktopProfileKey(active))
  if (!preview) {
    desktopCleanupPreviews.clear()
    return { ok: false, detail: 'The cleanup inventory is missing or expired. Review it again.' }
  }
  return await applyApprovedDesktopCleanup(preview, {
    acknowledged: payload?.acknowledged === true,
    confirmation: typeof payload?.confirmation === 'string' ? payload.confirmation : '',
  })
})

ipcMain.handle('desktop:cleanup:reveal-user-data', async (event) => {
  if (!trustedRecoveryIpc(event)) throw new Error('Untrusted Desktop cleanup request.')
  await shell.showItemInFolder(app.getPath('userData'))
  return true
})

// ── Legacy home migration (Phase 3 entry points) ─────────────────────────────
// The import logic lives once in Python (`opensquilla migrate opensquilla`,
// dry-run by default); the desktop only detects candidates, orchestrates the
// gateway lifecycle, and spawns the bundled CLI.
async function runMigrateCli(
  extraArgs: string[],
  timeoutMs = 0,
  writerReserved = false,
  subcommand = 'opensquilla',
): Promise<{ ok: boolean; stdout: string; stderr: string }> {
  const mutating = extraArgs.includes('--apply')
  const finishWriter = mutating && !writerReserved
    ? beginDesktopWriterOperation('complete profile import')
    : () => {}
  try {
    const primary = primaryDesktopProfile()
    const runtime = await resolveGatewayRuntime()
    const prefix = runtime.args.slice(0, -2) // drop the trailing ['gateway','run']
    const child = spawn(runtime.command, [...prefix, 'migrate', subcommand, ...extraArgs], {
      cwd: runtime.cwd,
      windowsHide: true,
      env: desktopChildEnvironment(primary, {
        // Complete profile import is always anchored to the primary H. Recovery
        // profiles are deliberately never valid import targets.
        PYTHONUNBUFFERED: '1',
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8:replace',
        ...(subcommand === 'verify-opensquilla-import'
          ? { OPENSQUILLA_RECOVERY_OFFLINE: '1' }
          : {}),
      }),
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => {
      stdout += String(chunk)
    })
    child.stderr.on('data', (chunk) => {
      stderr += String(chunk)
    })
    const code: number = await new Promise((res) => {
      let settled = false
      let timer: NodeJS.Timeout | null = null
      const finish = (value: number) => {
        if (settled) return
        settled = true
        if (timer) clearTimeout(timer)
        res(value)
      }
      child.once('exit', (c) => finish(c ?? 1))
      child.once('error', () => finish(1))
      if (timeoutMs > 0) {
        timer = setTimeout(() => {
          stderr += `Candidate metadata inspection exceeded ${timeoutMs}ms.`
          child.kill()
          finish(124)
        }, timeoutMs)
        timer.unref()
      }
    })
    return { ok: code === 0, stdout, stderr }
  } finally {
    finishWriter()
  }
}

function normalizedLegacyCandidateMetadata(
  value: unknown,
  expected: LegacyImportCandidate,
): LegacyImportCandidate | null {
  const root = migrationRecord(value)
  const candidate = migrationRecord(root?.candidate)
  if (
    !candidate
    || candidate.kind !== expected.kind
    || typeof candidate.path !== 'string'
    || !resolvedPathsEqual(candidate.path, expected.path)
  ) return null
  const version = candidate.version
  const activity = candidate.estimated_activity_at
  const sessions = candidate.session_count
  const size = candidate.size_bytes
  const imported = candidate.previously_imported
  if (
    (version !== null && typeof version !== 'string')
    || (activity !== null && typeof activity !== 'string')
    || (sessions !== null && (!Number.isSafeInteger(sessions) || Number(sessions) < 0))
    || (size !== null && (!Number.isSafeInteger(size) || Number(size) < 0))
    || typeof imported !== 'boolean'
  ) return null
  return {
    kind: expected.kind,
    path: expected.path,
    version: typeof version === 'string' ? version.slice(0, 80) : null,
    estimated_activity_at: typeof activity === 'string' ? activity : null,
    session_count: sessions === null ? null : Number(sessions),
    size_bytes: size === null ? null : Number(size),
    previously_imported: imported,
  }
}

async function inspectLegacyImportCandidate(
  candidate: LegacyImportCandidate,
): Promise<LegacyImportCandidate> {
  try {
    const result = await runMigrateCli([
      '--source', candidate.path,
      '--kind', candidate.kind,
      '--inspect-candidate',
      '--json',
    ], LEGACY_METADATA_TIMEOUT_MS)
    if (!result.ok) return candidate
    return normalizedLegacyCandidateMetadata(JSON.parse(result.stdout), candidate) ?? candidate
  } catch {
    return candidate
  }
}

async function enrichLegacyImportCandidates(
  candidates: LegacyImportCandidate[],
): Promise<LegacyImportCandidate[]> {
  const bounded = candidates.slice(0, LEGACY_METADATA_MAX_CANDIDATES)
  const enriched = new Array<LegacyImportCandidate>(bounded.length)
  let next = 0
  const worker = async () => {
    while (next < bounded.length) {
      const index = next
      next += 1
      const candidate = bounded[index]
      if (candidate) enriched[index] = await inspectLegacyImportCandidate(candidate)
    }
  }
  await Promise.all(
    Array.from(
      { length: Math.min(LEGACY_METADATA_WORKERS, bounded.length) },
      () => worker(),
    ),
  )
  return enriched
}

// Run the migrate CLI in JSON mode and parse its report. A blocked run exits
// non-zero but still prints a valid report (items with status "error"), so ok
// and report are independent signals.
async function migrateSummaryJson(
  extraArgs: string[],
  writerReserved = false,
): Promise<{ ok: boolean; report: Record<string, unknown> | null; raw: string }> {
  const { ok, stdout, stderr } = await runMigrateCli(
    [...extraArgs, '--json'],
    0,
    writerReserved,
  )
  try {
    return { ok, report: JSON.parse(stdout) as Record<string, unknown>, raw: stdout }
  } catch {
    return { ok: false, report: null, raw: stdout || stderr }
  }
}

type DesktopMigrationPhase = 'preview' | 'applying' | 'done' | 'error'

// Coarse migration progress for renderers (same publish pattern as
// publishDesktopUpdateState): the onboarding card and the settings panel show a
// spinner/label off these while the CLI runs.
function publishDesktopMigrationProgress(phase: DesktopMigrationPhase, detail?: string): void {
  const payload = detail === undefined ? { phase } : { phase, detail }
  for (const window of BrowserWindow.getAllWindows()) {
    try {
      if (!window.isDestroyed()) window.webContents.send('desktop:migration:progress', payload)
    } catch {
      // A renderer can disappear while the import replaces its gateway origin.
      // Progress is advisory; lifecycle cleanup and restart must still run.
    }
  }
}

interface MigrationProviderPrefill {
  provider: string
  model: string
  baseUrl: string
  apiKeyEnv: string
  apiKey: string
}

interface PendingMigrationProviderSetup extends MigrationProviderPrefill {
  version: 1
  phase: 'applying' | 'needs-setup'
  source: string
  sourceKind: LegacyImportCandidate['kind']
  target: string
  knownReceiptIds: string[]
  committedTransactionId: string
  credentialBackupPath: string
}

let transientPendingMigrationProviderSetup: PendingMigrationProviderSetup | null = null

function pendingMigrationProviderSetupPath(): string {
  return join(app.getPath('userData'), 'migration-provider-setup.json')
}

function migrationReceiptRoot(): string {
  return join(primaryDesktopHome(), 'migration', 'opensquilla')
}

interface ImportReceiptVerificationResult {
  schema_version: 1
  outcome: 'verified' | 'not_found' | 'invalid' | 'unsafe'
  stable_code: string
  source: string
  source_kind: LegacyImportCandidate['kind']
  target: string
  transaction_id: string
  matching_transaction_ids: string[]
  provider_connection: {
    provider: string
    model: string
    base_url: string
    api_key_env: string
  } | null
  report: Record<string, unknown> | null
}

function parseImportReceiptVerification(
  value: unknown,
  intent: Pick<PendingMigrationProviderSetup, 'source' | 'sourceKind' | 'target'>,
): ImportReceiptVerificationResult {
  const record = migrationRecord(value)
  const expectedKeys = [
    'schema_version',
    'outcome',
    'stable_code',
    'source',
    'source_kind',
    'target',
    'transaction_id',
    'matching_transaction_ids',
    'provider_connection',
    'report',
  ].sort()
  if (!record || Object.keys(record).sort().join('\0') !== expectedKeys.join('\0')) {
    throw new Error('Import receipt verifier returned an invalid protocol shape.')
  }
  if (
    record.schema_version !== 1
    || !['verified', 'not_found', 'invalid', 'unsafe'].includes(String(record.outcome))
    || typeof record.stable_code !== 'string'
    || !record.stable_code
    || typeof record.source !== 'string'
    || !resolvedPathsEqual(record.source, intent.source)
    || record.source_kind !== intent.sourceKind
    || typeof record.target !== 'string'
    || !resolvedPathsEqual(record.target, intent.target)
    || typeof record.transaction_id !== 'string'
    || !Array.isArray(record.matching_transaction_ids)
    || record.matching_transaction_ids.length > 128
    || record.matching_transaction_ids.some((id) => (
      typeof id !== 'string' || !MIGRATION_TRANSACTION_ID_RE.test(id)
    ))
  ) {
    throw new Error('Import receipt verifier returned invalid metadata.')
  }
  const outcome = record.outcome as ImportReceiptVerificationResult['outcome']
  const transactionId = record.transaction_id
  const report = migrationRecord(record.report)
  const providerConnection = migrationRecord(record.provider_connection)
  if (providerConnection && (
    Object.keys(providerConnection).sort().join('\0')
      !== ['api_key_env', 'base_url', 'model', 'provider'].join('\0')
    || Object.values(providerConnection).some((item) => typeof item !== 'string')
    || !String(providerConnection.provider).trim()
    || (
      providerConnection.api_key_env !== ''
      && !IMPORTED_PROVIDER_API_KEY_ENV_RE.test(String(providerConnection.api_key_env))
    )
  )) {
    throw new Error('Import receipt verifier returned an invalid provider connection.')
  }
  if (
    (outcome === 'verified'
      && (
        !MIGRATION_TRANSACTION_ID_RE.test(transactionId)
        || !record.matching_transaction_ids.includes(transactionId)
        || !report
      ))
    || (outcome !== 'verified' && (
      transactionId !== ''
      || record.report !== null
      || record.provider_connection !== null
    ))
  ) {
    throw new Error('Import receipt verifier returned an inconsistent outcome.')
  }
  if (report) {
    const validationError = migrationReportValidationError(report, {
      source: intent.source,
      sourceKind: intent.sourceKind,
      target: intent.target,
      apply: true,
    })
    if (validationError || migrationTransactionIdFromReport(report) !== transactionId) {
      throw new Error(validationError || 'Import receipt report transaction is invalid.')
    }
  }
  return {
    schema_version: 1,
    outcome,
    stable_code: record.stable_code,
    source: record.source,
    source_kind: record.source_kind as LegacyImportCandidate['kind'],
    target: record.target,
    transaction_id: transactionId,
    matching_transaction_ids: [...record.matching_transaction_ids] as string[],
    provider_connection: providerConnection
      ? {
          provider: String(providerConnection.provider),
          model: String(providerConnection.model),
          base_url: String(providerConnection.base_url),
          api_key_env: String(providerConnection.api_key_env),
        }
      : null,
    report,
  }
}

async function verifyCommittedProfileImport(
  intent: Pick<PendingMigrationProviderSetup, 'source' | 'sourceKind' | 'target'>,
  options: { transactionId?: string | null; excludedTransactionIds?: string[] } = {},
): Promise<ImportReceiptVerificationResult> {
  const excluded = options.excludedTransactionIds ?? []
  const args = [
    '--source', intent.source,
    '--target', intent.target,
    '--source-kind', intent.sourceKind,
    ...(options.transactionId ? ['--transaction-id', options.transactionId] : []),
    ...excluded.flatMap((id) => ['--exclude-transaction-id', id]),
    '--json',
  ]
  const result = await runMigrateCli(
    args,
    RECOVERY_COMMAND_TIMEOUT_MS,
    true,
    'verify-opensquilla-import',
  )
  if (!result.ok) throw new Error('Import receipt verifier did not complete successfully.')
  return parseImportReceiptVerification(JSON.parse(result.stdout), intent)
}

async function readPendingMigrationProviderSetup(): Promise<PendingMigrationProviderSetup | null> {
  if (transientPendingMigrationProviderSetup) return transientPendingMigrationProviderSetup
  let raw = ''
  try {
    raw = await readFile(pendingMigrationProviderSetupPath(), 'utf8')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw new Error(`Could not read the pending desktop import record: ${String(error)}`)
  }
  let payload: Record<string, unknown>
  try {
    const parsed = JSON.parse(raw) as unknown
    const record = migrationRecord(parsed)
    if (!record) throw new Error('record root is not an object')
    payload = record
  } catch (error) {
    throw new Error(`The pending desktop import record is malformed: ${String(error)}`)
  }

  // Accept the short-lived pre-release marker shape as a needs-setup record so
  // users who hit this recovery path while testing are not stranded.
  const legacyShape = payload.phase === undefined
  const phase = legacyShape ? 'needs-setup' : payload.phase
  const source = legacyShape ? '' : String(payload.source || '')
  const sourceKind = legacyShape ? 'cli-home' : String(payload.sourceKind || '')
  const target = legacyShape ? primaryDesktopHome() : String(payload.target || '')
  if (
    !legacyShape
    && (
      payload.version !== 1
      || !Array.isArray(payload.knownReceiptIds)
      || payload.knownReceiptIds.length > 128
      || payload.knownReceiptIds.some((value) => (
        typeof value !== 'string' || !MIGRATION_TRANSACTION_ID_RE.test(value)
      ))
      || typeof payload.committedTransactionId !== 'string'
      || typeof payload.credentialBackupPath !== 'string'
      || (
        payload.committedTransactionId !== ''
        && !MIGRATION_TRANSACTION_ID_RE.test(payload.committedTransactionId)
      )
    )
  ) {
    throw new Error('The pending desktop import record has invalid transaction metadata.')
  }
  const knownReceiptIds = legacyShape
    ? []
    : payload.knownReceiptIds as string[]
  const credentialBackupPath = legacyShape ? '' : String(payload.credentialBackupPath || '')
  if (
    (phase !== 'applying' && phase !== 'needs-setup')
    || !resolvedPathsEqual(target, primaryDesktopHome())
    || (!legacyShape && !source)
    || !['cli-home', 'desktop-home', 'windows-portable'].includes(sourceKind)
    || (
      credentialBackupPath !== ''
      && !resolvedPathsEqual(
        credentialBackupPath,
        importedCredentialBackupPath(String(payload.committedTransactionId || '')),
      )
    )
  ) {
    throw new Error('The pending desktop import record does not match this desktop profile.')
  }
  transientPendingMigrationProviderSetup = {
    version: 1,
    phase,
    source,
    sourceKind: sourceKind as LegacyImportCandidate['kind'],
    target,
    knownReceiptIds,
    committedTransactionId: legacyShape
      ? ''
      : String(payload.committedTransactionId || ''),
    credentialBackupPath,
    provider: String(payload.provider || ''),
    model: String(payload.model || ''),
    baseUrl: String(payload.baseUrl || ''),
    apiKeyEnv: String(payload.apiKeyEnv || ''),
    // Secrets are never copied from the imported target into this marker.
    apiKey: '',
  }
  return transientPendingMigrationProviderSetup
}

async function persistPendingMigrationProviderSetup(
  pending: PendingMigrationProviderSetup,
): Promise<void> {
  const durable = { ...pending, apiKey: '' }
  mkdirSync(app.getPath('userData'), { recursive: true })
  await atomicWriteFile(
    pendingMigrationProviderSetupPath(),
    JSON.stringify(durable, null, 2),
    0o600,
  )
  transientPendingMigrationProviderSetup = durable
}

async function beginMigrationReconciliationIntent(
  candidate: LegacyImportCandidate,
): Promise<PendingMigrationProviderSetup> {
  const verificationIntent = {
    source: candidate.path,
    sourceKind: candidate.kind,
    target: primaryDesktopHome(),
  }
  const existing = await verifyCommittedProfileImport(verificationIntent)
  if (existing.outcome === 'invalid' || existing.outcome === 'unsafe') {
    throw new Error(`Could not establish the import receipt baseline (${existing.stable_code}).`)
  }
  const intent: PendingMigrationProviderSetup = {
    version: 1,
    phase: 'applying',
    ...verificationIntent,
    knownReceiptIds: existing.matching_transaction_ids,
    committedTransactionId: '',
    credentialBackupPath: '',
    provider: '',
    model: '',
    baseUrl: '',
    apiKeyEnv: '',
    apiKey: '',
  }
  await persistPendingMigrationProviderSetup(intent)
  return intent
}

async function findAppliedReceiptForIntent(
  intent: PendingMigrationProviderSetup,
  reportedTransactionId: string | null = null,
): Promise<{
  transactionId: string
  report: Record<string, unknown>
  providerConnection: ImportReceiptVerificationResult['provider_connection']
} | null> {
  const known = new Set(intent.knownReceiptIds)
  if (reportedTransactionId && known.has(reportedTransactionId)) return null
  const result = await verifyCommittedProfileImport(intent, {
    transactionId: reportedTransactionId,
    excludedTransactionIds: intent.knownReceiptIds,
  })
  if (result.outcome === 'not_found') return null
  if (result.outcome !== 'verified' || !result.report) {
    throw new Error(`Import receipt verification is unsafe (${result.stable_code}).`)
  }
  return {
    transactionId: result.transaction_id,
    report: result.report,
    providerConnection: result.provider_connection,
  }
}

async function bindMigrationIntentToReceipt(
  intent: PendingMigrationProviderSetup,
  receipt: {
    transactionId: string
    providerConnection: ImportReceiptVerificationResult['provider_connection']
  },
): Promise<PendingMigrationProviderSetup> {
  if (!MIGRATION_TRANSACTION_ID_RE.test(receipt.transactionId)) {
    throw new Error('The committed import transaction id is invalid.')
  }
  const bound = {
    ...intent,
    committedTransactionId: receipt.transactionId,
    provider: receipt.providerConnection?.provider || '',
    model: receipt.providerConnection?.model || '',
    baseUrl: receipt.providerConnection?.base_url || '',
    apiKeyEnv: receipt.providerConnection?.api_key_env || '',
    apiKey: '',
  }
  await persistPendingMigrationProviderSetup(bound)
  return bound
}

function importedCredentialBackupPath(transactionId: string): string {
  return join(
    app.getPath('userData'),
    `desktop-credential.import-backup.${transactionId}.json`,
  )
}

async function prepareImportedCredentialBackup(
  intent: PendingMigrationProviderSetup,
): Promise<PendingMigrationProviderSetup> {
  if (!MIGRATION_TRANSACTION_ID_RE.test(intent.committedTransactionId)) {
    throw new Error('A verified transaction is required before credential backup.')
  }
  const expectedPath = importedCredentialBackupPath(intent.committedTransactionId)
  if (intent.credentialBackupPath) {
    if (!resolvedPathsEqual(intent.credentialBackupPath, expectedPath)) {
      throw new Error('The imported credential backup path is invalid.')
    }
    return intent
  }
  const current = await readOptionalDesktopText(primaryDesktopProfile().credentialPath)
  if (current === null) return intent
  // Python's settings transaction parks the existing credential at this exact
  // transaction-bound path with its handle-safe native no-replace primitive.
  // Electron records the location for recovery UI only and never writes bytes.
  const updated = { ...intent, credentialBackupPath: expectedPath }
  await persistPendingMigrationProviderSetup(updated)
  return updated
}

async function writePendingMigrationProviderSetup(
  prefill: MigrationProviderPrefill | null,
  intent: PendingMigrationProviderSetup,
): Promise<void> {
  if (!MIGRATION_TRANSACTION_ID_RE.test(intent.committedTransactionId)) {
    throw new Error('Provider setup cannot continue without a verified import transaction.')
  }
  const pending: PendingMigrationProviderSetup = {
    ...intent,
    phase: 'needs-setup',
    provider: prefill?.provider || '',
    model: prefill?.model || '',
    baseUrl: prefill?.baseUrl || '',
    apiKeyEnv: prefill?.apiKeyEnv || '',
    // A provider key never belongs in this durable prompt marker. Required keys
    // are entered explicitly by the user and stored through safeStorage.
    apiKey: '',
  }
  await persistPendingMigrationProviderSetup(pending)
}

async function clearPendingMigrationProviderSetup(): Promise<void> {
  transientPendingMigrationProviderSetup = null
  await rm(pendingMigrationProviderSetupPath(), { force: true })
}

async function loadPendingMigrationProviderSetup(): Promise<MigrationProviderPrefill | null> {
  const pending = await readPendingMigrationProviderSetup()
  if (!pending) return null
  if (pending.phase === 'applying') {
    throw new Error('An interrupted desktop import still needs reconciliation.')
  }
  return migrationProviderPrefill(pending)
}

function normalizedImportedBaseUrl(value: string): string {
  const raw = value.trim()
  try {
    const parsed = new URL(raw)
    parsed.hash = ''
    return parsed.toString().replace(/\/$/, '')
  } catch {
    return raw.replace(/\/+$/, '')
  }
}

function existingCredentialMatchesImportedReplay(
  existing: DesktopConnection,
  prefill: MigrationProviderPrefill,
  intent: PendingMigrationProviderSetup,
): boolean {
  const provider = normalizeProvider(prefill.provider)
  const defaults = providerDefaults(provider)
  return Boolean(intent.committedTransactionId)
    && existing.configAuthority === 'profile'
    && existing.importTransactionId === intent.committedTransactionId
    && normalizeProvider(existing.provider) === provider
    && existing.model.trim() === (prefill.model || defaults.model).trim()
    && normalizedImportedBaseUrl(existing.baseUrl)
      === normalizedImportedBaseUrl(prefill.baseUrl || defaults.baseUrl)
    && existing.apiKeyEnv === (prefill.apiKeyEnv || defaults.apiKeyEnv)
    && isConnectionReady(existing)
    && (!prefill.apiKey || decryptApiKey(existing) === prefill.apiKey)
}

async function reconcileImportedDesktopCredential(
  intent: PendingMigrationProviderSetup,
  writerReserved = false,
): Promise<{ requiresSetup: boolean }> {
  const prefill = migrationProviderPrefill(intent)
  const existing = await loadDesktopCredential()
  if (
    existing
    && prefill?.provider
    && existingCredentialMatchesImportedReplay(existing, prefill, intent)
  ) {
    await clearPendingMigrationProviderSetup()
    return { requiresSetup: false }
  }
  if (prefill?.provider) {
    const defaults = providerDefaults(prefill.provider)
    if (!defaults.requiresApiKey || prefill.apiKey) {
      try {
        await saveImportedDesktopCredential(
          prefill,
          intent.committedTransactionId,
          '',
          writerReserved,
        )
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error)
        if (!detail.startsWith('The OS keychain')) throw error
        desktopLog('migration_credential_adoption_deferred', { reason: 'keychain-unavailable' })
        await writePendingMigrationProviderSetup(prefill, intent)
        return { requiresSetup: true }
      }
      await clearPendingMigrationProviderSetup()
      return { requiresSetup: false }
    }
  }
  await writePendingMigrationProviderSetup(prefill, intent)
  return { requiresSetup: true }
}

async function recoverPendingMigrationReconciliation(): Promise<void> {
  const initial = await readPendingMigrationProviderSetup()
  if (!initial || initial.phase !== 'applying') return
  const finishWriter = beginDesktopWriterOperation('recover imported provider settings')
  try {
    let pending = await readPendingMigrationProviderSetup()
    if (!pending || pending.phase !== 'applying') return
    const receipt = await findAppliedReceiptForIntent(pending)
    if (!receipt) {
      // Profile inspection has already recovered any unfinished replacement
      // transaction. With no new layout receipt, this attempt never committed.
      await clearPendingMigrationProviderSetup()
      return
    }
    pending = await bindMigrationIntentToReceipt(pending, receipt)
    pending = await prepareImportedCredentialBackup(pending)
    await reconcileImportedDesktopCredential(pending, true)
  } finally {
    finishWriter()
  }
}

async function refreshPrimaryRecoveryAfterImportAttempt(): Promise<boolean> {
  const primary = primaryDesktopProfile()
  let inspection: RecoveryProtocolResult
  try {
    inspection = await inspectDesktopProfile(primary)
  } catch {
    inspection = recoveryFailureResult(primary.home, 'migration_failure_inspection_failed')
  }
  recoveryInspection = inspection
  primaryRecoveryInspection = inspection
  publishRecoveryState()
  createApplicationMenu()
  if (inspection.outcome !== 'recovery_required') return false
  bootError = null
  await restoreMainWindowToBootPage()
  return true
}

function migrationProviderPrefill(
  intent: PendingMigrationProviderSetup,
): MigrationProviderPrefill | null {
  if (!intent.provider) return null
  return {
    provider: intent.provider,
    model: intent.model,
    baseUrl: intent.baseUrl,
    apiKeyEnv: intent.apiKeyEnv,
    // Never parse or auto-adopt imported dotenv secrets. Dotenv syntax is richer
    // than this process can safely patch losslessly, so preserve the source bytes
    // and require explicit entry for providers that need a key.
    apiKey: '',
  }
}

interface TrustedDesktopMigrationPreview {
  id: string
  candidate: LegacyImportCandidate
  report: Record<string, unknown>
  createdAt: number
}

type DesktopMigrationFailureStage = 'preflight' | 'apply' | 'restart'

interface DesktopMigrationResult {
  id: string
  at: string
  ok: boolean
  migrationApplied: boolean
  restartOk: boolean
  requiresProviderSetup: boolean
  source?: string
  sourceKind?: MigrationSourceKind
  targetReplaced?: boolean
  credentialBackupPath?: string
  failureCode?: string
  failureStage?: DesktopMigrationFailureStage
  detail?: string
}

const DESKTOP_MIGRATION_PREVIEW_TTL_MS = 10 * 60 * 1000
let trustedDesktopMigrationPreview: TrustedDesktopMigrationPreview | null = null

function normalizeOwnedDesktopTargetGatewayPreview(
  report: Record<string, unknown>,
): Record<string, unknown> {
  const preflight = migrationRecord(report.preflight)
  if (
    !gatewayProcess
    || !gatewayState.owned
    || hasGatewayProcessExited(gatewayProcess)
    || gatewayProfileKey !== desktopProfileKey(primaryDesktopProfile())
    || preflight?.source_gateway_running !== false
    || preflight?.target_gateway_running !== true
    || !Array.isArray(report.items)
  ) return report

  const target = primaryDesktopHome()
  const matchingIndexes: number[] = []
  for (const [index, value] of report.items.entries()) {
    const item = migrationRecord(value)
    if (
      item?.kind === 'preflight/gateway'
      && item.status === 'error'
      && typeof item.source === 'string'
      && resolvedPathsEqual(item.source, target)
      && item.destination === null
      && item.reason === 'a gateway is running on the target home; stop it and re-run'
    ) matchingIndexes.push(index)
  }
  if (matchingIndexes.length !== 1) return report
  const matchingIndex = matchingIndexes[0]
  return {
    ...report,
    items: report.items.map((value, index) => (
      index !== matchingIndex
        ? value
        : {
            ...(migrationRecord(value) as Record<string, unknown>),
            status: 'skipped',
            reason: 'Desktop owns the target gateway and will stop it before apply',
          }
    )),
  }
}

function migrationPreviewAllowsApply(
  report: Record<string, unknown>,
  overwrite: boolean,
): boolean {
  const errors = migrationReportErrors(report)
  if (errors.length === 0) return !overwrite
  return overwrite
    && errors.length === 1
    && errors[0]?.kind === 'preflight/target'
}

function desktopMigrationResultPath(): string {
  return join(app.getPath('userData'), 'migration-last-result.json')
}

async function persistDesktopMigrationResult(result: DesktopMigrationResult): Promise<void> {
  try {
    mkdirSync(app.getPath('userData'), { recursive: true })
    await atomicWriteFile(
      desktopMigrationResultPath(),
      JSON.stringify(result, null, 2),
      0o600,
    )
  } catch (error) {
    desktopLog('migration_result_persist_failed', {
      error: error instanceof Error ? error.message : String(error),
    })
  }
}

async function readDesktopMigrationResult(): Promise<DesktopMigrationResult | null> {
  let raw = ''
  try {
    raw = await readFile(desktopMigrationResultPath(), 'utf8')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
  const parsed = migrationRecord(JSON.parse(raw))
  if (!parsed) throw new Error('Saved desktop migration result is malformed.')
  const result: DesktopMigrationResult = {
    id: String(parsed.id || ''),
    at: String(parsed.at || ''),
    ok: parsed.ok === true,
    migrationApplied: parsed.migrationApplied === true,
    restartOk: parsed.restartOk === true,
    requiresProviderSetup: parsed.requiresProviderSetup === true,
    ...(typeof parsed.source === 'string' && parsed.source ? { source: parsed.source } : {}),
    ...(parseMigrationSourceKind({ kind: parsed.sourceKind })
      ? { sourceKind: parsed.sourceKind as MigrationSourceKind }
      : {}),
    ...(typeof parsed.targetReplaced === 'boolean'
      ? { targetReplaced: parsed.targetReplaced }
      : {}),
    ...(typeof parsed.credentialBackupPath === 'string' && parsed.credentialBackupPath
      ? { credentialBackupPath: parsed.credentialBackupPath }
      : {}),
    ...(typeof parsed.failureCode === 'string' && parsed.failureCode
      ? { failureCode: parsed.failureCode }
      : {}),
    ...(['preflight', 'apply', 'restart'].includes(String(parsed.failureStage || ''))
      ? { failureStage: parsed.failureStage as DesktopMigrationFailureStage }
      : {}),
    ...(typeof parsed.detail === 'string' && parsed.detail ? { detail: parsed.detail } : {}),
  }
  if (!result.id || !result.at) throw new Error('Saved desktop migration result is malformed.')
  return result
}

async function takeDesktopMigrationResult(): Promise<DesktopMigrationResult | null> {
  const result = await readDesktopMigrationResult()
  if (result) await rm(desktopMigrationResultPath(), { force: true })
  return result
}

async function dismissDesktopMigrationResult(): Promise<{ ok: true }> {
  await rm(desktopMigrationResultPath(), { force: true })
  return { ok: true }
}

function migrationCandidateWithPreview(
  candidate: LegacyImportCandidate,
  report: Record<string, unknown>,
): LegacyImportCandidate {
  const preflight = migrationRecord(report.preflight)
  const sessionCount = preflight?.session_count
  return {
    ...candidate,
    session_count: Number.isSafeInteger(sessionCount) && Number(sessionCount) >= 0
      ? Number(sessionCount)
      : null,
  }
}

ipcMain.handle('desktop:migration:browse-source', async (event, payload?: unknown) => {
  if (!trustedRecoveryIpc(event)) {
    return { ok: false, error: 'Data transfer is available only from the trusted desktop window.' }
  }
  const sourceKind = parseMigrationSourceKind(payload)
  if (!sourceKind) {
    return { ok: false, error: 'Choose the OpenSquilla profile type first.' }
  }
  const window = currentMainWindow()
  const options: Electron.OpenDialogOptions = {
    title: 'Choose an OpenSquilla profile',
    properties: ['openDirectory'],
  }
  const choice = window
    ? await dialog.showOpenDialog(window, options)
    : await dialog.showOpenDialog(options)
  if (choice.canceled || choice.filePaths.length !== 1) return { ok: false, aborted: true }
  const path = choice.filePaths[0] || ''
  if (!path || !looksLikeOpenSquillaHome(path) || resolvedPathsEqual(path, primaryDesktopHome())) {
    return { ok: false, error: 'Choose a plain OpenSquilla profile directory.' }
  }
  const candidate = await inspectLegacyImportCandidate(legacyImportCandidate(sourceKind, path))
  manuallyApprovedMigrationCandidates.set(resolve(path), candidate)
  return { ok: true, candidate }
})

ipcMain.handle('desktop:migration:summary', async (event, payload?: { source?: unknown }) => {
  if (!trustedRecoveryIpc(event)) {
    return { ok: false, candidate: null, report: null, raw: 'Untrusted data transfer request.' }
  }
  trustedDesktopMigrationPreview = null
  const candidates = await enrichLegacyImportCandidates(detectLegacyImportCandidates())
  const source = typeof payload?.source === 'string' ? payload.source : ''
  const candidate = source
    ? candidates.find((item) => resolvedPathsEqual(item.path, source)) || null
    : null
  if (!candidate) {
    return { ok: true, candidates, candidate: null, report: null, requiresSelection: true }
  }
  // Dry-run is read-only, so the running gateway is deliberately left alone.
  publishDesktopMigrationProgress('preview')
  const { report, raw } = await migrateSummaryJson([
    '--source', candidate.path, '--kind', candidate.kind,
  ])
  const validationError = migrationReportValidationError(report, {
    source: candidate.path,
    sourceKind: candidate.kind,
    target: primaryDesktopHome(),
    apply: false,
  })
  if (validationError || !report) {
    const detail = validationError || raw || 'Invalid migration report'
    publishDesktopMigrationProgress('error', detail)
    return { ok: false, candidates, candidate, report: null, raw: detail }
  }
  const previewReport = normalizeOwnedDesktopTargetGatewayPreview(report)
  const previewOk = migrationReportErrors(previewReport).length === 0
  const preview: TrustedDesktopMigrationPreview = {
    id: randomUUID(),
    candidate: migrationCandidateWithPreview(candidate, previewReport),
    report: previewReport,
    createdAt: Date.now(),
  }
  trustedDesktopMigrationPreview = preview
  publishDesktopMigrationProgress(previewOk ? 'done' : 'error')
  return {
    ok: previewOk,
    candidates,
    candidate: preview.candidate,
    report: previewReport,
    previewId: preview.id,
  }
})

ipcMain.handle('desktop:migration:run', async (
  event,
  payload?: { overwrite?: boolean; previewId?: string },
) => {
  if (!trustedRecoveryIpc(event)) {
    return { ok: false, report: null, detail: 'Untrusted data transfer request.' }
  }
  const preview = trustedDesktopMigrationPreview
  if (
    !preview
    || payload?.previewId !== preview.id
    || Date.now() - preview.createdAt > DESKTOP_MIGRATION_PREVIEW_TTL_MS
  ) {
    trustedDesktopMigrationPreview = null
    return {
      ok: false,
      report: null,
      detail: 'The transfer preview is missing or expired. Review the data again.',
    }
  }
  const candidate = preview.candidate
  const overwrite = Boolean(payload?.overwrite)
  if (!migrationPreviewAllowsApply(preview.report, overwrite)) {
    trustedDesktopMigrationPreview = null
    return {
      ok: false,
      report: preview.report,
      detail: 'The approved preview does not permit this transfer mode. Review the data again.',
    }
  }
  if (!looksLikeOpenSquillaHome(candidate.path)) {
    trustedDesktopMigrationPreview = null
    return {
      ok: false,
      report: preview.report,
      detail: 'The approved migration source changed after preview. Preview again.',
    }
  }
  if (overwrite) {
    const options: Electron.MessageBoxOptions = {
      type: 'warning',
      buttons: [desktopT('migration.overwriteCancel'), desktopT('migration.overwriteConfirm')],
      defaultId: 0,
      cancelId: 0,
      title: desktopT('migration.overwriteTitle'),
      message: desktopT('migration.overwriteMessage'),
      detail: [
        desktopT('migration.overwriteDetail'),
        desktopT('migration.overwriteNoMerge'),
        desktopT('migration.overwriteSourceUntouched'),
        desktopT('migration.overwriteNoSync'),
        candidate.path,
      ].join('\n\n'),
    }
    const window = currentMainWindow()
    const confirmation = window
      ? await dialog.showMessageBox(window, options)
      : await dialog.showMessageBox(options)
    if (confirmation.response !== 1) {
      return { ok: false, aborted: true, report: preview.report, detail: 'cancelled' }
    }
  }
  const exclusive = desktopWriters.tryBeginExclusive('complete profile import')
  if (!exclusive) {
    return {
      ok: false,
      report: preview.report,
      detail: 'Another profile operation, update, or quit is already in progress.',
    }
  }
  let report: Record<string, unknown> | null = null
  let migrationVerified = false
  let migrationApplied = false
  let restartOk = false
  let requiresProviderSetup = false
  let failureCode = ''
  let failureStage: DesktopMigrationFailureStage | '' = ''
  let detail = ''
  let intent: PendingMigrationProviderSetup | null = null
  let restartAllowed = true
  let shouldRestart = true
  let lifecycleOwnsAdmission = false
  try {
    await waitForDesktopWriterOperations(1)
  trustedDesktopMigrationPreview = null

  // A live gateway not owned by this process must remain attached and untouched.
  // Do this before setting the quit latch so a refusal cannot suppress its later
  // crash reporting or fall through into the owned-gateway restart path.
  if ((!gatewayProcess || !gatewayState.owned) && gatewayState.url) {
    if (await healthCheck(gatewayState.url)) {
      const refused: DesktopMigrationResult = {
        id: randomUUID(),
        at: new Date().toISOString(),
        ok: false,
        migrationApplied: false,
        restartOk: true,
        requiresProviderSetup: false,
        failureCode: 'migration_apply_failed',
        failureStage: 'preflight',
        detail: 'A gateway is still serving this profile; stop it and retry.',
      }
      await persistDesktopMigrationResult(refused)
      return { ...refused, report: null }
    }
  }

  publishDesktopMigrationProgress('applying')
  isQuitting = true
  try {
    // Quiesce the owned gateway before the CLI writes (the uninstall-run
    // pattern): wait for the child to actually EXIT, bounded by the kill deadline.
    if (gatewayProcess && gatewayState.owned) {
      const child = gatewayProcess
      // We stay alive and await the exit, so let the gateway take its Windows
      // HTTP graceful drain instead of an immediate TerminateProcess.
      allowGracefulShutdownWhileQuitting = true
      try {
        stopGateway()
      } finally {
        allowGracefulShutdownWhileQuitting = false
      }
      const exited = await waitForGatewayProcessExit(child)
      if (!exited) {
        // Keep the still-live child visible to the rest of the lifecycle code and
        // refuse both the import and a replacement spawn against the same profile.
        gatewayProcess = child
        gatewayState.owned = true
        gatewayState.status = 'error'
        gatewayState.error = 'The desktop gateway did not exit before the transfer deadline.'
        restartAllowed = false
        throw new Error(gatewayState.error)
      }
    }

    // Refuse while an unmanaged gateway still serves this profile — the import
    // must not race live sessions.db/scheduler.db writers.
    if (gatewayState.url && (await healthCheck(gatewayState.url))) {
      gatewayState.owned = false
      gatewayState.status = 'ready'
      shouldRestart = false
      throw new Error('A gateway is still serving this profile; stop it and retry.')
    }

    // The intent is outside the target tree, so it survives target replacement.
    // Startup can use it plus a new target-side receipt to finish credential
    // adoption even if Electron dies after the Python commit but before this IPC
    // handler receives stdout.
    intent = await beginMigrationReconciliationIntent(candidate)
    const result = await runMigrateCli([
      '--source', candidate.path, '--kind', candidate.kind, '--apply',
      ...(overwrite ? [
        '--replace-target',
        '--confirm-replace-target', primaryDesktopHome(),
      ] : []),
      '--json',
    ], 0, true)
    try {
      report = migrationRecord(JSON.parse(result.stdout))
    } catch {
      report = null
    }
    const validationError = migrationReportValidationError(report, {
      source: candidate.path,
      sourceKind: candidate.kind,
      target: primaryDesktopHome(),
      apply: true,
    })
    const reportFailure = migrationFailureFromReport(report)
    migrationVerified = result.ok
      && report !== null
      && validationError === null
      && migrationReportErrors(report).length === 0
    if (!migrationVerified) {
      failureCode = reportFailure?.failureCode || 'migration_apply_failed'
      failureStage = reportFailure?.failureStage || 'apply'
      detail = validationError
        || reportFailure?.detail
        || conciseMigrationProcessError(result.stderr)
    }
  } catch (error) {
    failureCode ||= 'migration_apply_failed'
    failureStage ||= 'preflight'
    detail ||= error instanceof Error ? error.message : String(error)
  } finally {
    if (intent) {
      try {
        const receipt = await findAppliedReceiptForIntent(
          intent,
          migrationVerified ? migrationTransactionIdFromReport(report) : null,
        )
        if (receipt) {
          migrationApplied = true
          if (!migrationVerified) report = receipt.report
          // Publication of a validated, previously-unseen target receipt is the
          // durable commit authority. The CLI child can lose stdout or exit
          // nonzero after that atomic rename without making the import a failure.
          migrationVerified = true
          intent = await bindMigrationIntentToReceipt(intent, receipt)
          intent = await prepareImportedCredentialBackup(intent)
          const reconciliation = await reconcileImportedDesktopCredential(intent, true)
          requiresProviderSetup = reconciliation.requiresSetup
        } else {
          if (migrationVerified) {
            detail = 'The migration command succeeded without a valid target-side receipt.'
            failureCode = 'migration_apply_failed'
            failureStage = 'apply'
            migrationVerified = false
          }
          await clearPendingMigrationProviderSetup()
        }
      } catch (error) {
        migrationVerified = false
        failureCode ||= 'migration_apply_failed'
        failureStage ||= 'apply'
        const reconciliationError = error instanceof Error ? error.message : String(error)
        detail = detail ? `${detail}; ${reconciliationError}` : reconciliationError
      }
    }
    publishDesktopMigrationProgress(migrationVerified ? 'done' : 'error', detail || undefined)
    isQuitting = false
  }
  } finally {
    // Receipt verification, credential adoption (or a durable needs-setup
    // marker), and reconciliation are complete. Reopen writer admission before
    // restart: an imported provider that needs user input must be able to save
    // onboarding settings in this same process.
    isQuitting = false
    exclusive.finish()
    lifecycleOwnsAdmission = desktopWriters.hasOtherOwner(exclusive.admissionToken)
    desktopWriters.reopen(exclusive.admissionToken)
  }

  // Restart via the boot splash on every path unless update/quit closed writer
  // admission while the import drained. In that case the lifecycle owner takes
  // over and the durable pending marker resumes safely on the next launch.
  if (lifecycleOwnsAdmission || desktopWriters.closed) {
    restartAllowed = false
    shouldRestart = false
    if (migrationApplied && !detail) detail = 'Transfer applied; restart deferred to the active lifecycle operation.'
  } else if (restartAllowed && shouldRestart) {
    clearReusableGatewayState()
    bootError = null
    await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)
    await openOrResumeDesktopApp()
    restartOk = gatewayState.status === 'ready'
      && Boolean(gatewayState.url)
      && await healthCheck(gatewayState.url)
  } else if (!shouldRestart) {
    restartOk = Boolean(gatewayState.url) && await healthCheck(gatewayState.url)
  }
  if (migrationApplied && !restartOk) {
    failureCode = 'gateway_restart_failed'
    failureStage = 'restart'
    if (!detail) detail = 'Transfer applied, but the desktop gateway did not become healthy.'
  } else if (!migrationApplied && !migrationVerified) {
    failureCode ||= 'migration_apply_failed'
    failureStage ||= 'apply'
  }
  const finalResult: DesktopMigrationResult = {
    id: randomUUID(),
    at: new Date().toISOString(),
    ok: migrationVerified && restartOk,
    migrationApplied,
    restartOk,
    requiresProviderSetup,
    source: candidate.path,
    sourceKind: candidate.kind,
    targetReplaced: overwrite && migrationApplied,
    ...(intent?.credentialBackupPath
      ? { credentialBackupPath: intent.credentialBackupPath }
      : {}),
    ...(failureCode ? { failureCode } : {}),
    ...(failureStage ? { failureStage } : {}),
    ...(detail ? { detail } : {}),
  }
  await persistDesktopMigrationResult(finalResult)
  return { ...finalResult, report }
})

ipcMain.handle('desktop:migration:last-result', async (event) => (
  trustedRecoveryIpc(event) ? await takeDesktopMigrationResult() : null
))
ipcMain.handle('desktop:migration:peek-last-result', async (event) => (
  trustedRecoveryIpc(event) ? await readDesktopMigrationResult() : null
))
ipcMain.handle('desktop:migration:dismiss-last-result', async (event) => (
  trustedRecoveryIpc(event) ? await dismissDesktopMigrationResult() : { ok: false }
))

function trustedRecoveryIpc(event: Electron.IpcMainInvokeEvent): boolean {
  const window = currentMainWindow()
  if (!window || event.sender !== window.webContents) return false
  const url = event.senderFrame?.url || event.sender.getURL()
  try {
    const sender = new URL(url)
    if (sender.protocol === 'file:') {
      return resolve(fileURLToPath(sender)) === resolve(bootPagePath())
    }
    return trustedControlUiIpc(event)
  } catch {
    return false
  }
}

function trustedMainWindowControlIpc(event: Electron.IpcMainInvokeEvent): boolean {
  const window = currentMainWindow()
  if (!window || event.sender !== window.webContents || !gatewayState.url) {
    return false
  }
  try {
    const sender = new URL(event.senderFrame?.url || event.sender.getURL())
    const gateway = new URL(gatewayState.url)
    return sender.origin === gateway.origin
      && (sender.pathname === '/control' || sender.pathname.startsWith('/control/'))
  } catch {
    return false
  }
}

function trustedControlUiIpc(event: Electron.IpcMainInvokeEvent): boolean {
  return gatewayState.owned && trustedMainWindowControlIpc(event)
}

function trustedOnboardingIpc(
  event: Electron.IpcMainInvokeEvent,
  flow = onboardingFlows.active,
): boolean {
  const window = flow?.window
  if (!flow || !onboardingFlows.isCurrent(flow) || !window || window.isDestroyed()) return false
  if (event.sender !== window.webContents) return false
  return (event.senderFrame?.url || event.sender.getURL()).startsWith('data:text/html')
}

function onboardingSaveFailure(
  code: OnboardingSaveErrorCode,
  error: string,
): OnboardingSaveResult {
  return { ok: false, code, error }
}

function completeOnboardingFlow(flow: OnboardingFlow, credential: DesktopConnection): boolean {
  const window = flow.window
  if (
    !window
    || window.isDestroyed()
    || !onboardingFlows.complete(flow)
  ) return false

  const resolve = flow.resolve
  flow.resolve = null
  flow.reject = null
  window.close()
  resolve?.(credential)
  return true
}

async function performOnboardingSave(
  flow: OnboardingFlow,
  payload: OnboardingPayload,
): Promise<OnboardingSaveResult> {
  const telemetry = new OnboardingSaveTelemetry(
    ++onboardingSaveTelemetryAttempt,
    app.isPackaged,
    (event, detail) => desktopLog(event, detail),
  )
  try {
    // Keep the existing writer-admission boundary: lifecycle drains do not need
    // to wait for an inspect that has not begun a settings write.
    let recoveryRequired: boolean
    try {
      recoveryRequired = await telemetry.stage(
        'primary_recovery_inspect',
        () => refreshPrimaryRecoveryAfterImportAttempt(),
      )
    } catch (error) {
      if (flow.state === 'saving') flow.state = 'editing'
      throw error
    }
    if (recoveryRequired) {
      if (flow.state === 'saving') flow.state = 'editing'
      return telemetry.recordReturned(onboardingSaveFailure(
        'recovery_required',
        'The primary profile requires recovery before setup can write to it.',
      ))
    }
    if (!onboardingFlows.canComplete(flow)) {
      return telemetry.recordReturned(onboardingSaveFailure(
        'onboarding_inactive',
        'OpenSquilla setup is no longer active.',
      ))
    }

    let finishWriter: (() => void) | null = null
    try {
      finishWriter = beginDesktopWriterOperation('complete desktop onboarding')
      telemetry.markWriterAdmitted()
    } catch {
      if (flow.state === 'saving') flow.state = 'editing'
      return telemetry.recordReturned(onboardingSaveFailure(
        'lifecycle_deferred',
        'OpenSquilla is finishing another profile or lifecycle operation.',
      ))
    }

    let credential: DesktopConnection
    try {
      // Re-read the marker only after reserving the writer. Credential/config,
      // locale, and marker removal then converge as one lifecycle operation that
      // update/quit must drain. Imported .env bytes are deliberately untouched.
      const pendingMigration = await telemetry.stage(
        'pending_setup_read',
        () => readPendingMigrationProviderSetup(),
      )
      credential = await telemetry.stage('settings_persist', async () => {
        if (pendingMigration?.phase === 'needs-setup' && pendingMigration.provider) {
          return await saveImportedDesktopCredential(
            pendingMigration,
            pendingMigration.committedTransactionId,
            String(payload.apiKey || ''),
            true,
          )
        }
        return await saveDesktopCredential(payload, true)
      })
      telemetry.markSettingsPersistedConfirmed()
      await telemetry.stage('local_finalize', async () => {
        applyDesktopLocaleChoice(payload.locale)
        await clearPendingMigrationProviderSetup()
      })
    } catch (error) {
      if (flow.state === 'saving') flow.state = 'editing'
      throw error
    } finally {
      finishWriter()
    }

    return telemetry.stageSync('flow_handoff', () => {
      if (!onboardingFlows.canComplete(flow)) {
        return telemetry.recordReturned(onboardingSaveFailure(
          'onboarding_inactive',
          'OpenSquilla setup is no longer active.',
        ))
      }
      // Quit deferral deliberately leaves isQuitting=false while it drains writers,
      // so admission/exit phase are authoritative alongside the boolean latch.
      if (desktopWriters.closed || isQuitting || appExitPhase !== 'running') {
        abandonOnboardingFlow(
          flow,
          new Error('OpenSquilla setup completed, but startup was deferred by an active lifecycle operation.'),
          true,
        )
        return telemetry.recordReturned(onboardingSaveFailure(
          'lifecycle_deferred',
          'Setup was saved; startup was deferred by an active lifecycle operation.',
        ))
      }
      if (!completeOnboardingFlow(flow, credential)) {
        return telemetry.recordReturned(onboardingSaveFailure(
          'onboarding_inactive',
          'OpenSquilla setup is no longer active.',
        ))
      }
      return telemetry.recordReturned({ ok: true })
    })
  } finally {
    telemetry.finish()
  }
}

async function withRecoveryOperation<T>(
  operation: () => Promise<T>,
): Promise<{ ok: true; value: T; state: DesktopRecoveryViewState } | {
  ok: false
  error: string
  state: DesktopRecoveryViewState
}> {
  if (recoveryOperationBusy) {
    return {
      ok: false,
      error: 'Another recovery operation is already running.',
      state: recoveryStateSnapshot(),
    }
  }
  const exclusive = desktopWriters.tryBeginExclusive('Desktop profile recovery')
  if (!exclusive) {
    return {
      ok: false,
      error: 'OpenSquilla is finishing another profile or lifecycle operation. Try again shortly.',
      state: recoveryStateSnapshot(),
    }
  }
  recoveryOperationBusy = true
  recoveryOperationError = null
  publishRecoveryState()
  let outcome: { ok: true; value: T } | { ok: false; error: string }
  try {
    await waitForDesktopWriterOperations(1)
    const value = await operation()
    outcome = { ok: true, value }
  } catch (error) {
    recoveryOperationError = error instanceof Error ? error.message : String(error)
    desktopLog('recovery_operation_failed', { error: recoveryOperationError })
    outcome = { ok: false, error: recoveryOperationError }
  } finally {
    exclusive.finish()
    desktopWriters.reopen(exclusive.admissionToken)
    recoveryOperationBusy = false
    publishRecoveryState()
  }
  // Build the response only after the authoritative busy=false transition.
  // Otherwise the renderer can apply a stale busy snapshot after the final
  // event and leave every recovery action disabled.
  return { ...outcome, state: recoveryStateSnapshot() }
}

async function inspectPrimaryForRepair(): Promise<RecoveryProtocolResult> {
  const inspection = await inspectDesktopProfile(primaryDesktopProfile())
  primaryRecoveryInspection = inspection
  return inspection
}

async function retryDeferredProfileConsolidation(): Promise<{
  ok: boolean
  error?: string
}> {
  if (!desktopProfileConsolidationMaintenance) return { ok: true }
  const exited = await stopAndJoinAllLifecycleOwnedGateways()
  if (!exited) return { ok: false, error: desktopGatewayStillRunningMessage() }

  desktopProfileConsolidationDeferredThisProcess = false
  desktopProfileConsolidationMaintenance = null
  desktopProfileConsolidationFailureDetail = ''
  clearReusableGatewayState()
  bootError = null
  publishRecoveryState()
  await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)
  void openOrResumeDesktopApp()
  return { ok: true }
}

async function recoverPrimaryProfileTransaction(): Promise<RecoveryProtocolResult> {
  const primary = primaryDesktopProfile()
  let inspection = primaryRecoveryInspection
  if (!inspection) inspection = await inspectPrimaryForRepair()

  await stopOwnedGatewayAndWait()
  const result = await recoverInspectedProfileTransaction(primary, inspection)
  primaryRecoveryInspection = result
  recoveryInspection = result
  if (result.outcome !== 'recovery_required') {
    clearReusableGatewayState()
    bootError = null
    await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)
    void openOrResumeDesktopApp()
  }
  publishRecoveryState()
  return result
}

async function choosePrimaryWorkspace(
  requestedWorkspace: unknown,
  presentation: 'workspace' | 'legacy-agent-data' = 'workspace',
): Promise<RecoveryProtocolResult> {
  const primary = primaryDesktopProfile()
  let inspection = primaryRecoveryInspection
  if (!inspection) inspection = await inspectPrimaryForRepair()

  let workspace = ''
  if (typeof requestedWorkspace === 'string' && requestedWorkspace) {
    const candidate = inspection.candidates.find((item) => (
      ['canonical', 'legacy', 'external'].includes(item.kind)
      && item.path === requestedWorkspace
      && item.exists
      && item.valid
    ))
    if (!candidate) {
      throw new Error(presentation === 'legacy-agent-data'
        ? 'The selected Agent data location is not an inspected valid candidate.'
        : 'The selected workspace is not an inspected valid candidate.')
    }
    workspace = candidate.path
  } else {
    const window = currentMainWindow()
    const options: Electron.OpenDialogOptions = {
      title: presentation === 'legacy-agent-data'
        ? 'Choose legacy OpenSquilla Agent data'
        : 'Choose an OpenSquilla workspace',
      properties: ['openDirectory'],
    }
    const choice = window
      ? await dialog.showOpenDialog(window, options)
      : await dialog.showOpenDialog(options)
    if (choice.canceled || choice.filePaths.length !== 1) {
      throw new Error(presentation === 'legacy-agent-data'
        ? 'Agent data location selection was cancelled.'
        : 'Workspace selection was cancelled.')
    }
    workspace = choice.filePaths[0] || ''
  }
  if (!workspace) {
    throw new Error(presentation === 'legacy-agent-data'
      ? 'Choose an Agent data directory.'
      : 'Choose a workspace directory.')
  }
  if (!inspection.transaction_id) {
    throw new Error('Recovery inspection did not provide a transaction id.')
  }

  await stopOwnedGatewayAndWait()
  const result = await runRecoveryCli(primary, [
    'choose-workspace',
    '--home', primary.home,
    '--transaction-id', inspection.transaction_id,
    '--expected-revision', String(inspection.revision),
    '--workspace', workspace,
    '--json',
  ])
  primaryRecoveryInspection = result
  recoveryInspection = result
  if (result.outcome !== 'recovery_required') {
    clearReusableGatewayState()
    bootError = null
    await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)
    void openOrResumeDesktopApp()
  }
  publishRecoveryState()
  return result
}

ipcMain.handle('desktop:recovery:state', () => recoveryStateSnapshot())
ipcMain.handle('desktop:recovery:retry-consolidation', async (event) => {
  if (!trustedControlUiIpc(event)) {
    return { ok: false, error: 'Automatic repair is available only inside OpenSquilla.' }
  }
  return await retryDeferredProfileConsolidation()
})
ipcMain.handle('desktop:recovery:choose-workspace', async (
  event,
  payload?: { workspace?: unknown },
) => {
  if (!trustedRecoveryIpc(event)) {
    return { ok: false, error: 'Recovery actions are available only from the recovery page.' }
  }
  return withRecoveryOperation(() => choosePrimaryWorkspace(payload?.workspace))
})
ipcMain.handle('desktop:recovery:choose-legacy-agent-data', async (
  event,
  payload?: { workspace?: unknown },
) => {
  const inspection = recoveryInspection
  if (
    !trustedControlUiIpc(event)
    || inspection?.outcome !== 'attention'
    || ![
      'legacy_workspace_pinned',
      'legacy_workspace_deferred',
      'workspace_conflict',
    ].includes(inspection.stable_code)
    || !inspection.allowed_actions.includes('choose-workspace')
  ) {
    return { ok: false, error: 'No legacy Agent data location can be selected from this page.' }
  }
  return withRecoveryOperation(() => choosePrimaryWorkspace(
    payload?.workspace,
    'legacy-agent-data',
  ))
})
ipcMain.handle('desktop:recovery:recover-transaction', async (event) => {
  if (!trustedRecoveryIpc(event)) {
    return { ok: false, error: 'Recovery actions are available only from the recovery page.' }
  }
  return withRecoveryOperation(recoverPrimaryProfileTransaction)
})
ipcMain.handle('desktop:recovery:reveal-path', async (
  event,
  payload?: { target?: unknown },
) => {
  if (!trustedRecoveryIpc(event)) return false
  const target = payload?.target === 'backups'
    ? app.getPath('userData')
    : primaryDesktopProfile().home
  if (existsSync(target)) await shell.showItemInFolder(target)
  else await shell.openPath(dirname(target)).catch(() => null)
  return true
})
ipcMain.handle('desktop:recovery:copy-diagnostics', async (event) => {
  if (!trustedRecoveryIpc(event)) return false
  clipboard.writeText(sanitizedRecoveryDiagnostics())
  return true
})
ipcMain.handle('desktop:recovery:open-download', async (event) => {
  // A config authored by a newer build blocks startup until the app is
  // updated. The recovery page offers the canonical download entry because
  // the in-app updater is unavailable on unmanaged installs.
  if (!trustedRecoveryIpc(event)) return false
  await shell.openExternal(
    `https://github.com/${GITHUB_UPDATE_OWNER}/${GITHUB_UPDATE_REPO}/releases/latest`,
  )
  return true
})

ipcMain.handle('desktop:boot:state', () => ({
  status: bootStatus,
  error: bootError,
  gateway: { ...gatewayState },
  recovery: recoveryStateSnapshot(),
}))

interface BootResumeAuthority {
  child: ChildProcessWithoutNullStreams
  profileKey: string
  openFlowRevision: number
}

function currentBootResumeAuthority(): BootResumeAuthority | null {
  if (
    !gatewayProcess
    || !gatewayState.owned
    || !gatewayProfileKey
    || hasGatewayProcessExited(gatewayProcess)
  ) return null
  return {
    child: gatewayProcess,
    profileKey: gatewayProfileKey,
    openFlowRevision: desktopOpenFlowRevision,
  }
}

function bootResumeAuthorityIsCurrent(authority: BootResumeAuthority): boolean {
  return !isQuitting
    && gatewayProcess === authority.child
    && !hasGatewayProcessExited(authority.child)
    && gatewayState.owned
    && gatewayProfileKey === authority.profileKey
    && desktopProfileKey() === authority.profileKey
    && desktopOpenFlowRevision === authority.openFlowRevision
}

async function resumeBootStartup(): Promise<{ ok: boolean; error?: string }> {
  const pendingStart = gatewayStartPromise
  const initialAuthority = pendingStart ? null : currentBootResumeAuthority()
  bootError = null
  await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)
  if (initialAuthority && !bootResumeAuthorityIsCurrent(initialAuthority)) return { ok: true }
  try {
    const gateway = pendingStart
      ? await pendingStart
      : initialAuthority
        ? await resumeOwnedGatewayStartup(
          () => bootResumeAuthorityIsCurrent(initialAuthority),
        )
        : null
    if (!gateway) {
      // With no exact live child to resume, use the normal open flow so profile
      // inspection and recovery gates still run before any replacement spawn.
      void openOrResumeDesktopApp()
      return { ok: true }
    }
    const authority = pendingStart ? currentBootResumeAuthority() : initialAuthority
    if (!authority || !bootResumeAuthorityIsCurrent(authority)) return { ok: true }
    await loadControlUiIntoCurrentWindow(
      gateway.url,
      () => bootResumeAuthorityIsCurrent(authority),
    )
    return { ok: true }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    // The original open flow owns errors from a pending initial start. For a
    // direct resume, an exit handler or a newer quit/reset/restart owns any
    // state after this exact child/profile/revision loses authority.
    if (pendingStart || !initialAuthority || !bootResumeAuthorityIsCurrent(initialAuthority)) {
      return { ok: false, error: message }
    }
    if (gatewayState.status !== 'ready') {
      gatewayState.status = 'error'
      gatewayState.error = message
    }
    desktopLog('gateway_start_resume_failed', {
      gatewayPid: gatewayProcess?.pid,
      gatewayStatus: gatewayState.status,
      error: message,
    })
    sendBootError(message)
    return { ok: false, error: message }
  }
}

ipcMain.handle('desktop:boot:resume', async () => {
  if (bootResumePromise) return await bootResumePromise
  const promise = resumeBootStartup()
  bootResumePromise = promise
  try {
    return await promise
  } finally {
    if (bootResumePromise === promise) bootResumePromise = null
  }
})
ipcMain.handle('desktop:boot:retry', async () => {
  // The Control UI "Restart runtime" action intentionally forces a new child.
  // The boot page uses desktop:boot:resume instead so a slow owned child can be
  // accepted after it becomes healthy instead of being torn down first.
  if (gatewayStartPromise) {
    bootError = null
    void openOrResumeDesktopApp()
    return { ok: true }
  }

  // Otherwise force a real runtime restart. "Restart runtime" must relaunch the
  // child even when the current gateway is healthy, so join every lifecycle-
  // owned child before openOrResumeDesktopApp may respawn. This includes a
  // child whose stop was already initiated by another flow. Timeout fails
  // closed: spawning while any previous writer is live would race the profile
  // lock and recreate the startup failure this recovery action is meant to fix.
  const exited = await stopAndJoinAllLifecycleOwnedGateways()
  if (!exited) {
    const message = desktopGatewayStillRunningMessage()
    gatewayState.status = 'error'
    gatewayState.error = message
    desktopLog('gateway_restart_wait_timeout', {
      pids: liveLifecycleOwnedGatewayProcesses().map((child) => child.pid),
    })
    sendBootError(message)
    await restoreMainWindowToBootPage()
    return { ok: false, error: message }
  }
  clearReusableGatewayState()
  bootError = null
  await currentMainWindow()?.loadFile(bootPagePath()).catch(() => null)

  void openOrResumeDesktopApp()
  return { ok: true }
})
ipcMain.handle('desktop:boot:quit', () => {
  app.quit()
  return { ok: true }
})
ipcMain.handle('desktop:onboarding:defaults', () => ({
  providers: PROVIDER_CATALOG,
  searchProviders: SEARCH_PROVIDER_CATALOG,
  router: {
    modelRoutingModes: ['squilla_router', 'direct', 'llm_ensemble'],
    modes: ['recommended', 'openrouter-mix', 'disabled'],
    defaultTier: 'c1',
    textTiers: TEXT_ROUTER_TIERS,
    profiles: ROUTER_PROFILES,
  },
}))
ipcMain.handle('desktop:onboarding:probe', async (event, payload: OnboardingProbePayload) => {
  const flow = onboardingFlows.active
  if (!flow || flow.state !== 'editing' || !trustedOnboardingIpc(event, flow)) {
    return {
      ok: false,
      failureKind: 'unavailable',
      message: 'No trusted onboarding is in progress.',
      latencyMs: 0,
    } satisfies OnboardingProbeResult
  }
  try {
    return await probeOnboardingProvider(payload || {})
  } catch (error) {
    return {
      ok: false,
      failureKind: 'unavailable',
      message: error instanceof Error ? error.message : 'Configuration verification failed.',
      latencyMs: 0,
    } satisfies OnboardingProbeResult
  }
})
ipcMain.handle('desktop:onboarding:save', async (event, payload: OnboardingPayload) => {
  // Only honor this while an onboarding flow is actually awaiting a result. The
  // same preload bridge is attached to the Control UI window, so without this
  // guard any script on the gateway-served page could rewrite the credential and
  // regenerate config.toml outside onboarding.
  const flow = onboardingFlows.active
  if (!flow || !trustedOnboardingIpc(event, flow)) {
    return onboardingSaveFailure('onboarding_inactive', 'No trusted onboarding is in progress.')
  }
  const requestPayload = payload && typeof payload === 'object' ? payload : {}
  const request = onboardingFlows.requestSave(
    flow,
    requestPayload,
    () => performOnboardingSave(flow, requestPayload),
  )
  if (request.kind === 'conflict') {
    return onboardingSaveFailure(
      'save_in_progress',
      'OpenSquilla is already saving a different setup request.',
    )
  }
  if (request.kind === 'inactive') {
    return onboardingSaveFailure('onboarding_inactive', 'OpenSquilla setup is no longer active.')
  }
  return await request.promise
})
ipcMain.handle('desktop:onboarding:cancel', (event) => {
  const flow = onboardingFlows.active
  if (!flow || !trustedOnboardingIpc(event, flow)) {
    return onboardingSaveFailure('onboarding_inactive', 'No trusted onboarding is in progress.')
  }
  // An admitted atomic save cannot be cancelled safely. Mark this flow
  // abandoned so its late completion cannot resolve a replacement onboarding
  // window; before-quit drains its writer before Electron exits.
  abandonOnboardingFlow(flow, new Error('OpenSquilla setup was cancelled.'), true)
  // The onboarding "Quit" button routes here; it is a deliberate exit, so quit
  // the app instead of surfacing the cancellation as a boot failure panel.
  app.quit()
  return { ok: true }
})
// Keep the normal app-quit gateway drain single-flight. Every supported
// platform must keep Electron alive until its owned child has actually exited;
// otherwise a slow POSIX SIGTERM drain can outlive the parent and retain the
// profile writer lock across the next Desktop launch.
let quitGatewayDrainPromise: Promise<boolean> | null = null
let quitDeferredForDesktopWriters = false
let quitWriterAdmission: symbol | null = null

async function drainOwnedGatewayForQuit(
  child: ChildProcessWithoutNullStreams,
  url: string,
  requestShutdown: boolean,
): Promise<boolean> {
  if (hasGatewayProcessExited(child)) return true
  const accepted = requestShutdown ? await requestOwnedGatewayShutdown(child, url) : null
  desktopLog('quit_gateway_shutdown_requested', { accepted, alreadyStopping: !requestShutdown })
  let hardTerminated = false
  let exited = false
  if (accepted === null) {
    // Another lifecycle operation already initiated the full graceful stop.
    // Join it instead of issuing a second request against a possibly replaced
    // ownership record or shortening its drain deadline.
    exited = await waitForGatewayProcessExit(child)
  } else if (!accepted) {
    hardTerminated = true
    // POSIX SIGTERM is itself the graceful shutdown trigger and must retain the
    // full gateway drain budget. Windows TerminateProcess is immediate, so only
    // that platform uses the short observation backstop here.
    const signalBackstop = process.platform === 'win32'
      ? GATEWAY_HARD_KILL_BACKSTOP_MS
      : GATEWAY_SHUTDOWN_KILL_AFTER_MS
    hardTerminateGatewayProcess(child, signalBackstop)
    exited = await waitForGatewayProcessExit(child, signalBackstop + 1_000)
    if (process.platform === 'win32') await clearKnownOwnedGatewayPidFile()
  } else {
    exited = await waitForGatewayProcessExit(child)
    if (!exited) {
      hardTerminated = true
      hardTerminateGatewayProcess(child)
      exited = await waitForGatewayProcessExit(
        child,
        GATEWAY_HARD_KILL_BACKSTOP_MS + 1_000,
      )
      if (process.platform === 'win32') await clearKnownOwnedGatewayPidFile()
    }
  }
  // hardTerminateGatewayProcess schedules SIGKILL at the backstop. In case the
  // exit event is delayed past that timer, issue one final tree-aware SIGKILL
  // and wait again before allowing the Electron parent to disappear.
  if (!exited && !hasGatewayProcessExited(child)) {
    terminateGatewayProcess(child, 'SIGKILL')
    exited = await waitForGatewayProcessExit(child, GATEWAY_HARD_KILL_BACKSTOP_MS)
  }
  desktopLog('quit_gateway_exit', { exited, hardTerminated })
  return exited || hasGatewayProcessExited(child)
}

app.on('before-quit', (event) => {
  desktopUpdateCheckScheduler.stop()
  artifactPreviewLeaseBroker.clear()
  // Remove native child views immediately so they cannot outlive the trusted
  // Control UI while the gateway/writer shutdown drain keeps Electron alive.
  void nativeWorkbenchSurfaces.destroyAll()
  // Windows session shutdown cannot wait on our normal asynchronous quit
  // drain. Let the OS-owned close proceed and synchronously signal the current
  // child so the window can never be converted back into background mode.
  if (systemSessionEnding) {
    isQuitting = true
    setAppExitPhase('committed', 'Windows session ending')
    destroyWindowsTray()
    stopGateway()
    return
  }
  // An updater drain owns the lifecycle until every writer and gateway has
  // exited. A user Quit or repeated signal during this phase is remembered and
  // resumed if the update cannot hand off. Only quitAndInstall's synchronous
  // handoff is allowed through this guard.
  if (updateApplying) {
    if (updateInstallHandoffReady) {
      setAppExitPhase('committed', 'desktop updater owns exit')
      destroyWindowsTray()
      return
    }
    event.preventDefault()
    setAppExitPhase('deferred', 'waiting for desktop update handoff')
    quitRequestedDuringUpdateDrain = true
    desktopLog('quit_deferred_for_update_drain')
    return
  }
  if (quitGatewayDrainPromise) {
    event.preventDefault()
    setAppExitPhase('draining', 'Gateway quit drain already in progress')
    return
  }
  if (desktopWriters.activeCount > 0 || quitDeferredForDesktopWriters) {
    event.preventDefault()
    setAppExitPhase('deferred', 'waiting for desktop writers')
    if (!quitDeferredForDesktopWriters) {
      quitDeferredForDesktopWriters = true
      quitWriterAdmission ??= desktopWriters.close('quit')
      isQuitting = false
      desktopLog('quit_deferred_for_profile_writer', {
        activeWriters: desktopWriters.activeCount,
      })
      void waitForDesktopWriterOperations().then(() => {
        quitDeferredForDesktopWriters = false
        app.quit()
      })
    }
    return
  }
  quitWriterAdmission ??= desktopWriters.close('quit')
  desktopLog('before_quit', {
    platform: process.platform,
    gatewayDrainInFlight: quitGatewayDrainPromise !== null,
  })
  isQuitting = true
  // Defer the normal quit on every platform until every lifecycle-owned child
  // has exited. This includes a child already draining for restart, recovery,
  // cleanup, or update after stopGateway cleared the current process slot.
  const currentChild = gatewayProcess && gatewayState.owned
    && !hasGatewayProcessExited(gatewayProcess)
    ? gatewayProcess
    : null
  const children = liveLifecycleOwnedGatewayProcesses()
  if (children.length > 0) {
    event.preventDefault()
    setAppExitPhase('draining', 'stopping lifecycle-owned Gateway')
    const drain = Promise.all(children.map((child) => drainOwnedGatewayForQuit(
      child,
      currentChild === child ? gatewayState.url || '' : '',
      currentChild === child,
    )))
      .then((results) => results.every(Boolean))
      .catch((error) => {
        desktopLog('quit_gateway_drain_failed', {
          error: error instanceof Error ? error.message : String(error),
        })
        return false
      })
    quitGatewayDrainPromise = drain
    void drain.then((exited) => {
      if (exited) {
        setAppExitPhase('committed', 'all lifecycle-owned Gateways exited')
        destroyWindowsTray()
        app.exit(0)
        return
      }
      // Fail closed: keep Electron alive while a child we own is still live.
      // A later Quit retries the same exact handles; it never guesses via a PID
      // file, occupied port, or health response.
      quitGatewayDrainPromise = null
      isQuitting = false
      setAppExitPhase('running', 'Gateway quit drain failed safely')
      if (quitWriterAdmission) {
        desktopWriters.reopen(quitWriterAdmission)
        quitWriterAdmission = null
      }
      desktopLog('quit_gateway_still_running', {
        pids: liveLifecycleOwnedGatewayProcesses().map((child) => child.pid),
      })
      dialog.showErrorBox(
        'OpenSquilla could not quit safely',
        'The local Gateway is still shutting down. OpenSquilla stayed open to avoid leaving a background process; try Quit again.',
      )
    })
    return
  }
  setAppExitPhase('committed', 'no lifecycle-owned Gateway remains')
  destroyWindowsTray()
  stopGateway()
})

function shutdownFromSignal(): void {
  isQuitting = true
  // before-quit owns the child handle until its single-flight drain finishes.
  // Clearing it here would recreate the orphaned-gateway race on SIGINT/SIGTERM.
  app.quit()
}

// Keep repeated signals inside the same idempotent before-quit drain. Using
// once() would restore Node's default termination behavior after the first
// signal and let a second Ctrl-C/SIGTERM orphan the Gateway.
process.on('SIGINT', shutdownFromSignal)
process.on('SIGTERM', shutdownFromSignal)

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('activate', () => {
  revealDesktopApp()
})

app.on('will-quit', destroyWindowsTray)

configureChromiumKeychainPolicy()

const initialDesktopDeepLinkArguments = process.platform === 'win32'
  ? desktopDeepLinkArguments(process.argv)
  : []

// Bounded retry for the single-instance lock. A relaunch immediately after
// closing the previous instance can race that instance's teardown (Electron
// exit + gateway TerminateProcess), during which the lock is briefly still
// held. Without a retry the new process silently quits and no window appears
// (issue #446). Retry synchronously for a short window, then — if still
// unavailable — surface an explicit error instead of exiting silently.
function acquireSingleInstanceLockWithRetry(): boolean {
  const deadline = Date.now() + 5_000
  // Atomics.wait blocks this thread without an event loop (app.whenReady has not
  // fired) and, unlike a Date.now() spin, does not peg a CPU core. Larger sleep
  // slices also cut the retry count — each failed requestSingleInstanceLock
  // notifies the running instance (firing its second-instance handler).
  const sleepSignal = new Int32Array(new SharedArrayBuffer(4))
  let attempt = 0
  for (;;) {
    attempt += 1
    if (app.requestSingleInstanceLock()) {
      desktopLog('single_instance_lock_acquired', { attempt })
      return true
    }
    // A Windows protocol launch targets the current instance and does not need
    // the normal close/relaunch race retry. The failed lock request has already
    // delivered its command line through second-instance; exit the forwarding
    // process immediately instead of sending the same deep link for five seconds.
    if (initialDesktopDeepLinkArguments.length > 0) {
      desktopLog('single_instance_deep_link_forwarded', { attempt })
      return false
    }
    const remaining = deadline - Date.now()
    if (remaining <= 0) {
      desktopLog('single_instance_lock_unavailable', { attempt })
      return false
    }
    Atomics.wait(sleepSignal, 0, 0, Math.min(400, remaining))
  }
}

app.on('open-url', (event, rawUrl) => {
  event.preventDefault()
  handleDeepLink(rawUrl, 'open-url')
})

desktopLog('launch', { platform: process.platform, argv: process.argv.length })
const gotSingleInstanceLock = acquireSingleInstanceLockWithRetry()

if (!gotSingleInstanceLock) {
  // Another instance genuinely holds the lock past the retry window. Signal it
  // to surface its window (the second-instance handler calls
  // openOrResumeDesktopApp), show an explicit dialog so the launch is never a
  // silent no-op, then quit.
  desktopLog('launch_aborted_lock_held', {
    deepLinkHandoff: initialDesktopDeepLinkArguments.length > 0,
  })
  if (initialDesktopDeepLinkArguments.length === 0) {
    try {
      // This runs before app.whenReady, so app.getLocale() is unreliable; fall back
      // to the persisted onboarding locale (a plain file read) for this dialog.
      desktopLocale = loadPersistedDesktopLocale() ?? desktopLocale
      dialog.showErrorBox(
        desktopT('launch.alreadyRunningTitle'),
        desktopT('launch.alreadyRunningMessage'),
      )
    } catch {
      // Dialog is best-effort; the diagnostic log is the durable record.
    }
  }
  app.quit()
} else {
  if (process.platform === 'win32') {
    handleDeepLinksFromCommandLine(process.argv, 'initial-argv')
  }

  app.on('second-instance', (_event, commandLine) => {
    const hadDeepLink = handleDeepLinksFromCommandLine(commandLine, 'second-instance')
    desktopLog('second_instance', { hadDeepLink })
    if (hadDeepLink) return
    revealDesktopApp()
  })

  void app.whenReady().then(async () => {
    app.name = 'OpenSquilla'
    desktopLocale = loadPersistedDesktopLocale() ?? resolveDesktopLocale()
    createApplicationMenu()
    createWindowsTray()
    registerDesktopDeepLinkProtocolClient()
    desktopDeepLinkActivationReady = true
    if (!activatePendingDesktopDeepLink()) void openOrResumeDesktopApp()
    initAutoUpdater()
    if (mockUpdateVersion() !== null) {
      desktopUpdateCheckScheduler.start(MOCK_UPDATE_CHECK_INITIAL_DELAY_MS)
    } else if (desktopUpdateManaged()) {
      // Delay the silent startup check so it doesn't compete with gateway boot.
      desktopUpdateCheckScheduler.start(UPDATE_CHECK_INITIAL_DELAY_MS)
    }
  })
}
