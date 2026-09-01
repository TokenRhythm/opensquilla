import { describe, expect, it, vi } from 'vitest'

import { createV4AudioTranscription } from './audioTranscriptionV4'
import { HttpTransportError } from './privateHttpTransport'

type AudioHttpTransport = Parameters<typeof createV4AudioTranscription>[0]
type AudioRequestOptions = Parameters<AudioHttpTransport['requestJson']>[1]

describe('v4 AudioTranscription Adapter', () => {
  it('owns the multipart endpoint and projects the transcript text', async () => {
    const requestJson = vi.fn(async (_endpoint: string, _options: AudioRequestOptions) => ({
      text: '  hello world  ',
    }))
    const adapter = createV4AudioTranscription({
      requestJson: requestJson as AudioHttpTransport['requestJson'],
    })
    const recording = new Blob(['voice'], { type: 'audio/webm' })

    await expect(adapter.transcribe({ recording, mimeType: 'audio/webm' }))
      .resolves.toBe('hello world')

    expect(requestJson).toHaveBeenCalledWith('/api/audio/transcribe', expect.objectContaining({
      method: 'POST',
      timeoutMs: 60_000,
      form: expect.any(FormData),
    }))
    const form = requestJson.mock.calls[0]![1].form
    expect(form.get('mime')).toBe('audio/webm')
    expect(form.get('file')).toBeInstanceOf(Blob)
  })

  it.each([
    new HttpTransportError('http-status', 'unavailable', 503),
    new HttpTransportError('http-status', 'unavailable', 400, { code: 'UNAVAILABLE' }),
  ])('maps an unavailable transcription backend to the domain error', async (error) => {
    const adapter = createV4AudioTranscription({
      requestJson: vi.fn().mockRejectedValue(error) as AudioHttpTransport['requestJson'],
    })

    await expect(adapter.transcribe({
      recording: new Blob(['voice']),
      mimeType: 'audio/webm',
    })).rejects.toMatchObject({
      name: 'AudioTranscriptionError',
      kind: 'unavailable',
    })
  })

  it('rejects malformed responses without leaking transport payloads', async () => {
    const adapter = createV4AudioTranscription({
      requestJson: vi.fn(async () => ['not', 'a', 'transcript']) as AudioHttpTransport['requestJson'],
    })

    await expect(adapter.transcribe({
      recording: new Blob(['voice']),
      mimeType: 'audio/webm',
    })).rejects.toMatchObject({
      name: 'AudioTranscriptionError',
      kind: 'failed',
      message: 'Invalid transcription response.',
    })
  })
})
