<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import SetupModelCombobox from '@/components/setup/SetupModelCombobox.vue'
import type { DiscoveredModel } from '@/composables/setup/useSetupProviderForm'
import { APP_SETTINGS_KEY, type SettingChange } from '@/modules/appSettings'
import { SETUP_WORKFLOW_KEY } from '@/modules/setupWorkflow'

type VideoProvider = '' | 'openrouter' | 'gemini' | 'xai' | 'qwen' | 'qwen_token_plan' | 'tokenrhythm'
type ConfiguredVideoProvider = Exclude<VideoProvider, ''>
type VideoAspect = '16:9' | '9:16'
type VideoResolution = '720p' | '1080p'
type VideoProviderDraft = {
  primary: string
  baseUrl: string
  apiKeyEnv: string
}
type VideoProviderConnection = Pick<VideoProviderDraft, 'baseUrl' | 'apiKeyEnv'>
type VideoCredentialOption = {
  available: boolean
  source: string
  envKey: string
  clearable: boolean
}
type VideoDraft = {
  enabled: boolean
  provider: VideoProvider
  primary: string
  providerSettings: Partial<Record<ConfiguredVideoProvider, VideoProviderConnection>>
  durationSeconds: string
  maxDurationSeconds: string
  aspectRatio: VideoAspect
  resolution: VideoResolution
}

const { t } = useI18n()
const emit = defineEmits<{
  dirtyChange: [dirty: boolean]
  busyChange: [busy: boolean]
}>()
const settings = inject(APP_SETTINGS_KEY, null)
const setupWorkflow = inject(SETUP_WORKFLOW_KEY, null)
const phase = ref<'loading' | 'ready' | 'unsupported' | 'error'>('loading')
const busy = ref(false)
const providerFieldSupported = ref(false)
const providerSettingsSupported = ref(false)
const saved = ref<VideoDraft | null>(null)
const providerDrafts = new Map<ConfiguredVideoProvider, VideoProviderDraft>()
const savedProviderDrafts = new Map<ConfiguredVideoProvider, VideoProviderDraft>()
const storedDirectKeys = ref(new Set<ConfiguredVideoProvider>())
const credentialOptions = ref<Partial<Record<ConfiguredVideoProvider, VideoCredentialOption>>>({})
const supportedProviderIds = ref(new Set<ConfiguredVideoProvider>())
const discoveredModels = ref<Partial<Record<ConfiguredVideoProvider, {
  models: DiscoveredModel[]
  source: string
  verifiedIds: string[]
}>>>({})
const directApiKey = ref('')
const clearDirectKeyRequested = ref(false)
const enabled = ref(false)
const provider = ref<VideoProvider>('')
const primary = ref('')
const baseUrl = ref('')
const apiKeyEnv = ref('')
const durationSeconds = ref<string | number>('')
const maxDurationSeconds = ref<string | number>('8')
const aspectRatio = ref<VideoAspect>('16:9')
const resolution = ref<VideoResolution>('720p')
const allowedAspectRatios = ref<VideoAspect[]>(['16:9', '9:16'])
const allowedResolutions = ref<VideoResolution[]>(['720p', '1080p'])
const message = ref('')
const error = ref('')

const fallbackVideoProviders: Array<{ id: ConfiguredVideoProvider; label: string }> = [
  { id: 'openrouter', label: 'OpenRouter' },
  { id: 'gemini', label: 'Google Gemini' },
  { id: 'xai', label: 'xAI' },
  { id: 'qwen', label: 'Qwen (Standard DashScope)' },
  { id: 'qwen_token_plan', label: 'Qwen Token Plan' },
  { id: 'tokenrhythm', label: 'TokenRhythm' },
]

const fallbackSuggestedModels: Record<ConfiguredVideoProvider, string[]> = {
  openrouter: ['google/veo-3.1-fast', 'google/veo-3.1'],
  gemini: [
    'veo-3.1-fast-generate-preview',
    'veo-3.1-generate-preview',
    'veo-3.1-lite-generate-preview',
  ],
  xai: ['grok-imagine-video-1.5', 'grok-imagine-video-1.5-lite', 'grok-imagine-video'],
  qwen: ['wan2.7-t2v', 'wan2.7-t2v-2026-06-12', 'wan2.6-t2v'],
  qwen_token_plan: ['happyhorse-1.1-t2v'],
  tokenrhythm: ['wan3.0-video'],
}
const catalogProviders = ref<Partial<Record<ConfiguredVideoProvider, {
  label: string
  baseUrl: string
  apiKeyEnv: string
  defaultModel: string
  models: string[]
}>>>({})
const videoProviders = computed(() => fallbackVideoProviders.filter(option => (
  option.id === 'openrouter'
  || (option.id === 'gemini' && providerFieldSupported.value)
  || supportedProviderIds.value.has(option.id)
)).map(option => ({
  ...option,
  label: catalogProviders.value[option.id]?.label || option.label,
})))
const additionalProviders = computed(() => videoProviders.value.filter(option => (
  option.id !== 'openrouter' && option.id !== 'gemini'
)))
const providerDefaults: Record<ConfiguredVideoProvider, { baseUrl: string; apiKeyEnv: string }> = {
  openrouter: { baseUrl: 'https://openrouter.ai/api/v1', apiKeyEnv: 'OPENROUTER_API_KEY' },
  gemini: { baseUrl: 'https://generativelanguage.googleapis.com/v1beta', apiKeyEnv: 'GEMINI_API_KEY' },
  xai: { baseUrl: 'https://api.x.ai/v1', apiKeyEnv: 'XAI_API_KEY' },
  qwen: { baseUrl: 'https://dashscope.aliyuncs.com/api/v1', apiKeyEnv: 'DASHSCOPE_API_KEY' },
  qwen_token_plan: {
    baseUrl: 'https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1',
    apiKeyEnv: 'QWEN_TOKEN_PLAN_API_KEY',
  },
  tokenrhythm: { baseUrl: 'https://tokenrhythm.studio/v1', apiKeyEnv: 'TOKENRHYTHM_API_KEY' },
}
function providerDefaultConnection(id: ConfiguredVideoProvider): VideoProviderConnection {
  return {
    baseUrl: catalogProviders.value[id]?.baseUrl || providerDefaults[id].baseUrl,
    apiKeyEnv: catalogProviders.value[id]?.apiKeyEnv || providerDefaults[id].apiKeyEnv,
  }
}
const credentialHintKeys: Record<ConfiguredVideoProvider, string> = {
  openrouter: 'setup.video.openrouterCredentialHint',
  gemini: 'setup.video.geminiCredentialHint',
  xai: 'setup.video.xaiCredentialHint',
  qwen: 'setup.video.qwenCredentialHint',
  qwen_token_plan: 'setup.video.qwenTokenPlanCredentialHint',
  tokenrhythm: 'setup.video.tokenrhythmCredentialHint',
}

const modelSuggestions = computed<DiscoveredModel[]>(() => {
  if (!provider.value) return []
  const live = discoveredModels.value[provider.value]
  if (live?.models.length) return live.models
  const suggested = catalogProviders.value[provider.value]?.models
    || fallbackSuggestedModels[provider.value]
  return suggested.map(id => ({
    id,
    name: id,
    contextWindow: null,
    maxOutputTokens: null,
    capabilities: [],
    pricing: null,
    capabilitySource: '',
  }))
})

const modelPlaceholder = computed(() => (
  provider.value ? modelSuggestions.value[0]?.id || t('setup.video.modelPlaceholder') : ''
))
function providerDefaultModel(id: ConfiguredVideoProvider): string {
  return catalogProviders.value[id]?.defaultModel
    || catalogProviders.value[id]?.models[0]
    || fallbackSuggestedModels[id][0]
    || ''
}
const modelSource = computed(() => (
  provider.value ? discoveredModels.value[provider.value]?.source || 'catalog' : 'none'
))
const durationBounds = computed(() => {
  if (provider.value === 'gemini') return { minimum: 4, maximum: 8 }
  if (provider.value === 'tokenrhythm') return { minimum: 2, maximum: 30 }
  if (provider.value === 'qwen' || provider.value === 'qwen_token_plan') {
    return { minimum: primary.value.trim().startsWith('happyhorse-') ? 3 : 2, maximum: 15 }
  }
  if (provider.value === 'xai') return { minimum: 1, maximum: 15 }
  return { minimum: 1, maximum: 60 }
})
const tokenRhythmModelStatus = computed(() => {
  if (provider.value !== 'tokenrhythm' || primary.value.trim() !== 'wan3.0-video') return ''
  const discovered = discoveredModels.value.tokenrhythm
  if (discovered?.verifiedIds.includes('wan3.0-video')) return t('setup.video.tokenrhythmModelListed')
  if (discovered?.source === 'live' && !discovered.models.some(item => item.id === 'wan3.0-video')) {
    return t('setup.video.tokenrhythmModelNotListed')
  }
  return t('setup.video.tokenrhythmModelUnverified')
})
const savedDirectKeyAvailable = computed(() => (
  isVideoProvider(provider.value)
  && !clearDirectKeyRequested.value
  && storedDirectKeys.value.has(provider.value)
  && baseUrl.value.trim() === savedProviderDrafts.get(provider.value)?.baseUrl
  && apiKeyEnv.value.trim() === savedProviderDrafts.get(provider.value)?.apiKeyEnv
  && credentialOptions.value[provider.value]?.source === 'video_direct'
  && credentialOptions.value[provider.value]?.available === true
))
const canClearDirectKey = computed(() => (
  isVideoProvider(provider.value)
  && storedDirectKeys.value.has(provider.value)
  && credentialOptions.value[provider.value]?.source === 'video_direct'
  && credentialOptions.value[provider.value]?.clearable === true
))
const environmentManagedDirectKey = computed(() => (
  isVideoProvider(provider.value)
  && credentialOptions.value[provider.value]?.source === 'video_env_injected_direct'
  && credentialOptions.value[provider.value]?.clearable === false
  && baseUrl.value.trim() === savedProviderDrafts.get(provider.value)?.baseUrl
  && apiKeyEnv.value.trim() === savedProviderDrafts.get(provider.value)?.apiKeyEnv
))
const credentialStatus = computed(() => {
  if (!provider.value) return t('setup.video.credentialHint')
  if (directApiKey.value.trim()) return t('setup.video.directKeyPending')
  if (clearDirectKeyRequested.value) return t('setup.video.directKeyClearPending')
  if (environmentManagedDirectKey.value) {
    return t('setup.video.directKeyManagedByEnvironment', {
      name: credentialOptions.value[provider.value]?.envKey || '',
    })
  }
  if (savedDirectKeyAvailable.value) return t('setup.video.directKeyConfigured')
  const savedConnection = savedProviderDrafts.get(provider.value)
  const sameConnection = baseUrl.value.trim() === savedConnection?.baseUrl
    && apiKeyEnv.value.trim() === savedConnection?.apiKeyEnv
  if (sameConnection) {
    const option = credentialOptions.value[provider.value]
    if (option?.available) {
      if (option.source === 'image_direct') return t('setup.video.imageDirectKeyReuse')
      if (option.source === 'image_env') {
        return t('setup.video.imageEnvKeyReuse', { name: option.envKey })
      }
      if (option.source === 'llm_fallback') return t('setup.video.modelServiceKeyReuse')
      if (option.source === 'video_env') {
        return t('setup.video.videoEnvKeyAvailable', { name: option.envKey })
      }
    }
    if (option?.source === 'missing_env') {
      return t('setup.video.videoEnvKeyMissing', { name: option.envKey })
    }
  }
  if (storedDirectKeys.value.has(provider.value)) {
    return sameConnection && !credentialOptions.value[provider.value]
      ? t('setup.video.directKeyStoredUnverified')
      : t('setup.video.directKeyStoredUnavailable')
  }
  if (isCustomProviderOrigin(provider.value, baseUrl.value.trim())) {
    const selectedEnv = apiKeyEnv.value.trim()
    return selectedEnv && selectedEnv !== providerDefaultConnection(provider.value).apiKeyEnv
      ? t('setup.video.customCredentialHint', { name: selectedEnv })
      : t('setup.video.customCredentialMissingHint')
  }
  return credentialHint.value
})

const credentialHint = computed(() => {
  if (!isVideoProvider(provider.value)) return t('setup.video.credentialHint')
  const selectedEnv = apiKeyEnv.value.trim()
  const configured = providerDefaultConnection(provider.value)
  if (isCustomProviderOrigin(provider.value, baseUrl.value.trim())) {
    return selectedEnv && selectedEnv !== configured.apiKeyEnv
      ? t('setup.video.customCredentialHint', { name: selectedEnv })
      : t('setup.video.customCredentialMissingHint')
  }
  if (selectedEnv && (selectedEnv !== configured.apiKeyEnv
    || configured.apiKeyEnv !== providerDefaults[provider.value].apiKeyEnv)) {
    return t('setup.video.selectedEnvCredentialHint', { name: selectedEnv })
  }
  return t(credentialHintKeys[provider.value])
})

function isVideoProvider(value: unknown): value is ConfiguredVideoProvider {
  return fallbackVideoProviders.some(option => option.id === value)
}

function providerOrigin(endpoint: string): string {
  const url = new URL(endpoint)
  const hostname = url.hostname.toLowerCase().replace(/\.+$/, '')
  const port = url.port || (url.protocol === 'https:' ? '443' : '80')
  return `${url.protocol}//${hostname}:${port}`
}

function isCustomProviderOrigin(id: ConfiguredVideoProvider, endpoint: string): boolean {
  if (!endpoint) return false
  try {
    return providerOrigin(endpoint) !== providerOrigin(providerDefaultConnection(id).baseUrl)
  } catch {
    return false
  }
}

function providerDraft(): VideoProviderDraft {
  return {
    primary: primary.value.trim(),
    baseUrl: baseUrl.value.trim(),
    apiKeyEnv: apiKeyEnv.value.trim(),
  }
}

function savedCredentialAvailable(
  id: ConfiguredVideoProvider,
  connection: VideoProviderConnection,
): boolean {
  const savedConnection = savedProviderDrafts.get(id)
  // Gateway credential status describes the saved connection, not draft edits.
  return credentialOptions.value[id]?.available === true
    && connection.baseUrl.trim() === savedConnection?.baseUrl
    && connection.apiKeyEnv.trim() === savedConnection?.apiKeyEnv
    && !(provider.value === id && clearDirectKeyRequested.value)
}

function providerSettingsDraft(): Partial<Record<ConfiguredVideoProvider, VideoProviderConnection>> {
  const result: Partial<Record<ConfiguredVideoProvider, VideoProviderConnection>> = {}
  if (!providerSettingsSupported.value) return result
  for (const option of videoProviders.value) {
    const values = provider.value === option.id
      ? providerDraft()
      : providerDrafts.get(option.id)
    result[option.id] = {
      baseUrl: values?.baseUrl.trim() || '',
      apiKeyEnv: values?.apiKeyEnv.trim() || '',
    }
  }
  return result
}

function rememberProviderDraft(): void {
  if (isVideoProvider(provider.value)) providerDrafts.set(provider.value, providerDraft())
}

function applyProviderDraft(id: VideoProvider): void {
  const values = isVideoProvider(id) ? providerDrafts.get(id) : undefined
  primary.value = values?.primary || ''
  baseUrl.value = values?.baseUrl || ''
  apiKeyEnv.value = values?.apiKeyEnv || ''
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function allowedAspects(value: unknown): VideoAspect[] {
  if (!Array.isArray(value)) return ['16:9', '9:16']
  return value.filter((item): item is VideoAspect => item === '16:9' || item === '9:16')
}

function allowedSizes(value: unknown): VideoResolution[] {
  if (!Array.isArray(value)) return ['720p', '1080p']
  return value.filter((item): item is VideoResolution => item === '720p' || item === '1080p')
}

async function loadProviderCatalog(): Promise<void> {
  if (!setupWorkflow) return
  try {
    const catalog = await setupWorkflow.catalog()
    const entries = Array.isArray(catalog.videoGenerationProviders)
      ? catalog.videoGenerationProviders : []
    const next: typeof catalogProviders.value = {}
    for (const raw of entries) {
      const entry = record(raw)
      const id = entry.providerId
      if (!isVideoProvider(id) || entry.runtimeSupported === false) continue
      const suggestions = Array.isArray(entry.suggestedModels)
        ? entry.suggestedModels.filter((item): item is string => typeof item === 'string')
        : []
      const defaultModel = typeof entry.defaultModel === 'string' ? entry.defaultModel : ''
      next[id] = {
        label: typeof entry.label === 'string' && entry.label.trim()
          ? entry.label : fallbackVideoProviders.find(item => item.id === id)!.label,
        baseUrl: typeof entry.defaultBaseUrl === 'string' ? entry.defaultBaseUrl.trim() : '',
        apiKeyEnv: typeof entry.envKey === 'string' ? entry.envKey.trim() : '',
        defaultModel,
        models: suggestions.length ? suggestions : defaultModel ? [defaultModel] : fallbackSuggestedModels[id],
      }
    }
    catalogProviders.value = next
  } catch {
    // Older gateways can omit the video catalog; retain the local fallback.
  }
}

async function discoverVideoModels(id: ConfiguredVideoProvider): Promise<void> {
  if (!setupWorkflow?.discoverVideoGenerationModels) return
  try {
    const result = await setupWorkflow.discoverVideoGenerationModels(id)
    if (result.ok !== true || !Array.isArray(result.models)) return
    const verifiedIds: string[] = []
    const models = result.models.flatMap(item => {
      const row = record(item)
      const modelId = typeof row.id === 'string' ? row.id.trim() : ''
      if (!modelId) return []
      if (row.verified === true) verifiedIds.push(modelId)
      return [{
        id: modelId,
        name: typeof row.name === 'string' && row.name.trim() ? row.name : modelId,
        contextWindow: null,
        maxOutputTokens: null,
        capabilities: [],
        pricing: null,
        capabilitySource: '',
      } satisfies DiscoveredModel]
    })
    discoveredModels.value = {
      ...discoveredModels.value,
      [id]: { models, source: result.source === 'live' ? 'live' : 'catalog', verifiedIds },
    }
  } catch {
    // A discovery failure leaves the documented examples available for entry.
  }
}

let credentialStatusRequestId = 0
async function loadCredentialStatus(): Promise<void> {
  if (!setupWorkflow || phase.value !== 'ready') return
  const requestId = ++credentialStatusRequestId
  credentialOptions.value = {}
  try {
    const status = await setupWorkflow.status()
    if (requestId !== credentialStatusRequestId) return
    const state = record(status.videoGenerationState)
    const rows = Array.isArray(state.credentialOptions) ? state.credentialOptions : []
    const next: typeof credentialOptions.value = {}
    for (const raw of rows) {
      const option = record(raw)
      const id = option.providerId
      if (!isVideoProvider(id)) continue
      next[id] = {
        available: option.available === true,
        source: typeof option.source === 'string' ? option.source : 'none',
        envKey: typeof option.envKey === 'string' ? option.envKey : '',
        clearable: option.clearable === true,
      }
      if (option.available === true && option.source === 'video_direct') {
        storedDirectKeys.value.add(id)
      }
    }
    credentialOptions.value = next
  } catch {
    // Credentials remain unverified when status is unavailable.
  }
}

function draft(): VideoDraft {
  return {
    enabled: enabled.value,
    // Older gateways route video through OpenRouter without a provider field.
    // Selecting that only option is not a persisted change until a model is set.
    provider: providerFieldSupported.value
      ? provider.value
      : primary.value.trim() ? 'openrouter' : '',
    primary: primary.value.trim(),
    providerSettings: providerSettingsDraft(),
    durationSeconds: String(durationSeconds.value).trim(),
    maxDurationSeconds: String(maxDurationSeconds.value).trim(),
    aspectRatio: aspectRatio.value,
    resolution: resolution.value,
  }
}

const dirty = computed(() => saved.value !== null && (
  JSON.stringify(draft()) !== JSON.stringify(saved.value)
  || Boolean(directApiKey.value.trim())
  || clearDirectKeyRequested.value
))
watch(dirty, value => emit('dirtyChange', value), { immediate: true })
watch(busy, value => emit('busyChange', value), { immediate: true })

function providerConnectionError(
  id: ConfiguredVideoProvider,
  connection: VideoProviderConnection,
): string {
  const endpoint = connection.baseUrl.trim()
  if (!endpoint) return t('setup.video.invalidBaseUrl')
  if (/[?#%\\]/.test(endpoint)
    || endpoint.split('/').some(segment => segment === '.' || segment === '..')) {
    return t('setup.video.invalidBaseUrl')
  }
  try {
    const url = new URL(endpoint)
    const hostname = url.hostname.toLowerCase()
    const loopback = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]'
    if (
      (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
      || !hostname || url.username || url.password || url.search || url.hash
    ) return t('setup.video.invalidBaseUrl')
  } catch {
    return t('setup.video.invalidBaseUrl')
  }
  const origin = providerOrigin(endpoint)
  if ((Object.keys(providerDefaults) as ConfiguredVideoProvider[]).some(other => (
    other !== id && providerOrigin(providerDefaultConnection(other).baseUrl) === origin
  ))) return t('setup.video.invalidBaseUrl')
  const selectedEnv = connection.apiKeyEnv.trim()
  if (selectedEnv && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(selectedEnv)) {
    return t('setup.video.invalidApiKeyEnv')
  }
  const savedConnection = savedProviderDrafts.get(id)
  const directKeyEntered = provider.value === id && Boolean(directApiKey.value.trim())
  const clearRequested = provider.value === id && clearDirectKeyRequested.value
  const savedDirectKey = storedDirectKeys.value.has(id)
  const replacementEnv = Boolean(savedConnection && selectedEnv
    && selectedEnv !== savedConnection.apiKeyEnv
    && selectedEnv !== providerDefaultConnection(id).apiKeyEnv)
  if (savedDirectKey && !directKeyEntered && !clearRequested && savedConnection
    && endpoint !== savedConnection.baseUrl && !replacementEnv) {
    return t('setup.video.existingDirectKeyEndpointChanged')
  }
  const keepsSavedDirectKey = savedDirectKey && !clearRequested && savedConnection
    && endpoint === savedConnection.baseUrl
    && selectedEnv === savedConnection.apiKeyEnv
  if (isCustomProviderOrigin(id, endpoint) && !directKeyEntered && !keepsSavedDirectKey
    && !savedCredentialAvailable(id, connection) && (
    !selectedEnv || selectedEnv === providerDefaultConnection(id).apiKeyEnv
  )) return t('setup.video.customEndpointNeedsEnv')
  return ''
}

const validationError = computed(() => {
  if (enabled.value) {
    if (!provider.value) return t('setup.video.invalidProvider')
    const model = primary.value.trim()
    const parts = model.split('/')
    const allowedCharacters = provider.value === 'openrouter'
      ? /^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$/
      : /^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/
    if (
      !model
      || !allowedCharacters.test(model)
      || parts.some(part => !part || part === '.' || part === '..')
      || (provider.value === 'openrouter' && parts.length < 2)
    ) {
      return provider.value !== 'openrouter' && model.includes('/')
        ? t('setup.video.invalidNativeModel')
        : t('setup.video.invalidModel')
    }
    if (provider.value === 'gemini' && !fallbackSuggestedModels.gemini.includes(model)) {
      return t('setup.video.invalidGeminiModel')
    }
  }
  const connections = providerSettingsDraft()
  for (const option of videoProviders.value) {
    const connection = connections[option.id]
    if (!connection) continue
    const issue = providerConnectionError(option.id, connection)
    if (issue) return provider.value === option.id
      ? issue
      : t('setup.video.providerSettingsInvalid', { provider: option.label, detail: issue })
  }
  const maximum = Number(maxDurationSeconds.value)
  if (!Number.isInteger(maximum) || maximum < 1 || maximum > 60) {
    return t('setup.video.invalidMaxDuration')
  }
  const happyHorse = (provider.value === 'qwen' || provider.value === 'qwen_token_plan')
    && primary.value.trim().startsWith('happyhorse-')
  if (enabled.value && happyHorse && maximum < 3) return t('setup.video.invalidHappyHorseMaxDuration')
  if (provider.value === 'gemini' && maximum < 4) {
    return t('setup.video.invalidGeminiMaxDuration')
  }
  if (enabled.value && maximum < durationBounds.value.minimum) {
    return t('setup.video.invalidProviderMaxDuration', durationBounds.value)
  }
  if (provider.value === 'gemini' && resolution.value === '1080p' && maximum < 8) {
    return t('setup.video.invalidGemini1080p')
  }
  if (enabled.value && provider.value === 'xai' && primary.value.trim() === 'grok-imagine-video'
    && resolution.value === '1080p') {
    return t('setup.video.invalidXai1080p')
  }
  const requested = String(durationSeconds.value).trim()
  const duration = requested ? Number(requested) : null
  if (requested) {
    if (duration === null || !Number.isInteger(duration) || duration < 1 || duration > maximum) {
      return t('setup.video.invalidDuration', { max: maximum })
    }
  }
  if (enabled.value && happyHorse && duration !== null && duration < 3) {
    return t('setup.video.invalidHappyHorseDuration')
  }
  if (enabled.value && duration !== null && (
    duration < durationBounds.value.minimum || duration > durationBounds.value.maximum
  )) return t('setup.video.invalidProviderDuration', durationBounds.value)
  if (provider.value === 'gemini' && resolution.value === '1080p' && duration !== null && duration !== 8) {
    return t('setup.video.invalidGemini1080p')
  }
  if (provider.value === 'gemini' && duration !== null && ![4, 6, 8].includes(duration)) {
    return t('setup.video.invalidGeminiDuration')
  }
  return ''
})

async function load(): Promise<void> {
  if (!settings) {
    phase.value = 'unsupported'
    return
  }
  phase.value = 'loading'
  error.value = ''
  try {
    const all = await settings.readAll()
    if (!Object.prototype.hasOwnProperty.call(all, 'video_generation')) {
      phase.value = 'unsupported'
      return
    }
    const video = record(all.video_generation)
    providerFieldSupported.value = Object.prototype.hasOwnProperty.call(video, 'provider')
    providerSettingsSupported.value = Object.prototype.hasOwnProperty.call(video, 'providers')
    providerDrafts.clear()
    savedProviderDrafts.clear()
    storedDirectKeys.value.clear()
    directApiKey.value = ''
    clearDirectKeyRequested.value = false
    const providerSettings = record(video.providers)
    supportedProviderIds.value = new Set(
      Object.keys(providerSettings).filter(isVideoProvider),
    )
    for (const option of videoProviders.value) {
      const settings = record(providerSettings[option.id])
      if (settings.api_key_configured === true || Boolean(settings.api_key)) {
        storedDirectKeys.value.add(option.id)
      }
      const values: VideoProviderDraft = {
        primary: '',
        baseUrl: typeof settings.base_url === 'string' ? settings.base_url : '',
        apiKeyEnv: typeof settings.api_key_env === 'string' ? settings.api_key_env : '',
      }
      providerDrafts.set(option.id, { ...values })
      savedProviderDrafts.set(option.id, { ...values })
    }
    allowedAspectRatios.value = allowedAspects(video.allowed_aspect_ratios)
    allowedResolutions.value = allowedSizes(video.allowed_resolutions)
    enabled.value = video.enabled === true
    const savedPrimary = typeof video.primary === 'string' ? video.primary : ''
    provider.value = isVideoProvider(video.provider)
      ? video.provider
      : savedPrimary ? 'openrouter' : ''
    if (!providerSettingsSupported.value && provider.value !== 'openrouter' && provider.value !== 'gemini') {
      provider.value = ''
    }
    if (isVideoProvider(provider.value)) {
      const selected = providerDrafts.get(provider.value)!
      selected.primary = savedPrimary
      savedProviderDrafts.get(provider.value)!.primary = savedPrimary
    }
    applyProviderDraft(provider.value)
    if (isVideoProvider(provider.value)) void discoverVideoModels(provider.value)
    durationSeconds.value = typeof video.duration_seconds === 'number'
      ? String(video.duration_seconds)
      : ''
    maxDurationSeconds.value = typeof video.max_duration_seconds === 'number'
      ? String(video.max_duration_seconds)
      : '8'
    aspectRatio.value = video.aspect_ratio === '9:16' ? '9:16' : '16:9'
    resolution.value = video.resolution === '1080p' ? '1080p' : '720p'
    saved.value = draft()
    phase.value = 'ready'
  } catch {
    phase.value = 'error'
    error.value = t('setup.video.loadFailed')
  }
}

async function save(): Promise<boolean> {
  if (!dirty.value) return true
  if (!settings || phase.value !== 'ready' || busy.value || validationError.value) return false
  const next = draft()
  const previous = saved.value
  if (!previous) return false
  const values: Array<[keyof VideoDraft, string, string | number | boolean | null]> = [
    ['enabled', 'enabled', next.enabled],
    ...(providerFieldSupported.value
      ? [['provider', 'provider', next.provider] as [keyof VideoDraft, string, string]]
      : []),
    ['primary', 'primary', next.primary],
    ['durationSeconds', 'duration_seconds', next.durationSeconds ? Number(next.durationSeconds) : null],
    ['maxDurationSeconds', 'max_duration_seconds', Number(next.maxDurationSeconds)],
    ['aspectRatio', 'aspect_ratio', next.aspectRatio],
    ['resolution', 'resolution', next.resolution],
  ]
  const changes: SettingChange[] = values
    .filter(([name]) => next[name] !== previous[name])
    .map(([, field, value]) => ({ path: `video_generation.${field}`, value }))
  for (const option of videoProviders.value) {
    const connection = next.providerSettings[option.id]
    if (!connection) continue
    const prior = savedProviderDrafts.get(option.id)
    if (connection.baseUrl !== (prior?.baseUrl || '')) {
      changes.push({ path: `video_generation.providers.${option.id}.base_url`, value: connection.baseUrl })
    }
    if (connection.apiKeyEnv !== (prior?.apiKeyEnv || '')) {
      changes.push({ path: `video_generation.providers.${option.id}.api_key_env`, value: connection.apiKeyEnv })
    }
  }
  const enteredKey = directApiKey.value.trim()
  if (isVideoProvider(provider.value) && enteredKey) {
    changes.push({ path: `video_generation.providers.${provider.value}.api_key`, value: enteredKey })
  } else if (isVideoProvider(provider.value) && clearDirectKeyRequested.value) {
    changes.push({ path: `video_generation.providers.${provider.value}.api_key`, value: '' })
  }
  if (!changes.length) return true

  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const result = await settings.patch(changes)
    for (const option of videoProviders.value) {
      const connection = next.providerSettings[option.id]
      const prior = savedProviderDrafts.get(option.id)
      if (!connection || !prior) continue
      if (connection.baseUrl !== prior.baseUrl || connection.apiKeyEnv !== prior.apiKeyEnv) {
        credentialOptions.value = { ...credentialOptions.value, [option.id]: undefined }
        if (connection.apiKeyEnv !== prior.apiKeyEnv && !(provider.value === option.id && enteredKey)) {
          storedDirectKeys.value.delete(option.id)
        }
      }
    }
    if (isVideoProvider(provider.value) && enteredKey) storedDirectKeys.value.add(provider.value)
    if (isVideoProvider(provider.value) && clearDirectKeyRequested.value) {
      storedDirectKeys.value.delete(provider.value)
      credentialOptions.value = { ...credentialOptions.value, [provider.value]: undefined }
    }
    directApiKey.value = ''
    clearDirectKeyRequested.value = false
    rememberProviderDraft()
    for (const [id, values] of providerDrafts) savedProviderDrafts.set(id, { ...values })
    saved.value = next
    message.value = t(result.restartRequired
      ? 'setup.video.savedRestart'
      : 'setup.video.saved')
    await loadCredentialStatus()
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t('setup.video.saveFailed')
    return false
  } finally {
    busy.value = false
  }
}

function discard(): void {
  if (!saved.value || busy.value) return
  directApiKey.value = ''
  clearDirectKeyRequested.value = false
  enabled.value = saved.value.enabled
  providerDrafts.clear()
  for (const [id, values] of savedProviderDrafts) providerDrafts.set(id, { ...values })
  provider.value = saved.value.provider
  if (isVideoProvider(provider.value)) providerDrafts.get(provider.value)!.primary = saved.value.primary
  applyProviderDraft(provider.value)
  durationSeconds.value = saved.value.durationSeconds
  maxDurationSeconds.value = saved.value.maxDurationSeconds
  aspectRatio.value = saved.value.aspectRatio
  resolution.value = saved.value.resolution
  error.value = ''
  message.value = ''
}

function onProviderChange(event: Event): void {
  const selected = (event.target as HTMLSelectElement).value
  if (selected !== '' && !isVideoProvider(selected)) return
  if (selected !== 'openrouter' && !providerFieldSupported.value) return
  if (selected !== '' && selected !== 'openrouter' && selected !== 'gemini'
    && !providerSettingsSupported.value) return
  if (provider.value === selected) return
  directApiKey.value = ''
  clearDirectKeyRequested.value = false
  rememberProviderDraft()
  provider.value = selected
  applyProviderDraft(provider.value)
  if (providerFieldSupported.value && isVideoProvider(selected) && !primary.value.trim()) {
    primary.value = providerDefaultModel(selected)
  }
  if (isVideoProvider(selected)) void discoverVideoModels(selected)
}

function toggleDirectKeyClear(): void {
  directApiKey.value = ''
  clearDirectKeyRequested.value = !clearDirectKeyRequested.value
}

defineExpose({ save, discard, refreshCredentialStatus: loadCredentialStatus })

onMounted(() => {
  void loadProviderCatalog().then(() => load()).then(() => loadCredentialStatus())
})
</script>

<template>
  <details class="video-settings" data-testid="video-generation-settings">
    <summary class="video-settings__summary">
      <span class="video-settings__title">{{ t('setup.video.title') }}</span>
      <span class="video-settings__summary-end">
        <span v-if="phase === 'ready' && dirty" class="control-pill is-warn">{{ t('setup.video.unsaved') }}</span>
        <span v-else-if="phase === 'ready'" class="control-pill" :class="enabled ? 'is-ok' : 'is-muted'">
          {{ t(enabled ? 'setup.video.enabled' : 'setup.video.disabled') }}
        </span>
        <svg class="video-settings__chevron" aria-hidden="true" viewBox="0 0 20 20">
          <path d="m6 8 4 4 4-4" />
        </svg>
      </span>
    </summary>

    <div class="video-settings__panel">
      <p class="video-settings__description">{{ t('setup.video.description') }}</p>
      <p v-if="phase === 'unsupported'" role="status">{{ t('setup.video.unsupported') }}</p>
      <p v-else-if="phase === 'loading'" role="status">{{ t('shared.loading') }}</p>
      <div v-else-if="phase === 'error'">
        <p role="alert">{{ error }}</p>
        <button type="button" class="btn btn--ghost" @click="load">{{ t('setup.video.retry') }}</button>
      </div>
      <template v-else>
        <div class="control-row">
          <div class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.video.enableLabel') }}</span>
          </div>
          <div class="control-row__control">
            <ControlSwitch
              name="setup_video_enabled"
              :checked="enabled"
              :disabled="busy"
              :aria-label="t('setup.video.enableLabel')"
              @change="enabled = $event"
            />
          </div>
        </div>

        <label class="control-row">
          <span class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.video.providerLabel') }}</span>
          </span>
          <span class="control-row__control">
            <select class="control-input" name="setup_video_provider" :value="provider" :disabled="busy"
              @change="onProviderChange">
              <option value="">{{ t('setup.video.providerPlaceholder') }}</option>
              <option value="openrouter">OpenRouter</option>
              <option v-if="providerFieldSupported" value="gemini">Google Gemini</option>
              <template v-if="providerFieldSupported && providerSettingsSupported">
                <option v-for="option in additionalProviders"
                  :key="option.id" :value="option.id">{{ option.label }}</option>
              </template>
            </select>
          </span>
        </label>
        <p v-if="!providerFieldSupported" class="video-settings__hint">
          {{ t('setup.video.legacyProviderHint') }}
        </p>
        <p v-else-if="!providerSettingsSupported" class="video-settings__hint">
          {{ t('setup.video.legacySettingsHint') }}
        </p>

        <label v-if="provider === 'gemini'" class="control-row">
          <span class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.video.modelLabel') }}</span>
            <span class="control-row__desc">{{ t('setup.video.geminiModelHint') }}</span>
          </span>
          <span class="control-row__control">
            <select v-model="primary" class="control-input" name="setup_video_gemini_model" :disabled="busy">
              <option value="">{{ t('setup.video.modelPlaceholder') }}</option>
              <option v-for="model in fallbackSuggestedModels.gemini" :key="model" :value="model">{{ model }}</option>
            </select>
          </span>
        </label>
        <SetupModelCombobox
          v-else-if="provider"
          :field="{
            name: 'video_primary',
            label: t('setup.video.modelLabel'),
            description: t('setup.video.modelHint'),
            placeholder: modelPlaceholder,
          }"
          :value="primary"
          :models="modelSuggestions"
          :model-source="modelSource"
          :disabled="busy"
          @update="primary = $event"
        />
        <p v-if="tokenRhythmModelStatus" class="video-settings__hint" data-testid="video-model-status">
          {{ tokenRhythmModelStatus }}
        </p>

        <template v-if="provider && providerSettingsSupported">
          <label class="control-row">
            <span class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.video.baseUrlLabel') }}</span>
              <span class="control-row__desc">{{ t('setup.video.baseUrlHint') }}</span>
            </span>
            <span class="control-row__control">
              <input v-model="baseUrl" class="control-input" name="setup_video_base_url"
                type="url" inputmode="url" spellcheck="false" autocomplete="off"
                :disabled="busy" placeholder="https://api.example.com/v1">
            </span>
          </label>
          <label class="control-row">
            <span class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.video.apiKeyEnvLabel') }}</span>
              <span class="control-row__desc">{{ t('setup.video.apiKeyEnvHint') }}</span>
            </span>
            <span class="control-row__control">
              <input v-model="apiKeyEnv" class="control-input" name="setup_video_api_key_env"
                type="text" spellcheck="false" autocomplete="off" :disabled="busy">
            </span>
          </label>
          <div class="control-row">
            <span class="control-row__label-block">
              <span class="control-row__label">{{ t('setup.video.credentialSourceLabel') }}</span>
              <span class="control-row__desc">{{ t('setup.video.directApiKeyHint') }}</span>
            </span>
            <span class="control-row__control video-settings__credential-control">
              <span class="video-settings__credential-status" role="status">{{ credentialStatus }}</span>
              <input v-model="directApiKey" class="control-input" name="setup_video_api_key"
                type="password" autocomplete="off" :disabled="busy"
                data-1p-ignore data-bwignore data-form-type="other" data-lpignore="true"
                data-protonpass-ignore="true"
                :aria-label="t('setup.video.directApiKeyLabel')"
                :placeholder="savedDirectKeyAvailable
                  ? t('setup.video.directApiKeyKeepPlaceholder')
                  : t('setup.video.directApiKeyNewPlaceholder')"
                @input="clearDirectKeyRequested = false">
              <button v-if="canClearDirectKey" type="button" class="btn btn--ghost"
                :disabled="busy" @click="toggleDirectKeyClear">
                {{ t(clearDirectKeyRequested
                  ? 'setup.video.cancelDirectKeyClear'
                  : 'setup.video.clearDirectKey') }}
              </button>
            </span>
          </div>
        </template>

        <label class="control-row">
          <span class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.video.defaultDurationLabel') }}</span>
            <span class="control-row__desc">{{ t('setup.video.defaultDurationHint') }}</span>
          </span>
          <span class="control-row__control">
            <input v-model="durationSeconds" class="control-input" name="setup_video_duration"
              type="number" :min="enabled ? durationBounds.minimum : 1"
              :max="enabled ? Math.min(Number(maxDurationSeconds), durationBounds.maximum) : maxDurationSeconds"
              step="1" inputmode="numeric"
              :disabled="busy" :placeholder="t('setup.video.providerDefault')">
          </span>
        </label>

        <label class="control-row">
          <span class="control-row__label-block">
            <span class="control-row__label">{{ t('setup.video.maxDurationLabel') }}</span>
            <span class="control-row__desc">{{ t('setup.video.providerMaxDurationHint', durationBounds) }}</span>
          </span>
          <span class="control-row__control">
            <input v-model="maxDurationSeconds" class="control-input" name="setup_video_max_duration"
              type="number" :min="enabled ? durationBounds.minimum : 1" max="60"
              step="1" inputmode="numeric" :disabled="busy">
          </span>
        </label>

        <label class="control-row">
          <span class="control-row__label-block"><span class="control-row__label">{{ t('setup.video.aspectLabel') }}</span></span>
          <span class="control-row__control">
            <select v-model="aspectRatio" class="control-input" name="setup_video_aspect" :disabled="busy">
              <option v-for="value in allowedAspectRatios" :key="value" :value="value">{{ value }}</option>
            </select>
          </span>
        </label>

        <label class="control-row">
          <span class="control-row__label-block"><span class="control-row__label">{{ t('setup.video.resolutionLabel') }}</span></span>
          <span class="control-row__control">
            <select v-model="resolution" class="control-input" name="setup_video_resolution" :disabled="busy">
              <option v-for="value in allowedResolutions" :key="value" :value="value">{{ value }}</option>
            </select>
          </span>
        </label>

        <p class="video-settings__hint">
          {{ credentialStatus }} {{ t('setup.video.credentialVerificationHint') }}
        </p>
        <p v-if="validationError" class="video-settings__error" role="alert">{{ validationError }}</p>
        <p v-else-if="error" class="video-settings__error" role="alert">{{ error }}</p>
        <p v-if="message" class="video-settings__message" role="status">{{ message }}</p>
        <div class="video-settings__actions">
          <button type="button" class="btn btn--primary" :disabled="busy || !dirty || Boolean(validationError)"
            :aria-busy="busy ? 'true' : undefined" @click="save">
            {{ t('setup.video.save') }}
          </button>
        </div>
      </template>
    </div>
  </details>
</template>

<style scoped>
.video-settings {
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  overflow: hidden;
  margin-top: 12px;
}

.video-settings__summary {
  align-items: center;
  cursor: pointer;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  list-style: none;
  padding: 17px 18px;
}

.video-settings__summary::-webkit-details-marker { display: none; }
.video-settings__summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
.video-settings__title { font-size: 15px; font-weight: 650; }
.video-settings__summary-end { display: flex; align-items: center; gap: var(--sp-3); }
.video-settings__chevron {
  width: 18px;
  height: 18px;
  flex: 0 0 18px;
  color: var(--text-muted);
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.75;
  transition: transform var(--dur-fast) var(--ease-standard);
}
.video-settings[open] .video-settings__chevron { transform: rotate(180deg); }
.video-settings__panel { border-top: 1px solid var(--border); padding: 0 18px 16px; }
.video-settings__description, .video-settings__hint { color: var(--text-muted); font-size: 13px; line-height: 1.5; }
.video-settings__error { color: var(--danger); font-size: 13px; }
.video-settings__message { color: var(--ok); font-size: 13px; }
.video-settings__credential-control { display: grid; gap: 8px; }
.video-settings__credential-status { color: var(--text-muted); font-size: 13px; line-height: 1.4; }
.video-settings__actions { display: flex; justify-content: flex-end; margin-top: var(--sp-3); }
@media (prefers-reduced-motion: reduce) {
  .video-settings__chevron { transition: none; }
}
</style>
