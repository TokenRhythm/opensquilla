import { GRAPHVIZ_TIMEOUT_MS, MAX_SVG_CHARS } from './budget'
import { DiagramRenderError, throwIfAborted, type DiagramPalette } from './types'

export function renderGraphviz(source: string, palette: DiagramPalette, signal?: AbortSignal): Promise<string> {
  throwIfAborted(signal)
  return new Promise((resolve, reject) => {
    let worker: Worker
    try {
      worker = new Worker(new URL('./graphviz.worker.ts', import.meta.url), { type: 'module' })
    } catch {
      reject(new DiagramRenderError('render'))
      return
    }
    const finish = (error?: Error, svg?: string) => {
      clearTimeout(timeout)
      signal?.removeEventListener('abort', abort)
      worker.onmessage = null
      worker.onerror = null
      worker.terminate()
      if (error) reject(error)
      else resolve(svg!)
    }
    const abort = () => finish(new DOMException('Diagram rendering cancelled', 'AbortError'))
    const timeout = setTimeout(() => finish(new DiagramRenderError('timeout')), GRAPHVIZ_TIMEOUT_MS)
    signal?.addEventListener('abort', abort, { once: true })
    worker.onerror = () => finish(new DiagramRenderError('render'))
    worker.onmessage = event => {
      const svg = event.data?.svg
      if (typeof svg !== 'string' || !svg || svg.length > MAX_SVG_CHARS) {
        finish(new DiagramRenderError(typeof svg === 'string' ? 'budget' : 'render'))
      } else finish(undefined, svg)
    }
    try { worker.postMessage({ source, palette }) }
    catch { finish(new DiagramRenderError('render')) }
  })
}
