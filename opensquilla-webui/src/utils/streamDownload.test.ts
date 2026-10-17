// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { saveByteStream } from './streamDownload'
import { downloadBlob } from './browser'

vi.mock('./browser', () => ({ downloadBlob: vi.fn() }))
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks() })
function chunks(...values: string[]) {
  return new ReadableStream<Uint8Array>({ start(controller) {
    for (const value of values) controller.enqueue(new TextEncoder().encode(value))
    controller.close()
  } })
}
function disk() {
  const target = { write: vi.fn().mockResolvedValue(undefined), close: vi.fn().mockResolvedValue(undefined), abort: vi.fn().mockResolvedValue(undefined) }
  const picker = vi.fn().mockResolvedValue({ createWritable: async () => target })
  vi.stubGlobal('showSaveFilePicker', picker)
  return { target, picker }
}
describe('bounded file saving', () => {
  it('chooses the target first and writes chunks without a renderer Blob', async () => {
    const { target, picker } = disk()
    const source = vi.fn(async () => { expect(picker).toHaveBeenCalledOnce(); return chunks('a', '🙂', '\r\n') })
    expect(await saveByteStream('test.txt', source, { expectedBytes: 7 })).toBe('saved')
    expect(target.write).toHaveBeenCalledTimes(3)
    expect(target.close).toHaveBeenCalledOnce()
    expect(downloadBlob).not.toHaveBeenCalled()
  })
  it('picker cancellation does not fetch any body', async () => {
    vi.stubGlobal('showSaveFilePicker', vi.fn().mockRejectedValue(new DOMException('Cancelled', 'AbortError')))
    const source = vi.fn()
    expect(await saveByteStream('test.txt', source)).toBe('cancelled')
    expect(source).not.toHaveBeenCalled()
  })
  it('aborts incomplete files instead of committing a successful download', async () => {
    const { target } = disk()
    await expect(saveByteStream('test.txt', async () => chunks('short'), { expectedBytes: 20 })).rejects.toThrow('Incomplete')
    expect(target.abort).toHaveBeenCalledOnce()
    expect(target.close).not.toHaveBeenCalled()
  })
  it('aborts disk writes when the owner closes mid-read', async () => {
    const { target } = disk()
    const controller = new AbortController()
    const cancel = vi.fn()
    const save = saveByteStream('test.txt', async () => new ReadableStream({ pull() { controller.abort() }, cancel }), { signal: controller.signal })
    await expect(save).rejects.toThrow('Aborted')
    expect(target.abort).toHaveBeenCalledOnce()
    expect(target.close).not.toHaveBeenCalled()
    expect(cancel).toHaveBeenCalled()
  })
  it('keeps small compatibility downloads but rejects known large files before fetch', async () => {
    vi.stubGlobal('showSaveFilePicker', undefined)
    expect(await saveByteStream('a.txt', async () => chunks('ok'), { expectedBytes: 2 })).toBe('saved')
    expect(downloadBlob).toHaveBeenCalledOnce()
    const source = vi.fn()
    await expect(saveByteStream('huge.txt', source, { expectedBytes: 9 * 1024 * 1024 })).rejects.toThrow('unavailable')
    expect(source).not.toHaveBeenCalled()
  })
  it('unknown-length fallback cancels at its memory budget', async () => {
    vi.stubGlobal('showSaveFilePicker', undefined)
    const cancel = vi.fn()
    await expect(saveByteStream('huge.txt', async () => new ReadableStream({
      pull(controller) { controller.enqueue(new Uint8Array(1024 * 1024)) }, cancel,
    }))).rejects.toThrow('fallback limit')
    expect(cancel).toHaveBeenCalled()
    expect(downloadBlob).not.toHaveBeenCalled()
  })
})
