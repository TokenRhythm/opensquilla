import { onUnmounted, ref, shallowRef } from 'vue'
import type { Ref, ShallowRef } from 'vue'
import type { ArtifactPayload } from '@/types/rpc'
import { artifactExtension, artifactName } from '@/utils/chat/artifacts'
import {
  artifactAccessHeaders,
  artifactAccessUrl,
} from '@/utils/chat/artifactAccess'
import {
  artifactPreviewLimit,
  artifactWorkbenchPreviewKind,
  buildOfflineArtifactHtml,
  detectArtifactHtmlRelativeResources,
  renderArtifactMarkdown,
  responseMatchesArtifactPreviewKind,
  type ArtifactWorkbenchPreviewKind,
} from '@/utils/workbench/artifactPreview'

export type ArtifactPreviewResourceState =
  | 'crashed'
  | 'error'
  | 'idle'
  | 'loading'
  | 'missing-resource'
  | 'offline'
  | 'ready'
  | 'ready-with-warnings'
  | 'suspended'
  | 'unsupported'

export type ArtifactPreviewErrorCode =
  | 'download-failed'
  | 'integrity-error'
  | 'invalid-content'
  | 'missing-url'
  | 'native-error'
  | 'native-crashed'
  | 'offline'
  | 'preview-blocked'
  | 'too-large'
  | 'unsupported'

export interface NativeHtmlArtifactResource {
  artifact: ArtifactPayload
  data: ArrayBuffer
  hasRelativeResources: boolean
  mime: string
  relativeResourceCount: number
  sessionKey: string
}

export interface ArtifactPreviewResourceOptions {
  artifact: () => ArtifactPayload
  authToken?: () => string
  baseOrigin?: () => string
  createObjectUrl?: (blob: Blob) => string
  fetchImpl?: typeof fetch
  htmlCollectionStatus?: () => 'complete' | 'partial' | 'not_applicable'
  htmlLaunchUrl?: () => string
  htmlLeaseState?: () => 'ready' | 'pending' | 'blocked'
  nativeHtml?: () => boolean
  onNativeHtmlReady?: (resource: NativeHtmlArtifactResource) => void
  revokeObjectUrl?: (url: string) => void
  sessionKey?: () => string
}

export interface ArtifactPreviewResourceController {
  errorCode: Ref<ArtifactPreviewErrorCode | null>
  kind: Ref<ArtifactWorkbenchPreviewKind>
  markdownHtml: ShallowRef<string>
  objectUrl: ShallowRef<string>
  progress: Ref<number | null>
  relativeResources: ShallowRef<string[]>
  state: Ref<ArtifactPreviewResourceState>
  text: ShallowRef<string>
  dispose: () => void
  load: () => Promise<void>
  markNativeCrashed: () => void
  markNativeError: () => void
  reload: () => Promise<void>
  resume: () => Promise<void>
  suspend: () => void
}

class ArtifactPreviewTooLargeError extends Error {}
class ArtifactPreviewInvalidContentError extends Error {}
class ArtifactPreviewIntegrityError extends Error {}

const INLINE_WORKBENCH_ATTACHMENT_URL_PATTERN =
  /^data:([a-z0-9][a-z0-9.+-]*\/[a-z0-9][a-z0-9.+-]*(?:;charset=[^;,]+)?);base64,([a-z0-9+/=]*)$/i

const GENERIC_IMAGE_MIME_BY_EXTENSION: Record<string, string> = {
  avif: 'image/avif',
  bmp: 'image/bmp',
  gif: 'image/gif',
  ico: 'image/x-icon',
  jpeg: 'image/jpeg',
  jpg: 'image/jpeg',
  png: 'image/png',
  svg: 'image/svg+xml',
  webp: 'image/webp',
}

function defaultBaseOrigin(): string {
  if (typeof window !== 'undefined' && window.location?.origin) return window.location.origin
  return 'http://localhost'
}

function isSameOriginHttpUrl(url: string, baseOrigin: string): boolean {
  try {
    const resolved = new URL(url, baseOrigin)
    const base = new URL(baseOrigin)
    return (resolved.protocol === 'http:' || resolved.protocol === 'https:')
      && resolved.origin === base.origin
  } catch {
    return false
  }
}

function isInlineWorkbenchAttachmentUrl(artifact: ArtifactPayload, url: string): boolean {
  if (artifact.workbenchResourceType !== 'attachment') return false
  // Inline transcript attachments are returned only by the authenticated
  // read-only resource lookup. Keep the exception narrow: base64 data with a
  // concrete MIME type, never an arbitrary URL or executable data shorthand.
  return INLINE_WORKBENCH_ATTACHMENT_URL_PATTERN.test(url)
}

function inlineWorkbenchAttachmentResponse(url: string, limit: number): Response {
  const match = url.match(INLINE_WORKBENCH_ATTACHMENT_URL_PATTERN)
  if (!match) throw new ArtifactPreviewInvalidContentError()

  const contentType = match[1] || ''
  const encoded = match[2] || ''
  // Reject an oversized payload before atob allocates its decoded binary
  // string. The decoded-length check below handles padding and boundary cases.
  if (encoded.length > Math.ceil(limit / 3) * 4) {
    throw new ArtifactPreviewTooLargeError()
  }

  let decoded = ''
  try {
    decoded = globalThis.atob(encoded)
  } catch {
    throw new ArtifactPreviewInvalidContentError()
  }
  if (decoded.length > limit) throw new ArtifactPreviewTooLargeError()

  const bytes = new Uint8Array(decoded.length)
  for (let index = 0; index < decoded.length; index += 1) {
    bytes[index] = decoded.charCodeAt(index)
  }
  return new Response(bytesToArrayBuffer(bytes), {
    status: 200,
    headers: {
      'Content-Length': String(bytes.byteLength),
      'Content-Type': contentType,
    },
  })
}

async function verifyInlineWorkbenchAttachmentIntegrity(
  artifact: ArtifactPayload,
  bytes: Uint8Array,
): Promise<void> {
  if (artifact.size !== undefined && artifact.size !== null && artifact.size !== '') {
    const expectedSize = Number(artifact.size)
    if (
      !Number.isSafeInteger(expectedSize)
      || expectedSize < 0
      || bytes.byteLength !== expectedSize
    ) {
      throw new ArtifactPreviewIntegrityError()
    }
  }

  if (artifact.sha256 === undefined || artifact.sha256 === null || artifact.sha256 === '') return
  const expectedSha256 = String(artifact.sha256).trim().toLowerCase()
  if (!/^[a-f0-9]{64}$/.test(expectedSha256) || !globalThis.crypto?.subtle) {
    throw new ArtifactPreviewIntegrityError()
  }
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytesToArrayBuffer(bytes))
  const actualSha256 = [...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, '0'))
    .join('')
  if (actualSha256 !== expectedSha256) throw new ArtifactPreviewIntegrityError()
}

function defaultCreateObjectUrl(blob: Blob): string {
  return URL.createObjectURL(blob)
}

function defaultRevokeObjectUrl(url: string) {
  URL.revokeObjectURL(url)
}

function isAbortError(error: unknown): boolean {
  return !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
}

function isOfflineError(error: unknown): boolean {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return true
  return error instanceof TypeError && /fetch|network|offline/i.test(error.message)
}

function contentLength(response: Response): number | null {
  const raw = response.headers.get('Content-Length')
  if (!raw) return null
  const value = Number(raw)
  return Number.isFinite(value) && value >= 0 ? value : null
}

async function cancelResponseBody(response: Response) {
  try { await response.body?.cancel() } catch {}
}

async function responseErrorCode(response: Response): Promise<string> {
  if (response.status !== 409) {
    await cancelResponseBody(response)
    return ''
  }
  try {
    const payload: unknown = await response.json()
    if (!payload || typeof payload !== 'object' || !('code' in payload)) return ''
    const code = (payload as { code?: unknown }).code
    return typeof code === 'string' ? code : ''
  } catch {
    return ''
  }
}

async function readResponseBytes(
  response: Response,
  limit: number,
  signal: AbortSignal,
  onProgress: (progress: number | null) => void,
): Promise<Uint8Array> {
  const total = contentLength(response)
  if (total !== null && total > limit) {
    await cancelResponseBody(response)
    throw new ArtifactPreviewTooLargeError()
  }

  const body = response.body
  if (!body) {
    onProgress(null)
    const bytes = new Uint8Array(await response.arrayBuffer())
    if (bytes.byteLength > limit) throw new ArtifactPreviewTooLargeError()
    return bytes
  }

  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  onProgress(total && total > 0 ? 0 : null)

  try {
    for (;;) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      const { done, value } = await reader.read()
      if (done) break
      if (!value) continue
      received += value.byteLength
      if (received > limit) {
        await reader.cancel()
        throw new ArtifactPreviewTooLargeError()
      }
      chunks.push(value)
      if (total && total > 0) {
        onProgress(Math.min(99, Math.round((received / total) * 100)))
      }
    }
  } finally {
    reader.releaseLock()
  }

  const result = new Uint8Array(received)
  let offset = 0
  for (const chunk of chunks) {
    result.set(chunk, offset)
    offset += chunk.byteLength
  }
  return result
}

function bytesToArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer
}

export function createArtifactPreviewResource(
  options: ArtifactPreviewResourceOptions,
): ArtifactPreviewResourceController {
  const state = ref<ArtifactPreviewResourceState>('idle')
  const kind = ref<ArtifactWorkbenchPreviewKind>('unsupported')
  const errorCode = ref<ArtifactPreviewErrorCode | null>(null)
  const progress = ref<number | null>(null)
  const objectUrl = shallowRef('')
  const text = shallowRef('')
  const markdownHtml = shallowRef('')
  const relativeResources = shallowRef<string[]>([])

  const createObjectUrl = options.createObjectUrl || defaultCreateObjectUrl
  const revokeObjectUrl = options.revokeObjectUrl || defaultRevokeObjectUrl

  let activeController: AbortController | null = null
  let generation = 0
  let disposed = false
  let suspended = false
  let stateBeforeSuspend: ArtifactPreviewResourceState = 'idle'
  let nativePayloadDelivered = false
  let objectUrlOwned = false

  function revokeCurrentObjectUrl() {
    if (!objectUrl.value) return
    if (objectUrlOwned) {
      try { revokeObjectUrl(objectUrl.value) } catch {}
    }
    objectUrl.value = ''
    objectUrlOwned = false
  }

  function clearOutput() {
    revokeCurrentObjectUrl()
    text.value = ''
    markdownHtml.value = ''
    relativeResources.value = []
    nativePayloadDelivered = false
  }

  function abortActive() {
    generation += 1
    const controller = activeController
    activeController = null
    if (controller && !controller.signal.aborted) controller.abort()
  }

  function setFailure(
    nextState: Extract<ArtifactPreviewResourceState, 'error' | 'offline' | 'unsupported'>,
    code: ArtifactPreviewErrorCode,
  ) {
    state.value = nextState
    errorCode.value = code
    progress.value = null
  }

  async function load(): Promise<void> {
    if (disposed || suspended) return

    abortActive()
    clearOutput()
    errorCode.value = null
    progress.value = null

    const artifact = options.artifact()
    const nextKind = artifactWorkbenchPreviewKind(artifact)
    kind.value = nextKind
    if (nextKind === 'unsupported') {
      setFailure('unsupported', 'unsupported')
      return
    }
    if (nextKind === 'html') {
      const leaseState = options.htmlLeaseState?.() || 'ready'
      if (leaseState === 'pending') {
        state.value = 'loading'
        return
      }
      if (leaseState === 'blocked') {
        setFailure('error', 'preview-blocked')
        return
      }
    }

    if (nextKind === 'html') {
      const launchUrl = options.htmlLaunchUrl?.() || ''
      try {
        const parsed = new URL(launchUrl)
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
          objectUrl.value = parsed.toString()
          objectUrlOwned = false
          progress.value = 100
          state.value = options.htmlCollectionStatus?.() === 'partial'
            ? 'ready-with-warnings'
            : 'ready'
          return
        }
      } catch {}
    }

    const limit = artifactPreviewLimit(nextKind)
    const declaredSize = Number(artifact.size)
    if (Number.isFinite(declaredSize) && declaredSize > limit) {
      setFailure('unsupported', 'too-large')
      return
    }

    const baseOrigin = options.baseOrigin?.() || defaultBaseOrigin()
    const url = artifactAccessUrl(artifact, baseOrigin)
    const inlineAttachment = isInlineWorkbenchAttachmentUrl(artifact, url)
    if (!url || (!inlineAttachment && !isSameOriginHttpUrl(url, baseOrigin))) {
      setFailure('error', 'missing-url')
      return
    }

    const controller = new AbortController()
    activeController = controller
    const run = ++generation
    state.value = 'loading'

    try {
      let response: Response
      if (inlineAttachment) {
        response = inlineWorkbenchAttachmentResponse(url, limit)
      } else {
        const fetchImpl = options.fetchImpl
          || (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null)
        if (!fetchImpl) {
          setFailure('error', 'download-failed')
          return
        }
        response = await fetchImpl(url, {
          method: 'GET',
          credentials: 'same-origin',
          headers: artifactAccessHeaders(url, {
            authToken: options.authToken?.() || '',
            baseOrigin,
            sessionKey: options.sessionKey?.() || '',
          }),
          redirect: 'error',
          signal: controller.signal,
        })
      }
      if (disposed || suspended || run !== generation) {
        await cancelResponseBody(response)
        return
      }
      if (!response.ok) {
        const code = await responseErrorCode(response)
        if (disposed || suspended || run !== generation) return
        setFailure(
          'error',
          code === 'INTEGRITY_ERROR' ? 'integrity-error' : 'download-failed',
        )
        return
      }

      const responseMime = response.headers.get('Content-Type') || ''
      if (!responseMatchesArtifactPreviewKind(nextKind, responseMime)) {
        await cancelResponseBody(response)
        if (disposed || suspended || run !== generation) return
        setFailure('error', 'invalid-content')
        return
      }

      const bytes = await readResponseBytes(response, limit, controller.signal, value => {
        if (!disposed && run === generation) progress.value = value
      })
      if (disposed || suspended || run !== generation) return
      if (inlineAttachment) {
        await verifyInlineWorkbenchAttachmentIntegrity(artifact, bytes)
        if (disposed || suspended || run !== generation) return
      }

      const responseBaseMime = responseMime.split(';', 1)[0].trim().toLowerCase()
      const declaredMime = String(artifact.mime || '').split(';', 1)[0].trim().toLowerCase()
      const genericResponse = !responseBaseMime
        || responseBaseMime === 'application/octet-stream'
      const inferredImageMime = nextKind === 'image'
        ? GENERIC_IMAGE_MIME_BY_EXTENSION[artifactExtension(artifactName(artifact))]
        : ''
      const mime = nextKind === 'pdf' && genericResponse
        ? 'application/pdf'
        : genericResponse && inferredImageMime
          ? inferredImageMime
          : responseBaseMime || declaredMime || 'application/octet-stream'

      if (nextKind === 'html') {
        const source = new TextDecoder().decode(bytes)
        const missing = detectArtifactHtmlRelativeResources(source)
        relativeResources.value = missing

        if (options.nativeHtml?.() === true) {
          nativePayloadDelivered = true
          options.onNativeHtmlReady?.({
            artifact,
            data: bytesToArrayBuffer(bytes),
            hasRelativeResources: missing.length > 0,
            mime,
            relativeResourceCount: missing.length,
            sessionKey: options.sessionKey?.() || '',
          })
        } else {
          const offlineHtml = buildOfflineArtifactHtml(source)
          const blob = new Blob([offlineHtml], { type: 'text/html;charset=utf-8' })
          const nextObjectUrl = createObjectUrl(blob)
          if (disposed || suspended || run !== generation) {
            try { revokeObjectUrl(nextObjectUrl) } catch {}
            return
          }
          objectUrl.value = nextObjectUrl
          objectUrlOwned = true
        }
        state.value = missing.length > 0 ? 'missing-resource' : 'ready'
      } else if (nextKind === 'markdown') {
        markdownHtml.value = renderArtifactMarkdown(new TextDecoder().decode(bytes))
        state.value = 'ready'
      } else if (nextKind === 'text') {
        text.value = new TextDecoder().decode(bytes)
        state.value = 'ready'
      } else {
        const blob = new Blob([bytesToArrayBuffer(bytes)], { type: mime })
        const nextObjectUrl = createObjectUrl(blob)
        if (disposed || suspended || run !== generation) {
          try { revokeObjectUrl(nextObjectUrl) } catch {}
          return
        }
        objectUrl.value = nextObjectUrl
        objectUrlOwned = true
        state.value = 'ready'
      }
      progress.value = 100
    } catch (error) {
      if (disposed || run !== generation || isAbortError(error)) return
      if (error instanceof ArtifactPreviewTooLargeError) {
        setFailure('unsupported', 'too-large')
      } else if (error instanceof ArtifactPreviewInvalidContentError) {
        setFailure('error', 'invalid-content')
      } else if (error instanceof ArtifactPreviewIntegrityError) {
        setFailure('error', 'integrity-error')
      } else if (isOfflineError(error)) {
        setFailure('offline', 'offline')
      } else {
        setFailure('error', 'download-failed')
      }
    } finally {
      if (activeController === controller) activeController = null
    }
  }

  async function reload(): Promise<void> {
    await load()
  }

  function suspend() {
    if (disposed || suspended) return
    stateBeforeSuspend = state.value
    suspended = true
    abortActive()
    state.value = 'suspended'
    progress.value = null
  }

  async function resume(): Promise<void> {
    if (disposed || !suspended) return
    suspended = false
    const hasReadyOutput = !!(
      objectUrl.value
      || text.value
      || markdownHtml.value
      || nativePayloadDelivered
    )
    if (hasReadyOutput) {
      state.value = stateBeforeSuspend === 'missing-resource'
        || stateBeforeSuspend === 'ready-with-warnings'
        ? stateBeforeSuspend
        : 'ready'
      return
    }
    await load()
  }

  function markNativeCrashed() {
    if (disposed) return
    abortActive()
    state.value = 'crashed'
    errorCode.value = 'native-crashed'
    progress.value = null
  }

  function markNativeError() {
    if (disposed) return
    abortActive()
    state.value = 'error'
    errorCode.value = 'native-error'
    progress.value = null
  }

  function dispose() {
    if (disposed) return
    disposed = true
    abortActive()
    clearOutput()
    state.value = 'idle'
    progress.value = null
    errorCode.value = null
  }

  return {
    errorCode,
    kind,
    markdownHtml,
    objectUrl,
    progress,
    relativeResources,
    state,
    text,
    dispose,
    load,
    markNativeCrashed,
    markNativeError,
    reload,
    resume,
    suspend,
  }
}

export function useArtifactPreviewResource(
  options: ArtifactPreviewResourceOptions,
): ArtifactPreviewResourceController {
  const controller = createArtifactPreviewResource(options)
  onUnmounted(() => controller.dispose())
  return controller
}
