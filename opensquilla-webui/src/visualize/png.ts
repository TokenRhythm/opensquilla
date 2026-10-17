import { assertClipboardPreparationActive } from '@/utils/imageClipboard'

export const VISUALIZE_CAPTURE_MAX_BYTES = 16 * 1024 * 1024
export const VISUALIZE_CAPTURE_MAX_EDGE = 4096
export const VISUALIZE_CAPTURE_MAX_PIXELS = 16_777_216
export const VISUALIZE_CAPTURE_TIMEOUT_MS = 15_000
const MAX_LAYOUT_EDGE = 16_384
const PREFIX = 'data:image/png;base64,'
const SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10]

export class VisualizeCaptureError extends Error {
  constructor(readonly kind: 'invalidPng' | 'tooLarge' | 'blank' | 'timedOut' | 'unavailable') {
    super({ invalidPng: 'The canvas did not return a valid PNG image', tooLarge: 'The canvas image exceeds the export size limit',
      blank: 'The canvas image is empty', timedOut: 'The canvas image capture timed out', unavailable: 'The canvas is not ready to capture' }[kind])
    this.name = 'VisualizeCaptureError'
  }
}

export function checkCaptureLayout(width: number, height: number): void {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width < 2 || height < 2) throw new VisualizeCaptureError('blank')
  // 0.0.5 clamps scale to at least 0.1, so cap the source layout as well.
  if (width > MAX_LAYOUT_EDGE || height > MAX_LAYOUT_EDGE || width * height > VISUALIZE_CAPTURE_MAX_PIXELS) throw new VisualizeCaptureError('tooLarge')
}

function pngBytes(dataUrl: unknown): { bytes: Uint8Array<ArrayBuffer>; width: number; height: number } {
  if (typeof dataUrl !== 'string' || !dataUrl.startsWith(PREFIX)) throw new VisualizeCaptureError('invalidPng')
  const encoded = dataUrl.slice(PREFIX.length)
  if (encoded.length > Math.ceil(VISUALIZE_CAPTURE_MAX_BYTES / 3) * 4) throw new VisualizeCaptureError('tooLarge')
  if (!encoded || encoded.length % 4 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) throw new VisualizeCaptureError('invalidPng')
  let binary: string
  try { binary = atob(encoded) } catch { throw new VisualizeCaptureError('invalidPng') }
  if (binary.length > VISUALIZE_CAPTURE_MAX_BYTES) throw new VisualizeCaptureError('tooLarge')
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  if (bytes.length < 45 || SIGNATURE.some((value, index) => bytes[index] !== value)
    || bytes[12] !== 73 || bytes[13] !== 72 || bytes[14] !== 68 || bytes[15] !== 82) throw new VisualizeCaptureError('invalidPng')
  const header = new DataView(bytes.buffer)
  if (header.getUint32(8) !== 13) throw new VisualizeCaptureError('invalidPng')
  const width = header.getUint32(16), height = header.getUint32(20)
  if (width < 2 || height < 2) throw new VisualizeCaptureError('blank')
  if (width > VISUALIZE_CAPTURE_MAX_EDGE || height > VISUALIZE_CAPTURE_MAX_EDGE || width * height > VISUALIZE_CAPTURE_MAX_PIXELS) throw new VisualizeCaptureError('tooLarge')
  return { bytes, width, height }
}

function loadPng(blob: Blob, signal: AbortSignal, doc: Document): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    assertClipboardPreparationActive(signal)
    const image = doc.createElement('img')
    const url = URL.createObjectURL(blob)
    const cleanup = () => {
      image.onload = image.onerror = null
      signal.removeEventListener('abort', aborted)
      URL.revokeObjectURL(url)
    }
    const aborted = () => {
      cleanup(); image.removeAttribute('src')
      reject(signal.reason instanceof Error ? signal.reason : new DOMException('Cancelled', 'AbortError'))
    }
    image.onload = () => { cleanup(); resolve(image) }
    image.onerror = () => { cleanup(); reject(new VisualizeCaptureError('invalidPng')) }
    signal.addEventListener('abort', aborted, { once: true })
    image.decoding = 'async'
    image.src = url
  })
}

/** Accept only real decoded PNG pixels, never the source HTML receipt's MIME. */
export async function validateCapturePng(screenshot: unknown, signal: AbortSignal, doc: Document = document): Promise<Blob> {
  assertClipboardPreparationActive(signal)
  if (!screenshot || typeof screenshot !== 'object') throw new VisualizeCaptureError('invalidPng')
  const result = screenshot as { dataUrl?: unknown; width: number; height: number }
  checkCaptureLayout(result.width, result.height)
  const { bytes, width, height } = pngBytes(result.dataUrl)
  const blob = new Blob([bytes], { type: 'image/png' })
  const image = await loadPng(blob, signal, doc)
  const canvas = doc.createElement('canvas')
  try {
    assertClipboardPreparationActive(signal)
    if (image.naturalWidth !== width || image.naturalHeight !== height) throw new VisualizeCaptureError('invalidPng')
    canvas.width = width; canvas.height = height
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) throw new VisualizeCaptureError('unavailable')
    context.drawImage(image, 0, 0)
    const pixels = context.getImageData(0, 0, width, height).data
    // A completely transparent raster is unusable. A legitimate solid-color
    // canvas must remain exportable; visual fidelity needs browser acceptance.
    let visible = false
    for (let i = 3; i < pixels.length; i += 4) if (pixels[i]) { visible = true; break }
    if (!visible) throw new VisualizeCaptureError('blank')
    assertClipboardPreparationActive(signal)
    return blob
  } finally {
    canvas.width = canvas.height = 0
    image.removeAttribute('src')
  }
}
