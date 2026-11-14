import { computed, ref } from 'vue'
import type { ConfigureAudio } from '@/modules/setupWorkflow'

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

// The two OpenAI-compatible cloud engines own their credentials under
// [audio.stt.<engine>] — separate from the ElevenLabs generation key.
export function isCloudSttProvider(provider: string): boolean {
  return provider === 'openai_transcriptions' || provider === 'openai_chat'
}

interface PromotedConfigData {
  llm_request_timeout_seconds?: number
  llm?: { provider?: string; model?: string; thinking?: string }
  // Per-provider/per-model overrides. Model ids contain dots and colons, so
  // this subtree is written with deep-merge patches, never dot-path patches.
  models?: Record<string, Record<string, { context_window?: number }>>
  memory?: { auto_capture_enabled?: boolean }
  audio?: {
    enabled?: boolean
    tts?: { voice?: string; model?: string; language_code?: string; preset?: string }
    providers?: Record<string, { api_key?: string; api_key_env?: string; base_url?: string }>
    stt?: {
      provider?: string
      language?: string
      sensevoice?: { model_dir?: string }
      openai_transcriptions?: { base_url?: string; api_key?: string; model?: string }
      openai_chat?: { base_url?: string; api_key?: string; model?: string }
    }
    presets?: Record<string, { label?: string; voice_id?: string }>
  }
}

export function useSettingsPromotedForm() {
  const llmTimeoutSeconds = ref(DEFAULT_LLM_TIMEOUT_SECONDS)
  // Global thinking level for the direct/primary model ('' = unset default).
  const llmThinking = ref('')
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
  // STT engine switch: which backend POST /api/audio/transcribe routes to.
  // Persisted with the audio save as a dot-path config patch, not through the
  // ElevenLabs provider mutation; the gateway reloads the chain per request.
  const sttProvider = ref('elevenlabs')
  const sttModelDir = ref('')
  // Cloud-engine credentials (openai_transcriptions / openai_chat). The key
  // is a one-time paste like audioApiKey: config.get redacts stored secrets,
  // so presence alone drives the "configured" hint and the field stays blank.
  const sttCloudApiKey = ref('')
  const sttCloudBaseUrl = ref('')
  const sttCloudModel = ref('')
  const sttCloudKeyConfigured = ref(false)
  // Last loaded audio config, kept so switching between the two cloud engines
  // can rehydrate that engine's own saved section without a config round-trip.
  const sttAudioConfigCache = ref<PromotedConfigData['audio'] | null>(null)
  // Baselines are refs, not plain objects: a dirty computed must be able to
  // track a rebase (acceptSavedStt / initAudioFromConfig) that happens between
  // its reads.
  const sttBaseline = ref({ provider: 'elevenlabs', modelDir: '', cloudBaseUrl: '', cloudModel: '' })
  // Generation-axis preset selection (PRD F3.6): '' = raw [audio.tts] fields;
  // a name switches default TTS output (and, later, pet speech) to that pack.
  const audioPreset = ref('')
  // Must be a ref: sttDirty reads it, and acceptSavedPreset rebaselines it
  // without changing audioPreset.value — a plain let would leave the computed
  // serving a stale cached "dirty" result.
  const presetBaseline = ref('')
  const audioProviderId = [101, 108, 101, 118, 101, 110, 108, 97, 98, 115]
    .map(code => String.fromCharCode(code))
    .join('')

  const audioSerialized = computed(() => JSON.stringify([
    audioEnabled.value, audioApiKey.value, audioApiKeyEnv.value,
    audioBaseUrl.value, audioTtsVoice.value, audioTtsModel.value, audioLanguageCode.value,
  ]))
  // Dirty state for the STT switch and the generation-axis preset choice. The
  // comparison is plain reactivity: all fields are refs and every write path
  // (edit, init, accept) goes through them.
  const sttDirty = computed(() => (
    sttProvider.value !== sttBaseline.value.provider
    || sttModelDir.value.trim() !== sttBaseline.value.modelDir
    || (isCloudSttProvider(sttProvider.value) && (
      sttCloudApiKey.value !== ''
      || sttCloudBaseUrl.value.trim() !== sttBaseline.value.cloudBaseUrl
      || sttCloudModel.value.trim() !== sttBaseline.value.cloudModel
    ))
    || audioPreset.value !== presetBaseline.value
  ))

  // Seed from the initial state so the pristine form is never dirty while config loads.
  const timeoutBaseline = ref(llmTimeoutSeconds.value)
  const thinkingBaseline = ref(llmThinking.value)
  const contextWindowBaseline = ref(contextWindowTokens.value)
  const captureBaseline = ref(memoryAutoCapture.value)
  const audioBaseline = ref(audioSerialized.value)

  // The STT engine choice is a draft on its own: a pending switch must keep
  // both audio-axis cards (识别 / 生成) visible as drafts until Save.
  const sttDraftPending = computed(() => sttDirty.value)

  const timeoutDirty = computed(() => llmTimeoutSeconds.value !== timeoutBaseline.value)
  const thinkingDirty = computed(() => llmThinking.value !== thinkingBaseline.value)
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

  function initProviderFromConfig(config: PromotedConfigData, preserveThinking = false) {
    const timeout = Number(config.llm_request_timeout_seconds)
    llmTimeoutSeconds.value = Number.isFinite(timeout) && timeout >= 1 ? timeout : DEFAULT_LLM_TIMEOUT_SECONDS
    if (!preserveThinking) acceptLlmThinking(config.llm?.thinking)
    // Seed the context-window field from the saved provider+model override.
    contextWindowTokens.value = contextWindowOverrideFor(
      config,
      String(config.llm?.provider || ''),
      String(config.llm?.model || ''),
    )
    timeoutBaseline.value = llmTimeoutSeconds.value
    contextWindowBaseline.value = contextWindowTokens.value
  }

  function initMemoryCaptureFromConfig(config: PromotedConfigData) {
    memoryAutoCapture.value = config.memory?.auto_capture_enabled !== false
    captureBaseline.value = memoryAutoCapture.value
  }

  // Reseed the cloud fields from the cached config section of the given
  // engine. Each cloud engine owns its credentials, so switching engines
  // shows that engine's saved state and drops any unsaved paste.
  function hydrateCloudSttFields(provider: string) {
    const stt = sttAudioConfigCache.value?.stt
    const section = (provider === 'openai_transcriptions' ? stt?.openai_transcriptions
      : provider === 'openai_chat' ? stt?.openai_chat
        : undefined) || {}
    sttCloudBaseUrl.value = String(section.base_url || '')
    sttCloudModel.value = String(section.model || '')
    sttCloudKeyConfigured.value = Boolean(section.api_key)
    sttCloudApiKey.value = ''
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
    sttProvider.value = String(config.audio?.stt?.provider || '').trim() || 'elevenlabs'
    sttModelDir.value = String(config.audio?.stt?.sensevoice?.model_dir || '')
    sttAudioConfigCache.value = config.audio || null
    hydrateCloudSttFields(sttProvider.value)
    audioPreset.value = String(config.audio?.tts?.preset || '').trim()
    sttBaseline.value = {
      provider: sttProvider.value,
      modelDir: sttModelDir.value.trim(),
      cloudBaseUrl: sttCloudBaseUrl.value.trim(),
      cloudModel: sttCloudModel.value.trim(),
    }
    presetBaseline.value = audioPreset.value

    audioBaseline.value = audioSerialized.value
  }

  // Reseed the STT switch from freshly loaded config (called after a save
  // reload so the next reload does not preserve a just-persisted draft).
  function acceptSavedStt(
    provider: string,
    modelDir: string,
    cloud?: { baseUrl: string; model: string; keySaved: boolean },
  ) {
    sttProvider.value = String(provider || '').trim() || 'elevenlabs'
    sttModelDir.value = String(modelDir || '')
    if (cloud) {
      sttCloudBaseUrl.value = cloud.baseUrl
      sttCloudModel.value = cloud.model
      sttCloudApiKey.value = ''
      if (cloud.keySaved) sttCloudKeyConfigured.value = true
      // Keep the engine-switch cache in sync so toggling engines before the
      // post-save config reload still shows the just-persisted state.
      if (isCloudSttProvider(sttProvider.value) && sttAudioConfigCache.value) {
        const stt = { ...(sttAudioConfigCache.value.stt || {}) }
        const section = {
          base_url: cloud.baseUrl,
          model: cloud.model,
          api_key: sttCloudKeyConfigured.value ? '[redacted]' : '',
        }
        if (sttProvider.value === 'openai_transcriptions') stt.openai_transcriptions = section
        else stt.openai_chat = section
        sttAudioConfigCache.value = { ...sttAudioConfigCache.value, stt }
      }
    }
    sttBaseline.value = {
      provider: sttProvider.value,
      modelDir: sttModelDir.value.trim(),
      cloudBaseUrl: sttCloudBaseUrl.value.trim(),
      cloudModel: sttCloudModel.value.trim(),
    }
  }

  // Reseed the generation-axis preset from its just-persisted value.
  function acceptSavedPreset(preset: string) {
    audioPreset.value = String(preset || '').trim()
    presetBaseline.value = audioPreset.value
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
    else if (key === 'sttProvider') {
      sttProvider.value = String(value)
      // Each cloud engine has its own saved section; rehydrate on switch and
      // rebaseline the cloud fields so the switch itself is the only draft.
      hydrateCloudSttFields(sttProvider.value)
      sttBaseline.value = {
        ...sttBaseline.value,
        cloudBaseUrl: sttCloudBaseUrl.value.trim(),
        cloudModel: sttCloudModel.value.trim(),
      }
    }
    else if (key === 'sttModelDir') sttModelDir.value = String(value)
    else if (key === 'sttCloudApiKey') sttCloudApiKey.value = String(value)
    else if (key === 'sttCloudBaseUrl') sttCloudBaseUrl.value = String(value)
    else if (key === 'sttCloudModel') sttCloudModel.value = String(value)
    else if (key === 'preset') audioPreset.value = String(value)
  }

  function providerPatches(): Record<string, unknown> {
    if (!timeoutDirty.value) return {}
    return { llm_request_timeout_seconds: llmTimeoutSeconds.value }
  }

  function acceptLlmThinking(value: unknown) {
    llmThinking.value = String(value ?? '').trim()
    thinkingBaseline.value = llmThinking.value
  }

  function setLlmThinking(value: string) {
    llmThinking.value = value
  }

  function resetLlmThinking() {
    llmThinking.value = thinkingBaseline.value
  }

  // Dot-path patch for the global thinking level. Clearing the select ('')
  // writes null, which deletes the llm.thinking key back to the unset default
  // (the same clear semantics the context-window override uses). Rides the
  // provider-save snapshot so it persists with the same save action.
  function thinkingPatch(): Record<string, unknown> | null {
    if (!thinkingDirty.value) return null
    return { 'llm.thinking': llmThinking.value.trim() || null }
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

  // Dot-path patches for the STT switch and the preset selection. The gateway
  // rebuilds the provider chain per transcription request, so this needs no
  // restart. model_dir and preset are plain `str` config fields, so clearing
  // them writes "" (the schema default) rather than null — pydantic rejects
  // null for non-nullable strings, and "" already means "unset / default".
  function sttPatches(): Record<string, unknown> {
    if (!sttDirty.value) return {}
    const patches: Record<string, unknown> = {}
    const providerChanged = sttProvider.value !== sttBaseline.value.provider
    const modelDirChanged = sttModelDir.value.trim() !== sttBaseline.value.modelDir
    const cloud = isCloudSttProvider(sttProvider.value)
    const cloudBaseUrl = sttCloudBaseUrl.value.trim()
    const cloudModel = sttCloudModel.value.trim()
    const cloudChanged = cloud && (
      sttCloudApiKey.value !== ''
      || cloudBaseUrl !== sttBaseline.value.cloudBaseUrl
      || cloudModel !== sttBaseline.value.cloudModel
    )
    if (providerChanged || modelDirChanged || cloudChanged) {
      patches['audio.stt.provider'] = sttProvider.value
      if (sttProvider.value === 'sensevoice') {
        patches['audio.stt.sensevoice.model_dir'] = sttModelDir.value.trim()
      }
      if (cloud) {
        // Plain str fields: "" restores the schema default (the provider
        // falls back to api.openai.com), never null. The key is only sent
        // when the operator pasted a new one; blank means "keep stored".
        if (providerChanged || cloudBaseUrl !== sttBaseline.value.cloudBaseUrl) {
          patches[`audio.stt.${sttProvider.value}.base_url`] = cloudBaseUrl
        }
        if (providerChanged || cloudModel !== sttBaseline.value.cloudModel) {
          patches[`audio.stt.${sttProvider.value}.model`] = cloudModel
        }
        if (sttCloudApiKey.value) {
          patches[`audio.stt.${sttProvider.value}.api_key`] = sttCloudApiKey.value
        }
      }
    }
    if (audioPreset.value !== presetBaseline.value) {
      patches['audio.tts.preset'] = audioPreset.value.trim()
    }
    return patches
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
    llmThinking,
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
    sttProvider,
    sttModelDir,
    sttCloudApiKey,
    sttCloudBaseUrl,
    sttCloudModel,
    sttCloudKeyConfigured,
    sttSavedProvider: computed(() => sttBaseline.value.provider),
    audioPreset,
    timeoutDirty,
    thinkingDirty,
    contextWindowDirty,
    captureDirty,
    audioDirty,
    sttDirty,
    sttDraftPending,
    initFromConfig,
    initProviderFromConfig,
    initMemoryCaptureFromConfig,
    initAudioFromConfig,
    setLlmTimeoutSeconds,
    setLlmThinking,
    acceptLlmThinking,
    resetLlmThinking,
    setContextWindowTokens,
    reseedContextWindow,
    setMemoryAutoCapture,
    updateAudioField,
    acceptSavedStt,
    acceptSavedPreset,
    providerPatches,
    thinkingPatch,
    contextWindowPatch,
    memoryPatches,
    audioPayload,
    sttPatches,
  }
}
