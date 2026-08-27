import { computed, ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { ChatRouterTierConfig } from '@/types/chat'
import type {
  ImageInputAdmission,
  ModelRoutingCapabilitiesByMode,
  ModelRoutingMode,
  ModelRoutingSnapshot,
} from '@/types/modelRouting'
import { normalizeModelRoutingMode } from '@/types/modelRouting'
import { normalizeRouterTier, sortRouterTiers } from '@/utils/chat/routerTiers'
import { encodeRouterShape, decodeRouterShape } from '@/utils/chat/routerShapeCache'
import {
  DEFAULT_ROUTER_VISUAL_MODE,
  normalizeRouterVisualMode,
} from '@/utils/chat/routerVisualMode'
import { useRouterVisualEffectsPreference } from '@/composables/useRouterVisualEffectsPreference'
import {
  waitForSessionRpcConnection,
} from '@/composables/chat/sessionBootstrapAdmission'
import type { RpcCallOptions, RpcConnectionWaitOptions } from '@/lib/rpc'

type RpcClient = {
  waitForConnection: (
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: RpcConnectionWaitOptions,
  ) => Promise<void>
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    callOptions?: RpcCallOptions,
  ) => Promise<T>
  on?: (event: string, handler: (payload: unknown) => void) => () => void
  supportsMethod?: (method: string) => boolean
}

export interface UseChatFeatureTogglesOptions {
  rpc: RpcClient
  readCallOptions?: RpcCallOptions
  setGlobalElevatedMode: (mode: string) => void
  loadCurrentSessionUsage: () => void | Promise<void>
}

interface ChatFeatureConfig {
  squilla_router?: {
    enabled?: boolean
    rollout_phase?: string
    visual_mode?: string
    tiers?: Record<string, {
      model?: string
      supports_image?: boolean
      supportsImage?: boolean
      image_only?: boolean
      imageOnly?: boolean
      ensemble_enabled?: boolean
      ensembleEnabled?: boolean
      ensemble_selection_mode?: string
      ensembleSelectionMode?: string
    }>
  }
  permissions?: {
    default_mode?: string
  }
  skills?: {
    coding_mode?: boolean
  }
  llm_ensemble?: {
    enabled?: boolean
    selection_mode?: string
  }
}

const ROUTER_SHAPE_KEY = 'opensquilla.router.shape'

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function parseCapabilitiesByMode(value: unknown): ModelRoutingCapabilitiesByMode | null {
  const source = record(value)
  if (!source) return null

  const parsed = {} as ModelRoutingCapabilitiesByMode
  for (const mode of ['direct', 'router', 'ensemble'] as const) {
    const modeCapabilities = record(source[mode])
    const imageInput = record(modeCapabilities?.image_input)
    const admission = imageInput?.admission
    const reason = imageInput?.reason
    if (
      (admission !== 'allowed' && admission !== 'blocked' && admission !== 'unknown')
      || typeof reason !== 'string'
      || !reason
    ) return null
    parsed[mode] = {
      image_input: {
        admission,
        reason,
      },
    }
  }
  return parsed
}

function isMethodNotFound(error: unknown): boolean {
  const candidate = record(error)
  const message = error instanceof Error
    ? error.message
    : String(candidate?.message || error || '')
  return candidate?.code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

export function useChatFeatureToggles(options: UseChatFeatureTogglesOptions) {
  const { pushToast } = useToasts()
  const routerEnabled = ref(false)
  const {
    enabled: routerVisualEffectsEnabled,
    setEnabled: setRouterVisualEffectsEnabled,
  } = useRouterVisualEffectsPreference()
  const routerVisualMode = ref(DEFAULT_ROUTER_VISUAL_MODE)
  const routerSettingsBusy = ref(false)
  const codingModeEnabled = ref(false)
  const codingModeSettingsBusy = ref(false)
  const llmEnsembleEnabled = ref(false)
  const llmEnsembleSelectionMode = ref('')
  const llmEnsembleSettingsBusy = ref(false)
  const modelRoutingSettingsBusy = ref(false)
  const globalImageInputAdmission = ref<ImageInputAdmission>('unknown')
  const globalImageInputAdmissionReason = ref('capability_unknown')
  const modelRoutingCapabilitiesByMode = ref<ModelRoutingCapabilitiesByMode | null>(null)
  let hasCanonicalImageAdmission = false
  let modelRoutingRequestGeneration = 0
  let modelRoutingEventGeneration = 0
  let latestModelRoutingSnapshot: ModelRoutingSnapshot | undefined
  const routerSlots = ref<string[]>([])
  const routerModels = ref<Record<string, string>>({})
  const routerTierConfigs = ref<Record<string, ChatRouterTierConfig>>({})

  const modelRoutingMode = computed<ModelRoutingMode>(() => {
    if (llmEnsembleEnabled.value) return 'llm_ensemble'
    return routerEnabled.value ? 'squilla_router' : 'off'
  })

  function applyModelRoutingSnapshot(snapshot: ModelRoutingSnapshot | undefined) {
    const mode = snapshot?.mode
    if (!mode) return false
    latestModelRoutingSnapshot = snapshot
    llmEnsembleEnabled.value = mode === 'ensemble'
    // The product control remains one three-state selector. The existing
    // routerEnabled ref means "routing feature active" to ChatView, so Ensemble
    // remains active here even when its static selection mode bypasses the
    // SquillaRouter implementation internally.
    routerEnabled.value = mode !== 'direct'
    modelRoutingCapabilitiesByMode.value = parseCapabilitiesByMode(
      snapshot.capabilities_by_mode,
    )
    if (typeof snapshot.selection_mode === 'string') {
      llmEnsembleSelectionMode.value = snapshot.selection_mode
    }
    const admission = snapshot.image_input?.admission
    if (admission === 'allowed' || admission === 'blocked' || admission === 'unknown') {
      hasCanonicalImageAdmission = true
      globalImageInputAdmission.value = admission
      globalImageInputAdmissionReason.value = String(
        snapshot.image_input?.reason || 'capability_unknown',
      )
    } else if (mode === 'ensemble') {
      hasCanonicalImageAdmission = false
      globalImageInputAdmission.value = 'blocked'
      globalImageInputAdmissionReason.value = 'ensemble_mode_unsupported'
    } else {
      hasCanonicalImageAdmission = false
      globalImageInputAdmission.value = 'unknown'
      globalImageInputAdmissionReason.value = 'capability_unknown'
    }
    return true
  }

  // Seed the last-known router shape synchronously so the router-strip reserve
  // twin can hold its slot on the first turn, before config.get resolves.
  hydrateRouterShape()

  async function applyFeatureConfig(cfg: ChatFeatureConfig | undefined, applyOptions: { refreshUsage?: boolean } = {}) {
    const router = cfg?.squilla_router || {}
    const ensembleEnabled = cfg?.llm_ensemble?.enabled === true

    routerEnabled.value = ensembleEnabled || Boolean(router.enabled && router.rollout_phase !== 'observe')
    codingModeEnabled.value = cfg?.skills?.coding_mode === true
    llmEnsembleEnabled.value = ensembleEnabled
    llmEnsembleSelectionMode.value = String(cfg?.llm_ensemble?.selection_mode || '')
    if (!hasCanonicalImageAdmission) {
      globalImageInputAdmission.value = ensembleEnabled ? 'blocked' : 'unknown'
      globalImageInputAdmissionReason.value = ensembleEnabled
        ? 'ensemble_mode_unsupported'
        : 'capability_unknown'
    }
    routerVisualMode.value = normalizeRouterVisualMode(router.visual_mode)
    const tiers = router.tiers
    const tierKeys: string[] = []
    const tierModels: Record<string, string> = {}
    const tierConfigs: Record<string, ChatRouterTierConfig> = {}
    if (tiers && typeof tiers === 'object') {
      Object.keys(tiers).forEach((tier) => {
        if (!tier) return
        const lower = normalizeRouterTier(tier)
        if (!lower) return
        tierKeys.push(lower)
        const rawTier = tiers[tier] || {}
        const rawTierRecord = rawTier as Record<string, unknown>
        const model = rawTier.model
        if (typeof model === 'string' && model.trim()) {
          tierModels[lower] = model.trim()
        }
        const explicitEnsembleEnabled = typeof rawTierRecord.ensemble_enabled === 'boolean'
          ? rawTierRecord.ensemble_enabled
          : typeof rawTierRecord.ensembleEnabled === 'boolean'
            ? rawTierRecord.ensembleEnabled
            : undefined
        const legacyEnsembleMode = String(
          rawTierRecord.ensemble_selection_mode
          ?? rawTierRecord.ensembleSelectionMode
          ?? '',
        ).trim()
        tierConfigs[lower] = {
          model: typeof model === 'string' ? model.trim() : '',
          supportsImage: rawTierRecord.supports_image === true || rawTierRecord.supportsImage === true,
          imageOnly: rawTierRecord.image_only === true || rawTierRecord.imageOnly === true,
          // New Gateways expose the explicit execution switch. Older PR
          // snapshots only expose the legacy selection mode, which still
          // means this tier runs the shared ensemble pipeline.
          ensembleEnabled: explicitEnsembleEnabled ?? Boolean(legacyEnsembleMode),
        }
      })
    }

    routerSlots.value = sortRouterTiers(tierKeys)
    routerModels.value = tierModels
    routerTierConfigs.value = tierConfigs
    persistRouterShape()
    options.setGlobalElevatedMode(cfg?.permissions?.default_mode || '')
    if (applyOptions.refreshUsage) {
      await options.loadCurrentSessionUsage()
    }
  }

  async function applyLegacyModelRoutingFallback(cfg: ChatFeatureConfig | undefined) {
    latestModelRoutingSnapshot = undefined
    modelRoutingCapabilitiesByMode.value = null
    hasCanonicalImageAdmission = false
    await applyFeatureConfig(cfg)
  }

  async function loadFeatureToggles() {
    const requestGeneration = ++modelRoutingRequestGeneration
    const eventGeneration = modelRoutingEventGeneration
    let cfg: ChatFeatureConfig | undefined
    try {
      await waitForSessionRpcConnection(options.rpc, options.readCallOptions)
      cfg = options.readCallOptions
        ? await options.rpc.call<ChatFeatureConfig>(
            'config.get',
            undefined,
            options.readCallOptions,
          )
        : await options.rpc.call<ChatFeatureConfig>('config.get')
      if (requestGeneration !== modelRoutingRequestGeneration) return
      await applyFeatureConfig(cfg, { refreshUsage: true })
      if (requestGeneration !== modelRoutingRequestGeneration) return
      // Config remains the compatibility source for older Gateways. A routing
      // event observed while it loaded is newer, so restore that atomic public
      // snapshot and retire this read before it can start a stale canonical GET.
      if (eventGeneration !== modelRoutingEventGeneration) {
        if (latestModelRoutingSnapshot) {
          applyModelRoutingSnapshot(latestModelRoutingSnapshot)
        }
        return
      }
      if (options.rpc.supportsMethod?.('models.routing.get') === false) {
        await applyLegacyModelRoutingFallback(cfg)
        return
      }
      try {
        const routing = options.readCallOptions
          ? await options.rpc.call<ModelRoutingSnapshot>(
              'models.routing.get',
              undefined,
              options.readCallOptions,
            )
          : await options.rpc.call<ModelRoutingSnapshot>('models.routing.get')
        if (
          requestGeneration === modelRoutingRequestGeneration
          && eventGeneration === modelRoutingEventGeneration
        ) {
          applyModelRoutingSnapshot(routing)
        }
      } catch (error) {
        if (
          requestGeneration !== modelRoutingRequestGeneration
          || eventGeneration !== modelRoutingEventGeneration
        ) return
        if (!isMethodNotFound(error)) {
          // A timeout or transient server/transport error is not evidence that
          // the Gateway contract disappeared. Preserve the last authoritative
          // matrix so a known block cannot accidentally degrade to unknown.
          if (latestModelRoutingSnapshot) {
            applyModelRoutingSnapshot(latestModelRoutingSnapshot)
          }
          return
        }
        // A successful connection to an older Gateway must not retain a
        // matrix learned from the previous connection. Clear the canonical
        // cache as one unit, then re-apply this Gateway's config projection.
        await applyLegacyModelRoutingFallback(cfg)
      }
    } catch {
      // Feature toggles are optional for older gateways.
    }
  }

  // Hydrate the router shape from localStorage into the live refs. Synchronous
  // and side-effect-free on failure so it is safe to call at composable init.
  function hydrateRouterShape() {
    try {
      const cached = decodeRouterShape(localStorage.getItem(ROUTER_SHAPE_KEY))
      if (!cached) return
      routerEnabled.value = cached.enabled
      routerSlots.value = cached.slots
      routerModels.value = cached.models
      routerTierConfigs.value = cached.configs
    } catch {}
  }

  // Persist the just-loaded shape so the next page load can seed the reserve.
  // Skip when there are no tier models — a degenerate shape would only seed a
  // <=1-cell reserve, which the reserve gate rejects anyway.
  function persistRouterShape() {
    try {
      if (Object.keys(routerModels.value).length === 0) return
      localStorage.setItem(ROUTER_SHAPE_KEY, encodeRouterShape({
        enabled: routerEnabled.value,
        slots: routerSlots.value,
        models: routerModels.value,
        configs: routerTierConfigs.value,
      }))
    } catch {}
  }

  async function setRouterEnabled(enabled: boolean) {
    await setModelRoutingMode(enabled ? 'squilla_router' : 'off')
  }

  async function setCodingModeEnabled(enabled: boolean): Promise<boolean> {
    if (codingModeSettingsBusy.value) return false
    const nextEnabled = Boolean(enabled)
    const previous = codingModeEnabled.value
    codingModeSettingsBusy.value = true
    try {
      await options.rpc.waitForConnection()
      await options.rpc.call('config.patch.safe', {
        patches: {
          'skills.coding_mode': nextEnabled,
        },
      })
      const cfg = await options.rpc.call<ChatFeatureConfig>('config.get')
      await applyFeatureConfig(cfg)
      return codingModeEnabled.value === nextEnabled
    } catch (err) {
      codingModeEnabled.value = previous
      console.warn('Failed to update Coding mode:', err instanceof Error ? err.message : String(err))
      return false
    } finally {
      codingModeSettingsBusy.value = false
    }
  }

  async function setLlmEnsembleEnabled(enabled: boolean) {
    await setModelRoutingMode(enabled ? 'llm_ensemble' : 'off')
  }

  async function setModelRoutingMode(mode: ModelRoutingMode) {
    if (modelRoutingSettingsBusy.value) return
    const nextMode = normalizeModelRoutingMode(mode)
    const previousRouter = routerEnabled.value
    const previousEnsemble = llmEnsembleEnabled.value
    const nextEnsemble = nextMode === 'llm_ensemble'

    // This is only optimistic presentation. The Gateway owns the actual
    // direct/router/ensemble transition (including ensemble dependency rules)
    // and the post-write config refresh remains authoritative.
    routerEnabled.value = nextMode !== 'off'
    llmEnsembleEnabled.value = nextEnsemble
    modelRoutingSettingsBusy.value = true
    routerSettingsBusy.value = true
    llmEnsembleSettingsBusy.value = true
    try {
      await options.rpc.waitForConnection()
      await options.rpc.call('models.routing.set', {
        mode: nextMode === 'off'
          ? 'direct'
          : nextMode === 'squilla_router' ? 'router' : 'ensemble',
      })
      await loadFeatureToggles()
    } catch (err) {
      routerEnabled.value = previousRouter
      llmEnsembleEnabled.value = previousEnsemble
      console.warn('Failed to update model routing:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t('chat.modelRouting.updateFailed'), { tone: 'danger' })
    } finally {
      modelRoutingSettingsBusy.value = false
      routerSettingsBusy.value = false
      llmEnsembleSettingsBusy.value = false
    }
  }

  function bindFeatureRefresh(scheduleHistorySync?: () => void) {
    let timer: ReturnType<typeof setTimeout> | null = null
    const schedule = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => {
        timer = null
        loadFeatureToggles().finally(() => scheduleHistorySync?.())
      }, 120)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') schedule()
    }
    const onFocus = () => schedule()
    const unbindRouting = options.rpc.on?.('models.routing.changed', (payload) => {
      modelRoutingEventGeneration += 1
      if (payload && typeof payload === 'object') {
        applyModelRoutingSnapshot(payload as ModelRoutingSnapshot)
        scheduleHistorySync?.()
      }
    })
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('focus', onFocus)
    return () => {
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('focus', onFocus)
      unbindRouting?.()
    }
  }

  return {
    routerEnabled,
    routerVisualEffectsEnabled,
    routerVisualMode,
    routerSettingsBusy,
    modelRoutingMode,
    modelRoutingSettingsBusy,
    globalImageInputAdmission,
    globalImageInputAdmissionReason,
    modelRoutingCapabilitiesByMode,
    codingModeEnabled,
    codingModeSettingsBusy,
    llmEnsembleEnabled,
    llmEnsembleSelectionMode,
    llmEnsembleSettingsBusy,
    routerSlots,
    routerModels,
    routerTierConfigs,
    loadFeatureToggles,
    setRouterEnabled,
    setModelRoutingMode,
    setCodingModeEnabled,
    setLlmEnsembleEnabled,
    setRouterVisualEffectsEnabled,
    bindFeatureRefresh,
  }
}
