import { computed, ref, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'

interface ProviderSpec {
  providerId: string
  envKey?: string
  requiresApiKey?: boolean
  defaultBaseUrl?: string
  defaultModel?: string
  [key: string]: unknown
}

interface ConfigData {
  search_provider?: string
  search_api_key_env?: string
  search_max_results?: number
  search_proxy?: string
  search_use_env_proxy?: boolean
  search_fallback_policy?: string
  search_diagnostics?: boolean
  memory?: {
    embedding?: {
      provider?: string
      mode?: string
      remote?: {
        model?: string
        api_key_env?: string
        base_url?: string
      }
      local?: { onnx_dir?: string }
    }
  }
  image_generation?: {
    size?: string
    output_format?: string
    fallbacks?: string[]
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
  }
}

interface StatusData {
  imageGenerationEnabled?: boolean
  imageGenerationConfigured?: boolean
  imageGenerationProvider?: string
  imageGenerationPrimary?: string
  imageGenerationSource?: string
  imageGenerationState?: {
    mode?: 'unconfigured' | 'disabled' | 'custom' | 'follow_llm' | string
    effective?: {
      providerId?: string
      primary?: string
      credentialSource?: string
      credentialOwner?: string
    }
    recommendation?: { providerId?: string } | null
    credentialOptions?: ImageCredentialOption[]
  }
}

export interface ImageCredentialOption {
  providerId: string
  available: boolean
  source: string
  owner: string
  kind?: string
  envKey?: string
  reason?: string
}

type CapabilityId = 'search' | 'memory_embedding' | 'image_generation' | 'audio'

interface CapabilitiesPanelContext {
  searchProviders: ComputedRef<Array<{
    providerId: string
    label: string
    requiresApiKey?: boolean
  }>>
  memoryProviders: ComputedRef<Array<{ providerId: string; label: string }>>
  imageProviders: ComputedRef<Array<{ providerId: string; label: string }>>
  imageSpec: ComputedRef<ProviderSpec | null>
  imageRecommendation: ComputedRef<{
      providerId: string
      label: string
      canReuseCredential: boolean
      actionRequired: boolean
      registrationUrl: string
    } | null>
  imageCredentialOptions: ComputedRef<ImageCredentialOption[]>
  imageModels: ComputedRef<Array<{
    id: string
    name: string
    contextWindow: number | null
    maxOutputTokens: number | null
    capabilities: string[]
    pricing: { inputPer1k: number; outputPer1k: number } | null
    capabilitySource: string
  }>>
  imageModelSource: ComputedRef<string>
  searchRequiresKey: ComputedRef<boolean>
  searchKeyPlaceholder: ComputedRef<string>
  searchDraftDirty: ComputedRef<boolean>
  searchDraftMissingKey: ComputedRef<boolean>
  searchDraftStatusText: ComputedRef<string>
  searchEnvPlaceholder: ComputedRef<string>
  searchAdvancedOpen: ComputedRef<boolean>
  searchNeeds: ComputedRef<string[]>
  searchEnvCommand: ComputedRef<string>
  searchStatusText: () => string
  memoryApiKeyEnabled: ComputedRef<boolean>
  memoryRemoteOptionsOpen: ComputedRef<boolean>
  memoryRemoteOptionsSummary: ComputedRef<string>
  memoryModelPlaceholder: ComputedRef<string>
  memoryBasePlaceholder: ComputedRef<string>
  memoryOnnxPlaceholder: ComputedRef<string>
  memoryApiKeyLabel: ComputedRef<string>
  memoryApiKeyPlaceholder: ComputedRef<string>
  memoryEnvPlaceholder: ComputedRef<string>
  memoryNeeds: ComputedRef<string[]>
  memoryStatusText: ComputedRef<string>
  memoryModeTitle: ComputedRef<string>
  memoryModeDescription: ComputedRef<string>
  memoryExpandable: ComputedRef<boolean>
  memoryEnvCommand: ComputedRef<string>
  imageNeeds: ComputedRef<string[]>
  imageStatusText: ComputedRef<string>
  imageEnvCommand: ComputedRef<string>
  capabilityBadgeTone: (name: CapabilityId) => string
  capabilityBadgeLabel: (name: CapabilityId) => string
  memoryAutoCapture: Ref<boolean>
  audioEnabled: Ref<boolean>
  audioApiKey: Ref<string>
  audioApiKeyEnv: Ref<string>
  audioBaseUrl: Ref<string>
  audioTtsVoice: Ref<string>
  audioTtsModel: Ref<string>
  audioLanguageCode: Ref<string>
  audioStatusText: ComputedRef<string>
  audioBadgeTone: ComputedRef<string>
  audioBadgeLabel: ComputedRef<string>
  audioKeyPlaceholder: ComputedRef<string>
  resettable: (name: CapabilityId) => boolean
  resetPending: Ref<CapabilityId | ''>
}

export interface SearchFormValues {
  providerId: string
  apiKey: string
  apiKeyEnv: string
  maxResults: number
  proxy: string
  useEnvProxy: boolean
  fallbackPolicy: string
  diagnostics: boolean
}

export interface MemoryFormValues {
  providerId: string
  model: string
  apiKey: string
  apiKeyEnv: string
  baseUrl: string
  onnxDir: string
}

export interface ImageFormValues {
  enabled: boolean
  providerId: string
  primary: string
  apiKey: string
  apiKeyEnv: string
  baseUrl: string
  size: string
  outputFormat: string
  fallbacks: string
}

export type ImageTouchedField = 'apiKey' | 'apiKeyEnv' | 'baseUrl' | 'fallbacks'
export type ImageCredentialSource =
  | 'explicit'
  | 'env'
  | 'llm_fallback'
  | 'missing_env'
  | 'configured'
  | 'none'

interface ImagePayloadOptions {
  clearFallbacks?: boolean
}

interface ImageProviderDraft {
  primary: string
  apiKeyEnv: string
  baseUrl: string
  credentialConfigured: boolean
  credentialSource: ImageCredentialSource
  touched: Set<ImageTouchedField>
}

function normalizeImageCredentialSource(value: unknown): ImageCredentialSource {
  const source = String(value || '').trim()
  if (
    source === 'explicit'
    || source === 'env'
    || source === 'llm_fallback'
    || source === 'missing_env'
  ) {
    return source
  }
  return 'none'
}

// Fallbacks are entered as one comma/newline-separated string; split to the
// provider/model array the backend expects.
export function parseImageFallbacks(raw: string): string[] {
  return raw
    .split(/[\n,]/)
    .map((s) => s.trim())
    .map((model) => model === 'openrouter/auto' ? 'openrouter/openrouter/auto' : model)
    .filter(Boolean)
}

export function imageModelForDisplay(providerId: string, raw: string): string {
  const provider = providerId.trim()
  const model = raw.trim()
  const prefix = provider ? `${provider}/` : ''
  return prefix && model.startsWith(prefix)
    ? model.slice(prefix.length).trim()
    : model
}

function isCanonicalImageModelRef(providerId: string, raw: string): boolean {
  const provider = providerId.trim()
  const model = raw.trim()
  const prefix = provider ? `${provider}/` : ''
  if (!prefix || !model.startsWith(prefix)) return false

  const nestedModel = model.slice(prefix.length)
  // `openrouter/auto` is a valid OpenRouter wire model, not a routing prefix
  // followed by the local model `auto`. Provider catalogs use a second slash
  // for canonical OpenRouter references such as `openrouter/google/...`.
  return provider !== 'openrouter' || nestedModel.includes('/')
}

export function imageModelRefForPayload(providerId: string, raw: string): string {
  const provider = providerId.trim()
  const model = isCanonicalImageModelRef(provider, raw)
    ? imageModelForDisplay(provider, raw)
    : raw.trim()
  return provider && model ? `${provider}/${model}` : ''
}

export function buildSearchPayload(values: SearchFormValues): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: values.providerId }
  if (values.apiKey) params.apiKey = values.apiKey
  else if (values.apiKeyEnv) params.apiKeyEnv = values.apiKeyEnv
  params.maxResults = values.maxResults
  if (values.proxy) params.proxy = values.proxy
  params.useEnvProxy = values.useEnvProxy
  params.fallbackPolicy = values.fallbackPolicy
  params.diagnostics = values.diagnostics
  return params
}

export function buildMemoryPayload(values: MemoryFormValues): Record<string, unknown> {
  const params: Record<string, unknown> = { providerId: values.providerId }
  if (values.model) params.model = values.model
  if (values.apiKey) params.apiKey = values.apiKey
  if (values.apiKeyEnv) params.apiKeyEnv = values.apiKeyEnv
  if (values.baseUrl) params.baseUrl = values.baseUrl
  if (values.onnxDir) params.onnxDir = values.onnxDir
  return params
}

export function buildImagePayload(
  values: ImageFormValues,
  touched: ReadonlySet<ImageTouchedField> = new Set(),
  options: ImagePayloadOptions = {},
): Record<string, unknown> {
  const apiKey = values.apiKey.trim()
  const apiKeyEnv = values.apiKeyEnv.trim()
  const params: Record<string, unknown> = {
    enabled: values.enabled,
    providerId: values.providerId.trim(),
  }
  const primary = imageModelRefForPayload(values.providerId, values.primary)
  if (primary) params.primary = primary
  // UI edits keep these mutually exclusive. Prefer the direct key as a final
  // payload guard if a caller constructs an inconsistent form value.
  if (touched.has('apiKey')) {
    params.credentialMode = 'direct'
    if (apiKey) params.apiKey = apiKey
  } else if (touched.has('apiKeyEnv')) {
    params.credentialMode = 'env'
    if (apiKeyEnv) params.apiKeyEnv = apiKeyEnv
  }
  if (touched.has('baseUrl')) params.baseUrl = values.baseUrl.trim()
  if (values.size.trim()) params.size = values.size.trim()
  if (values.outputFormat.trim()) params.outputFormat = values.outputFormat.trim()
  if (touched.has('fallbacks')) {
    const fallbacks = parseImageFallbacks(values.fallbacks)
    params.fallbacks = fallbacks
    if (options.clearFallbacks && fallbacks.length === 0) params.clearFallbacks = true
  }
  return params
}

export function useSetupCapabilitiesForm() {
  const searchProvider = ref('duckduckgo')
  const searchMaxResults = ref(10)
  const searchApiKey = ref('')
  const searchApiKeyEnv = ref('')
  const searchProxy = ref('')
  const searchUseEnvProxy = ref(false)
  const searchFallbackPolicy = ref('off')
  const searchDiagnostics = ref(false)

  const memoryProvider = ref('auto')
  const memoryModel = ref('')
  const memoryApiKey = ref('')
  const memoryApiKeyEnv = ref('')
  const memoryBaseUrl = ref('')
  const memoryOnnxDir = ref('')

  // An unconfigured capability has no selected provider. Catalog order is a
  // presentation detail, not persisted state; choosing providers[0] here made
  // a pristine install look configured and hid whether a recommendation had
  // actually been accepted.
  const imageProvider = ref('')
  const imagePrimary = ref('')
  const imageApiKey = ref('')
  const imageApiKeyEnv = ref('')
  const imageBaseUrl = ref('')
  const imageEnabled = ref(true)
  const imageSize = ref('1024x1024')
  const imageOutputFormat = ref('png')
  const imageFallbacks = ref('')
  const imageKeyConfigured = ref(false)
  const imageCredentialSource = ref<ImageCredentialSource>('none')
  const imageTouchedFields = ref<Set<ImageTouchedField>>(new Set())
  const imageGlobalTouchedFields = ref<Set<ImageTouchedField>>(new Set())
  const imageClearFallbacks = ref(false)
  const imageProviderDrafts = new Map<string, ImageProviderDraft>()
  const imageCredentialOptions = new Map<string, ImageCredentialOption>()
  let imageProviderSpecs: ProviderSpec[] = []
  let imageRecommendedProviderId = ''

  const searchSerialized = computed(() => JSON.stringify([
    searchProvider.value, searchMaxResults.value, searchApiKey.value, searchApiKeyEnv.value,
    searchProxy.value, searchUseEnvProxy.value, searchFallbackPolicy.value, searchDiagnostics.value,
  ]))
  const memorySerialized = computed(() => JSON.stringify([
    memoryProvider.value, memoryModel.value, memoryApiKey.value, memoryApiKeyEnv.value,
    memoryBaseUrl.value, memoryOnnxDir.value,
  ]))
  const imageSerialized = computed(() => JSON.stringify([
    imageEnabled.value,
    imageProvider.value, imagePrimary.value, imageApiKey.value, imageApiKeyEnv.value,
    imageBaseUrl.value,
    imageSize.value, imageOutputFormat.value, imageFallbacks.value,
  ]))
  // Seed from the initial state so the pristine forms are never dirty while config loads.
  const searchBaseline = ref(searchSerialized.value)
  const memoryBaseline = ref(memorySerialized.value)
  const imageBaseline = ref(imageSerialized.value)
  const searchDirty = computed(() => searchSerialized.value !== searchBaseline.value)
  const memoryDirty = computed(() => memorySerialized.value !== memoryBaseline.value)
  const imageDirty = computed(() => imageSerialized.value !== imageBaseline.value)

  const memoryRemoteControlEnabled = computed(() => !['none', 'local'].includes(memoryProvider.value))
  const memoryLocalControlEnabled = computed(() => memoryProvider.value === 'local')
  const selectedSearchProvider = computed(() => searchProvider.value)
  const selectedMemoryProvider = computed(() => memoryProvider.value)
  const selectedImageProvider = computed(() => imageProvider.value)
  const imageIsEnabled = computed(() => imageEnabled.value)
  const searchAdvancedOpen = computed(() => Boolean(searchProxy.value || searchUseEnvProxy.value || searchFallbackPolicy.value !== 'off' || searchDiagnostics.value))
  const searchApiKeyValue = computed(() => searchApiKey.value)
  const searchApiKeyEnvValue = computed(() => searchApiKeyEnv.value)
  const memoryApiKeyEnvValue = computed(() => memoryApiKeyEnv.value)
  const imageApiKeyEnvValue = computed(() => imageApiKeyEnv.value)
  const imageApiKeyValue = computed(() => imageApiKey.value)
  const imagePrimaryValue = computed(() => imagePrimary.value)
  const imageBaseUrlValue = computed(() => imageBaseUrl.value)
  const imageKeyConfiguredValue = computed(() => imageKeyConfigured.value)
  const imageCredentialSourceValue = computed(() => imageCredentialSource.value)
  const memoryRemoteOptionsOpen = computed(() => memoryProvider.value !== 'auto' || Boolean(memoryModel.value || memoryApiKey.value || memoryApiKeyEnv.value || memoryBaseUrl.value))
  const memoryRemoteOptionsSummary = computed(() => i18n.global.t(memoryProvider.value === 'auto' ? 'setup.memory.remoteFallbackOptions' : 'setup.memory.connectionOptions'))
  const memoryModelPlaceholder = computed(() => memoryProvider.value === 'ollama' ? 'nomic-embed-text' : (memoryRemoteControlEnabled.value ? 'remote-embedding-model' : i18n.global.t('setup.memory.notUsedByProvider')))
  const memoryBasePlaceholder = computed(() => memoryProvider.value === 'ollama' ? 'http://localhost:11434' : (memoryRemoteControlEnabled.value ? 'https://api.example.com/v1' : i18n.global.t('setup.memory.notUsedByProvider')))
  const memoryOnnxPlaceholder = computed(() => memoryLocalControlEnabled.value ? 'models/bge-onnx' : i18n.global.t('setup.memory.onnxOnlyLocal'))
  const memoryApiKeyLabel = computed(() => i18n.global.t(memoryProvider.value === 'auto' ? 'setup.memory.fallbackApiKey' : 'setup.common.apiKey'))

  function initSearchFromConfig(config: ConfigData, providers: ProviderSpec[]) {
    searchProvider.value = config.search_provider || providers.find(p => p.providerId === 'duckduckgo')?.providerId || providers[0]?.providerId || 'duckduckgo'
    searchMaxResults.value = config.search_max_results || 10
    searchApiKeyEnv.value = config.search_api_key_env || ''
    searchProxy.value = config.search_proxy || ''
    searchUseEnvProxy.value = config.search_use_env_proxy === true
    searchFallbackPolicy.value = config.search_fallback_policy || 'off'
    searchDiagnostics.value = config.search_diagnostics === true
    searchApiKey.value = ''
    searchBaseline.value = searchSerialized.value
  }

  function initMemoryFromConfig(config: ConfigData) {
    const current = config.memory?.embedding || {}
    const effective = current.provider || current.mode || 'auto'
    memoryProvider.value = effective
    const remote = current.remote || {}
    memoryModel.value = remote.model || ''
    memoryApiKeyEnv.value = remote.api_key_env || ''
    memoryBaseUrl.value = remote.base_url || ''
    const local = current.local || {}
    memoryOnnxDir.value = local.onnx_dir || ''
    memoryApiKey.value = ''
    memoryBaseline.value = memorySerialized.value
  }

  function initImageFromConfig(config: ConfigData, status: StatusData, providers: ProviderSpec[]) {
    const imageConfig = config.image_generation || {}
    const imageState = status.imageGenerationState
    const stateMode = String(imageState?.mode || '').trim().toLowerCase()
    const stateIsAuthoritative = Boolean(imageState && stateMode)
    const hasPersistedSelection = !stateIsAuthoritative || stateMode !== 'unconfigured'
    const primaryRef = hasPersistedSelection
      ? String(
          imageState?.effective?.primary
          || status.imageGenerationPrimary
          || '',
        ).trim()
      : ''
    const primaryProvider = primaryRef.split('/', 1)[0]
    const persistedProvider = hasPersistedSelection
      ? String(
          imageState?.effective?.providerId
          || status.imageGenerationProvider
          || '',
        ).trim()
      : ''
    imageProviderSpecs = providers
    imageRecommendedProviderId = String(imageState?.recommendation?.providerId || '').trim()
    imageCredentialOptions.clear()
    for (const option of imageState?.credentialOptions || []) {
      imageCredentialOptions.set(String(option.providerId || '').trim(), option)
    }
    // A persisted primary is the route source of truth. Credential status can
    // lag behind it (for example after a provider change), so consult the
    // status provider only when the primary does not identify a catalog row.
    const selected = providers.find(p => p.providerId === primaryProvider)?.providerId
      || providers.find(p => p.providerId === persistedProvider)?.providerId
      || ''
    imageProviderDrafts.clear()
    imageGlobalTouchedFields.value = new Set()
    imageClearFallbacks.value = false
    for (const spec of providers) {
      const providerConfig = (imageConfig.providers || {})[spec.providerId] || {}
      const configuredPrimary = spec.providerId === primaryProvider ? primaryRef : ''
      const keyConfigured = Boolean(providerConfig.api_key)
      const isStatusProvider = spec.providerId === persistedProvider
      const configuredEnv = String(providerConfig.api_key_env || '').trim()
      // config.get materializes each provider's schema-default env key. For a
      // provider that is not the persisted route, that default is not proof
      // that the variable exists and must not suppress the key editor after a
      // recommendation is accepted. A custom env reference remains authored.
      const authoredEnv = Boolean(
        configuredEnv
        && (isStatusProvider || configuredEnv !== String(spec.envKey || '').trim()),
      )
      const statusSource = isStatusProvider
        ? normalizeImageCredentialSource(
            imageState?.effective?.credentialSource
            || status.imageGenerationSource,
          )
        : 'none'
      const reusableOption = imageCredentialOptions.get(spec.providerId)
      const optionSource: ImageCredentialSource = reusableOption?.available === true
        ? normalizeImageCredentialSource(reusableOption.source)
        : reusableOption?.source === 'missing_env'
          ? 'missing_env'
          : 'none'
      const credentialSource: ImageCredentialSource = keyConfigured
        ? 'explicit'
        : statusSource !== 'none'
          ? statusSource
          : optionSource !== 'none'
            ? optionSource
          : authoredEnv
            ? 'env'
            : status.imageGenerationConfigured === true && isStatusProvider
              ? 'configured'
              : 'none'
      const credentialConfigured = ['explicit', 'env', 'llm_fallback', 'configured']
        .includes(credentialSource)
      imageProviderDrafts.set(spec.providerId, {
        primary: imageModelForDisplay(
          spec.providerId,
          configuredPrimary || spec.defaultModel || '',
        ),
        apiKeyEnv: keyConfigured
          ? ''
          : (
              configuredEnv
              || reusableOption?.envKey
              || (spec.requiresApiKey ? spec.envKey || '' : '')
            ),
        baseUrl: providerConfig.base_url || spec.defaultBaseUrl || '',
        credentialConfigured,
        credentialSource,
        touched: new Set(),
      })
    }
    imageProvider.value = selected
    if (selected) {
      applyImageProviderDraft(
        imageProviderDrafts.get(selected) || createDefaultImageProviderDraft(
          providers.find(p => p.providerId === selected),
        ),
      )
    } else {
      clearImageProviderDraft()
    }
    imageEnabled.value = status.imageGenerationEnabled === true
    imageSize.value = imageConfig.size || '1024x1024'
    imageOutputFormat.value = imageConfig.output_format || 'png'
    imageFallbacks.value = (imageConfig.fallbacks || []).join(', ')
    imageBaseline.value = imageSerialized.value
  }

  function createDefaultImageProviderDraft(
    spec: ProviderSpec | null | undefined,
  ): ImageProviderDraft {
    const option = imageCredentialOptions.get(spec?.providerId || '')
    const credentialSource: ImageCredentialSource = option?.available === true
      ? normalizeImageCredentialSource(option.source)
      : option?.source === 'missing_env'
        ? 'missing_env'
        : 'none'
    return {
      primary: imageModelForDisplay(spec?.providerId || '', spec?.defaultModel || ''),
      apiKeyEnv: option?.envKey || (spec?.requiresApiKey ? spec.envKey || '' : ''),
      baseUrl: spec?.defaultBaseUrl || '',
      credentialConfigured: option?.available === true,
      credentialSource,
      touched: new Set(),
    }
  }

  function applyImageProviderDraft(draft: ImageProviderDraft) {
    imagePrimary.value = draft.primary
    imageApiKey.value = ''
    imageApiKeyEnv.value = draft.apiKeyEnv
    imageBaseUrl.value = draft.baseUrl
    imageKeyConfigured.value = draft.credentialConfigured
    imageCredentialSource.value = draft.credentialSource
    imageTouchedFields.value = new Set(draft.touched)
  }

  function clearImageProviderDraft() {
    imagePrimary.value = ''
    imageApiKey.value = ''
    imageApiKeyEnv.value = ''
    imageBaseUrl.value = ''
    imageKeyConfigured.value = false
    imageCredentialSource.value = 'none'
    imageTouchedFields.value = new Set()
  }

  function saveCurrentImageProviderDraft() {
    const draft = imageProviderDrafts.get(imageProvider.value)
    if (!draft) return
    draft.primary = imagePrimary.value
    draft.baseUrl = imageBaseUrl.value
    draft.touched = new Set(imageTouchedFields.value)
    // A pasted key is write-only and must not survive a provider switch. Keep
    // the prior env-reference draft when discarding that transient secret.
    if (imageApiKey.value.trim()) {
      draft.touched.delete('apiKey')
      draft.touched.delete('apiKeyEnv')
    } else {
      draft.apiKeyEnv = imageApiKeyEnv.value
    }
  }

  function switchImageProvider(
    providerId: string,
    spec?: ProviderSpec | null,
  ) {
    const nextProviderId = providerId.trim()
    if (!nextProviderId) return

    if (nextProviderId === imageProvider.value) {
      // The 0.5.0 panel emitted updateField(provider) before the dedicated
      // provider-change event. The first event now performs the switch; the
      // second must be harmless while still allowing a pre-init caller to
      // supply the provider defaults that updateField did not have.
      if (!imageProviderDrafts.has(nextProviderId)) {
        const draft = createDefaultImageProviderDraft(spec)
        imageProviderDrafts.set(nextProviderId, draft)
        applyImageProviderDraft(draft)
      }
      imageEnabled.value = true
      return
    }

    saveCurrentImageProviderDraft()
    const draft = imageProviderDrafts.get(nextProviderId)
      || createDefaultImageProviderDraft(spec)
    imageProviderDrafts.set(nextProviderId, draft)
    imageProvider.value = nextProviderId
    // Selecting a provider is the explicit enable/configure action. The wire
    // configure RPC owns persistence; this local flag keeps draft guidance
    // (credential requirements, hints) aligned before Save.
    imageEnabled.value = true
    applyImageProviderDraft(draft)
  }

  function touchImageField(field: ImageTouchedField) {
    const fields = field === 'fallbacks'
      ? imageGlobalTouchedFields
      : imageTouchedFields
    const next = new Set(fields.value)
    next.add(field)
    fields.value = next
  }

  function onSearchProviderChange(_spec: ProviderSpec | null | undefined) {
    // Existing env references are hydrated and preserved until the user
    // actually changes provider. A newly selected provider must be configured
    // visibly in this client: never author a hidden default env reference that
    // turns into a confusing "needs attention" state only after Save.
    searchApiKeyEnv.value = ''
    searchApiKey.value = ''
  }

  function onMemoryProviderChange(spec: ProviderSpec | null | undefined, apiKeyEnabled: boolean) {
    if (apiKeyEnabled && spec && !memoryApiKeyEnv.value) {
      memoryApiKeyEnv.value = spec.envKey || ''
    }
  }

  function onImageProviderChange(spec: ProviderSpec | null | undefined) {
    if (!spec) return
    switchImageProvider(spec.providerId, spec)
  }

  function updateField(
    group: 'search' | 'memory' | 'image',
    key: string,
    value: string | number | boolean,
  ) {
    if (group === 'search') {
      if (key === 'provider') searchProvider.value = String(value)
      else if (key === 'maxResults') searchMaxResults.value = Number(value)
      else if (key === 'apiKey') searchApiKey.value = String(value)
      else if (key === 'apiKeyEnv') searchApiKeyEnv.value = String(value)
      else if (key === 'proxy') searchProxy.value = String(value)
      else if (key === 'useEnvProxy') searchUseEnvProxy.value = Boolean(value)
      else if (key === 'fallbackPolicy') searchFallbackPolicy.value = String(value)
      else if (key === 'diagnostics') searchDiagnostics.value = Boolean(value)
      return
    }
    if (group === 'memory') {
      if (key === 'provider') memoryProvider.value = String(value)
      else if (key === 'model') memoryModel.value = String(value)
      else if (key === 'apiKey') memoryApiKey.value = String(value)
      else if (key === 'apiKeyEnv') memoryApiKeyEnv.value = String(value)
      else if (key === 'baseUrl') memoryBaseUrl.value = String(value)
      else if (key === 'onnxDir') memoryOnnxDir.value = String(value)
      return
    }
    if (key === 'provider') switchImageProvider(String(value))
    else if (key === 'primary') imagePrimary.value = String(value)
    else if (key === 'apiKey') {
      imageApiKey.value = String(value)
      imageApiKeyEnv.value = ''
      const next = new Set(imageTouchedFields.value)
      next.delete('apiKeyEnv')
      // Blank must mean "keep". A field emptied after typing has to be
      // indistinguishable from an untouched one: a touched-but-empty key
      // would send credentialMode 'direct' with no key, which the backend
      // reads as an authored switch to direct mode and deletes a stored
      // env reference.
      if (String(value).trim()) next.add('apiKey')
      else next.delete('apiKey')
      imageTouchedFields.value = next
    } else if (key === 'apiKeyEnv') {
      imageApiKeyEnv.value = String(value)
      imageApiKey.value = ''
      const next = new Set(imageTouchedFields.value)
      next.delete('apiKey')
      // Same keep-on-blank rule as the direct key: a touched-but-empty env
      // reference would author credentialMode 'env', which the backend reads
      // as a source switch and deletes a stored direct key.
      if (String(value).trim()) next.add('apiKeyEnv')
      else next.delete('apiKeyEnv')
      imageTouchedFields.value = next
    } else if (key === 'baseUrl') {
      imageBaseUrl.value = String(value)
      touchImageField('baseUrl')
    }
    else if (key === 'enabled') {
      imageEnabled.value = Boolean(value)
      if (imageEnabled.value && !imageProvider.value) {
        const spec = imageProviderSpecs.find(
          provider => provider.providerId === imageRecommendedProviderId,
        )
        if (spec) switchImageProvider(spec.providerId, spec)
      }
    }
    else if (key === 'size') imageSize.value = String(value)
    else if (key === 'outputFormat') imageOutputFormat.value = String(value)
    else if (key === 'fallbacks') {
      const nextFallbacks = String(value)
      const hadFallbacks = parseImageFallbacks(imageFallbacks.value).length > 0
      const hasFallbacks = parseImageFallbacks(nextFallbacks).length > 0
      imageFallbacks.value = nextFallbacks
      if (hasFallbacks) imageClearFallbacks.value = false
      else if (hadFallbacks) imageClearFallbacks.value = true
      touchImageField('fallbacks')
    }
  }

  function searchPayload(): Record<string, unknown> {
    return buildSearchPayload({
      providerId: searchProvider.value,
      apiKey: searchApiKey.value,
      apiKeyEnv: searchApiKeyEnv.value,
      maxResults: searchMaxResults.value,
      proxy: searchProxy.value,
      useEnvProxy: searchUseEnvProxy.value,
      fallbackPolicy: searchFallbackPolicy.value,
      diagnostics: searchDiagnostics.value,
    })
  }

  function memoryPayload(): Record<string, unknown> {
    return buildMemoryPayload({
      providerId: memoryProvider.value,
      model: memoryModel.value,
      apiKey: memoryApiKey.value,
      apiKeyEnv: memoryApiKeyEnv.value,
      baseUrl: memoryBaseUrl.value,
      onnxDir: memoryOnnxDir.value,
    })
  }

  function imagePayload(): Record<string, unknown> {
    const touchedFields = new Set([
      ...imageTouchedFields.value,
      ...imageGlobalTouchedFields.value,
    ])
    return buildImagePayload({
      enabled: imageEnabled.value,
      providerId: imageProvider.value,
      primary: imagePrimary.value,
      apiKey: imageApiKey.value,
      apiKeyEnv: imageApiKeyEnv.value,
      baseUrl: imageBaseUrl.value,
      size: imageSize.value,
      outputFormat: imageOutputFormat.value,
      fallbacks: imageFallbacks.value,
    }, touchedFields, { clearFallbacks: imageClearFallbacks.value })
  }

  function createPanel(context: CapabilitiesPanelContext) {
    return computed(() => ({
      form: {
        searchProvider: searchProvider.value,
        searchMaxResults: searchMaxResults.value,
        searchApiKey: searchApiKey.value,
        searchApiKeyEnv: searchApiKeyEnv.value,
        searchProxy: searchProxy.value,
        searchUseEnvProxy: searchUseEnvProxy.value,
        searchFallbackPolicy: searchFallbackPolicy.value,
        searchDiagnostics: searchDiagnostics.value,
        memoryProvider: memoryProvider.value,
        memoryModel: memoryModel.value,
        memoryApiKey: memoryApiKey.value,
        memoryApiKeyEnv: memoryApiKeyEnv.value,
        memoryBaseUrl: memoryBaseUrl.value,
        memoryOnnxDir: memoryOnnxDir.value,
        imageProvider: imageProvider.value,
        imagePrimary: imagePrimary.value,
        imageApiKey: imageApiKey.value,
        // Whether a working credential already exists for the selected
        // provider — a persisted write-only key, a saved env reference, or
        // the backend-computed configured status. Drives the key field's
        // placeholder/state hint; the key itself is never echoed back.
        imageKeyConfigured: imageKeyConfigured.value,
        imageCredentialSource: imageCredentialSource.value,
        imageApiKeyEnv: imageApiKeyEnv.value,
        imageBaseUrl: imageBaseUrl.value,
        imageEnabled: imageEnabled.value,
        imageSize: imageSize.value,
        imageOutputFormat: imageOutputFormat.value,
        imageFallbacks: imageFallbacks.value,
        memoryAutoCapture: context.memoryAutoCapture.value,
        audioEnabled: context.audioEnabled.value,
        audioApiKey: context.audioApiKey.value,
        audioApiKeyEnv: context.audioApiKeyEnv.value,
        audioBaseUrl: context.audioBaseUrl.value,
        audioTtsVoice: context.audioTtsVoice.value,
        audioTtsModel: context.audioTtsModel.value,
        audioLanguageCode: context.audioLanguageCode.value,
      },
      options: {
        searchProviders: context.searchProviders.value,
        memoryProviders: context.memoryProviders.value,
        imageProviders: context.imageProviders.value,
        imageSpec: context.imageSpec.value,
        imageRecommendation: context.imageRecommendation.value,
        imageCredentialOptions: context.imageCredentialOptions.value,
        imageModels: context.imageModels.value,
      },
      state: {
        searchRequiresKey: context.searchRequiresKey.value,
        searchKeyPlaceholder: context.searchKeyPlaceholder.value,
        searchDraftDirty: context.searchDraftDirty.value,
        searchDraftMissingKey: context.searchDraftMissingKey.value,
        searchDraftStatusText: context.searchDraftStatusText.value,
        searchEnvPlaceholder: context.searchEnvPlaceholder.value,
        searchAdvancedOpen: searchAdvancedOpen.value,
        searchNeeds: context.searchNeeds.value,
        searchEnvCommand: context.searchEnvCommand.value,
        searchStatusText: context.searchStatusText(),
        memoryLocalControlEnabled: memoryLocalControlEnabled.value,
        memoryRemoteControlEnabled: memoryRemoteControlEnabled.value,
        memoryApiKeyEnabled: context.memoryApiKeyEnabled.value,
        memoryRemoteOptionsOpen: memoryRemoteOptionsOpen.value,
        memoryRemoteOptionsSummary: memoryRemoteOptionsSummary.value,
        memoryModelPlaceholder: memoryModelPlaceholder.value,
        memoryBasePlaceholder: memoryBasePlaceholder.value,
        memoryOnnxPlaceholder: memoryOnnxPlaceholder.value,
        memoryApiKeyLabel: memoryApiKeyLabel.value,
        memoryApiKeyPlaceholder: context.memoryApiKeyPlaceholder.value,
        memoryEnvPlaceholder: context.memoryEnvPlaceholder.value,
        memoryNeeds: context.memoryNeeds.value,
        memoryStatusText: context.memoryStatusText.value,
        memoryModeTitle: context.memoryModeTitle.value,
        memoryModeDescription: context.memoryModeDescription.value,
        memoryExpandable: context.memoryExpandable.value,
        memoryEnvCommand: context.memoryEnvCommand.value,
        imageNeeds: context.imageNeeds.value,
        imageStatusText: context.imageStatusText.value,
        imageModelSource: context.imageModelSource.value,
        imageEnvCommand: context.imageEnvCommand.value,
        capabilityBadgeTone: context.capabilityBadgeTone,
        capabilityBadgeLabel: context.capabilityBadgeLabel,
        audioStatusText: context.audioStatusText.value,
        audioBadgeTone: context.audioBadgeTone.value,
        audioBadgeLabel: context.audioBadgeLabel.value,
        audioKeyPlaceholder: context.audioKeyPlaceholder.value,
        resettable: context.resettable,
        resetPending: context.resetPending.value,
      },
    }))
  }

  return {
    selectedSearchProvider,
    selectedMemoryProvider,
    selectedImageProvider,
    imageIsEnabled,
    searchDirty,
    memoryDirty,
    imageDirty,
    searchAdvancedOpen,
    searchApiKeyValue,
    searchApiKeyEnvValue,
    memoryApiKeyEnvValue,
    imageApiKeyEnvValue,
    imageApiKeyValue,
    imagePrimaryValue,
    imageBaseUrlValue,
    imageKeyConfiguredValue,
    imageCredentialSourceValue,
    memoryRemoteOptionsOpen,
    memoryRemoteOptionsSummary,
    memoryModelPlaceholder,
    memoryBasePlaceholder,
    memoryOnnxPlaceholder,
    memoryApiKeyLabel,
    memoryRemoteControlEnabled,
    memoryLocalControlEnabled,
    initSearchFromConfig,
    initMemoryFromConfig,
    initImageFromConfig,
    onSearchProviderChange,
    onMemoryProviderChange,
    onImageProviderChange,
    updateField,
    searchPayload,
    memoryPayload,
    imagePayload,
    createPanel,
  }
}
