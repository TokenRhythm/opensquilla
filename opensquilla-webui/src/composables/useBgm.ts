import { computed, ref } from 'vue'
import { readStaticJson } from '@/platform/staticAssets'

/**
 * One entry from `public/music/playlist.json`. `src` is either a filename
 * relative to the music directory (bundled into the build) or an absolute
 * HTTPS URL streamed at runtime.
 */
export interface BgmTrack {
  id: string
  title: string
  src: string
}

/**
 * Synthetic id for an ad-hoc "Choose local file…" pick. Session-only: the
 * object URL cannot be restored after a reload, so on init this id falls back
 * to the playlist default with playback off.
 */
export const BGM_LOCAL_TRACK_ID = '__local__'

const STORAGE_KEY = 'opensquilla-bgm'
const DEFAULT_VOLUME = 0.6
const ABSOLUTE_URL_SCHEME = /^[a-z][a-z\d+.-]*:/i
const HTTPS_STREAM = /^https:\/\//i
const URL_ESCAPE = /%[0-9a-f]{2}/i

// Module-level singleton state (mirrors useAgentOptions/useConfirm): one
// <audio> element and one state tree app-wide, however many components mount.
const tracks = ref<BgmTrack[]>([])
const playing = ref(false)
const currentTrackId = ref('')
const volume = ref(DEFAULT_VOLUME)
const playlistError = ref(false)
// Display name of the session-only local file; '' until one has been picked.
const localTrackTitle = ref('')

let audio: HTMLAudioElement | null = null
let localObjectUrl = ''
// Dedupes concurrent initBgm() calls onto a single load, like useAgentOptions.
let initPromise: Promise<void> | null = null
// Async play() settlements may arrive after a disable, pause, or newer play
// request. Only the latest generation may publish state; the intent tells a
// stale successful request whether it must re-pause the shared element.
let playbackGeneration = 0
let playbackIntent = false

interface PersistedBgm {
  enabled?: boolean
  playing?: boolean
  trackId?: string
  volume?: number
}

function readPersisted(): PersistedBgm {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as PersistedBgm) : {}
  } catch {
    return {}
  }
}

// Opt-in feature gate: the topbar control only renders when this is on
// (Settings → Appearance, or the command palette). Read synchronously at
// module init — App.vue's v-if needs the answer before any component mounts
// or initBgm() runs.
const enabled = ref(readPersisted().enabled === true)

function persist() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        enabled: enabled.value,
        playing: playing.value,
        trackId: currentTrackId.value,
        volume: volume.value,
      }),
    )
  } catch {}
}

function isSafeRelativeMusicSource(file: string): boolean {
  if (!file || ABSOLUTE_URL_SCHEME.test(file) || file.startsWith('//')) return false

  // Check the path independently from its query/fragment. Repeated decoding
  // catches both ordinary and nested percent-encoded traversal; every
  // successful pass shortens the string, so the loop is naturally bounded.
  let decodedPath = file.split(/[?#]/, 1)[0]
  try {
    while (URL_ESCAPE.test(decodedPath)) {
      const next = decodeURIComponent(decodedPath)
      if (next === decodedPath) break
      decodedPath = next
    }
  } catch {
    return false
  }

  const slashPath = decodedPath.replace(/\\/g, '/')
  if (!slashPath || slashPath.startsWith('/')) return false
  return slashPath.split('/').every(segment => segment !== '.' && segment !== '..')
}

/**
 * Resolve a playlist `src` to a fetchable URL. Absolute HTTPS URLs pass
 * through; bundled filenames mirror App.vue's brandMarkUrl: Vite serves
 * `public/` at the root in dev, while the packaged UI serves those assets from
 * `${base}/static/dist/` under the gateway's base path.
 */
function musicAssetUrl(file: string): string {
  if (HTTPS_STREAM.test(file)) return file
  if (!isSafeRelativeMusicSource(file)) return ''
  if (import.meta.env.DEV) return `/music/${file}`
  const base = document.getElementById('opensquilla-data')?.dataset.basePath || '/control'
  return `${base.replace(/\/$/, '')}/static/dist/music/${file}`
}

function getAudio(): HTMLAudioElement {
  if (!audio) {
    audio = new Audio()
    audio.loop = true
    audio.preload = 'none'
    audio.volume = volume.value
    // Belt-and-braces state sync: toggle()/playTrack() set `playing` directly
    // (test DOMs don't fire media events), while these listeners keep the ref
    // honest when playback changes outside our calls — OS media keys, a
    // mid-stream network failure, or the element pausing itself on error.
    audio.addEventListener('play', () => {
      playbackGeneration += 1
      if (!enabled.value) {
        playbackIntent = false
        audio?.pause()
        playing.value = false
      } else {
        playbackIntent = true
        playing.value = true
      }
      persist()
    })
    audio.addEventListener('pause', () => {
      playbackGeneration += 1
      playbackIntent = false
      playing.value = false
      persist()
    })
    audio.addEventListener('error', () => {
      playbackGeneration += 1
      playbackIntent = false
      playing.value = false
      persist()
    })
  }
  return audio
}

function trackById(id: string): BgmTrack | undefined {
  return tracks.value.find(t => t.id === id)
}

function sourceForId(id: string): string {
  if (id === BGM_LOCAL_TRACK_ID) return localObjectUrl
  const track = trackById(id)
  return track ? musicAssetUrl(track.src) : ''
}

async function fetchManifest(name: string): Promise<BgmTrack[] | null> {
  const data = await readStaticJson<{ tracks?: Array<Partial<BgmTrack>> }>(musicAssetUrl(name))
  if (!data) return null
  if (!Array.isArray(data.tracks)) return null
  return data.tracks
    .map(t => ({
      id: String(t.id || '').trim(),
      title: String(t.title || '').trim(),
      src: String(t.src || '').trim(),
    }))
    .filter(t => (
      !!t.id
      && !!t.src
      && (HTTPS_STREAM.test(t.src) || isSafeRelativeMusicSource(t.src))
    ))
    .map(t => ({ ...t, title: t.title || t.src }))
}

async function loadPlaylist(): Promise<void> {
  playlistError.value = false
  try {
    // `playlist.local.json` is the user's gitignored personal manifest (it
    // rides along with the gitignored audio files); when present it replaces
    // the tracked, deliberately-empty `playlist.json` entirely. Its absence is
    // the normal case, not an error.
    const local = await fetchManifest('playlist.local.json').catch(() => null)
    if (local) {
      tracks.value = local
      return
    }
    const base = await fetchManifest('playlist.json')
    if (base === null) throw new Error('playlist.json missing or malformed')
    tracks.value = base
  } catch (err: unknown) {
    // Missing manifests are a supported setup (no bundled music): the control
    // degrades to "Choose local file…" only.
    console.warn('[useBgm] playlist load failed:', err instanceof Error ? err.message : err)
    playlistError.value = true
    tracks.value = []
  }
}

async function playAudio(el: HTMLAudioElement, failureLabel: string): Promise<void> {
  if (!enabled.value) return
  const generation = ++playbackGeneration
  playbackIntent = true
  try {
    await el.play()
    if (generation !== playbackGeneration || !enabled.value) {
      if (!enabled.value || !playbackIntent) el.pause()
      return
    }
    playing.value = true
  } catch (err: unknown) {
    if (generation !== playbackGeneration) return
    // Autoplay policy or a missing/broken file: reflect reality as paused
    // rather than a stuck "playing" button.
    playbackIntent = false
    console.warn(`[useBgm] ${failureLabel}:`, err instanceof Error ? err.message : err)
    playing.value = false
  }
  persist()
}

async function playTrack(id: string): Promise<void> {
  if (!enabled.value) return
  const src = sourceForId(id)
  if (!src) return
  const el = getAudio()
  // Only swap the source on a genuine track change so re-playing the current
  // track resumes from where it paused instead of restarting.
  if (currentTrackId.value !== id || !el.src) {
    el.src = src
    currentTrackId.value = id
  }
  await playAudio(el, 'play blocked/failed')
}

/**
 * Background-music state + controls, backed by a single shared `Audio`
 * element. Persisted (localStorage `opensquilla-bgm`): the opt-in feature
 * gate, the selected playlist track, volume, and whether playback was left
 * on — `initBgm()` restores them, degrading to paused when the browser
 * rejects the resume without a user gesture.
 */
export function useBgm() {
  function initBgm(): Promise<void> {
    if (initPromise) return initPromise
    initPromise = (async () => {
      const saved = readPersisted()
      const restoreGeneration = playbackGeneration
      if (typeof saved.volume === 'number' && Number.isFinite(saved.volume)) {
        volume.value = Math.min(1, Math.max(0, saved.volume))
      }
      await loadPlaylist()
      // Loading manifests yields to user interaction. Never apply the stale
      // startup snapshot over a local-file selection or an intervening
      // disable/re-enable cycle. Keep a real selection, otherwise choose the
      // new default, and persist the current (necessarily non-stale) state.
      if (restoreGeneration !== playbackGeneration) {
        if (!currentTrackId.value) currentTrackId.value = tracks.value[0]?.id || ''
        persist()
        return
      }
      // Restore the selection: a persisted playlist id wins; the session-only
      // local slot (or an id gone from the manifest) falls back to the first
      // manifest entry — the designated default track — unplayed.
      const savedId = String(saved.trackId || '')
      const restorable = savedId !== BGM_LOCAL_TRACK_ID && !!trackById(savedId)
      currentTrackId.value = restorable ? savedId : tracks.value[0]?.id || ''
      // Object URLs and removed playlist entries cannot survive a reload.
      // Normalize that stale persisted state now so the fallback selection is
      // visibly paused and a later reload cannot reinterpret it as playable.
      if (!restorable && (savedId !== '' || saved.playing === true)) persist()
      // Resume only while the feature gate is on: a disable while a session
      // was left playing must never come back as sound on the next launch. A
      // stale/local selection also falls back paused instead of silently
      // switching to and starting the playlist default.
      if (enabled.value && saved.playing === true && restorable) {
        await playTrack(currentTrackId.value)
      }
    })()
    return initPromise
  }

  function setEnabled(on: boolean) {
    enabled.value = on
    if (!on) {
      // Invalidate even a play() that has not yet set `playing`; its eventual
      // settlement cannot restore sound or persisted playback state.
      playbackGeneration += 1
      playbackIntent = false
      audio?.pause()
      playing.value = false
    }
    persist()
  }

  async function toggle(): Promise<void> {
    if (!enabled.value) return
    if (playing.value) {
      playbackGeneration += 1
      playbackIntent = false
      getAudio().pause()
      playing.value = false
      persist()
      return
    }
    // Nothing selected yet (fresh profile): start the default first track.
    const id = currentTrackId.value && sourceForId(currentTrackId.value)
      ? currentTrackId.value
      : tracks.value[0]?.id || ''
    if (!id) return
    await playTrack(id)
  }

  async function selectTrack(id: string): Promise<void> {
    if (!enabled.value) return
    if (id === currentTrackId.value && playing.value) return
    await playTrack(id)
  }

  function setVolume(v: number) {
    const clamped = Math.min(1, Math.max(0, v))
    volume.value = clamped
    if (audio) audio.volume = clamped
    persist()
  }

  async function playLocalFile(file: File): Promise<void> {
    if (!enabled.value) return
    if (localObjectUrl) URL.revokeObjectURL(localObjectUrl)
    localObjectUrl = URL.createObjectURL(file)
    localTrackTitle.value = file.name
    // Set the source directly: playTrack's same-id guard would skip the swap
    // when a *different* file is picked into the same local slot.
    const el = getAudio()
    el.src = localObjectUrl
    currentTrackId.value = BGM_LOCAL_TRACK_ID
    await playAudio(el, 'local file play failed')
  }

  const currentTitle = computed(() => {
    if (currentTrackId.value === BGM_LOCAL_TRACK_ID) return localTrackTitle.value
    return trackById(currentTrackId.value)?.title || ''
  })

  return {
    enabled,
    tracks,
    playing,
    currentTrackId,
    currentTitle,
    volume,
    playlistError,
    localTrackTitle,
    initBgm,
    setEnabled,
    toggle,
    selectTrack,
    setVolume,
    playLocalFile,
  }
}
