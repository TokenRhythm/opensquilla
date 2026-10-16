import type { PDFDocumentLoadingTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { readBundledStaticAsset } from '@/platform/staticAssets'

// Vite emits these package-pinned resources alongside the app. The factories
// accept only known package filenames; PDF data cannot supply an external URL.
const resources = import.meta.glob<string>([
  '../../node_modules/pdfjs-dist/cmaps/*.bcmap',
  '../../node_modules/pdfjs-dist/standard_fonts/*.{pfb,ttf}',
  '../../node_modules/pdfjs-dist/wasm/*.wasm',
], { eager: true, query: '?url', import: 'default' })

async function resourceBytes(directory: string, filename: string, signal: AbortSignal): Promise<Uint8Array> {
  const url = resources[`../../node_modules/pdfjs-dist/${directory}/${filename}`]
  if (!url) throw new Error('Unknown PDF resource')
  return readBundledStaticAsset(url, signal)
}

export async function createPdfLoadingTask(data: ArrayBuffer, signal: AbortSignal): Promise<PDFDocumentLoadingTask> {
  const { getDocument, GlobalWorkerOptions } = await import('pdfjs-dist')
  signal.throwIfAborted()
  GlobalWorkerOptions.workerSrc = workerUrl
  const task = getDocument({
    // PDF.js transfers ownership to its worker. Retain the caller's bytes for
    // retries and other views by transferring this bounded independent copy.
    data: new Uint8Array(data.slice(0)),
    isEvalSupported: false,
    enableXfa: false,
    useSystemFonts: false,
    useWorkerFetch: false,
    disableAutoFetch: true,
    disableStream: true,
    disableRange: true,
    maxImageSize: 16_777_216,
    canvasMaxAreaInBytes: 16_777_216 * 4,
    CMapReaderFactory: class {
      async fetch({ name }: { name: string }) {
        return { cMapData: await resourceBytes('cmaps', `${name}.bcmap`, signal), isCompressed: true }
      }
    },
    StandardFontDataFactory: class {
      fetch({ filename }: { filename: string }) { return resourceBytes('standard_fonts', filename, signal) }
    },
    WasmFactory: class {
      fetch({ filename }: { filename: string }) { return resourceBytes('wasm', filename, signal) }
    },
  })
  return task
}

export const PDF_MIN_SCALE = 0.25
export const PDF_MAX_SCALE = 4
export const PDF_MAX_CANVAS_PIXELS = 8_388_608
export const PDF_MAX_CANVAS_SIDE = 8192

export function pdfCanvasSize(width: number, height: number, pixelRatio: number) {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error('Invalid PDF page dimensions')
  }
  const density = Math.min(Math.max(1, pixelRatio || 1), 2,
    PDF_MAX_CANVAS_SIDE / width, PDF_MAX_CANVAS_SIDE / height,
    Math.sqrt(PDF_MAX_CANVAS_PIXELS / width / height))
  return { width: Math.max(1, Math.floor(width * density)), height: Math.max(1, Math.floor(height * density)), density }
}
