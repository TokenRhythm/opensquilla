import type { ImageLightboxItem } from '@/composables/chat/useArtifactImageLightbox'

export type ClipboardImageSource = ImageLightboxItem
export type ImageClipboardMode = 'image' | 'svg-source'
export interface ClipboardImageDescriptor { name?: unknown; mime?: unknown }

export const IMAGE_CLIPBOARD_MAX_BYTES = 30 * 1024 * 1024
export const SVG_CLIPBOARD_MAX_BYTES = 1024 * 1024
export const IMAGE_CLIPBOARD_MAX_EDGE = 8192
export const IMAGE_CLIPBOARD_MAX_PIXELS = 16_777_216
export const IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS = 30_000

export class ImageClipboardError extends Error {
  constructor(readonly kind: 'unsupported' | 'failed' | 'invalidImage' | 'invalidSvg' | 'tooLarge' | 'timedOut') {
    super(kind)
    this.name = 'ImageClipboardError'
  }
}

const SVG_MIME = 'image/svg+xml'
const SVG_NAMESPACE = 'http://www.w3.org/2000/svg'
const GENERIC_MIMES = new Set(['', 'application/octet-stream'])
const SVG_FALLBACK_MIMES = new Set([...GENERIC_MIMES, 'text/plain', 'text/xml', 'application/xml'])
const RASTER_EXTENSIONS: Record<string, string> = {
  png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp',
  gif: 'image/gif', avif: 'image/avif', bmp: 'image/bmp', ico: 'image/x-icon',
}

function mime(value: unknown): string {
  return typeof value === 'string' ? value.split(';', 1)[0]!.trim().toLowerCase() : ''
}

function extension(value: unknown): string {
  if (typeof value !== 'string') return ''
  const name = value.trim().toLowerCase()
  const dot = name.lastIndexOf('.')
  return dot < 0 ? '' : name.slice(dot + 1)
}

export function isSvgClipboardCandidate(descriptor: ClipboardImageDescriptor): boolean {
  const type = mime(descriptor.mime)
  return type === SVG_MIME || (extension(descriptor.name) === 'svg' && SVG_FALLBACK_MIMES.has(type))
}

export function isClipboardImageCandidate(descriptor: ClipboardImageDescriptor): boolean {
  const type = mime(descriptor.mime)
  return type.startsWith('image/') || isSvgClipboardCandidate(descriptor)
    || (GENERIC_MIMES.has(type) && !!RASTER_EXTENSIONS[extension(descriptor.name)])
}

/** Compare content identity without treating a fresh UI wrapper as a new file. */
export function imageClipboardSourceIdentity(source: ClipboardImageSource | null): unknown[] {
  if (!source) return []
  if (source.kind === 'artifact') {
    const file = source.artifact
    return [source.kind, file.id, file.key, file.name, file.mime, file.size, file.download_url,
      file.sha256, file.source, file.documentId, file.previewPagePath, file.sessionKey,
      file.session_key, file.epoch, file.generationEpoch, file.generation_epoch,
      JSON.stringify(file.reference)]
  }
  const file = source.attachment
  return [source.kind, file.kind, file.displayId, file.renderKey, file.name, file.mime, file.size,
    file.download_url, file.sha256_ref, file.attachmentId, file.data, file.dataUrl,
    file.downloadData, file.localFile, file.workspaceFile?.workspaceId,
    file.workspaceFile?.relativePath, file.workspaceFile?.name, file.workspaceFile?.mime,
    file.workspaceFile?.size]
}

export function assertClipboardPreparationActive(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason instanceof Error
    ? signal.reason : new DOMException('Cancelled', 'AbortError')
}

/** Race even non-cooperative image decoders/transports against cancellation. */
export function abortableClipboardPreparation<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const aborted = () => reject(signal.reason instanceof Error
      ? signal.reason : new DOMException('Cancelled', 'AbortError'))
    signal.addEventListener('abort', aborted, { once: true })
    if (signal.aborted) aborted()
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', aborted))
      .catch(() => {})
  })
}

/** Invoke during the click, before any await, to retain Safari user activation. */
export function writePreparedClipboard(
  mode: ImageClipboardMode,
  prepare: () => Promise<Blob>,
): Promise<void> {
  const type = mode === 'image' ? 'image/png' : 'text/plain'
  try {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.write
      || typeof ClipboardItem === 'undefined'
      || (typeof ClipboardItem.supports === 'function' && !ClipboardItem.supports(type))) {
      return Promise.reject(new ImageClipboardError('unsupported'))
    }
    const preparation = Promise.resolve().then(prepare)
    // A constructor/write rejection can happen before the browser consumes it.
    void preparation.catch(() => {})
    return navigator.clipboard.write([new ClipboardItem({ [type]: preparation })])
  } catch (error) {
    return Promise.reject(error)
  }
}

function checkDimensions(width: number, height: number): { width: number; height: number } {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new ImageClipboardError('invalidImage')
  }
  const dimensions = { width: Math.ceil(width), height: Math.ceil(height) }
  if (dimensions.width > IMAGE_CLIPBOARD_MAX_EDGE || dimensions.height > IMAGE_CLIPBOARD_MAX_EDGE
    || dimensions.width * dimensions.height > IMAGE_CLIPBOARD_MAX_PIXELS) {
    throw new ImageClipboardError('tooLarge')
  }
  return dimensions
}

function svgLength(value: string | null): number | null {
  if (!value) return null
  const match = value.trim().match(/^([+]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)(px|in|cm|mm|q|pt|pc)?$/i)
  if (!match) {
    if (/^(?:\d+\.?\d*|\.\d+)%$/.test(value.trim()) && Number.parseFloat(value) > 0) return null
    throw new ImageClipboardError('invalidSvg')
  }
  const unit = (match[2] || 'px').toLowerCase()
  const multiplier: Record<string, number> = { px: 1, in: 96, cm: 96 / 2.54, mm: 96 / 25.4, q: 96 / 101.6, pt: 96 / 72, pc: 16 }
  return Number(match[1]) * multiplier[unit]!
}

function svgDimensions(root: Element): { width: number; height: number } {
  let width = svgLength(root.getAttribute('width'))
  let height = svgLength(root.getAttribute('height'))
  const box = root.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number)
  const validBox = box?.length === 4 && box.every(Number.isFinite) && box[2]! > 0 && box[3]! > 0
  if (width === null || height === null) {
    if (!validBox || !box) throw new ImageClipboardError('invalidSvg')
    if (width !== null) height = width * box[3]! / box[2]!
    else if (height !== null) width = height * box[2]! / box[3]!
    else { width = box[2]!; height = box[3]! }
  }
  return checkDimensions(width, height!)
}

async function readSvg(blob: Blob, signal: AbortSignal): Promise<{ text: string; root: Element }> {
  const bytes = await abortableClipboardPreparation(blob.arrayBuffer(), signal)
  assertClipboardPreparationActive(signal)
  let text: string
  try {
    // ignoreBOM retains an original UTF-8 BOM in the source copied as text.
    text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)
  } catch { throw new ImageClipboardError('invalidSvg') }
  if (text.includes('\0')) throw new ImageClipboardError('invalidSvg')
  const doc = new DOMParser().parseFromString(text, SVG_MIME)
  const root = doc.documentElement
  if (!root || root.localName !== 'svg' || root.namespaceURI !== SVG_NAMESPACE
    || doc.getElementsByTagName('parsererror').length) throw new ImageClipboardError('invalidSvg')
  return { text, root }
}

function loadClipboardImage(blob: Blob, signal: AbortSignal): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    assertClipboardPreparationActive(signal)
    const image = new Image()
    const url = URL.createObjectURL(blob)
    const cleanup = () => {
      image.onload = null
      image.onerror = null
      signal.removeEventListener('abort', aborted)
      URL.revokeObjectURL(url)
    }
    const aborted = () => {
      cleanup()
      image.removeAttribute('src')
      reject(signal.reason instanceof Error ? signal.reason : new DOMException('Cancelled', 'AbortError'))
    }
    image.onload = () => { cleanup(); resolve(image) }
    image.onerror = () => { cleanup(); reject(new ImageClipboardError('invalidImage')) }
    signal.addEventListener('abort', aborted, { once: true })
    image.decoding = 'async'
    // Never attach the SVG document or image to the DOM. SVG image mode disables
    // scripts and external subresources; the canvas receives pixels only.
    image.src = url
  })
}

function canvasPng(canvas: HTMLCanvasElement, signal: AbortSignal): Promise<Blob> {
  return abortableClipboardPreparation(new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(blob => blob ? resolve(blob) : reject(new ImageClipboardError('invalidImage')), 'image/png')
  }), signal)
}

export async function prepareClipboardBlob(
  blob: Blob,
  descriptor: ClipboardImageDescriptor,
  mode: ImageClipboardMode,
  signal: AbortSignal,
): Promise<Blob> {
  assertClipboardPreparationActive(signal)
  if (blob.size > (mode === 'image' ? IMAGE_CLIPBOARD_MAX_BYTES : SVG_CLIPBOARD_MAX_BYTES)) {
    throw new ImageClipboardError('tooLarge')
  }
  const responseMime = mime(blob.type)
  const declaredMime = mime(descriptor.mime)
  const svg = isSvgClipboardCandidate(descriptor)
  if (svg) {
    if (responseMime !== SVG_MIME && !SVG_FALLBACK_MIMES.has(responseMime)) {
      throw new ImageClipboardError('invalidSvg')
    }
  } else if (mode === 'svg-source' || !isClipboardImageCandidate(descriptor)
    || responseMime === SVG_MIME
    || (!GENERIC_MIMES.has(responseMime) && !responseMime.startsWith('image/'))) {
    throw new ImageClipboardError(mode === 'svg-source' ? 'invalidSvg' : 'invalidImage')
  }
  let imageBlob = blob
  if (svg) {
    const { text, root } = await readSvg(blob, signal)
    if (mode === 'svg-source') {
      assertClipboardPreparationActive(signal)
      return new Blob([text], { type: 'text/plain' })
    }
    const dimensions = svgDimensions(root)
    // Give viewBox-only/relative-size SVGs explicit, bounded raster dimensions.
    root.setAttribute('width', String(dimensions.width))
    root.setAttribute('height', String(dimensions.height))
    imageBlob = new Blob([new XMLSerializer().serializeToString(root)], { type: SVG_MIME })
  } else if (GENERIC_MIMES.has(responseMime)) {
    const inferred = declaredMime.startsWith('image/') ? declaredMime : RASTER_EXTENSIONS[extension(descriptor.name)]
    if (!inferred) throw new ImageClipboardError('invalidImage')
    imageBlob = blob.slice(0, blob.size, inferred)
  }
  const image = await loadClipboardImage(imageBlob, signal)
  const canvas = document.createElement('canvas')
  try {
    assertClipboardPreparationActive(signal)
    const dimensions = checkDimensions(image.naturalWidth, image.naturalHeight)
    canvas.width = dimensions.width
    canvas.height = dimensions.height
    const context = canvas.getContext('2d')
    if (!context) throw new ImageClipboardError('invalidImage')
    context.drawImage(image, 0, 0)
    const png = await canvasPng(canvas, signal)
    assertClipboardPreparationActive(signal)
    if (mime(png.type) !== 'image/png') throw new ImageClipboardError('invalidImage')
    return png
  } finally {
    image.removeAttribute('src')
    canvas.width = 0
    canvas.height = 0
  }
}
