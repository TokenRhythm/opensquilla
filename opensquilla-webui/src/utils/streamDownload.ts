import { downloadBlob } from '@/utils/browser'

const FALLBACK_BYTE_LIMIT = 8 * 1024 * 1024
interface SaveTarget {
  write(data: Uint8Array): Promise<void>
  close(): Promise<void>
  abort(): Promise<void>
}
interface SavePicker {
  (options: { suggestedName: string }): Promise<{ createWritable(): Promise<SaveTarget> }>
}

/** Pick the destination before requesting bytes. Older browsers get a bounded fallback. */
export async function saveByteStream(
  filename: string,
  source: () => Promise<ReadableStream<Uint8Array>>,
  options: { signal?: AbortSignal; expectedBytes?: number; mime?: string } = {},
): Promise<'saved' | 'cancelled'> {
  const picker = (globalThis as typeof globalThis & { showSaveFilePicker?: SavePicker }).showSaveFilePicker
  let target: SaveTarget | undefined
  const active = () => {
    if (options.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
  }
  active()
  if (picker) {
    try { target = await (await picker({ suggestedName: filename })).createWritable() }
    catch (error) {
      if (error instanceof Error && error.name === 'AbortError') return 'cancelled'
      throw error
    }
  } else if (options.expectedBytes !== undefined && options.expectedBytes > FALLBACK_BYTE_LIMIT) {
    throw new Error('Streaming file saving is unavailable in this browser')
  }
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  const chunks: Uint8Array<ArrayBuffer>[] = []
  let size = 0
  const abort = () => { void reader?.cancel().catch(() => {}) }
  options.signal?.addEventListener('abort', abort, { once: true })
  try {
    active()
    reader = (await source()).getReader()
    active()
    for (;;) {
      const { done, value } = await reader.read()
      active()
      if (done) break
      size += value.byteLength
      if (options.expectedBytes !== undefined && size > options.expectedBytes) throw new Error('Download size mismatch')
      if (target) await target.write(value)
      else {
        if (size > FALLBACK_BYTE_LIMIT) throw new Error('Download exceeds browser fallback limit')
        chunks.push(new Uint8Array(value))
      }
    }
    if (options.expectedBytes !== undefined && size !== options.expectedBytes) throw new Error('Incomplete download')
    active()
    if (target) await target.close()
    else downloadBlob(new Blob(chunks, { type: options.mime || 'application/octet-stream' }), filename)
    return 'saved'
  } catch (error) {
    await reader?.cancel().catch(() => {})
    await target?.abort().catch(() => {})
    throw error
  } finally {
    options.signal?.removeEventListener('abort', abort)
    reader?.releaseLock()
  }
}
