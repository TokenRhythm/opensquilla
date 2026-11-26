import { ref, type Ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import type { AppSettings } from '@/modules/appSettings'
import type { SttCloudEngine, SttModelDiscovery } from '@/modules/audioTranscription'

// The recognition engines the gateway's [audio.stt] chain understands, in
// test-friendliness order (local offline first, then the cloud backends).
export const VOICE_ENGINE_IDS = [
  'sensevoice',
  'elevenlabs',
  'openai_transcriptions',
  'openai_chat',
] as const
export type VoiceEngineId = (typeof VOICE_ENGINE_IDS)[number]

export function isVoiceEngineId(value: string): value is VoiceEngineId {
  return (VOICE_ENGINE_IDS as readonly string[]).includes(value)
}

// One entry per unique OpenAI-compatible endpoint: when both cloud engines
// point at the same base URL they are one platform in the picker, and the
// available models are fetched once for it.
export interface VoiceCloudPlatform {
  key: string
  label: string
  baseUrl: string
  engines: SttCloudEngine[]
  hasKey: boolean
}

export interface VoiceCloudModelState {
  status: 'loading' | 'ready' | 'error' | 'nokey'
  models: string[]
}

export interface UseVoiceEngineOptions {
  appSettings: AppSettings
  connectionAvailable?: Readonly<Ref<boolean>>
  discoverModels?: (input: {
    engine: SttCloudEngine
    signal?: AbortSignal
  }) => Promise<SttModelDiscovery>
}

const DEFAULT_OPENAI_BASE_URL = 'https://api.openai.com/v1'

// Model ids that speak the multipart /audio/transcriptions protocol; anything
// else on a shared platform is assumed to be chat-style input_audio ASR.
const TRANSCRIPTION_MODEL_RE = /whisper|transcribe|sensevoice|paraformer|funasr|\bstt\b/i

// Recognition surfaces must only offer speech-to-text models: a platform's
// /models catalog also lists LLM and TTS entries. Prefer ids with an explicit
// ASR marker; when none carry one, fall back to dropping the clearly non-ASR
// families (TTS / voice design / embeddings / image) rather than hiding an
// endpoint whose ASR model uses an unrecognizable name.
const ASR_MODEL_RE = /\b(?:asr|stt)\b|whisper|transcribe|sensevoice|paraformer|funasr|speech[-_]?to[-_]?text|audio[-_]?input|gpt-4o[^/]*audio/i
const NON_ASR_MODEL_RE = /\btts\b|text[-_]?to[-_]?speech|voice[-_]?clone|voice[-_]?design|voicedesign|voiceclone|embedding|rerank|dall[-_]?e|\bimage\b/i

export function filterAsrModels(models: string[]): string[] {
  const asr = models.filter(id => ASR_MODEL_RE.test(id))
  if (asr.length) return asr
  return models.filter(id => !NON_ASR_MODEL_RE.test(id))
}

function normalizeBaseUrl(value: string): string {
  const trimmed = String(value || '').trim().replace(/\/+$/, '')
  return trimmed || DEFAULT_OPENAI_BASE_URL
}

function platformLabel(baseUrl: string): string {
  try {
    return new URL(baseUrl).hostname || baseUrl
  } catch {
    return baseUrl
  }
}

export function pickCloudEngine(
  platform: VoiceCloudPlatform,
  modelId: string,
): SttCloudEngine {
  if (platform.engines.length === 1) return platform.engines[0]
  const preferred: SttCloudEngine = TRANSCRIPTION_MODEL_RE.test(modelId)
    ? 'openai_transcriptions'
    : 'openai_chat'
  return platform.engines.includes(preferred) ? preferred : platform.engines[0]
}

// Composer-level recognition-engine switch. Reads the saved [audio.stt]
// provider and persists a change through the same config.patch dot-path the
// settings capabilities card uses; the gateway rebuilds the transcription
// chain per request, so no restart is needed.
export function useVoiceEngine(options: UseVoiceEngineOptions) {
  const { pushToast } = useToasts()
  const voiceEngineId = ref<VoiceEngineId | ''>('')
  const voiceEngineBusy = ref(false)
  // Model each engine actually transcribes with: fixed for the local and
  // ElevenLabs backends, operator-configured for the OpenAI-compatible ones.
  // The voice menu shows these instead of wire-protocol endpoint paths.
  const voiceEngineModels = ref<Record<string, string>>({})
  // Which fixed engines are actually configured: SenseVoice needs a model
  // directory, ElevenLabs needs a key. Unconfigured engines stay out of the
  // picker entirely.
  const voiceEngineConfigured = ref<Record<string, boolean>>({})
  const voiceCloudPlatforms = ref<VoiceCloudPlatform[]>([])
  const voiceCloudModels = ref<Record<string, VoiceCloudModelState>>({})
  let generation = 0
  let cloudSections: Partial<Record<SttCloudEngine, { baseUrl: string; model: string; hasKey: boolean }>> = {}

  async function discoverPlatformModels(platform: VoiceCloudPlatform, request: number): Promise<void> {
    if (!options.discoverModels) {
      voiceCloudModels.value = { ...voiceCloudModels.value, [platform.key]: { status: 'error', models: [] } }
      return
    }
    if (!platform.hasKey) {
      voiceCloudModels.value = { ...voiceCloudModels.value, [platform.key]: { status: 'nokey', models: [] } }
      return
    }
    voiceCloudModels.value = { ...voiceCloudModels.value, [platform.key]: { status: 'loading', models: [] } }
    const engine = platform.engines[0]
    try {
      const result = await options.discoverModels({ engine })
      if (request !== generation) return
      voiceCloudModels.value = {
        ...voiceCloudModels.value,
        [platform.key]: result.ok
          ? { status: 'ready', models: filterAsrModels(result.models) }
          : { status: 'error', models: [] },
      }
    } catch {
      if (request !== generation) return
      voiceCloudModels.value = { ...voiceCloudModels.value, [platform.key]: { status: 'error', models: [] } }
    }
  }

  async function loadVoiceEngine(): Promise<void> {
    if (options.connectionAvailable?.value === false) return
    const request = ++generation
    try {
      const config = await options.appSettings.readAll()
      if (request !== generation) return
      const stt = (config as {
        audio?: {
          providers?: { elevenlabs?: { api_key?: string } }
          stt?: {
            provider?: string
            sensevoice?: { model_dir?: string }
            openai_transcriptions?: { base_url?: string; model?: string; api_key?: string }
            openai_chat?: { base_url?: string; model?: string; api_key?: string }
          }
        }
      })?.audio
      const sttSection = stt?.stt
      const provider = String(sttSection?.provider || '').trim()
      voiceEngineId.value = isVoiceEngineId(provider) ? provider : 'elevenlabs'
      voiceEngineConfigured.value = {
        sensevoice: Boolean(String(sttSection?.sensevoice?.model_dir || '').trim()),
        elevenlabs: Boolean(String(stt?.providers?.elevenlabs?.api_key || '').trim()),
      }
      voiceEngineModels.value = {
        sensevoice: 'SenseVoiceSmall',
        elevenlabs: 'scribe_v2',
        openai_transcriptions: String(sttSection?.openai_transcriptions?.model || '').trim(),
        openai_chat: String(sttSection?.openai_chat?.model || '').trim(),
      }
      cloudSections = {}
      const grouped = new Map<string, VoiceCloudPlatform>()
      for (const engine of ['openai_transcriptions', 'openai_chat'] as const) {
        const section = sttSection?.[engine]
        const baseUrl = normalizeBaseUrl(String(section?.base_url || ''))
        const hasKey = Boolean(String(section?.api_key || '').trim())
        cloudSections[engine] = {
          baseUrl,
          model: String(section?.model || '').trim(),
          hasKey,
        }
        const existing = grouped.get(baseUrl)
        if (existing) {
          existing.engines.push(engine)
          existing.hasKey = existing.hasKey || hasKey
        } else {
          grouped.set(baseUrl, {
            key: baseUrl,
            label: platformLabel(baseUrl),
            baseUrl,
            engines: [engine],
            hasKey,
          })
        }
      }
      // Only configured platforms appear in the picker; an endpoint without
      // a key is something to set up in Settings, not a picker entry.
      voiceCloudPlatforms.value = [...grouped.values()].filter(platform => platform.hasKey)
      // Fetch each platform's model list in the background; the menu renders
      // immediately and rows update as discovery lands.
      for (const platform of voiceCloudPlatforms.value) {
        void discoverPlatformModels(platform, request)
      }
    } catch {
      // Engine discovery is an enhancement; recording keeps the saved engine.
    }
  }

  async function setVoiceEngine(id: VoiceEngineId): Promise<void> {
    if (voiceEngineBusy.value || id === voiceEngineId.value) return
    const previous = voiceEngineId.value
    const request = generation
    // Optimistic presentation; the patch response stays authoritative.
    voiceEngineId.value = id
    voiceEngineBusy.value = true
    try {
      await options.appSettings.patch([{ path: 'audio.stt.provider', value: id }])
    } catch (err) {
      if (request === generation) voiceEngineId.value = previous
      console.warn('Failed to switch recognition engine:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t('chat.voiceEngineUpdateFailed'), { tone: 'danger' })
    } finally {
      voiceEngineBusy.value = false
    }
  }

  // Selecting a model on a (possibly merged) cloud platform persists both the
  // engine choice and the model id in one patch.
  async function selectCloudModel(platformKey: string, modelId: string): Promise<void> {
    const platform = voiceCloudPlatforms.value.find(item => item.key === platformKey)
    if (!platform || voiceEngineBusy.value || !modelId) return
    const engine = pickCloudEngine(platform, modelId)
    const previousEngine = voiceEngineId.value
    const previousModel = voiceEngineModels.value[engine] || ''
    const request = generation
    voiceEngineId.value = engine
    voiceEngineModels.value = { ...voiceEngineModels.value, [engine]: modelId }
    voiceEngineBusy.value = true
    try {
      await options.appSettings.patch([
        { path: 'audio.stt.provider', value: engine },
        { path: `audio.stt.${engine}.model`, value: modelId },
      ])
    } catch (err) {
      if (request === generation) {
        voiceEngineId.value = previousEngine
        voiceEngineModels.value = { ...voiceEngineModels.value, [engine]: previousModel }
      }
      console.warn('Failed to select recognition model:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t('chat.voiceEngineUpdateFailed'), { tone: 'danger' })
    } finally {
      voiceEngineBusy.value = false
    }
  }

  return {
    voiceEngineId,
    voiceEngineBusy,
    voiceEngineModels,
    voiceEngineConfigured,
    voiceCloudPlatforms,
    voiceCloudModels,
    loadVoiceEngine,
    setVoiceEngine,
    selectCloudModel,
  }
}