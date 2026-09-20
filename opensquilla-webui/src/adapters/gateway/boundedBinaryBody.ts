/** Byte limits stay inside the binary capability, before its consumer buffers content. */
export interface ReadableBinaryBody {
  readonly metadata: {
    readonly contentLength?: number
    readonly contentType?: string
  }
  blob(): Promise<Blob>
  stream(): ReadableStream<Uint8Array> | null
}

export class BinaryBodyTooLargeError extends Error {
  readonly name = 'BinaryBodyTooLargeError'
  readonly code = 'too_large'

  constructor() {
    super('File exceeds the requested byte limit.')
  }
}

export function assertBinarySize(size: number, maxBytes?: number): void {
  if (maxBytes === undefined) return
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) {
    throw new RangeError('Binary byte limit must be a non-negative safe integer.')
  }
  if (size > maxBytes) throw new BinaryBodyTooLargeError()
}

export function assertBinaryReadActive(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
}

function cancelBody(body: { cancel(reason?: unknown): Promise<unknown> }, reason: unknown): void {
  try { void body.cancel(reason).catch(() => undefined) } catch {}
}

async function readWithAbort<T>(
  read: () => Promise<T>,
  signal?: AbortSignal,
  cancel?: (reason: unknown) => void,
): Promise<T> {
  assertBinaryReadActive(signal)
  if (!signal) return read()
  let removeListener: (() => void) | undefined
  try {
    return await new Promise<T>((resolve, reject) => {
      const onAbort = () => {
        const error = new DOMException('Aborted', 'AbortError')
        cancel?.(error)
        reject(error)
      }
      signal.addEventListener('abort', onAbort, { once: true })
      removeListener = () => signal.removeEventListener('abort', onAbort)
      if (signal.aborted) { onAbort(); return }
      read().then(resolve, reject)
    })
  } finally {
    removeListener?.()
  }
}

export async function readBinaryBlob(
  response: ReadableBinaryBody,
  options: { maxBytes?: number; signal?: AbortSignal } = {},
): Promise<Blob> {
  const { maxBytes, signal } = options
  assertBinaryReadActive(signal)
  // Existing download consumers retain their one-shot Blob path and no new ceiling.
  if (maxBytes === undefined) return readWithAbort(() => response.blob(), signal)
  assertBinarySize(0, maxBytes)
  const body = response.stream()
  const contentLength = response.metadata.contentLength
  if (typeof contentLength === 'number' && Number.isFinite(contentLength) && contentLength > maxBytes) {
    const error = new BinaryBodyTooLargeError()
    if (body) cancelBody(body, error)
    throw error
  }
  if (!body) {
    const blob = await readWithAbort(() => response.blob(), signal)
    assertBinaryReadActive(signal)
    assertBinarySize(blob.size, maxBytes)
    return blob
  }

  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let received = 0
  try {
    for (;;) {
      const { done, value } = await readWithAbort(
        () => reader.read(), signal, reason => cancelBody(reader, reason),
      )
      assertBinaryReadActive(signal)
      if (done) break
      received += value.byteLength
      assertBinarySize(received, maxBytes)
      chunks.push(value)
    }
    return new Blob(chunks as BlobPart[], { type: response.metadata.contentType || '' })
  } catch (error) {
    cancelBody(reader, error)
    throw error
  } finally {
    try { reader.releaseLock() } catch {}
  }
}
