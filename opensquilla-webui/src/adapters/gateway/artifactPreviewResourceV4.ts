import { ref, shallowRef } from 'vue'
import type {
  ArtifactPreviewResourceController,
  ArtifactPreviewResourceErrorCode,
  ArtifactPreviewResourceOptions,
  ArtifactPreviewResourceState,
} from '@/modules/artifactWorkbench'
import type { ArtifactPayload } from '@/types/artifacts'
import { artifactExtension, artifactName } from '@/utils/chat/artifacts'
import {
  artifactHttpAccessUrl,
  bindArtifactBinaryRequest,
  runtimeArtifactHttpBaseOrigin,
} from './privateArtifactHttpTransport'
import {
  HttpTransportError,
} from './privateHttpTransport'
import {
  type ArtifactWorkbenchPreviewKind,
  artifactPreviewLimit,
  artifactWorkbenchPreviewKind,
  buildOfflineArtifactHtml,
  detectArtifactHtmlRelativeResources,
  renderArtifactMarkdown,
  responseMatchesArtifactPreviewKind,
} from '@/utils/workbench/artifactPreview'

class ArtifactPreviewTooLargeError extends Error {}
class ArtifactPreviewInvalidContentError extends Error {}
class ArtifactPreviewIntegrityError extends Error {}

interface ArtifactPreviewResourceBinaryResponse {
  readonly metadata: {
    readonly contentLength?: number
    readonly contentType?: string
    readonly status: number
  }
  blob(): Promise<Blob>
  stream(): ReadableStream<Uint8Array> | null
}

interface ArtifactPreviewResourceHttpTransport {
  requestBinary(endpoint: string, options?: {
    sessionKey?: string
    signal?: AbortSignal
    timeoutMs?: number
  }): Promise<ArtifactPreviewResourceBinaryResponse>
}

interface ArtifactPreviewResourceAdapterOptions extends ArtifactPreviewResourceOptions {
  /** Adapter-only endpoint origin; domain consumers receive a bound capability. */
  baseOrigin?: () => string
}

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
  return runtimeArtifactHttpBaseOrigin()
}

function isInlineWorkbenchAttachmentUrl(artifact: ArtifactPayload, url: string): boolean {
  if (artifact.workbenchResourceType !== 'attachment') return false
  // Inline transcript attachments are returned only by the authenticated
  // read-only resource lookup. Keep the exception narrow: base64 data with a
  // concrete MIME type, never an arbitrary URL or executable data shorthand.
  return INLINE_WORKBENCH_ATTACHMENT_URL_PATTERN.test(url)
}

function inlineWorkbenchAttachmentResponse(
  url: string,
  limit: number,
): ArtifactPreviewResourceBinaryResponse {
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
  const blob = new Blob([bytesToArrayBuffer(bytes)], { type: contentType })
  return {
    metadata: {
      status: 200,
      contentLength: bytes.byteLength,
      contentType,
    },
    blob: async () => blob,
    stream: () => blob.stream(),
  }
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
  return error instanceof HttpTransportError
    ? error.kind === 'aborted'
    : !!error && typeof error === 'object' && 'name' in error && error.name === 'AbortError'
}

function isOfflineError(error: unknown): boolean {
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return true
  if (error instanceof HttpTransportError) return error.kind === 'network'
  return error instanceof TypeError && /fetch|network|offline/i.test(error.message)
}

function contentLength(response: ArtifactPreviewResourceBinaryResponse): number | null {
  const value = response.metadata.contentLength
  if (value === undefined) return null
  return Number.isFinite(value) && value >= 0 ? value : null
}

async function cancelResponseBody(response: ArtifactPreviewResourceBinaryResponse) {
  try { await response.stream()?.cancel() } catch {}
}

async function readResponseBytes(
  response: ArtifactPreviewResourceBinaryResponse,
  limit: number,
  signal: AbortSignal,
  onProgress: (progress: number | null) => void,
): Promise<Uint8Array> {
  const total = contentLength(response)
  if (total !== null && total > limit) {
    await cancelResponseBody(response)
    throw new ArtifactPreviewTooLargeError()
  }

  const body = response.stream()
  if (!body) {
    onProgress(null)
    const blob = await response.blob()
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (blob.size > limit) throw new ArtifactPreviewTooLargeError()
    return new Uint8Array(await blob.arrayBuffer())
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
  http: Pick<ArtifactPreviewResourceHttpTransport, 'requestBinary'>,
  options: ArtifactPreviewResourceAdapterOptions,
): ArtifactPreviewResourceController {
  const state = ref<ArtifactPreviewResourceState>('idle')
  const kind = ref<ArtifactWorkbenchPreviewKind>('unsupported')
  const errorCode = ref<ArtifactPreviewResourceErrorCode | null>(null)
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
    code: ArtifactPreviewResourceErrorCode,
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
    const url = artifactHttpAccessUrl(artifact, baseOrigin)
    const inlineAttachment = isInlineWorkbenchAttachmentUrl(artifact, url)
    const request = inlineAttachment
      ? null
      : bindArtifactBinaryRequest(http, artifact, {
        baseOrigin,
        policy: 'trusted-origin',
      })
    if (!url || (!inlineAttachment && !request)) {
      setFailure('error', 'missing-url')
      return
    }

    const controller = new AbortController()
    activeController = controller
    const run = ++generation
    state.value = 'loading'

    try {
      let response: ArtifactPreviewResourceBinaryResponse
      if (inlineAttachment) {
        response = inlineWorkbenchAttachmentResponse(url, limit)
      } else {
        response = await request!.execute({
          sessionKey: options.sessionKey?.() || '',
          signal: controller.signal,
          timeoutMs: 0,
        })
      }
      if (disposed || suspended || run !== generation) {
        await cancelResponseBody(response)
        return
      }
      const responseMime = response.metadata.contentType || ''
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
      } else if (
        error instanceof HttpTransportError
        && error.kind === 'http-status'
        && error.status === 409
        && error.payload
        && typeof error.payload === 'object'
        && 'code' in error.payload
        && (error.payload as { code?: unknown }).code === 'INTEGRITY_ERROR'
      ) {
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
