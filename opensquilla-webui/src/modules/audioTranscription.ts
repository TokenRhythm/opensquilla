import type { InjectionKey } from 'vue'

export type AudioTranscriptionErrorKind = 'unavailable' | 'aborted' | 'failed'

export class AudioTranscriptionError extends Error {
  readonly name = 'AudioTranscriptionError'

  constructor(
    readonly kind: AudioTranscriptionErrorKind,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
  }
}

export interface AudioTranscriptionOwner {
  sessionKey?: string
  sessionId?: string
  sessionEpoch?: number
}

export type SttCloudEngine = 'openai_transcriptions' | 'openai_chat'

export interface SttModelDiscovery {
  ok: boolean
  models: string[]
  failureKind?: string
}

export interface SenseVoicePrepareStatus {
  phase: 'idle' | 'checking' | 'downloading' | 'ready' | 'failed' | 'cancelled'
  resource?: string
  completedBytes?: number
  totalBytes?: number
  modelDir?: string
  error?: { resource?: string; reason?: string; status?: number; detail?: string }
}

export interface AudioTranscription {
  transcribe(input: AudioTranscriptionOwner & {
    recording: Blob
    mimeType: string
    signal?: AbortSignal
  }): Promise<string>
  /** List models served by a configured OpenAI-compatible STT endpoint. */
  discoverModels?(input: {
    engine: SttCloudEngine
    signal?: AbortSignal
  }): Promise<SttModelDiscovery>
  /** Start (or join) the pinned local SenseVoice model download. */
  startSenseVoicePrepare?(): Promise<SenseVoicePrepareStatus>
  /** Poll the local SenseVoice download progress. */
  senseVoicePrepareStatus?(): Promise<SenseVoicePrepareStatus>
  /** Cancel an in-flight local SenseVoice download. */
  cancelSenseVoicePrepare?(): Promise<SenseVoicePrepareStatus>
}

export const AUDIO_TRANSCRIPTION_KEY: InjectionKey<AudioTranscription> = Symbol('AudioTranscription')
