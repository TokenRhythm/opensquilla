import { ref } from 'vue'

import i18n from '@/i18n'
import { useToasts } from '@/composables/useToasts'
import { detectPlatformId } from '@/platform/capabilities'
import { usePlatform } from '@/platform'
import {
  AudioTranscriptionError,
  type AudioTranscription,
  type AudioTranscriptionOwner,
} from '@/modules/audioTranscription'

// Abort a hung transcription request so voiceBusy cannot pin the mic button
// disabled forever. 60s is deliberately generous: a long dictation against a
// slow provider can legitimately take tens of seconds, and aborting a working
// request loses the recording, so we only give up once the request is far
// beyond any plausible success.
const TRANSCRIBE_TIMEOUT_MS = 60_000

// Silence gate: a recording that never carried signal must not reach the
// metered provider — ElevenLabs Scribe charges for silence and returns an
// empty string, which reads as "voice input is broken".
const SILENCE_FRAME_MS = 30
const SILENCE_PEAK_THRESHOLD = 0.0045
const SILENCE_RMS_THRESHOLD = 0.0018
const SILENCE_ACTIVE_RMS_THRESHOLD = 0.006
const SILENCE_MIN_ACTIVE_RATIO = 0.02

// One capture-device choice per browser profile. "" follows the system
// default; an id targets a specific microphone (headset Hands-Free channel,
// a second physical mic, ...). Persisted so the selection survives reloads
// and both browser and desktop shells share the mechanism.
const DEVICE_STORAGE_KEY = 'opensquilla.voiceDeviceId'

export interface VoiceInputDevice {
  deviceId: string
  label: string
}

// Chromium lists the OS default routing as extra "default"/"communications"
// entries next to the real endpoints, so one physical microphone can appear
// three times. The picker already offers its own system-default option and
// the OS sound settings list only the physical devices — drop the routing
// duplicates so the picker matches what the system shows.
function isPhysicalAudioInput(device: MediaDeviceInfo): boolean {
  return device.kind === 'audioinput'
    && device.deviceId !== 'default'
    && device.deviceId !== 'communications'
}

function readStoredDeviceId(): string {
  try {
    return localStorage.getItem(DEVICE_STORAGE_KEY) || ''
  } catch {
    return ''
  }
}

interface SilenceMeter {
  /** Ratio of voiced frames; 1 when no frames could be sampled (never blocks). */
  activeRatio(): number
  stop(): void
}

// Sample the live microphone stream with a throwaway audio graph. Returns null
// when unavailable (unsupported browser, blocked analyser), and an
// un-sampled meter reports "voiced" — a metering problem must never drop a
// real recording.
function startSilenceMeter(stream: MediaStream): SilenceMeter | null {
  const AudioCtx = window.AudioContext
    ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!AudioCtx) return null
  let context: AudioContext | null = null
  let timer: number | null = null
  let stopped = false
  const levels: number[] = []
  try {
    context = new AudioCtx()
    const source = context.createMediaStreamSource(stream)
    const analyser = context.createAnalyser()
    analyser.fftSize = 2048
    source.connect(analyser)
    const buffer = new Float32Array(analyser.fftSize)
    const collect = () => {
      if (stopped || !context) return
      analyser.getFloatTimeDomainData(buffer)
      let peak = 0
      let sumSq = 0
      for (let i = 0; i < buffer.length; i++) {
        const value = Math.abs(buffer[i])
        if (value > peak) peak = value
        sumSq += buffer[i] * buffer[i]
      }
      levels.push(Math.sqrt(sumSq / buffer.length) + peak * 0.25)
    }
    timer = window.setInterval(collect, SILENCE_FRAME_MS)
  } catch {
    void context?.close().catch(() => {})
    return null
  }
  return {
    activeRatio() {
      if (!levels.length) return 1
      let active = 0
      let peakMax = 0
      let rmsSum = 0
      for (const level of levels) {
        rmsSum += level
        if (level > SILENCE_ACTIVE_RMS_THRESHOLD) active += 1
        if (level > peakMax) peakMax = level
      }
      const rms = rmsSum / levels.length
      if (peakMax < SILENCE_PEAK_THRESHOLD || rms < SILENCE_RMS_THRESHOLD) return 0
      return active / levels.length
    },
    stop() {
      stopped = true
      if (timer !== null) window.clearInterval(timer)
      void context?.close().catch(() => {})
    },
  }
}

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

export function useVoiceInput(transcription: AudioTranscription, recordingOwner?: () => AudioTranscriptionOwner | Promise<AudioTranscriptionOwner>) {
  const { pushToast } = useToasts()
  const platform = usePlatform()
  const voiceBusy = ref(false)
  const voiceRecording = ref(false)
  const voiceDevices = ref<VoiceInputDevice[]>([])
  const voiceDeviceId = ref(readStoredDeviceId())
  // Friendly name of the device the OS currently routes as default, resolved
  // from Chromium's synthetic "default" entry so the picker's follow-default
  // option can show the real microphone instead of an abstract label.
  const voiceDefaultLabel = ref('')
  // Device id behind that label; the picker hides it from the explicit list
  // so the default microphone is never shown twice.
  const voiceDefaultDeviceId = ref('')
  let recorder: MediaRecorder | null = null
  let activeStream: MediaStream | null = null
  let silenceMeter: SilenceMeter | null = null
  let chunks: BlobPart[] = []
  let recordingGeneration = 0
  let transcriptionController: AbortController | null = null
  let unsubscribeWindowHidden: (() => void) | null = null
  let cleanedUp = false
  // A cancellation toast pushed while the document is hidden auto-dismisses
  // before the user can ever see it — they would just come back to a dead mic.
  // Remember the pending notice and deliver it once the surface is visible.
  let pendingCancelNotice = false

  // deviceId-scoped labels are only exposed after a permission grant; before
  // that enumerateDevices lists the devices with empty labels. One probe keeps
  // the picker honest: if a saved id is no longer present, fall back to the
  // system default instead of failing with an unanswerable OverconstrainedError.
  async function refreshVoiceDevices(): Promise<void> {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      let raw = await navigator.mediaDevices.enumerateDevices()
      let inputs = raw.filter(device => device.kind === 'audioinput')
      // Before the first permission grant, enumerateDevices reports empty
      // labels. One throwaway getUserMedia unlocks the real device names so
      // the picker is usable from the first open, not just after the first
      // recording. The probe decision uses the raw list: the pre-permission
      // placeholder may itself be a routing entry.
      if (inputs.length && inputs.every(device => !device.label)
        && navigator.mediaDevices.getUserMedia) {
        try {
          const probe = await navigator.mediaDevices.getUserMedia({ audio: true })
          probe.getTracks().forEach(track => track.stop())
          raw = await navigator.mediaDevices.enumerateDevices()
          inputs = raw.filter(device => device.kind === 'audioinput')
        } catch {
          // Permission was refused or the device is busy; the id-only list is
          // still functional and labels arrive after the first recording.
        }
      }
      const physical = inputs.filter(isPhysicalAudioInput)
      voiceDevices.value = physical.map(device => ({
        deviceId: device.deviceId,
        label: device.label,
      }))
      // The synthetic "default" entry carries the OS-localized prefix plus
      // the default device's own label; suffix matching resolves the real
      // microphone name without depending on the UI language.
      const defaultEntry = raw.find(device => device.kind === 'audioinput' && device.deviceId === 'default')
      const defaultMatch = defaultEntry?.label
        ? physical.find(device => device.label && defaultEntry.label.endsWith(device.label))
        : undefined
      voiceDefaultLabel.value = defaultMatch?.label || ''
      voiceDefaultDeviceId.value = defaultMatch?.deviceId || ''
      const stored = voiceDeviceId.value
      if (stored && (!physical.length || deviceIdsMatch(physical.map(device => device.deviceId), stored))) {
        return
      }
      if (stored) setVoiceDevice('')
    } catch {
      // Device enumeration is an enhancement; recording never depends on it.
    }
  }

  function deviceIdsMatch(ids: string[], wanted: string): boolean {
    return ids.some(id => id === wanted)
  }

  function setVoiceDevice(deviceId: string): void {
    voiceDeviceId.value = deviceId
    try {
      if (deviceId) localStorage.setItem(DEVICE_STORAGE_KEY, deviceId)
      else localStorage.removeItem(DEVICE_STORAGE_KEY)
    } catch { /* storage unavailable */ }
  }

  function audioConstraints(): MediaStreamConstraints {
    const deviceId = voiceDeviceId.value
    if (deviceId) {
      return { audio: { deviceId: { exact: deviceId } } }
    }
    return { audio: true }
  }

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
      const owner = await recordingOwner?.()
      if (cleanedUp || generation !== recordingGeneration) return
      let stream: MediaStream
      try {
        stream = await navigator.mediaDevices.getUserMedia(audioConstraints())
      } catch (err) {
        // A saved device may have been unplugged since the last recording.
        // Retry once with the system default so the mic never dies on a stale
        // selection, and keep the picker in sync with reality.
        const name = err instanceof Error ? err.name : ''
        const stale = (name === 'OverconstrainedError' || name === 'NotFoundError')
          && voiceDeviceId.value
        if (!stale || cleanedUp || generation !== recordingGeneration) throw err
        setVoiceDevice('')
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      }
      if (cleanedUp || generation !== recordingGeneration) {
        stream.getTracks().forEach(track => track.stop())
        return
      }
      activeStream = stream
      silenceMeter = startSilenceMeter(stream)
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
        void transcribeChunks(mime, onText, generation, owner)
      }
      mediaRecorder.start()
      voiceRecording.value = true
      // Labels become readable once permission is held; refresh in the
      // background so the picker lists friendly names on first open.
      void refreshVoiceDevices()
    } catch (err) {
      if (generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice recording failed:', err instanceof Error ? err.message : String(err))
      // Silent failure here is indistinguishable from a broken mic button, so
      // tell the user why nothing is recording (denied vs missing vs other).
      pushToast(i18n.global.t(recordingFailureKey(err)), { tone: 'danger' })
      stopSilenceMeter()
      stopTracks()
    } finally {
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopRecording() {
    const mediaRecorder = recorder
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      voiceRecording.value = false
      stopSilenceMeter()
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
      stopSilenceMeter()
      stopTracks()
    }
  }

  async function transcribeChunks(
    mime: string,
    onText: (text: string) => void,
    generation: number,
    owner?: AudioTranscriptionOwner,
  ) {
    if (generation !== recordingGeneration || cleanedUp) return
    const payload = new Blob(chunks, { type: mime })
    chunks = []
    const activeRatio = silenceMeter?.activeRatio() ?? 1
    stopSilenceMeter()
    stopTracks()
    recorder = null
    if (!payload.size) {
      if (generation === recordingGeneration) voiceBusy.value = false
      return
    }
    // A capture device that never carried signal (wrong mic, muted headset)
    // would cost the provider a request and the user a confusing empty
    // input. Skip the request and name the fix directly.
    if (activeRatio < SILENCE_MIN_ACTIVE_RATIO) {
      pushToast(i18n.global.t('chat.toast.voiceNoSignal'), { tone: 'info' })
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
      const text = await transcription.transcribe({
        recording: payload,
        mimeType: mime,
        signal: controller.signal,
        ...owner,
      })
      if (generation !== recordingGeneration || cleanedUp) return
      if (text) onText(text)
    } catch (err) {
      if (controller.signal.aborted && !timedOut) return
      if (generation !== recordingGeneration || cleanedUp) return
      console.warn('Voice transcription failed:', err instanceof Error ? err.message : String(err))
      pushToast(i18n.global.t(
        err instanceof AudioTranscriptionError && err.kind === 'unavailable'
          ? 'chat.toast.voiceUnavailable'
          : 'chat.toast.voiceTranscribeFailed',
      ), { tone: 'danger' })
    } finally {
      clearTimeout(timeoutTimer)
      if (transcriptionController === controller) transcriptionController = null
      if (generation === recordingGeneration) voiceBusy.value = false
    }
  }

  function stopSilenceMeter() {
    silenceMeter?.stop()
    silenceMeter = null
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
    stopSilenceMeter()
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

  // Device discovery is left to the caller (the composer requests it when the
  // picker opens, and after permission unlocks friendly labels): a probe at
  // mount would fire getUserMedia on every ChatView mount and can strand a
  // pending permission dialog nobody started.
  if (typeof window !== 'undefined') {
    const unsubscribe = platform.window.onHidden?.(() => cancelRecording({ notify: true }))
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
    voiceDevices,
    voiceDeviceId,
    voiceDefaultLabel,
    voiceDefaultDeviceId,
    setVoiceDevice,
    refreshVoiceDevices,
    toggleVoiceInput,
    cancel: cancelRecording,
    cleanup,
  }
}
