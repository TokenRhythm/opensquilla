import { computed, ref } from 'vue'
import type { ConfigureAudio } from '@/modules/setupWorkflow'

// Curated keys promoted into Settings beyond the classic wizard fields.
// Timeout and memory capture persist through the config.patch RPC as
// dot-path patches; audio saves through onboarding.audio.configure.

export const DEFAULT_LLM_TIMEOUT_SECONDS = 120

// Parse a raw token-count input string into a positive token count, or null
// when it is blank/zero/non-numeric ("use the auto-detected value"). Shared
// with SetupProviderPanel so the field and its readout agree on the rule.
// Accepts plain counts plus k/M unit suffixes (case-insensitive, decimal
// scale to match the compact catalog readouts): "64k" → 64000, "1M" → 1e6.
export function parseTokenCountInput(value: unknown): number | null {
  const raw = String(value ?? '').trim().toLowerCase()
  if (!raw) return null
  const match = /^(\d+(?:\.\d+)?)([km])?$/.exec(raw)
  if (!match) return null
  const magnitude = Number(match[1])
  if (!Number.isFinite(magnitude) || magnitude <= 0) return null
  const scale = match[2] === 'k' ? 1_000 : match[2] === 'm' ? 1_000_000 : 1
  return Math.floor(magnitude * scale)
}

// Compact lossless display form for a saved token count: prefer k/M suffixes
// when they divide evenly (1024000 → "1024k", 1000000 → "1M"), else the plain
// integer (8192). Round-trips through parseTokenCountInput unchanged.
export function formatTokenCountInput(count: number | null | undefined): string {
  if (typeof count !== 'number' || !Number.isFinite(count) || count <= 0) return ''
  const floored = Math.floor(count)
  if (floored % 1_000_000 === 0) return `${floored / 1_000_000}M`
  if (floored % 1_000 === 0) return `${floored / 1_000}k`
  return String(floored)
}

interface PromotedConfigData {
  llm_request_timeout_seconds?: number
  llm?: { provider?: string; model?: string }
  // Per-provider/per-model overrides. Model ids contain dots and colons, so
  // this subtree is written with deep-merge patches, never dot-path patches.
  models?: Record<
    string,
    Record<
      string,
      {
        context_window?: number
        max_output_tokens?: number
        supports_vision?: boolean
        supports_video?: boolean
      }
    >
  >
  memory?: { auto_capture_enabled?: boolean }
  audio?: {
    enabled?: boolean
    tts?: { voice?: string; model?: string; language_code?: string }
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
  }
}

export function useSettingsPromotedForm() {
  const llmTimeoutSeconds = ref(DEFAULT_LLM_TIMEOUT_SECONDS)
  // Per-model overrides, kept as raw input strings ('' = auto) and booleans
  // (false = inherit the catalog's capability; the UI only forces ON — an
  // explicit false stays config-TOML territory).
  const contextWindowTokens = ref('')
  const maxOutputTokens = ref('')
  const modelSupportsVision = ref(false)
  const modelSupportsVideo = ref(false)
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
  const maxOutputBaseline = ref(maxOutputTokens.value)
  const visionBaseline = ref(modelSupportsVision.value)
  const videoBaseline = ref(modelSupportsVideo.value)
  const captureBaseline = ref(memoryAutoCapture.value)
  const audioBaseline = ref(audioSerialized.value)

  const timeoutDirty = computed(() => llmTimeoutSeconds.value !== timeoutBaseline.value)
  const contextWindowDirty = computed(() => contextWindowTokens.value !== contextWindowBaseline.value)
  const maxOutputDirty = computed(() => maxOutputTokens.value !== maxOutputBaseline.value)
  const modelCapsDirty = computed(() => (
    modelSupportsVision.value !== visionBaseline.value
    || modelSupportsVideo.value !== videoBaseline.value
  ))
  // Any per-model override change marks the provider draft dirty.
  const modelOverridesDirty = computed(() => (
    contextWindowDirty.value || maxOutputDirty.value || modelCapsDirty.value
  ))
  const captureDirty = computed(() => memoryAutoCapture.value !== captureBaseline.value)
  const audioDirty = computed(() => audioSerialized.value !== audioBaseline.value)

  // Resolve the saved per-model override for a provider+model into the raw
  // field values ('' / false = no override / inherit).
  function modelOverrideFor(
    config: PromotedConfigData,
    provider: string,
    model: string,
  ): {
    context_window?: number
    max_output_tokens?: number
    supports_vision?: boolean
    supports_video?: boolean
  } {
    const p = String(provider || '')
    const m = String(model || '')
    return (p && m ? config.models?.[p]?.[m] : undefined) || {}
  }

  function initFromConfig(config: PromotedConfigData) {
    initProviderFromConfig(config)
    initMemoryCaptureFromConfig(config)
    initAudioFromConfig(config)
  }

  function initProviderFromConfig(config: PromotedConfigData) {
    const timeout = Number(config.llm_request_timeout_seconds)
    llmTimeoutSeconds.value = Number.isFinite(timeout) && timeout >= 1 ? timeout : DEFAULT_LLM_TIMEOUT_SECONDS
    // Seed the per-model override fields from the saved provider+model overrides.
    reseedModelOverrides(
      config,
      String(config.llm?.provider || ''),
      String(config.llm?.model || ''),
    )
    timeoutBaseline.value = llmTimeoutSeconds.value
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

  function commitModelOverrideBaselines() {
    contextWindowBaseline.value = contextWindowTokens.value
    maxOutputBaseline.value = maxOutputTokens.value
    visionBaseline.value = modelSupportsVision.value
    videoBaseline.value = modelSupportsVideo.value
  }

  function setLlmTimeoutSeconds(value: number) {
    llmTimeoutSeconds.value = Number.isFinite(value) && value >= 1 ? value : DEFAULT_LLM_TIMEOUT_SECONDS
  }

  function setContextWindowTokens(value: string) {
    contextWindowTokens.value = String(value ?? '').trim()
  }

  function setMaxOutputTokens(value: string) {
    maxOutputTokens.value = String(value ?? '').trim()
  }

  function setModelCap(name: 'vision' | 'video', value: boolean) {
    if (name === 'vision') modelSupportsVision.value = Boolean(value)
    else if (name === 'video') modelSupportsVideo.value = Boolean(value)
  }

  // Reseed the per-model override fields (values + baselines) from the saved
  // overrides for a newly-selected provider/model. Called when the provider
  // changes or the model field is edited so no field ever shows a stale
  // override that belongs to a different provider+model pair.
  function reseedModelOverrides(config: PromotedConfigData, provider: string, model: string) {
    const saved = modelOverrideFor(config, provider, model)
    // Display saved counts in compact k/M form (1024000 → "1024k"); the
    // parser accepts both forms so the round-trip stays lossless.
    contextWindowTokens.value = formatTokenCountInput(
      typeof saved.context_window === 'number' && saved.context_window > 0
        ? Math.floor(saved.context_window)
        : null,
    )
    maxOutputTokens.value = formatTokenCountInput(
      typeof saved.max_output_tokens === 'number' && saved.max_output_tokens > 0
        ? Math.floor(saved.max_output_tokens)
        : null,
    )
    modelSupportsVision.value = saved.supports_vision === true
    modelSupportsVideo.value = saved.supports_video === true
    commitModelOverrideBaselines()
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

  // Deep-merge patch for the per-model overrides. Model ids contain dots and
  // colons (e.g. "qwen3:8b", "deepseek/deepseek-v4-pro"), so this CANNOT ride
  // the dot-path `patches` form — the caller must send it via config.patch's
  // deep-merge `patch` envelope. Clearing a field writes null, which deletes
  // the key on the gateway side. Capability checkboxes only force true; an
  // unchecked box deletes the override (inherit the catalog again).
  function modelOverridesPatch(providerId: string, modelId: string): Record<string, unknown> | null {
    if (!modelOverridesDirty.value) return null
    const provider = String(providerId || '').trim()
    const model = String(modelId || '').trim()
    if (!provider || !model) return null
    const fields: Record<string, unknown> = {}
    if (contextWindowDirty.value) {
      fields.context_window = parseTokenCountInput(contextWindowTokens.value)
    }
    if (maxOutputDirty.value) {
      fields.max_output_tokens = parseTokenCountInput(maxOutputTokens.value)
    }
    if (modelSupportsVision.value !== visionBaseline.value) {
      fields.supports_vision = modelSupportsVision.value ? true : null
    }
    if (modelSupportsVideo.value !== videoBaseline.value) {
      fields.supports_video = modelSupportsVideo.value ? true : null
    }
    if (!Object.keys(fields).length) return null
    return { models: { [provider]: { [model]: fields } } }
  }

  function memoryPatches(): Record<string, unknown> {
    if (!captureDirty.value) return {}
    return { 'memory.auto_capture_enabled': memoryAutoCapture.value }
  }

  function audioPayload(): ConfigureAudio {
    // Configuration implies enablement for new clients. Older clients may
    // continue sending an explicit `enabled` field to the compatible RPC.
    const params: ConfigureAudio = { providerId: audioProviderId }
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
    maxOutputTokens,
    modelSupportsVision,
    modelSupportsVideo,
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
    maxOutputDirty,
    modelCapsDirty,
    modelOverridesDirty,
    captureDirty,
    audioDirty,
    initFromConfig,
    initProviderFromConfig,
    initMemoryCaptureFromConfig,
    initAudioFromConfig,
    setLlmTimeoutSeconds,
    setContextWindowTokens,
    setMaxOutputTokens,
    setModelCap,
    reseedModelOverrides,
    setMemoryAutoCapture,
    updateAudioField,
    providerPatches,
    modelOverridesPatch,
    memoryPatches,
    audioPayload,
  }
}
