import {
  AudioTranscriptionError,
  type AudioTranscription,
  type SenseVoicePrepareStatus,
  type SttModelDiscovery,
} from '@/modules/audioTranscription'
import { HttpTransportError } from './privateHttpTransport'

// Structural subset of the private transport's request options: bodyless
// GET/POST/DELETE plus the multipart POST used for transcription. Kept local
// so the exported adapter signature never names a private transport symbol.
type AudioRequestOptions =
  | { method?: 'GET'; signal?: AbortSignal; timeoutMs?: number }
  | { method: 'POST'; form: FormData; signal?: AbortSignal; timeoutMs?: number }
  | { method: 'POST' | 'DELETE'; signal?: AbortSignal; timeoutMs?: number }

interface AudioHttpTransport {
  requestJson<T>(endpoint: string, options?: AudioRequestOptions): Promise<T>
}

function prepareStatus(raw: unknown): SenseVoicePrepareStatus {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { phase: 'idle' }
  const payload = raw as Record<string, unknown>
  const phase = typeof payload.phase === 'string' ? payload.phase : 'idle'
  const error = payload.error && typeof payload.error === 'object' && !Array.isArray(payload.error)
    ? payload.error as SenseVoicePrepareStatus['error']
    : undefined
  return {
    phase: phase as SenseVoicePrepareStatus['phase'],
    resource: typeof payload.resource === 'string' ? payload.resource : undefined,
    completedBytes: typeof payload.completedBytes === 'number' ? payload.completedBytes : undefined,
    totalBytes: typeof payload.totalBytes === 'number' ? payload.totalBytes : undefined,
    modelDir: typeof payload.modelDir === 'string' ? payload.modelDir : undefined,
    error,
  }
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
      if (input.sessionKey) form.append('session_key', input.sessionKey)
      if (input.sessionId) form.append('session_id', input.sessionId)
      if (input.sessionEpoch !== undefined) form.append('session_epoch', String(input.sessionEpoch))
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
    async discoverModels(input): Promise<SttModelDiscovery> {
      const raw = await http.requestJson<unknown>(
        `/api/audio/stt/models?engine=${encodeURIComponent(input.engine)}`,
        { method: 'GET', signal: input.signal, timeoutMs: 20_000 },
      )
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        return { ok: false, models: [], failureKind: 'invalid_response' }
      }
      const payload = raw as Record<string, unknown>
      const models = Array.isArray(payload.models)
        ? payload.models.filter((id): id is string => typeof id === 'string' && Boolean(id))
        : []
      return {
        ok: payload.ok === true,
        models,
        failureKind: typeof payload.failureKind === 'string' ? payload.failureKind : '',
      }
    },
    async startSenseVoicePrepare(): Promise<SenseVoicePrepareStatus> {
      return prepareStatus(await http.requestJson<unknown>(
        '/api/audio/stt/sensevoice/prepare',
        { method: 'POST', timeoutMs: 20_000 },
      ))
    },
    async senseVoicePrepareStatus(): Promise<SenseVoicePrepareStatus> {
      return prepareStatus(await http.requestJson<unknown>(
        '/api/audio/stt/sensevoice/prepare',
        { method: 'GET', timeoutMs: 10_000 },
      ))
    },
    async cancelSenseVoicePrepare(): Promise<SenseVoicePrepareStatus> {
      return prepareStatus(await http.requestJson<unknown>(
        '/api/audio/stt/sensevoice/prepare',
        { method: 'DELETE', timeoutMs: 10_000 },
      ))
    },
  }
}
