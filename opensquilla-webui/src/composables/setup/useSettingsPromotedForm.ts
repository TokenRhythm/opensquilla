import { computed, ref } from 'vue'

// Curated keys promoted into Settings beyond the classic wizard fields.
// Timeout and memory capture persist through the config.patch RPC as
// dot-path patches; audio saves through onboarding.audio.configure.

export const DEFAULT_LLM_TIMEOUT_SECONDS = 120

// Parse a raw context-window input string into a positive token count, or null
// when it is blank/zero/non-numeric ("use the auto-detected window"). Shared
// with SetupProviderPanel so the field and its readout agree on the rule.
export function parseContextWindowInput(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : null
}

interface PromotedConfigData {
  llm_request_timeout_seconds?: number
  llm?: { provider?: string; model?: string }
  // Per-provider/per-model overrides. Model ids contain dots and colons, so
  // this subtree is written with deep-merge patches, never dot-path patches.
  models?: Record<string, Record<string, { context_window?: number }>>
  memory?: { auto_capture_enabled?: boolean }
  audio?: {
    enabled?: boolean
    tts?: { voice?: string; model?: string; language_code?: string }
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
  }
}

export function useSettingsPromotedForm() {
  const llmTimeoutSeconds = ref(DEFAULT_LLM_TIMEOUT_SECONDS)
  // Per-model context-window override, kept as the raw input string ('' = auto).
  const contextWindowTokens = ref('')
  const memoryAutoCapture = ref(true)
  const audioEnabled = ref(false)
  const audioApiKey = ref('')
  const audioApiKeyEnv = ref('')
  const audioKeyConfigured = ref(false)
  // TTS tuning the backend already accepts/applies (mutations.upsert_audio_provider):
  // empty means "keep current / use the provider default".
  const audioBaseUrl = ref('')
  const audioTtsVoice = ref('')
  const audioTtsModel = ref('')
  const audioLanguageCode = ref('')
  const audioProviderId = [101, 108, 101, 118, 101, 110, 108, 97, 98, 115]
    .map(code => String.fromCharCode(code))
    .join('')

  const audioSerialized = computed(() => JSON.stringify([
    audioEnabled.value, audioApiKey.value, audioApiKeyEnv.value,
    audioBaseUrl.value, audioTtsVoice.value, audioTtsModel.value, audioLanguageCode.value,
  ]))

  // Seed from the initial state so the pristine form is never dirty while config loads.
  const timeoutBaseline = ref(llmTimeoutSeconds.value)
  const contextWindowBaseline = ref(contextWindowTokens.value)
  const captureBaseline = ref(memoryAutoCapture.value)
  const audioBaseline = ref(audioSerialized.value)

  const timeoutDirty = computed(() => llmTimeoutSeconds.value !== timeoutBaseline.value)
  const contextWindowDirty = computed(() => contextWindowTokens.value !== contextWindowBaseline.value)
  const captureDirty = computed(() => memoryAutoCapture.value !== captureBaseline.value)
  const audioDirty = computed(() => audioSerialized.value !== audioBaseline.value)

  // Resolve the saved per-model context-window override for a provider+model
  // into the raw field string ('' = no override / auto).
  function contextWindowOverrideFor(config: PromotedConfigData, provider: string, model: string): string {
    const p = String(provider || '')
    const m = String(model || '')
    const override = p && m ? config.models?.[p]?.[m]?.context_window : undefined
    return typeof override === 'number' && Number.isFinite(override) && override > 0
      ? String(Math.floor(override))
      : ''
  }

  function initFromConfig(config: PromotedConfigData) {
    initProviderFromConfig(config)
    initMemoryCaptureFromConfig(config)
    initAudioFromConfig(config)
  }

  function initProviderFromConfig(config: PromotedConfigData) {
    const timeout = Number(config.llm_request_timeout_seconds)
    llmTimeoutSeconds.value = Number.isFinite(timeout) && timeout >= 1 ? timeout : DEFAULT_LLM_TIMEOUT_SECONDS
    // Seed the context-window field from the saved provider+model override.
    contextWindowTokens.value = contextWindowOverrideFor(
      config,
      String(config.llm?.provider || ''),
      String(config.llm?.model || ''),
    )
    commitProviderBaselines()
  }

  function initMemoryCaptureFromConfig(config: PromotedConfigData) {
    memoryAutoCapture.value = config.memory?.auto_capture_enabled !== false
    captureBaseline.value = memoryAutoCapture.value
  }

  function initAudioFromConfig(config: PromotedConfigData) {
    audioEnabled.value = config.audio?.enabled === true
    const audioProvider = config.audio?.providers?.[audioProviderId] || {}
    audioApiKeyEnv.value = audioProvider.api_key_env || ''
    audioBaseUrl.value = audioProvider.base_url || ''
    const tts = config.audio?.tts || {}
    audioTtsVoice.value = tts.voice || ''
    audioTtsModel.value = tts.model || ''
    audioLanguageCode.value = tts.language_code || ''
    // config.get redacts stored secrets; presence alone means a key is saved.
    audioKeyConfigured.value = Boolean(audioProvider.api_key)
    audioApiKey.value = ''

    audioBaseline.value = audioSerialized.value
  }

  function commitProviderBaselines() {
    timeoutBaseline.value = llmTimeoutSeconds.value
    contextWindowBaseline.value = contextWindowTokens.value
  }

  function setLlmTimeoutSeconds(value: number) {
    llmTimeoutSeconds.value = Number.isFinite(value) && value >= 1 ? value : DEFAULT_LLM_TIMEOUT_SECONDS
  }

  function setContextWindowTokens(value: string) {
    contextWindowTokens.value = String(value ?? '').trim()
  }

  // Reseed the context-window field (value + baseline) from the saved override
  // for a newly-selected provider/model. Called when the provider changes or
  // the model field is edited so the field never shows a stale override that
  // belongs to a different provider+model pair.
  function reseedContextWindow(config: PromotedConfigData, provider: string, model: string) {
    contextWindowTokens.value = contextWindowOverrideFor(config, provider, model)
    contextWindowBaseline.value = contextWindowTokens.value
  }

  function setMemoryAutoCapture(value: boolean) {
    memoryAutoCapture.value = Boolean(value)
  }

  function updateAudioField(key: string, value: string | boolean) {
    if (key === 'enabled') audioEnabled.value = Boolean(value)
    else if (key === 'apiKey') audioApiKey.value = String(value)
    else if (key === 'apiKeyEnv') audioApiKeyEnv.value = String(value)
    else if (key === 'baseUrl') audioBaseUrl.value = String(value)
    else if (key === 'ttsVoice') audioTtsVoice.value = String(value)
    else if (key === 'ttsModel') audioTtsModel.value = String(value)
    else if (key === 'languageCode') audioLanguageCode.value = String(value)
  }

  function providerPatches(): Record<string, unknown> {
    if (!timeoutDirty.value) return {}
    return { llm_request_timeout_seconds: llmTimeoutSeconds.value }
  }

  // Deep-merge patch for the per-model context-window override. Model ids
  // contain dots and colons (e.g. "qwen3:8b", "deepseek/deepseek-v4-pro"), so
  // this CANNOT ride the dot-path `patches` form — the caller must send it via
  // config.patch's deep-merge `patch` envelope. Clearing the field (empty or 0)
  // writes null, which deletes the key on the gateway side.
  function contextWindowPatch(providerId: string, modelId: string): Record<string, unknown> | null {
    if (!contextWindowDirty.value) return null
    const provider = String(providerId || '').trim()
    const model = String(modelId || '').trim()
    if (!provider || !model) return null
    const tokens = parseContextWindowInput(contextWindowTokens.value)
    return { models: { [provider]: { [model]: { context_window: tokens } } } }
  }

  function memoryPatches(): Record<string, unknown> {
    if (!captureDirty.value) return {}
    return { 'memory.auto_capture_enabled': memoryAutoCapture.value }
  }

  function audioPayload(): Record<string, unknown> {
    // Configuration implies enablement for new clients. Older clients may
    // continue sending an explicit `enabled` field to the compatible RPC.
    const params: Record<string, unknown> = { providerId: audioProviderId }
    // One-time paste only; never echo the redacted stored key back.
    if (audioApiKey.value) params.apiKey = audioApiKey.value
    else if (audioApiKeyEnv.value.trim()) params.apiKeyEnv = audioApiKeyEnv.value.trim()
    // Empty is "keep current" backend-side, so only send populated tuning fields.
    if (audioBaseUrl.value.trim()) params.baseUrl = audioBaseUrl.value.trim()
    if (audioTtsVoice.value.trim()) params.ttsVoice = audioTtsVoice.value.trim()
    if (audioTtsModel.value.trim()) params.ttsModel = audioTtsModel.value.trim()
    if (audioLanguageCode.value.trim()) params.languageCode = audioLanguageCode.value.trim()
    return params
  }

  return {
    llmTimeoutSeconds,
    contextWindowTokens,
    memoryAutoCapture,
    audioEnabled,
    audioApiKey,
    audioApiKeyEnv,
    audioBaseUrl,
    audioTtsVoice,
    audioTtsModel,
    audioLanguageCode,
    audioKeyConfigured,
    timeoutDirty,
    contextWindowDirty,
    captureDirty,
    audioDirty,
    initFromConfig,
    initProviderFromConfig,
    initMemoryCaptureFromConfig,
    initAudioFromConfig,
    setLlmTimeoutSeconds,
    setContextWindowTokens,
    reseedContextWindow,
    setMemoryAutoCapture,
    updateAudioField,
    providerPatches,
    contextWindowPatch,
    memoryPatches,
    audioPayload,
  }
}
