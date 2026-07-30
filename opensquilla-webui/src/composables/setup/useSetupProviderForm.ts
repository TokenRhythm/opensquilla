import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { useRpcStore } from '@/stores/rpc'

interface ProviderField {
  name: string
  label: string
  type?: string
  secret?: boolean
  default?: string | boolean | number
  [key: string]: unknown
}

interface ProviderSpec {
  providerId: string
  fields?: ProviderField[]
}

interface ProviderConfig {
  provider?: string
  model?: string
  base_url?: string
  proxy?: string
  api_key_env?: string
  api_key?: string
  [key: string]: unknown
}

interface SetupStatus {
  hasConfig?: boolean
  llmConfigured?: boolean
  llmSource?: string
}

interface ProviderPanelContext {
  currentConfig: ComputedRef<ProviderConfig>
  providerSummary: ComputedRef<string>
  runtimeProviders: ComputedRef<Array<{ providerId: string; label: string }>>
  routerSupportTone: ComputedRef<string>
  routerSupportText: ComputedRef<string>
  canConfigureRouter: ComputedRef<boolean>
  providerNeeds: ComputedRef<string[]>
  providerCoreFields: ComputedRef<ProviderField[]>
  providerAdvancedFields: ComputedRef<ProviderField[]>
  providerCredentialPanel: ComputedRef<ProviderCredentialPanelState | null>
  providerAdvancedOpen: ComputedRef<boolean>
  providerEnvMissing: ComputedRef<boolean>
  providerEnvKey: ComputedRef<string>
  providerEnvCommand: ComputedRef<string>
  llmTimeoutSeconds: Ref<number>
  contextWindowTokens: Ref<string>
  contextWindowGlobal: ComputedRef<number | null>
  effectiveMaxTokens: ComputedRef<EffectiveMaxTokens | null>
  providerIsLocal: ComputedRef<boolean>
  configuredProviders?: ComputedRef<Array<{
    providerId: string
    label: string
    active: boolean
    ready: boolean
    credentialSource: string
    credentialEnv: string
    endpointSource: string
    reason: string
    primaryEligible: boolean
    primaryBlockReason: string
    probeModelAvailable: boolean
    lastProbe: { ok: boolean; at: string; configChanged: boolean; failureKind: string } | null
  }>>
  editingPrimary?: ComputedRef<boolean>
  selectedStoredProfile?: ComputedRef<boolean>
  editingNew?: ComputedRef<boolean>
  routingEnabled?: ComputedRef<boolean>
  routerEnabled?: ComputedRef<boolean>
  routerBinding?: ComputedRef<'follow_primary' | 'custom' | 'legacy'>
  crossProviderRoutingEnabled?: ComputedRef<boolean>
  ensembleEnabled?: ComputedRef<boolean>
  activationRouterConflict?: ComputedRef<boolean>
  configuredProviderProbes?: Ref<Record<string, ConnectionState>>
  activation?: Ref<{
    providerId: string
    phase: 'idle' | 'discovering' | 'ready' | 'activating' | 'error'
    models: DiscoveredModel[]
    suggestedModel: string
    error: string
  }>
}

// ---------------------------------------------------------------------------
// Connection state machine (probe + model discovery)
// ---------------------------------------------------------------------------

/**
 * Lifecycle of the optional "Test connection" accelerator. Saving is NEVER
 * gated on this state — a user can save an unverified (or even failing)
 * config at any time; the machine only powers inline feedback and the
 * discovered-model combobox.
 *
 *   unconfigured -- selectProvider(id) --> unverified
 *   unverified   -- probeConnection()  --> probing
 *   probing      -- probe ok           --> verified (auto-fires discoverModels)
 *   probing      -- auth-ish failure   --> key_invalid
 *   probing      -- other failure/RPC error --> unreachable
 *   any          -- credential/provider/baseUrl/proxy edit --> unverified
 */
export type ConnectionPhase =
  | 'unconfigured'
  | 'unverified'
  | 'probing'
  | 'verified'
  | 'key_invalid'
  | 'unreachable'

export interface DiscoveredModelPricing {
  inputPer1k: number
  outputPer1k: number
}

/** One row of the onboarding.models.discover wire envelope (camelCase, frozen). */
export interface DiscoveredModel {
  id: string
  name: string
  contextWindow: number | null
  maxOutputTokens: number | null
  capabilities: string[]
  pricing: DiscoveredModelPricing | null
  capabilitySource: string
}

export interface DiscoveredModelCatalog {
  models: DiscoveredModel[]
  source: 'live' | 'none'
}

export interface EffectiveMaxTokens {
  value: number
  source: 'config' | 'catalog' | 'default'
}

/** Provider ids are normalized before they become catalog keys. */
export type DiscoveredModelsByProvider = Record<string, DiscoveredModelCatalog>

export interface ConnectionState {
  phase: ConnectionPhase
  failureKind: string
  detail: string
  /** Time until the first model response event, null when an older gateway does not report it. */
  firstResponseMs: number | null
  /** Full model probe duration through the terminal event, null when unknown. */
  totalMs: number | null
  /** @deprecated Legacy gateway field retained only for in-memory compatibility. */
  latencyMs: number | null
  models: DiscoveredModel[]
  modelSource: 'live' | 'none'
  discoverError: string
}

export interface ProviderCredentialPanelState {
  providerLabel: string
  providerSelected: boolean
  acceptsApiKey: boolean
  requiresApiKey: boolean
  source: string
  available: boolean
  removable: boolean
  removing: boolean
  envKey: string
  masked: string
  revealAllowed: boolean
  revealed: string
  revealError: string
  replacing: boolean
  apiKeyValue: string
  apiKeyEnvValue: string
  /** Unsaved credential input, kept distinct from saved credential readiness. */
  draftCredentialSource?: '' | 'key' | 'env'
  probeReady: boolean
  probeDisabledReason: string
  probeButtonLabel: string
  connection: ConnectionState
  onReveal?: () => void
  onHideReveal?: () => void
  onReplace?: () => void
  onCancelReplace?: () => void
  onRemoveCredential?: () => void
}

// Probe failure kinds that mean "the credential itself was rejected" (vs. the
// endpoint being unreachable/unhappy). Kept to the unambiguous case: other
// kinds (rate limits, credits, overload) get their own human sentence under
// the generic "couldn't connect" headline.
const AUTH_FAILURE_KINDS = new Set(['auth_invalid'])

// Editing credentials or deployment transport changes both what the small
// current-settings probe would execute and which model catalog is reachable.
// A model selection only invalidates the probe verdict: the catalog itself is
// still valid for the same provider deployment and must remain available in
// the combobox.
const DEPLOYMENT_CONNECTION_FIELDS = new Set(['api_key', 'api_key_env', 'base_url', 'proxy'])

export const PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS = 30_000

function freshConnection(providerId: string): ConnectionState {
  return {
    phase: providerId ? 'unverified' : 'unconfigured',
    failureKind: '',
    detail: '',
    firstResponseMs: null,
    totalMs: null,
    latencyMs: null,
    models: [],
    modelSource: 'none',
    discoverError: '',
  }
}

function normalizeLegacyLatencyMs(value: unknown): number | null {
  // The gateway sends latencyMs=0 as the "never reached the network" sentinel
  // (missing key / build failure), so a zero is not a real round trip — treat
  // it as unknown. New explicit timing fields legitimately may be zero.
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null
}

function normalizeProbeDurationMs(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

export function normalizeProbeTimings(response: {
  ok?: unknown
  firstResponseMs?: unknown
  totalMs?: unknown
  latencyMs?: unknown
} | null | undefined): Pick<ConnectionState, 'firstResponseMs' | 'totalMs' | 'latencyMs'> {
  const latencyMs = normalizeLegacyLatencyMs(response?.latencyMs)
  const firstResponseMs = normalizeProbeDurationMs(response?.firstResponseMs)
  let totalMs = normalizeProbeDurationMs(response?.totalMs) ?? latencyMs
  // New gateways use totalMs=0 when a failure was rejected before any network
  // or model response. Preserve legitimate zero-duration success measurements,
  // but do not present this failure sentinel as a completed probe.
  if (response?.ok === false && firstResponseMs == null && totalMs === 0) totalMs = null
  return {
    firstResponseMs,
    // Older gateways expose only latencyMs. It measured the complete probe,
    // never TTFT, so preserve it solely as a total-duration fallback.
    totalMs,
    latencyMs,
  }
}

export function normalizeDiscoveredModels(rows: unknown): DiscoveredModel[] {
  if (!Array.isArray(rows)) return []
  return rows
    .filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === 'object')
    .map(row => {
      const pricing = row.pricing
      return {
        id: String(row.id ?? ''),
        name: String(row.name ?? row.id ?? ''),
        contextWindow: typeof row.contextWindow === 'number' ? row.contextWindow : null,
        maxOutputTokens: typeof row.maxOutputTokens === 'number' ? row.maxOutputTokens : null,
        capabilities: Array.isArray(row.capabilities) ? row.capabilities.map(String) : [],
        pricing: pricing && typeof pricing === 'object'
          ? {
              inputPer1k: Number((pricing as Record<string, unknown>).inputPer1k ?? 0),
              outputPer1k: Number((pricing as Record<string, unknown>).outputPer1k ?? 0),
            }
          : null,
        capabilitySource: String(row.capabilitySource ?? ''),
      }
    })
    .filter(model => model.id)
}

function camel(name: string): string {
  return String(name || '').replace(/_([a-z])/g, (_, c) => c.toUpperCase())
}

export function buildProviderPayload(providerId: string, values: Record<string, unknown>): Record<string, unknown> {
  const payload: Record<string, unknown> = { providerId }
  Object.entries(values).forEach(([key, value]) => {
    if (value !== '' && value !== undefined) payload[camel(key)] = value
  })
  return payload
}

export function hasEffectiveProvider(config: ProviderConfig, status: SetupStatus): boolean {
  if (!config.provider) return false
  if (status.hasConfig !== false) return true
  if (status.llmConfigured === true) return true
  if (status.llmConfigured === false) return false
  return ['explicit', 'env', 'not_required'].includes(String(status.llmSource || ''))
}

export function useSetupProviderForm() {
  const providerSelected = ref('')
  const providerFieldValues = ref<Record<string, unknown>>({})
  const touchedFields = ref<Set<string>>(new Set())
  const replacingCredential = ref(false)
  const revealedCredential = ref('')
  const revealError = ref('')
  const selectedProvider = computed(() => providerSelected.value)
  let revealTimer: ReturnType<typeof setTimeout> | null = null

  const serialized = computed(() => JSON.stringify({ p: providerSelected.value, v: providerFieldValues.value }))
  // Seed from the initial state so the pristine form is never dirty while config loads.
  const baseline = ref(serialized.value)
  const isDirty = computed(() => serialized.value !== baseline.value)

  // -------------------------------------------------------------------------
  // Connection state machine
  // -------------------------------------------------------------------------

  const connection = ref<ConnectionState>(freshConnection(''))
  // Monotonic token: bumped by every reset and probe start so an in-flight
  // RPC result that raced a credential edit is discarded instead of applied.
  let connectionEpoch = 0
  let discoverPromise: Promise<void> | null = null

  function resetConnection() {
    connectionEpoch += 1
    discoverPromise = null
    connection.value = freshConnection(providerSelected.value)
  }

  function invalidateProbeVerdictPreservingCatalog() {
    // A model edit must cancel a probe that is still judging the previous
    // model. It must not cancel an independent catalog request, though: model
    // discovery is deployment-scoped and remains valid while the user types or
    // chooses a model from that same provider.
    if (connection.value.phase === 'probing') {
      connectionEpoch += 1
      discoverPromise = null
    }
    connection.value = {
      ...connection.value,
      phase: providerSelected.value ? 'unverified' : 'unconfigured',
      failureKind: '',
      detail: '',
      firstResponseMs: null,
      totalMs: null,
      latencyMs: null,
    }
  }

  function clearRevealTimer() {
    if (revealTimer) {
      clearTimeout(revealTimer)
      revealTimer = null
    }
  }

  function clearRevealedCredential() {
    clearRevealTimer()
    revealedCredential.value = ''
  }

  function resetCredentialUiState() {
    clearRevealTimer()
    replacingCredential.value = false
    revealedCredential.value = ''
    revealError.value = ''
  }

  // Params for probe/discover: the CURRENT form values, including an unsaved
  // pasted key — this is what makes "test before save" possible. Empty values
  // are dropped (the gateway falls back to the stored config / spec env key).
  function connectionParams(defaultModel = '', modelOverride?: string): Record<string, unknown> {
    const p = payload()
    const params: Record<string, unknown> = { providerId: providerSelected.value }
    for (const key of ['apiKey', 'apiKeyEnv', 'baseUrl', 'proxy'] as const) {
      if (p[key] !== undefined) params[key] = p[key]
    }
    const model = modelOverride !== undefined
      ? String(modelOverride).trim()
      : String(p.model ?? '').trim() || String(defaultModel || '').trim()
    if (model) params.model = model
    return params
  }

  function profileDraftParams(defaultModel = '', modelOverride?: string): Record<string, unknown> {
    const params = connectionParams(defaultModel, modelOverride)
    // Empty endpoint fields are meaningful in a draft: they mean “remove the
    // stored override and use the registry/global fallback”. buildProviderPayload
    // intentionally drops empties for ordinary saves, so restore these two from
    // the live form when the user explicitly edited or hydrated them.
    for (const [field, wire] of [['base_url', 'baseUrl'], ['proxy', 'proxy']] as const) {
      if (Object.prototype.hasOwnProperty.call(providerFieldValues.value, field)) {
        params[wire] = String(providerFieldValues.value[field] ?? '')
      }
    }
    return {
      ...params,
      keepCurrentSecret: params.apiKey === undefined && params.apiKeyEnv === undefined,
    }
  }

  async function probeConnection(options: {
    defaultModel?: string
    modelOverride?: string
    storedProfile?: boolean
    draftProfile?: boolean
  } = {}): Promise<void> {
    if (!providerSelected.value || connection.value.phase === 'probing') return
    const epoch = ++connectionEpoch
    discoverPromise = null
    connection.value = { ...freshConnection(providerSelected.value), phase: 'probing' }
    const rpc = useRpcStore()
    let outcome: ConnectionState
    try {
      const params = connectionParams(options.defaultModel, options.modelOverride)
      const draftParams = profileDraftParams(options.defaultModel, options.modelOverride)
      const res = await rpc.call<{
        ok?: boolean
        failureKind?: string
        message?: string
        firstResponseMs?: number
        totalMs?: number
        latencyMs?: number
      }>(
        options.draftProfile
          ? 'onboarding.llmProfile.draft.probe'
          : (options.storedProfile ? 'onboarding.llmProfile.probe' : 'onboarding.provider.probe'),
        options.draftProfile
          ? draftParams
          : (options.storedProfile
              ? { providerId: providerSelected.value, model: params.model || options.defaultModel || '' }
              : params),
      )
      if (epoch !== connectionEpoch) return
      const timings = normalizeProbeTimings(res)
      if (res?.ok) {
        outcome = { ...freshConnection(providerSelected.value), phase: 'verified', ...timings }
      } else {
        const kind = String(res?.failureKind || '')
        outcome = {
          ...freshConnection(providerSelected.value),
          phase: AUTH_FAILURE_KINDS.has(kind) ? 'key_invalid' : 'unreachable',
          failureKind: kind,
          detail: String(res?.message || ''),
          ...timings,
        }
      }
    } catch (err) {
      if (epoch !== connectionEpoch) return
      outcome = {
        ...freshConnection(providerSelected.value),
        phase: 'unreachable',
        detail: err instanceof Error ? err.message : String(err),
      }
    }
    connection.value = outcome
    if (outcome.phase === 'verified') {
      // Verified endpoint: immediately offer discovered models. The combined
      // verified+models state is kept live only; every explicit test click
      // re-probes so a newly issued key or recovered provider is not masked by
      // a stale verdict.
      await discoverModels({
        modelOverride: options.modelOverride,
        storedProfile: options.storedProfile,
        draftProfile: options.draftProfile,
      })
    }
  }

  function discoverModels(options: {
    modelOverride?: string
    storedProfile?: boolean
    draftProfile?: boolean
  } = {}): Promise<void> {
    if (!providerSelected.value) return Promise.resolve()
    if (discoverPromise) return discoverPromise
    const epoch = connectionEpoch
    const rpc = useRpcStore()
    const request = (async () => {
      try {
        const res = await rpc.call<{
          ok?: boolean
          failureKind?: string
          detail?: string
          source?: string
          models?: unknown
        }>(
          options.draftProfile
            ? 'onboarding.llmProfile.draft.models.discover'
            : (options.storedProfile
                ? 'onboarding.llmProfile.models.discover'
                : 'onboarding.models.discover'),
          options.draftProfile
            ? profileDraftParams('', options.modelOverride)
            : (options.storedProfile
                ? { providerId: providerSelected.value }
                : connectionParams('', options.modelOverride)),
        )
        if (epoch !== connectionEpoch) return
        if (res?.ok) {
          const modelSource = res.source === 'live' ? 'live' : 'none'
          connection.value = {
            ...connection.value,
            models: modelSource === 'live' ? normalizeDiscoveredModels(res.models) : [],
            modelSource,
            discoverError: '',
          }
        } else {
          connection.value = {
            ...connection.value,
            models: [],
            modelSource: 'none',
            discoverError: String(res?.detail || res?.failureKind || 'discover failed'),
          }
        }
      } catch (err) {
        if (epoch !== connectionEpoch) return
        connection.value = {
          ...connection.value,
          models: [],
          modelSource: 'none',
          discoverError: err instanceof Error ? err.message : String(err),
        }
      }
    })()
    const tracked = request.finally(() => {
      if (discoverPromise === tracked) {
        discoverPromise = null
      }
    })
    discoverPromise = tracked
    return tracked
  }

  function initFromConfig(
    config: ProviderConfig,
    status: SetupStatus,
    providers: ProviderSpec[],
    configured = hasEffectiveProvider(config, status),
  ) {
    resetCredentialUiState()
    touchedFields.value = new Set()
    providerSelected.value = ''
    providerFieldValues.value = {}
    if (configured && config.provider) {
      providerSelected.value = config.provider
      const spec = providers.find(p => p.providerId === config.provider)
      spec?.fields?.forEach(field => {
        // Secrets are write-only: config.get returns the literal "[redacted]",
        // which must never be seeded into the form or echoed back on save.
        if (field.secret || field.type === 'password') return
        if (field.name === 'api_key_env') return
        const value = config[field.name]
        if (value !== undefined) providerFieldValues.value[field.name] = value
      })
    }
    baseline.value = serialized.value
    resetConnection()
  }

  function resetForProvider(
    spec: { fields?: ProviderField[] } | null | undefined,
    options: { inheritModelDefault?: boolean } = {},
  ) {
    resetCredentialUiState()
    touchedFields.value = new Set()
    providerFieldValues.value = {}
    spec?.fields?.forEach(field => {
      if (field.name === 'model' && options.inheritModelDefault) return
      providerFieldValues.value[field.name] = field.default ?? ''
    })
    resetConnection()
  }

  /**
   * Select an already-persisted non-primary profile for inspection/editing.
   * Secrets remain write-only, while the public model and endpoint fields are
   * hydrated from config.get and baselined so selecting the profile stays clean.
   */
  function initStoredProfile(providerId: string, config: ProviderConfig = {}) {
    resetCredentialUiState()
    touchedFields.value = new Set()
    providerSelected.value = providerId
    providerFieldValues.value = {}
    // Profile secrets and credential references stay write-only/implicit, but
    // model and endpoint fields are public config and must be present in the
    // editor. Seeding them also makes a later partial edit round-trip the
    // deployment instead of silently dropping its direct model or transport.
    const savedModel = String(config.model ?? '').trim()
    if (savedModel) providerFieldValues.value.model = savedModel
    for (const name of ['base_url', 'proxy'] as const) {
      if (config[name] !== undefined) providerFieldValues.value[name] = config[name]
    }
    baseline.value = serialized.value
    resetConnection()
  }

  function fieldValue(field: ProviderField, current: ProviderConfig): string {
    const name = field.name
    if (providerFieldValues.value[name] !== undefined) {
      return String(providerFieldValues.value[name] || '')
    }
    if (name === 'model') return String(current.model || field.default || '')
    if (name === 'base_url') return String(current.base_url || field.default || '')
    if (name === 'proxy') return String(current.proxy || '')
    if (name === 'api_key_env') return String(current.api_key_env || (current.api_key ? '' : field.default || ''))
    return ''
  }

  function isNonEmpty(value: unknown): boolean {
    return typeof value === 'string' ? value.trim() !== '' : value !== undefined && value !== null && value !== ''
  }

  function updateField(name: string, value: unknown) {
    touchedFields.value = new Set([...touchedFields.value, name])
    providerFieldValues.value[name] = value
    // api_key (pasted) and api_key_env (env reference) are mutually exclusive:
    // the gateway rejects a save that carries both. Setting one to a non-empty
    // value clears the other in the form so the two can never be submitted
    // together (the env field is often pre-filled from a detected variable).
    if (isNonEmpty(value)) {
      if (name === 'api_key') providerFieldValues.value.api_key_env = ''
      else if (name === 'api_key_env') providerFieldValues.value.api_key = ''
    }
    if (name === 'api_key' || name === 'api_key_env') {
      clearRevealedCredential()
      revealError.value = ''
    }
    // Any field used by the provider-bounded probe invalidates its earned
    // verdict. A model choice does not invalidate the provider's model
    // listing, while credential/endpoint changes do.
    if (name === 'model') invalidateProbeVerdictPreservingCatalog()
    else if (DEPLOYMENT_CONNECTION_FIELDS.has(name)) resetConnection()
  }

  function startCredentialReplace() {
    replacingCredential.value = true
    clearRevealedCredential()
    revealError.value = ''
  }

  function cancelCredentialReplace() {
    replacingCredential.value = false
    providerFieldValues.value.api_key = ''
    clearRevealedCredential()
    revealError.value = ''
  }

  function setRevealedCredential(value: string) {
    clearRevealTimer()
    revealedCredential.value = value
    revealError.value = ''
    if (value) {
      revealTimer = setTimeout(() => {
        revealedCredential.value = ''
        revealTimer = null
      }, PROVIDER_CREDENTIAL_REVEAL_TIMEOUT_MS)
    }
  }

  function hideRevealedCredential() {
    clearRevealedCredential()
  }

  function setRevealError(value: string) {
    clearRevealedCredential()
    revealError.value = value
  }

  function selectProvider(value: string) {
    providerSelected.value = value
    touchedFields.value = new Set()
    resetCredentialUiState()
    resetConnection()
  }

  function payload(): Record<string, unknown> {
    // Hard guard (independent of UI state): never submit both a pasted key and
    // an env reference. A non-empty pasted api_key wins; otherwise the env
    // reference is used. buildProviderPayload drops empty values.
    const values: Record<string, unknown> = { ...providerFieldValues.value }
    if (isNonEmpty(values.api_key)) {
      delete values.api_key_env // a real pasted key wins
    } else {
      delete values.api_key // blank/whitespace paste is not a credential, keep env reference
      if (!isNonEmpty(values.api_key_env)) delete values.api_key_env
    }
    return buildProviderPayload(providerSelected.value, values)
  }

  function fieldTouched(name: string): boolean {
    return touchedFields.value.has(name)
  }

  function createPanel(context: ProviderPanelContext) {
    return computed(() => ({
      providerSummary: context.providerSummary.value,
      providerSelected: providerSelected.value,
      runtimeProviders: context.runtimeProviders.value,
      routerSupportTone: context.routerSupportTone.value,
      routerSupportText: context.routerSupportText.value,
      canConfigureRouter: context.canConfigureRouter.value,
      providerNeeds: context.providerNeeds.value,
      providerCoreFields: context.providerCoreFields.value,
      providerAdvancedFields: context.providerAdvancedFields.value,
      credentialPanel: context.providerCredentialPanel.value,
      providerAdvancedOpen: context.providerAdvancedOpen.value,
      providerEnvMissing: context.providerEnvMissing.value,
      providerEnvKey: context.providerEnvKey.value,
      providerEnvCommand: context.providerEnvCommand.value,
      llmTimeoutSeconds: context.llmTimeoutSeconds.value,
      contextWindowTokens: context.contextWindowTokens.value,
      contextWindowGlobal: context.contextWindowGlobal.value,
      effectiveMaxTokens: context.effectiveMaxTokens.value,
      providerIsLocal: context.providerIsLocal.value,
      configuredProviders: context.configuredProviders?.value ?? [],
      editingPrimary: context.editingPrimary?.value ?? true,
      selectedStoredProfile: context.selectedStoredProfile?.value ?? false,
      editingNew: context.editingNew?.value ?? false,
      routingEnabled: context.routingEnabled?.value ?? false,
      routerEnabled: context.routerEnabled?.value ?? false,
      routerBinding: context.routerBinding?.value ?? 'legacy',
      crossProviderRoutingEnabled: context.crossProviderRoutingEnabled?.value ?? false,
      ensembleEnabled: context.ensembleEnabled?.value ?? false,
      activationRouterConflict: context.activationRouterConflict?.value ?? false,
      configuredProviderProbes: context.configuredProviderProbes?.value ?? {},
      activation: context.activation?.value ?? {
        providerId: '', phase: 'idle', models: [], suggestedModel: '', error: '',
      },
      connection: connection.value,
      providerFieldValue: (field: ProviderField) => fieldValue(field, context.currentConfig.value),
    }))
  }

  return {
    selectedProvider,
    isDirty,
    connection,
    providerFieldValues,
    replacingCredential,
    revealedCredential,
    revealError,
    initFromConfig,
    initStoredProfile,
    resetForProvider,
    fieldValue,
    fieldTouched,
    selectProvider,
    updateField,
    startCredentialReplace,
    cancelCredentialReplace,
    setRevealedCredential,
    hideRevealedCredential,
    setRevealError,
    resetConnectionState: resetConnection,
    invalidateProbeVerdict: invalidateProbeVerdictPreservingCatalog,
    payload,
    probeConnection,
    discoverModels,
    createPanel,
  }
}
