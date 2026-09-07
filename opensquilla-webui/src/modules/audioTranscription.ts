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

export interface AudioTranscription {
  transcribe(input: {
    recording: Blob
    mimeType: string
    signal?: AbortSignal
  }): Promise<string>
}

export const AUDIO_TRANSCRIPTION_KEY: InjectionKey<AudioTranscription> = Symbol('AudioTranscription')
