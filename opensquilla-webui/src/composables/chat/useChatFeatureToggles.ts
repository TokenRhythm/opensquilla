import { computed, ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { ChatRouterTierConfig } from '@/types/chat'
import type { ImageInputAdmission, ModelRoutingMode } from '@/types/modelRouting'
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

interface ModelRoutingSnapshot {
  mode?: 'direct' | 'router' | 'ensemble'
  selection_mode?: string
  image_input?: {
    admission?: 'allowed' | 'blocked' | 'unknown'
    reason?: string
  }
}

const ROUTER_SHAPE_KEY = 'opensquilla.router.shape'
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
  const imageInputAdmission = ref<ImageInputAdmission>('unknown')
  const imageInputAdmissionReason = ref('capability_unknown')
  let hasCanonicalImageAdmission = false
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
    llmEnsembleEnabled.value = mode === 'ensemble'
    // The product control remains one three-state selector. The existing
    // routerEnabled ref means "routing feature active" to ChatView, so Ensemble
    // remains active here even when its static selection mode bypasses the
    // SquillaRouter implementation internally.
    routerEnabled.value = mode !== 'direct'
    if (typeof snapshot.selection_mode === 'string') {
      llmEnsembleSelectionMode.value = snapshot.selection_mode
    }
    const admission = snapshot.image_input?.admission
    if (admission === 'allowed' || admission === 'blocked' || admission === 'unknown') {
      hasCanonicalImageAdmission = true
      imageInputAdmission.value = admission
      imageInputAdmissionReason.value = String(
        snapshot.image_input?.reason || 'capability_unknown',
      )
    } else if (mode === 'ensemble') {
      hasCanonicalImageAdmission = false
      imageInputAdmission.value = 'blocked'
      imageInputAdmissionReason.value = 'ensemble_mode_unsupported'
    } else {
      hasCanonicalImageAdmission = false
      imageInputAdmission.value = 'unknown'
      imageInputAdmissionReason.value = 'capability_unknown'
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
      imageInputAdmission.value = ensembleEnabled ? 'blocked' : 'unknown'
      imageInputAdmissionReason.value = ensembleEnabled
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

  async function loadFeatureToggles() {
    try {
      await waitForSessionRpcConnection(options.rpc, options.readCallOptions)
      const cfg = options.readCallOptions
        ? await options.rpc.call<ChatFeatureConfig>(
            'config.get',
            undefined,
            options.readCallOptions,
          )
        : await options.rpc.call<ChatFeatureConfig>('config.get')
      await applyFeatureConfig(cfg, { refreshUsage: true })
      try {
        const routing = options.readCallOptions
          ? await options.rpc.call<ModelRoutingSnapshot>(
              'models.routing.get',
              undefined,
              options.readCallOptions,
            )
          : await options.rpc.call<ModelRoutingSnapshot>('models.routing.get')
        applyModelRoutingSnapshot(routing)
      } catch {
        // Older Gateways have no canonical routing RPC; the config projection
        // applied above remains the read-only compatibility snapshot.
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
    imageInputAdmission,
    imageInputAdmissionReason,
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
