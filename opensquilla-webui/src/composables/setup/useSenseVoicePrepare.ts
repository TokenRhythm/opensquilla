import { onUnmounted, ref } from 'vue'

import type { AudioTranscription, SenseVoicePrepareStatus } from '@/modules/audioTranscription'

const POLL_INTERVAL_MS = 600

// Drives the pinned local SenseVoice download from the settings card: start,
// poll progress, cancel, and hand the verified directory back to the caller
// (which fills the model_dir draft field). The gateway owns the actual
// download; this composable only mirrors its published state.
export function useSenseVoicePrepare(
  audio: AudioTranscription | undefined,
  onReady: (modelDir: string) => void,
) {
  const status = ref<SenseVoicePrepareStatus>({ phase: 'idle' })
  const supported = Boolean(
    audio?.startSenseVoicePrepare && audio?.senseVoicePrepareStatus,
  )
  let timer: ReturnType<typeof setInterval> | null = null

  function stopPolling() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function apply(next: SenseVoicePrepareStatus) {
    status.value = next
    if (next.phase === 'ready') {
      stopPolling()
      if (next.modelDir) onReady(next.modelDir)
    } else if (next.phase === 'failed' || next.phase === 'cancelled' || next.phase === 'idle') {
      stopPolling()
    }
  }

  function beginPolling() {
    stopPolling()
    timer = setInterval(() => {
      void audio?.senseVoicePrepareStatus?.().then(apply).catch(() => {})
    }, POLL_INTERVAL_MS)
  }

  async function start() {
    if (!audio?.startSenseVoicePrepare) return
    try {
      apply(await audio.startSenseVoicePrepare())
      // Poll unless the preparation already settled; a stale "idle" first
      // response (older gateway) must still start the progress loop.
      if (!['ready', 'failed', 'cancelled'].includes(status.value.phase)) beginPolling()
    } catch {
      status.value = { phase: 'failed', error: { reason: 'network' } }
    }
  }

  async function cancel() {
    if (!audio?.cancelSenseVoicePrepare) return
    try {
      apply(await audio.cancelSenseVoicePrepare())
    } catch {
      // Cancellation is best-effort; the next poll reconciles the state.
    }
  }

  async function refresh() {
    if (!audio?.senseVoicePrepareStatus) return
    try {
      apply(await audio.senseVoicePrepareStatus())
    } catch {
      // Status is an enhancement; leave the last known state on failure.
    }
  }

  onUnmounted(stopPolling)

  return { status, supported, start, cancel, refresh }
}