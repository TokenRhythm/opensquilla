// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { pickCloudEngine, filterAsrModels, useVoiceEngine, type VoiceCloudPlatform } from './useVoiceEngine'
import type { AppSettings } from '@/modules/appSettings'

const { toasts } = useToasts()

function stubSettings(config: Record<string, unknown>, patchImpl?: () => Promise<unknown>) {
  const readAll = vi.fn(async () => config)
  const patch = vi.fn(patchImpl ?? (async () => ({ patched: ['audio.stt.provider'] })))
  const appSettings = { readAll, patch } as unknown as AppSettings
  return { appSettings, readAll, patch }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  toasts.value = []
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useVoiceEngine', () => {
  it('loads the saved recognition engine from config', async () => {
    const { appSettings } = stubSettings({ audio: { stt: { provider: 'sensevoice' } } })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    expect(engine.voiceEngineId.value).toBe('sensevoice')
  })

  it('falls back to elevenlabs for unknown or missing providers', async () => {
    const { appSettings } = stubSettings({ audio: { stt: { provider: 'mystery' } } })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    expect(engine.voiceEngineId.value).toBe('elevenlabs')
  })

  it('skips the load while the gateway is unavailable', async () => {
    const { appSettings, readAll } = stubSettings({})
    const engine = useVoiceEngine({ appSettings, connectionAvailable: ref(false) })
    await engine.loadVoiceEngine()
    expect(readAll).not.toHaveBeenCalled()
  })

  it('persists a switch through the audio.stt.provider dot-path', async () => {
    const { appSettings, patch } = stubSettings({ audio: { stt: { provider: 'elevenlabs' } } })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    await engine.setVoiceEngine('openai_chat')
    expect(patch).toHaveBeenCalledWith([{ path: 'audio.stt.provider', value: 'openai_chat' }])
    expect(engine.voiceEngineId.value).toBe('openai_chat')
    expect(engine.voiceEngineBusy.value).toBe(false)
    expect(toasts.value).toHaveLength(0)
  })

  it('ignores a switch to the already-selected engine', async () => {
    const { appSettings, patch } = stubSettings({ audio: { stt: { provider: 'sensevoice' } } })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    await engine.setVoiceEngine('sensevoice')
    expect(patch).not.toHaveBeenCalled()
  })

  it('rolls back and toasts when the patch fails', async () => {
    const { appSettings } = stubSettings(
      { audio: { stt: { provider: 'elevenlabs' } } },
      async () => { throw new Error('gateway offline') },
    )
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    await engine.setVoiceEngine('sensevoice')
    expect(engine.voiceEngineId.value).toBe('elevenlabs')
    expect(engine.voiceEngineBusy.value).toBe(false)
    expect(toasts.value.map(toast => toast.message)).toEqual([
      i18n.global.t('chat.voiceEngineUpdateFailed'),
    ])
  })
})

describe('useVoiceEngine cloud platforms', () => {
  const sharedUrlConfig = {
    audio: {
      stt: {
        provider: 'openai_transcriptions',
        openai_transcriptions: { base_url: 'https://api.siliconflow.cn/v1/', api_key: '[redacted]', model: 'whisper-1' },
        openai_chat: { base_url: 'https://api.siliconflow.cn/v1', api_key: '[redacted]', model: '' },
      },
    },
  }

  it('merges engines that share one base URL into a single platform', async () => {
    const { appSettings } = stubSettings(sharedUrlConfig)
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    expect(engine.voiceCloudPlatforms.value).toHaveLength(1)
    const platform = engine.voiceCloudPlatforms.value[0]
    expect(platform.key).toBe('https://api.siliconflow.cn/v1')
    expect(platform.label).toBe('api.siliconflow.cn')
    expect(platform.engines).toEqual(['openai_transcriptions', 'openai_chat'])
    expect(platform.hasKey).toBe(true)
  })

  it('keeps distinct URLs as separate platforms', async () => {
    const { appSettings } = stubSettings({
      audio: {
        stt: {
          provider: 'elevenlabs',
          openai_transcriptions: { base_url: 'https://api.openai.com/v1', api_key: '[redacted]' },
          openai_chat: { base_url: 'https://api.xiaomimimo.com/v1', api_key: '[redacted]' },
        },
      },
    })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    expect(engine.voiceCloudPlatforms.value).toHaveLength(2)
  })

  it('fetches the model list once per platform', async () => {
    const { appSettings } = stubSettings(sharedUrlConfig)
    const discoverModels = vi.fn(async () => ({ ok: true, models: ['whisper-1', 'SenseVoiceSmall'] }))
    const engine = useVoiceEngine({ appSettings, discoverModels })
    await engine.loadVoiceEngine()
    await vi.waitFor(() => {
      expect(engine.voiceCloudModels.value['https://api.siliconflow.cn/v1']?.status).toBe('ready')
    })
    expect(discoverModels).toHaveBeenCalledTimes(1)
    expect(engine.voiceCloudModels.value['https://api.siliconflow.cn/v1'].models)
      .toEqual(['whisper-1', 'SenseVoiceSmall'])
  })

  it('hides keyless platforms from the picker without fetching', async () => {
    const { appSettings } = stubSettings({
      audio: { stt: { provider: 'elevenlabs', openai_transcriptions: { base_url: 'https://api.openai.com/v1' } } },
    })
    const discoverModels = vi.fn()
    const engine = useVoiceEngine({ appSettings, discoverModels })
    await engine.loadVoiceEngine()
    expect(engine.voiceCloudPlatforms.value).toHaveLength(0)
    expect(discoverModels).not.toHaveBeenCalled()
  })

  it('flags fixed engines as configured only when their setup exists', async () => {
    const { appSettings } = stubSettings({
      audio: {
        providers: { elevenlabs: { api_key: '[redacted]' } },
        stt: { provider: 'sensevoice', sensevoice: { model_dir: 'D:\\models\\sv' } },
      },
    })
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    expect(engine.voiceEngineConfigured.value).toEqual({ sensevoice: true, elevenlabs: true })

    const bare = useVoiceEngine({ appSettings: stubSettings({ audio: { stt: { provider: 'elevenlabs' } } }).appSettings })
    await bare.loadVoiceEngine()
    expect(bare.voiceEngineConfigured.value).toEqual({ sensevoice: false, elevenlabs: false })
  })

  it('selecting a model persists the engine and model in one patch', async () => {
    const { appSettings, patch } = stubSettings(sharedUrlConfig)
    const engine = useVoiceEngine({ appSettings })
    await engine.loadVoiceEngine()
    await engine.selectCloudModel('https://api.siliconflow.cn/v1', 'whisper-large-v3')
    expect(patch).toHaveBeenCalledWith([
      { path: 'audio.stt.provider', value: 'openai_transcriptions' },
      { path: 'audio.stt.openai_transcriptions.model', value: 'whisper-large-v3' },
    ])
    expect(engine.voiceEngineId.value).toBe('openai_transcriptions')
    expect(engine.voiceEngineModels.value.openai_transcriptions).toBe('whisper-large-v3')
  })
})

describe('pickCloudEngine', () => {
  const merged: VoiceCloudPlatform = {
    key: 'https://api.example.com/v1',
    label: 'api.example.com',
    baseUrl: 'https://api.example.com/v1',
    engines: ['openai_transcriptions', 'openai_chat'],
    hasKey: true,
  }

  it('routes transcription-style model ids to the transcriptions engine', () => {
    expect(pickCloudEngine(merged, 'whisper-1')).toBe('openai_transcriptions')
    expect(pickCloudEngine(merged, 'gpt-4o-mini-transcribe')).toBe('openai_transcriptions')
    expect(pickCloudEngine(merged, 'SenseVoiceSmall')).toBe('openai_transcriptions')
  })

  it('routes other model ids to the chat engine', () => {
    expect(pickCloudEngine(merged, 'mimo-v2.5-asr')).toBe('openai_chat')
    expect(pickCloudEngine(merged, 'gpt-4o-audio-preview')).toBe('openai_chat')
  })

  it('uses the only configured engine when the platform is not merged', () => {
    const single: VoiceCloudPlatform = { ...merged, engines: ['openai_chat'] }
    expect(pickCloudEngine(single, 'whisper-1')).toBe('openai_chat')
  })
})

describe('filterAsrModels', () => {
  it('keeps only ASR models when the catalog marks them (MiMo shape)', () => {
    expect(filterAsrModels([
      'mimo-v2.5-asr',
      'mimo-v2.5-tts',
      'mimo-v2.5-tts-voiceclone',
      'mimo-v2.5-tts-voicedesign',
      'mimo-v2.6-flash',
      'mimo-v2.6-pro',
      'mimo-v2.6-pro-ultraspeed',
    ])).toEqual(['mimo-v2.5-asr'])
  })

  it('keeps transcription models on OpenAI-style catalogs', () => {
    expect(filterAsrModels([
      'whisper-1',
      'gpt-4o-transcribe',
      'gpt-4o-mini-transcribe',
      'gpt-4o-audio-preview',
      'tts-1',
      'tts-1-hd',
      'gpt-4o',
      'text-embedding-3-small',
      'dall-e-3',
    ])).toEqual(['whisper-1', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe', 'gpt-4o-audio-preview'])
  })

  it('falls back to dropping only explicit non-ASR families when nothing is marked', () => {
    expect(filterAsrModels(['my-custom-model', 'some-tts-voice'])).toEqual(['my-custom-model'])
  })
})