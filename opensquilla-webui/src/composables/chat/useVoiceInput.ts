import { ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { detectPlatformId } from '@/platform/capabilities'

interface TranscriptionResponse {
  text?: string
  error?: string
  code?: string
}

// Abort a hung transcription request so voiceBusy cannot pin the mic button
// disabled forever. 60s is deliberately generous: a long dictation against a
// slow provider can legitimately take tens of seconds, and aborting a working
// request loses the recording, so we only give up once the request is far
// beyond any plausible success.
const TRANSCRIBE_TIMEOUT_MS = 60_000

// getUserMedia failures need different remedies, so map the error to the toast
// naming its fix: a permission denial is fixable by allowing microphone
// access, a missing input device is not, and anything else (device busy,
// hardware fault) at least gets a visible failure instead of silence.
function recordingFailureKey(err: unknown): string {
  const name = err instanceof Error ? err.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    // In the desktop shell there are no "browser settings" — the microphone
    // permission lives in OS system settings, so point the user there instead.
    return detectPlatformId() === 'desktop'
      ? 'chat.toast.voiceMicDeniedDesktop'
      : 'chat.toast.voiceMicDenied'
  }
  if (name === 'NotFoundError' || name === 'OverconstrainedError') {
    return 'chat.toast.voiceMicMissing'
  }
  return 'chat.toast.voiceRecordFailed'
}

interface DesktopWindowVisibilityBridge {
  onWindowHidden?: (callback: () => void) => void | (() => void)
}

function authToken(): string {
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

export function useVoiceInput() {
  const { pushToast } = useToasts()
  const voiceBusy = ref(false)
  const voiceRecording = ref(false)
  let recorder: MediaRecorder | null = null
  let activeStream: MediaStream | null = null
  let chunks: BlobPart[] = []
  let recordingGeneration = 0
  let transcriptionController: AbortController | null = null
  let unsubscribeWindowHidden: (() => void) | null = null
  let cleanedUp = false
  // A cancellation toast pushed while the document is hidden auto-dismisses
  // before the user can ever see it — they would just come back to a dead mic.
  // Remember the pending notice and deliver it once the surface is visible.
  let pendingCancelNotice = false

  async function toggleVoiceInput(onText: (text: string) => void) {
    if (voiceRecording.value) {
      stopRecording()
      return
    }
    await startRecording(onText)
  }

  async function startRecording(onText: (text: string) => void) {
    if (cleanedUp || voiceBusy.value || voiceRecording.value) return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      pushToast(i18n.global.t('chat.toast.voiceUnsupported'), { tone: 'danger' })
      return
    }
    const generation = ++recordingGeneration
    voiceBusy.value = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (cleanedUp || generation !== recordingGeneration) {
        stream.getTracks().forEach(track => track.stop())
        return
      }
      activeStream = stream
      chunks = []
      const mediaRecorder = new MediaRecorder(stream)
      recorder = mediaRecorder
      mediaRecorder.ondataavailable = event => {
        if (generation !== recordingGeneration || recorder !== mediaRecorder) return
        if (event.data && event.data.size > 0) chunks.push(event.data)
      }
      mediaRecorder.onstop = () => {
        if (generation !== recordingGeneration || recorder !== mediaRecorder) return
        const mime = mediaRecorder.mimeType || 'audio/webm'
        void transcribeChunks(mime, onText, generation)
      }
      mediaRecorder.start()
      voiceRecording.value = true
    } catch (err) {
      if (generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice recording failed:', err instanceof Error ? err.message : String(err))
      // Silent failure here is indistinguishable from a broken mic button, so
      // tell the user why nothing is recording (denied vs missing vs other).
      pushToast(i18n.global.t(recordingFailureKey(err)), { tone: 'danger' })
      stopTracks()
    } finally {
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopRecording() {
    const mediaRecorder = recorder
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      voiceRecording.value = false
      stopTracks()
      return
    }
    voiceRecording.value = false
    // MediaRecorder.stop() delivers dataavailable/onstop asynchronously in
    // browsers. Keep the input busy until that recording has handed its chunks
    // to transcription, otherwise a rapid second click can replace
    // activeStream and strand the old microphone track.
    voiceBusy.value = true
    try {
      mediaRecorder.stop()
    } catch (err) {
      if (recorder === mediaRecorder) recorder = null
      chunks = []
      voiceBusy.value = false
      console.warn('Voice recording stop failed:', err instanceof Error ? err.message : String(err))
    } finally {
      // Releasing the device does not discard the recorder's buffered final
      // data; its queued dataavailable event still owns the Blob chunks.
      stopTracks()
    }
  }

  async function transcribeChunks(
    mime: string,
    onText: (text: string) => void,
    generation: number,
  ) {
    if (generation !== recordingGeneration || cleanedUp) return
    const payload = new Blob(chunks, { type: mime })
    chunks = []
    stopTracks()
    recorder = null
    if (!payload.size) {
      if (generation === recordingGeneration) voiceBusy.value = false
      return
    }

    const controller = new AbortController()
    transcriptionController = controller
    // A hung endpoint must not wedge the mic: fire the shared controller after
    // the deadline, and remember that it was us so the abort still surfaces
    // the failure toast (external aborts — hidden window, cleanup — stay
    // silent in the catch below).
    let timedOut = false
    const timeoutTimer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, TRANSCRIBE_TIMEOUT_MS)
    voiceBusy.value = true
    try {
      const form = new FormData()
      form.append('file', payload, 'voice.webm')
      form.append('mime', mime)
      const headers: Record<string, string> = {}
      const token = authToken()
      if (token) headers.Authorization = `Bearer ${token}`
      const response = await fetch('/api/audio/transcribe', {
        method: 'POST',
        headers,
        body: form,
        credentials: 'same-origin',
        signal: controller.signal,
      })
      const data = (await response.json().catch(() => ({}))) as TranscriptionResponse
      if (generation !== recordingGeneration || cleanedUp) return
      if (!response.ok) {
        // A 503/UNAVAILABLE means voice transcription isn't configured on the
        // backend (audio disabled or no ElevenLabs key). The mic button is
        // normally gated on readiness, so this is a race/stale-status backstop:
        // surface a visible, actionable toast instead of failing silently.
        const unavailable = response.status === 503 || data.code === 'UNAVAILABLE'
        console.warn('Voice transcription failed:', data.error || `HTTP ${response.status}`)
        pushToast(
          i18n.global.t(unavailable ? 'chat.toast.voiceUnavailable' : 'chat.toast.voiceTranscribeFailed'),
          { tone: 'danger' },
        )
        return
      }
      const text = String(data.text || '').trim()
      if (text) onText(text)
    } catch (err) {
      if (controller.signal.aborted && !timedOut) return
      if (generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice transcription failed:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t('chat.toast.voiceTranscribeFailed'), { tone: 'danger' })
    } finally {
      clearTimeout(timeoutTimer)
      if (transcriptionController === controller) transcriptionController = null
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopTracks() {
    if (!activeStream) return
    activeStream.getTracks().forEach(track => track.stop())
    activeStream = null
  }

  function cancelRecording(options?: { notify?: boolean }) {
    // Notify only when the cancellation actually discarded something: a live
    // recording, a stop still waiting to hand off its chunks, or an in-flight
    // transcription (all keep voiceRecording or voiceBusy set). A cancel while
    // idle stays silent.
    const wasActive = voiceRecording.value || voiceBusy.value
    recordingGeneration += 1
    chunks = []
    transcriptionController?.abort()
    transcriptionController = null
    const mediaRecorder = recorder
    recorder = null
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.ondataavailable = null
      mediaRecorder.onstop = null
      try {
        mediaRecorder.stop()
      } catch {}
    }
    voiceBusy.value = false
    voiceRecording.value = false
    stopTracks()
    if (options?.notify && wasActive) {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        pendingCancelNotice = true
      } else {
        pushToast(i18n.global.t('chat.toast.voiceCancelledHidden'), { tone: 'danger' })
      }
    }
  }

  function onVisibilityChange() {
    if (document.visibilityState === 'hidden') {
      cancelRecording({ notify: true })
      return
    }
    if (pendingCancelNotice) {
      pendingCancelNotice = false
      pushToast(i18n.global.t('chat.toast.voiceCancelledHidden'), { tone: 'danger' })
    }
  }

  if (typeof window !== 'undefined') {
    const desktop = (window as unknown as {
      opensquillaDesktop?: DesktopWindowVisibilityBridge
    }).opensquillaDesktop
    const unsubscribe = desktop?.onWindowHidden?.(() => cancelRecording({ notify: true }))
    if (typeof unsubscribe === 'function') unsubscribeWindowHidden = unsubscribe
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }

  function cleanup() {
    if (cleanedUp) return
    cleanedUp = true
    // A deferred notice must not outlive the surface it belongs to.
    pendingCancelNotice = false
    try {
      unsubscribeWindowHidden?.()
    } catch {}
    unsubscribeWindowHidden = null
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
    cancelRecording()
  }

  return {
    voiceBusy,
    voiceRecording,
    toggleVoiceInput,
    cleanup,
  }
}
