import { computed, nextTick, onUnmounted, ref, watch, type Ref } from 'vue'
import type { ArtifactPayload } from '@/types/rpc'
import { fetchArtifactBlob } from '@/utils/chat/artifactAccess'

export type InlineMediaKind = 'audio' | 'video'
export type InlineMediaState = 'idle' | 'loading' | 'ready' | 'error' | 'unsupported'

interface InlineMediaArtifactOptions {
  artifact: () => ArtifactPayload
  sessionKey: () => string | undefined
  authToken: () => string | undefined
  kind: InlineMediaKind
  element: Ref<HTMLMediaElement | null>
}

/**
 * Authenticated, explicit-load lifecycle shared by transcript audio and video.
 * The fetched bytes and object URL remain runtime-only and are discarded when
 * the artifact identity or its session context changes.
 */
export function useInlineMediaArtifact(options: InlineMediaArtifactOptions) {
  const state = ref<InlineMediaState>('idle')
  const objectUrl = ref('')
  let requestController: AbortController | null = null

  const identity = computed(() => {
    const artifact = options.artifact()
    return [
      artifact.id,
      artifact.key,
      artifact.download_url,
      artifact.name,
      artifact.mime,
      artifact.size,
    ].map(value => String(value || '')).join('\u0000')
  })

  function revokeObjectUrl() {
    const url = objectUrl.value
    objectUrl.value = ''
    if (!url) return
    try { URL.revokeObjectURL(url) } catch {}
  }

  function reset() {
    requestController?.abort()
    requestController = null
    try { options.element.value?.pause() } catch {}
    revokeObjectUrl()
    state.value = 'idle'
  }

  function supportedByBrowser(blob: Blob): boolean {
    const artifact = options.artifact()
    const responseMime = String(blob.type || '').split(';', 1)[0].trim().toLowerCase()
    const declaredMime = String(artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
    const prefix = `${options.kind}/`
    const mime = responseMime.startsWith(prefix) ? responseMime : declaredMime
    if (!mime.startsWith(prefix)) return true
    try {
      const probe = document.createElement(options.kind)
      return typeof probe.canPlayType !== 'function' || probe.canPlayType(mime) !== ''
    } catch {
      return true
    }
  }

  async function load() {
    if (state.value === 'loading' || state.value === 'ready') return
    requestController?.abort()
    const controller = new AbortController()
    requestController = controller
    state.value = 'loading'
    try {
      const fetched = await fetchArtifactBlob(options.artifact(), {
        baseOrigin: window.location.origin,
        sessionKey: options.sessionKey(),
        authToken: options.authToken(),
        signal: controller.signal,
        requireSameOrigin: true,
      })
      if (controller.signal.aborted || requestController !== controller) return
      requestController = null
      if (!fetched.ok) {
        state.value = 'error'
        return
      }
      if (!supportedByBrowser(fetched.blob)) {
        state.value = 'unsupported'
        return
      }
      objectUrl.value = URL.createObjectURL(fetched.blob)
      state.value = 'ready'
      await nextTick()
      const playback = options.element.value?.play()
      if (playback && typeof playback.catch === 'function') void playback.catch(() => undefined)
    } catch (error) {
      if (controller.signal.aborted || (
        typeof DOMException !== 'undefined' && error instanceof DOMException && error.name === 'AbortError'
      )) return
      if (requestController === controller) requestController = null
      state.value = 'error'
    }
  }

  function markUnsupported() {
    try { options.element.value?.pause() } catch {}
    revokeObjectUrl()
    state.value = 'unsupported'
  }

  watch(
    () => [identity.value, options.sessionKey() || '', options.authToken() || ''],
    (_next, previous) => { if (previous) reset() },
  )

  onUnmounted(reset)

  return {
    state,
    objectUrl,
    load,
    markUnsupported,
  }
}
