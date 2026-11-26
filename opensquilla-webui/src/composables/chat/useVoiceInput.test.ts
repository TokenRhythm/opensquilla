// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { useVoiceInput as useVoiceInputComposable } from './useVoiceInput'
import { AudioTranscriptionError, type AudioTranscription } from '@/modules/audioTranscription'

function useVoiceInput() {
  return useVoiceInputComposable({
    async transcribe({ recording, mimeType, signal }) {
      const form = new FormData()
      form.append('file', recording, 'voice.webm')
      form.append('mime', mimeType)
      const headers: Record<string, string> = {}
      const token = sessionStorage.getItem('opensquilla.wsToken') || ''
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetch('/api/audio/transcribe', {
        method: 'POST',
        headers,
        body: form,
        credentials: 'same-origin',
        signal,
      })
      const data = await response.json().catch(() => ({})) as {
        text?: string
        error?: string
        code?: string
      }
      if (!response.ok) {
        const kind = response.status === 503 || data.code === 'UNAVAILABLE'
          ? 'unavailable'
          : 'failed'
        throw new AudioTranscriptionError(kind, data.error || `HTTP ${response.status}`)
      }
      return String(data.text || '').trim()
    },
  })
}

// useToasts is a module-level singleton, so this is the same queue the
// composable writes to.
const { toasts } = useToasts()
const originalVisibilityState = Object.getOwnPropertyDescriptor(document, 'visibilityState')

// Minimal MediaRecorder stand-in: stop() synchronously emits one chunk and
// fires onstop, which is what drives useVoiceInput into transcribeChunks().
class FakeMediaRecorder {
  state: 'inactive' | 'recording' = 'inactive'
  mimeType = 'audio/webm'
  ondataavailable: ((event: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  constructor(public stream: unknown) {}
  start() {
    this.state = 'recording'
  }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
    this.onstop?.()
  }
}

class AsyncStopMediaRecorder extends FakeMediaRecorder {
  override stop() {
    this.state = 'inactive'
    setTimeout(() => {
      this.ondataavailable?.({ data: new Blob(['audio'], { type: this.mimeType }) })
      this.onstop?.()
    }, 0)
  }
}

function stubMedia(recorderClass: typeof FakeMediaRecorder = FakeMediaRecorder) {
  const stopTrack = vi.fn()
  const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] }))
  ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = recorderClass
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { getUserMedia, stopTrack }
}

function stubMediaWithAudioGraph(
  frames: number[],
  recorderClass: typeof FakeMediaRecorder = FakeMediaRecorder,
) {
  const stream = { getTracks: () => [{ stop: vi.fn() }] }
  const getUserMedia = vi.fn(async () => stream)
  // The analyser callback samples the stream every SILENCE_FRAME_MS; onstop
  // fires immediately (stopRecording), so the meter must hold the level before
  // the first sample lands. FakeMediaRecorder.stop() is synchronous, so run
  // one collect pass up front via the interval's own timer contract: stub
  // setInterval to fire once inline.
  const timeDomainFiller = (values: number[]) => (buffer: Float32Array) => {
    const source = values.shift() ?? 0
    buffer.fill(source)
  }
  const analyser = { fftSize: 4 }
  const close = vi.fn(async () => {})
  Object.defineProperty(analyser, 'getFloatTimeDomainData', {
    value: timeDomainFiller([...frames]),
  })
  const context = {
    createMediaStreamSource: () => ({ connect: vi.fn() }),
    createAnalyser: () => analyser,
    close,
  }
  vi.stubGlobal('AudioContext', function AudioContext() { return context })
  const realSetInterval = window.setInterval.bind(window)
  const realClearInterval = window.clearInterval.bind(window)
  vi.stubGlobal('setInterval', (fn: () => void, ms: number) => { fn(); return realSetInterval(fn, ms) })
  vi.stubGlobal('clearInterval', (id: number) => realClearInterval(id))
  ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = recorderClass
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { getUserMedia, analyser, close }
}

function stubMediaRejecting(error: unknown) {
  const getUserMedia = vi.fn(async () => {
    throw error
  })
  ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeMediaRecorder
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { getUserMedia }
}

// A transcription request that never settles on its own and only rejects when
// its AbortSignal fires — the shape of a hung endpoint.
function stubHangingFetch() {
  let abortSignal: AbortSignal | undefined
  const fetch = vi.fn(
    (_input: unknown, init?: { signal?: AbortSignal }) =>
      new Promise((_resolve, reject) => {
        abortSignal = init?.signal
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('Aborted', 'AbortError')),
        )
      }),
  )
  vi.stubGlobal('fetch', fetch)
  return { fetch, signal: () => abortSignal }
}

function stubWindowHiddenBridge() {
  let onHidden: (() => void) | null = null
  const unsubscribe = vi.fn()
  const onWindowHidden = vi.fn((callback: () => void) => {
    onHidden = callback
    return unsubscribe
  })
  ;(window as unknown as {
    opensquillaDesktop?: { onWindowHidden: typeof onWindowHidden }
  }).opensquillaDesktop = { onWindowHidden }
  return {
    hideWindow: () => onHidden?.(),
    onWindowHidden,
    unsubscribe,
  }
}

async function waitUntil(condition: () => boolean, tries = 40) {
  for (let i = 0; i < tries; i++) {
    if (condition()) return
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

// Start then stop a recording, then wait for the async transcription to settle
// (a toast surfaced or the transcript delivered).
async function runTranscription() {
  const onText = vi.fn()
  const { cleanup, toggleVoiceInput, voiceBusy } = useVoiceInput()
  await toggleVoiceInput(onText)
  await toggleVoiceInput(onText)
  await waitUntil(
    () => !voiceBusy.value && (toasts.value.length > 0 || onText.mock.calls.length > 0),
  )
  cleanup()
  return onText
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  toasts.value = []
  delete (window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop
  stubMedia()
})

afterEach(() => {
  delete (window as unknown as { opensquillaDesktop?: unknown }).opensquillaDesktop
  if (originalVisibilityState) {
    Object.defineProperty(document, 'visibilityState', originalVisibilityState)
  } else {
    delete (document as unknown as { visibilityState?: DocumentVisibilityState }).visibilityState
  }
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useVoiceInput transcription feedback', () => {
  it('keeps the recording owner when the active chat changes', async () => {
    let owner = { sessionKey: 'original-session', sessionId: 'original-id', sessionEpoch: 0 }
    const transcribe = vi.fn(async () => 'recorded text')
    const voice = useVoiceInputComposable({ transcribe }, () => owner)
    const onText = vi.fn()
    await voice.toggleVoiceInput(onText)
    owner = { sessionKey: 'next-session', sessionId: 'next-id', sessionEpoch: 1 }
    await voice.toggleVoiceInput(onText)
    await waitUntil(() => transcribe.mock.calls.length > 0)
    expect(transcribe).toHaveBeenCalledWith(expect.objectContaining({
      sessionKey: 'original-session', sessionId: 'original-id', sessionEpoch: 0,
    }))
    voice.cleanup()
  })
  it('surfaces an "enable in settings" toast when transcription is unavailable (503)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        json: async () => ({ error: 'audio transcription is disabled', code: 'UNAVAILABLE' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(i18n.global.t('chat.toast.voiceUnavailable'))
    expect(toasts.value.every(t => t.tone === 'danger')).toBe(true)
  })

  it('surfaces a generic failure toast on a provider error (502)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => ({ error: 'ELEVENLABS_API_KEY is not set', code: 'PROVIDER_ERROR' }),
      })),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('surfaces a generic failure toast when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down')
      }),
    )

    await runTranscription()

    expect(toasts.value.map(t => t.message)).toContain(
      i18n.global.t('chat.toast.voiceTranscribeFailed'),
    )
  })

  it('inserts the transcript and raises no error toast on success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ text: '  hello world  ' }),
      })),
    )

    const onText = await runTranscription()

    expect(onText).toHaveBeenCalledWith('hello world')
    expect(toasts.value).toHaveLength(0)
  })

  it('aborts a hung transcription after the timeout and re-enables the mic', async () => {
    vi.useFakeTimers()
    try {
      const { fetch, signal } = stubHangingFetch()
      const { getUserMedia } = stubMedia()
      const onText = vi.fn()
      const { cleanup, toggleVoiceInput, voiceBusy, voiceRecording } = useVoiceInput()

      await toggleVoiceInput(onText)
      await toggleVoiceInput(onText)
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(voiceBusy.value).toBe(true)

      // Just short of the deadline the request is still allowed to finish.
      await vi.advanceTimersByTimeAsync(59_999)
      expect(voiceBusy.value).toBe(true)
      expect(signal()?.aborted).toBe(false)
      expect(toasts.value).toHaveLength(0)

      await vi.advanceTimersByTimeAsync(1)
      expect(signal()?.aborted).toBe(true)
      expect(voiceBusy.value).toBe(false)
      expect(onText).not.toHaveBeenCalled()
      expect(toasts.value.map(t => t.message)).toEqual([
        i18n.global.t('chat.toast.voiceTranscribeFailed'),
      ])

      // The mic is usable again after the timeout.
      await toggleVoiceInput(onText)
      expect(getUserMedia).toHaveBeenCalledTimes(2)
      expect(voiceRecording.value).toBe(true)

      cleanup()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('useVoiceInput recording failures', () => {
  it('toasts the settings hint when microphone permission is denied', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    stubMediaRejecting(new DOMException('Permission denied', 'NotAllowedError'))
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)

    expect(voiceBusy.value).toBe(false)
    expect(voiceRecording.value).toBe(false)
    expect(fetch).not.toHaveBeenCalled()
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceMicDenied'),
    ])
    expect(toasts.value[0].tone).toBe('danger')
    cleanup()
  })

  it('points a denied desktop user at system settings, not browser settings', async () => {
    vi.stubGlobal('fetch', vi.fn())
    // The desktop shell exposes window.opensquillaDesktop — the same bridge
    // this composable already uses for hidden-window cancellation.
    stubWindowHiddenBridge()
    stubMediaRejecting(new DOMException('Permission denied', 'NotAllowedError'))
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput } = useVoiceInput()

    await toggleVoiceInput(onText)

    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceMicDeniedDesktop'),
    ])
    expect(toasts.value[0].tone).toBe('danger')
    cleanup()
  })

  it('toasts the missing-device copy when no microphone device exists', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMediaRejecting(new DOMException('No capture device found', 'NotFoundError'))
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy } = useVoiceInput()

    await toggleVoiceInput(onText)

    expect(voiceBusy.value).toBe(false)
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceMicMissing'),
    ])
    expect(toasts.value[0].tone).toBe('danger')
    cleanup()
  })

  it('toasts on any other recording failure instead of failing silently', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMediaRejecting(new Error('device is wedged'))
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy } = useVoiceInput()

    await toggleVoiceInput(onText)

    expect(voiceBusy.value).toBe(false)
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceRecordFailed'),
    ])
    expect(toasts.value[0].tone).toBe('danger')
    cleanup()
  })
})

describe('useVoiceInput device selection', () => {
  it('passes the selected deviceId as an exact constraint', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const { getUserMedia } = stubMedia()
    const onText = vi.fn()
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    voice.setVoiceDevice('mic-42')
    await voice.toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: { deviceId: { exact: 'mic-42' } } })
    voice.cleanup()
    getUserMedia.mockClear()

    // Empty selection returns to the unconstrained system default.
    const voice2 = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    voice2.setVoiceDevice('')
    await voice2.toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    voice2.cleanup()
  })

  it('clears a stale device after an OverconstrainedError and retries with the default', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const stopTrack = vi.fn()
    const getUserMedia = vi
      .fn<(constraints: MediaStreamConstraints) => Promise<MediaStream>>()
      .mockRejectedValueOnce(new DOMException('gone', 'OverconstrainedError'))
      .mockResolvedValueOnce({ getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream)
    ;(globalThis as unknown as { MediaRecorder: unknown }).MediaRecorder = FakeMediaRecorder
    Object.defineProperty(globalThis.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    voice.setVoiceDevice('unplugged-mic')
    const onText = vi.fn()
    await voice.toggleVoiceInput(onText)
    expect(voice.voiceRecording.value).toBe(true)
    expect(voice.voiceDeviceId.value).toBe('')
    expect(localStorage.getItem('opensquilla.voiceDeviceId')).toBeNull()
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: true })
    voice.cleanup()
  })

  it('enumerates audioinput devices for the picker', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'a', label: 'Mic A' },
        { kind: 'videoinput', deviceId: 'v', label: 'Cam' },
        { kind: 'audioinput', deviceId: 'b', label: 'Mic B' },
      ]),
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    await voice.refreshVoiceDevices()
    expect(voice.voiceDevices.value).toEqual([
      { deviceId: 'a', label: 'Mic A' },
      { deviceId: 'b', label: 'Mic B' },
    ])
    voice.cleanup()
  })

  it('hides the synthetic default/communications routing entries', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    // The real Windows shape: Chromium lists the OS default routing twice
    // ("默认 - …"/"通讯 - …") next to the physical endpoints, while the OS
    // sound settings show only the two physical microphones.
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'default', label: '默认 - 耳机 (HONOR Earbuds A Hands-Free AG Audio)' },
        { kind: 'audioinput', deviceId: 'communications', label: '通讯 - 耳机 (HONOR Earbuds A Hands-Free AG Audio)' },
        { kind: 'audioinput', deviceId: 'steam-mic', label: '麦克风 (Steam Streaming Microphone)' },
        { kind: 'audioinput', deviceId: 'honor-mic', label: '耳机 (HONOR Earbuds A Hands-Free AG Audio)' },
      ]),
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    await voice.refreshVoiceDevices()
    expect(voice.voiceDevices.value).toEqual([
      { deviceId: 'steam-mic', label: '麦克风 (Steam Streaming Microphone)' },
      { deviceId: 'honor-mic', label: '耳机 (HONOR Earbuds A Hands-Free AG Audio)' },
    ])
    voice.cleanup()
  })

  it('filters routing entries after the permission probe unlocks labels', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const { getUserMedia } = stubMedia()
    const enumerateDevices = vi.fn()
      .mockResolvedValueOnce([
        { kind: 'audioinput', deviceId: '', label: '' },
      ])
      .mockResolvedValueOnce([
        { kind: 'audioinput', deviceId: 'default', label: '默认 - Mic' },
        { kind: 'audioinput', deviceId: 'communications', label: '通讯 - Mic' },
        { kind: 'audioinput', deviceId: 'real-mic', label: 'Mic' },
      ])
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: enumerateDevices,
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    await voice.refreshVoiceDevices()
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(enumerateDevices).toHaveBeenCalledTimes(2)
    expect(voice.voiceDevices.value).toEqual([
      { deviceId: 'real-mic', label: 'Mic' },
    ])
    voice.cleanup()
  })

  it('resets a stored routing-id selection to the system default', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    // A selection saved before routing entries were hidden must not strand
    // the picker on an invisible row.
    voice.setVoiceDevice('default')
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'default', label: '默认 - Mic' },
        { kind: 'audioinput', deviceId: 'real-mic', label: 'Mic' },
      ]),
    })
    await voice.refreshVoiceDevices()
    expect(voice.voiceDeviceId.value).toBe('')
    expect(localStorage.getItem('opensquilla.voiceDeviceId')).toBeNull()
    voice.cleanup()
  })

  it('resolves the real default device name from the synthetic default entry', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'default', label: '默认 - 麦克风 (Real Mic)' },
        { kind: 'audioinput', deviceId: 'communications', label: '通讯 - 麦克风 (Real Mic)' },
        { kind: 'audioinput', deviceId: 'real-mic', label: '麦克风 (Real Mic)' },
        { kind: 'audioinput', deviceId: 'other-mic', label: '麦克风 (Other Mic)' },
      ]),
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    await voice.refreshVoiceDevices()
    expect(voice.voiceDefaultLabel.value).toBe('麦克风 (Real Mic)')
    expect(voice.voiceDefaultDeviceId.value).toBe('real-mic')
    voice.cleanup()
  })

  it('leaves the default label empty when no routing entry is exposed', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'real-mic', label: '麦克风 (Real Mic)' },
      ]),
    })
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    await voice.refreshVoiceDevices()
    expect(voice.voiceDefaultLabel.value).toBe('')
    expect(voice.voiceDefaultDeviceId.value).toBe('')
    voice.cleanup()
  })

  it('drops a stored device that no longer exists', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    voice.setVoiceDevice('ghost-mic')
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'real-mic', label: 'Real' },
      ]),
    })
    await voice.refreshVoiceDevices()
    expect(voice.voiceDeviceId.value).toBe('')
    expect(localStorage.getItem('opensquilla.voiceDeviceId')).toBeNull()
    voice.cleanup()
  })

  it('keeps a stored device that is still present', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    const voice = useVoiceInputComposable({ transcribe: vi.fn(async () => '') } as AudioTranscription)
    voice.setVoiceDevice('real-mic')
    Object.defineProperty(globalThis.navigator.mediaDevices, 'enumerateDevices', {
      configurable: true,
      value: vi.fn(async () => [
        { kind: 'audioinput', deviceId: 'real-mic', label: 'Real' },
      ]),
    })
    await voice.refreshVoiceDevices()
    expect(voice.voiceDeviceId.value).toBe('real-mic')
    voice.cleanup()
  })
})

describe('useVoiceInput silence gate', () => {
  it('skips the provider and toasts when the mic captured no signal', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const transcribe = vi.fn(async () => 'never')
    // All-zero frames: below every silence threshold.
    stubMediaWithAudioGraph([0, 0, 0, 0])
    const voice = useVoiceInputComposable({ transcribe })
    const onText = vi.fn()
    await voice.toggleVoiceInput(onText)
    await voice.toggleVoiceInput(onText)
    await waitUntil(() => toasts.value.length > 0)
    expect(transcribe).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    expect(toasts.value.map(t => t.message)).toEqual([i18n.global.t('chat.toast.voiceNoSignal')])
    voice.cleanup()
  })

  it('transcribes when the mic captured real signal', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const transcribe = vi.fn(async () => 'hello')
    // A loud frame mixed with quiet frames: active ratio clears the gate.
    stubMediaWithAudioGraph([0.5, 0.4, 0.3, 0.2])
    const voice = useVoiceInputComposable({ transcribe })
    const onText = vi.fn()
    await voice.toggleVoiceInput(onText)
    await voice.toggleVoiceInput(onText)
    await waitUntil(() => onText.mock.calls.length > 0)
    expect(transcribe).toHaveBeenCalledTimes(1)
    expect(onText).toHaveBeenCalledWith('hello')
    expect(toasts.value).toHaveLength(0)
    voice.cleanup()
  })

  it('transcribes anyway when no audio graph is available', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const transcribe = vi.fn(async () => 'kept')
    stubMedia() // no AudioContext stub → meter is null → never blocks
    const voice = useVoiceInputComposable({ transcribe })
    const onText = vi.fn()
    await voice.toggleVoiceInput(onText)
    await voice.toggleVoiceInput(onText)
    await waitUntil(() => onText.mock.calls.length > 0)
    expect(transcribe).toHaveBeenCalledTimes(1)
    expect(toasts.value).toHaveLength(0)
    voice.cleanup()
  })
})

describe('useVoiceInput hidden-window cancellation', () => {
  it('does not strand an old stream when stop dispatches asynchronously', async () => {
    const fetch = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ text: 'first recording' }),
    }))
    vi.stubGlobal('fetch', fetch)
    const { getUserMedia, stopTrack } = stubMedia(AsyncStopMediaRecorder)
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    await toggleVoiceInput(onText)
    expect(voiceBusy.value).toBe(true)
    expect(stopTrack).toHaveBeenCalledTimes(1)

    // A rapid restart is ignored until the old onstop/transcription finishes,
    // so its stream cannot be replaced before it is released.
    await toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledTimes(1)

    await waitUntil(() => !voiceBusy.value && onText.mock.calls.length === 1)
    await toggleVoiceInput(onText)
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(voiceRecording.value).toBe(true)

    cleanup()
    expect(stopTrack).toHaveBeenCalledTimes(2)
  })

  it('cancels each recording when the desktop window is hidden without transcribing', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    const bridge = stubWindowHiddenBridge()
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    expect(voiceRecording.value).toBe(true)
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    // Discarding the audio is correct, but it must not be silent.
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceCancelledHidden'),
    ])
    expect(toasts.value[0].tone).toBe('danger')

    await toggleVoiceInput(onText)
    expect(voiceRecording.value).toBe(true)
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(2)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    expect(bridge.onWindowHidden).toHaveBeenCalledTimes(1)
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceCancelledHidden'),
      i18n.global.t('chat.toast.voiceCancelledHidden'),
    ])

    cleanup()
    expect(bridge.unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('stays silent when the window hides while voice input is idle', async () => {
    vi.stubGlobal('fetch', vi.fn())
    const bridge = stubWindowHiddenBridge()
    let visibilityState: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibilityState,
    })
    const { cleanup } = useVoiceInput()

    bridge.hideWindow()
    visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))

    expect(toasts.value).toHaveLength(0)
    cleanup()
  })

  it('cancels an in-flight transcription when hidden and reports it exactly once', async () => {
    const { fetch, signal } = stubHangingFetch()
    const bridge = stubWindowHiddenBridge()
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceBusy } = useVoiceInput()

    await toggleVoiceInput(onText)
    await toggleVoiceInput(onText)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(voiceBusy.value).toBe(true)

    bridge.hideWindow()

    expect(signal()?.aborted).toBe(true)
    expect(voiceBusy.value).toBe(false)
    // Let the aborted fetch settle: the abort path must not stack a second
    // transcription-failed toast on top of the cancellation toast.
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceCancelledHidden'),
    ])
    expect(onText).not.toHaveBeenCalled()

    cleanup()
  })

  it('keeps visibilitychange hidden as a browser fallback', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    let visibilityState: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibilityState,
    })
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    // A toast fired while the tab is hidden auto-dismisses unseen, so the
    // notice must wait until the user comes back.
    expect(toasts.value).toHaveLength(0)

    visibilityState = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))

    expect(toasts.value.map(t => t.message)).toEqual([
      i18n.global.t('chat.toast.voiceCancelledHidden'),
    ])
    expect(toasts.value[0].tone).toBe('danger')

    // The deferred notice fires exactly once, not on every return to the tab.
    document.dispatchEvent(new Event('visibilitychange'))
    expect(toasts.value).toHaveLength(1)

    cleanup()
  })

  it('drops a deferred cancellation notice on cleanup instead of firing after unmount', async () => {
    vi.stubGlobal('fetch', vi.fn())
    stubMedia()
    let visibilityState: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => visibilityState,
    })
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput } = useVoiceInput()

    await toggleVoiceInput(onText)
    visibilityState = 'hidden'
    document.dispatchEvent(new Event('visibilitychange'))
    expect(toasts.value).toHaveLength(0)

    cleanup()
    visibilityState = 'visible'
    document.dispatchEvent(new Event('visibilitychange'))

    expect(toasts.value).toHaveLength(0)
  })

  it('cleanup unsubscribes and stops an active stream without transcribing', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { stopTrack } = stubMedia()
    const bridge = stubWindowHiddenBridge()
    const onText = vi.fn()
    const { cleanup, toggleVoiceInput, voiceRecording } = useVoiceInput()

    await toggleVoiceInput(onText)
    cleanup()
    bridge.hideWindow()

    expect(voiceRecording.value).toBe(false)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(bridge.unsubscribe).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(onText).not.toHaveBeenCalled()
    // Teardown is not a hidden window: unmount cancellation stays silent.
    expect(toasts.value).toHaveLength(0)
  })

  it('cleanup aborts transcription and ignores its late success after a conversation owner is replaced', async () => {
    let complete!: (text: string) => void
    let signal: AbortSignal | undefined
    const transcription = { transcribe: vi.fn<AudioTranscription['transcribe']>(({ signal: requestedSignal }) => {
      signal = requestedSignal
      return new Promise<string>(resolve => { complete = resolve })
    }) }
    const oldText = vi.fn()
    const owner = useVoiceInputComposable(transcription)
    await owner.toggleVoiceInput(oldText)
    await owner.toggleVoiceInput(oldText)
    expect(transcription.transcribe).toHaveBeenCalledOnce()
    owner.cleanup()
    expect(signal?.aborted).toBe(true)
    const replacementText = vi.fn()
    const replacement = useVoiceInputComposable({ transcribe: vi.fn(async () => 'Current owner') })
    complete('Retired owner')
    await Promise.resolve(); await Promise.resolve()
    expect(oldText).not.toHaveBeenCalled()
    expect(replacementText).not.toHaveBeenCalled()
    expect(toasts.value).toEqual([])
    await replacement.toggleVoiceInput(replacementText)
    await replacement.toggleVoiceInput(replacementText)
    await Promise.resolve(); await Promise.resolve()
    expect(replacementText).toHaveBeenCalledWith('Current owner')
    replacement.cleanup()
  })
})
