import { describe, it, expect } from 'vitest'
import { useSettingsPromotedForm, parseTokenCountInput } from './useSettingsPromotedForm'

// The audio TTS tuning fields (voice/model/base_url/language_code) are accepted
// and applied by the backend (mutations.upsert_audio_provider); these cover the
// UI round-trip: read from config, mark dirty on edit, send only populated fields.

describe('useSettingsPromotedForm — audio TTS fields', () => {
  it('reads tts voice/model/language_code and provider base_url from config', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      audio: {
        enabled: true,
        tts: { voice: 'voice-x', model: 'model-y', language_code: 'en-US' },
        providers: { elevenlabs: { api_key_env: 'ELEVENLABS_API_KEY', base_url: 'https://api.example.com', api_key: 'redacted' } },
      },
    })
    expect(f.audioTtsVoice.value).toBe('voice-x')
    expect(f.audioTtsModel.value).toBe('model-y')
    expect(f.audioLanguageCode.value).toBe('en-US')
    expect(f.audioBaseUrl.value).toBe('https://api.example.com')
    expect(f.audioDirty.value).toBe(false) // pristine after load
  })

  it('marks dirty and includes only populated tuning fields in the payload', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ audio: { enabled: true, providers: { elevenlabs: {} } } })
    expect(f.audioDirty.value).toBe(false)
    f.updateAudioField('ttsVoice', 'rachel')
    f.updateAudioField('languageCode', 'zh-CN')
    expect(f.audioDirty.value).toBe(true)
    const p = f.audioPayload()
    expect(p.providerId).toBe('elevenlabs')
    expect(p).not.toHaveProperty('enabled')
    expect(p.ttsVoice).toBe('rachel')
    expect(p.languageCode).toBe('zh-CN')
    expect('ttsModel' in p).toBe(false) // empty → omitted (backend keeps current)
    expect('baseUrl' in p).toBe(false)
  })

  it('omits whitespace-only tuning fields from the payload', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ audio: { enabled: true, providers: { elevenlabs: {} } } })
    f.updateAudioField('ttsModel', '   ')
    expect('ttsModel' in f.audioPayload()).toBe(false)
  })

  it('prefers a one-time pasted key over a hydrated environment reference', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      audio: {
        enabled: false,
        providers: {
          elevenlabs: {
            api_key_env: 'ELEVENLABS_API_KEY',
            base_url: 'https://api.elevenlabs.io',
          },
        },
      },
    })
    f.updateAudioField('apiKey', 'test-inline-key')

    expect(f.audioPayload()).toEqual({
      providerId: 'elevenlabs',
      apiKey: 'test-inline-key',
      baseUrl: 'https://api.elevenlabs.io',
    })
  })
})

// The per-model overrides persist through config.patch's deep-merge `patch`
// envelope because model ids ("qwen3:8b", "deepseek/deepseek-v4-pro") contain
// separators that break dot-path patches.
describe('useSettingsPromotedForm — per-model overrides', () => {
  it('seeds from the saved provider+model override and stays pristine', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: { ollama: { 'qwen3:8b': { context_window: 16384 } } },
    })
    expect(f.contextWindowTokens.value).toBe('16384')
    expect(f.modelOverridesDirty.value).toBe(false)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toBeNull() // pristine → nothing to send
  })

  it('leaves the fields blank when no override is saved for the model', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      llm: { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' },
      models: { ollama: { 'qwen3:8b': { context_window: 16384 } } },
    })
    expect(f.contextWindowTokens.value).toBe('')
    expect(f.maxOutputTokens.value).toBe('')
    expect(f.modelOverridesDirty.value).toBe(false)
  })

  it('builds a deep-merge patch keyed by the raw model id when edited', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ llm: { provider: 'ollama', model: 'qwen3:8b' } })
    f.setContextWindowTokens('32768')
    expect(f.contextWindowDirty.value).toBe(true)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { context_window: 32768 } } },
    })
  })

  it('clearing the field deletes the override with a null leaf', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: { ollama: { 'qwen3:8b': { context_window: 16384 } } },
    })
    f.setContextWindowTokens('')
    expect(f.contextWindowDirty.value).toBe(true)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { context_window: null } } },
    })
  })

  it('treats zero as clear and skips the patch without a provider or model id', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: { ollama: { 'qwen3:8b': { context_window: 16384 } } },
    })
    f.setContextWindowTokens('0')
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { context_window: null } } },
    })
    expect(f.modelOverridesPatch('ollama', '')).toBeNull()
    expect(f.modelOverridesPatch('', 'qwen3:8b')).toBeNull()
  })

  it('sends max_output_tokens overrides through the same patch envelope', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ llm: { provider: 'ollama', model: 'qwen3:8b' } })
    f.setMaxOutputTokens('4096')
    expect(f.maxOutputDirty.value).toBe(true)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { max_output_tokens: 4096 } } },
    })
  })

  it('capability checkboxes only force true; unchecking deletes the override', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: {
        ollama: { 'qwen3:8b': { supports_vision: true, supports_video: true } },
      },
    })
    expect(f.modelSupportsVision.value).toBe(true)
    expect(f.modelSupportsVideo.value).toBe(true)
    expect(f.modelCapsDirty.value).toBe(false)

    // Unchecking a saved-true capability deletes the stored override.
    f.setModelCap('video', false)
    expect(f.modelCapsDirty.value).toBe(true)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { supports_video: null } } },
    })

    // Re-checking restores the baseline, so the draft is pristine again.
    f.setModelCap('video', true)
    expect(f.modelCapsDirty.value).toBe(false)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toBeNull()
  })

  it('checking a capability that is not saved forces the override on', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ llm: { provider: 'ollama', model: 'qwen3:8b' } })
    f.setModelCap('vision', true)
    f.setModelCap('video', true)
    expect(f.modelCapsDirty.value).toBe(true)
    expect(f.modelOverridesPatch('ollama', 'qwen3:8b')).toEqual({
      models: { ollama: { 'qwen3:8b': { supports_vision: true, supports_video: true } } },
    })
  })

  it('reloading config rebaselines the fields so saved values are no longer dirty', () => {
    const f = useSettingsPromotedForm()
    f.initFromConfig({ llm: { provider: 'ollama', model: 'qwen3:8b' } })
    f.setContextWindowTokens('32768')
    f.setMaxOutputTokens('4096')
    expect(f.modelOverridesDirty.value).toBe(true)
    f.initFromConfig({
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: {
        ollama: { 'qwen3:8b': { context_window: 32768, max_output_tokens: 4096 } },
      },
    })
    expect(f.contextWindowTokens.value).toBe('32768')
    expect(f.maxOutputTokens.value).toBe('4096')
    expect(f.modelOverridesDirty.value).toBe(false)
  })

  it('reseeds values + baselines from the saved overrides for a switched provider+model', () => {
    const f = useSettingsPromotedForm()
    const config = {
      llm: { provider: 'ollama', model: 'qwen3:8b' },
      models: {
        ollama: { 'qwen3:8b': { context_window: 16384, supports_vision: true } },
        vllm: { 'meta/llama-4': { context_window: 65536, supports_video: true } },
      },
    }
    f.initFromConfig(config)
    expect(f.contextWindowTokens.value).toBe('16384')
    expect(f.modelSupportsVision.value).toBe(true)

    // Provider switch: reseed from the new provider+model override, pristine.
    f.reseedModelOverrides(config, 'vllm', 'meta/llama-4')
    expect(f.contextWindowTokens.value).toBe('65536')
    expect(f.modelSupportsVision.value).toBe(false)
    expect(f.modelSupportsVideo.value).toBe(true)
    expect(f.modelOverridesDirty.value).toBe(false)

    // Model switch to one with no saved override: clears everything, pristine.
    f.reseedModelOverrides(config, 'vllm', 'meta/llama-4-mini')
    expect(f.contextWindowTokens.value).toBe('')
    expect(f.modelSupportsVideo.value).toBe(false)
    expect(f.modelOverridesDirty.value).toBe(false)
  })
})

describe('parseTokenCountInput', () => {
  it('returns a floored positive integer or null for blank/zero/non-numeric', () => {
    expect(parseTokenCountInput('16384')).toBe(16384)
    expect(parseTokenCountInput('32768.9')).toBe(32768)
    expect(parseTokenCountInput('')).toBeNull()
    expect(parseTokenCountInput('0')).toBeNull()
    expect(parseTokenCountInput('-5')).toBeNull()
    expect(parseTokenCountInput('abc')).toBeNull()
  })
})
