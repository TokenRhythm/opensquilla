import {
  AudioTranscriptionError,
  type AudioTranscription,
} from '@/modules/audioTranscription'
import { HttpTransportError } from './privateHttpTransport'

interface AudioHttpTransport {
  requestJson<T>(endpoint: string, options: {
    method: 'POST'
    form: FormData
    signal?: AbortSignal
    timeoutMs?: number
  }): Promise<T>
}

function unavailable(error: HttpTransportError): boolean {
  if (error.status === 503) return true
  const payload = error.payload
  return Boolean(
    payload
    && typeof payload === 'object'
    && !Array.isArray(payload)
    && String((payload as Record<string, unknown>).code || '').toUpperCase() === 'UNAVAILABLE',
  )
}

function mapError(error: unknown): AudioTranscriptionError {
  if (error instanceof AudioTranscriptionError) return error
  if (error instanceof HttpTransportError) {
    if (unavailable(error)) return new AudioTranscriptionError('unavailable', error.message, error)
    if (error.kind === 'aborted') return new AudioTranscriptionError('aborted', error.message, error)
  }
  const message = error instanceof Error ? error.message : String(error)
  return new AudioTranscriptionError('failed', message, error)
}

export function createV4AudioTranscription(http: AudioHttpTransport): AudioTranscription {
  return {
    async transcribe(input) {
      const form = new FormData()
      form.append('file', input.recording, 'voice.webm')
      form.append('mime', input.mimeType)
      try {
        const raw = await http.requestJson<unknown>('/api/audio/transcribe', {
          method: 'POST',
          form,
          signal: input.signal,
          timeoutMs: 60_000,
        })
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
          throw new AudioTranscriptionError('failed', 'Invalid transcription response.')
        }
        return String((raw as Record<string, unknown>).text || '').trim()
      } catch (error) {
        throw mapError(error)
      }
    },
  }
}
