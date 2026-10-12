import { instance } from '@viz-js/viz'
import { MAX_SVG_CHARS } from './budget'
import type { DiagramPalette } from './types'

// Vite bundles Viz's embedded WASM into this local worker. No CDN, external
// Graphviz process or file-system access is needed by the host application.
globalThis.onmessage = async (event: MessageEvent<{ source: string; palette: DiagramPalette }>) => {
  try {
    const { source, palette } = event.data
    const viz = await instance()
    const svg = viz.renderString(source, {
      format: 'svg',
      engine: 'dot',
      graphAttributes: { bgcolor: 'transparent', color: palette.border, fontcolor: palette.text, fontname: 'Arial', pad: '0.2' },
      nodeAttributes: { color: palette.border, fontcolor: palette.text, fillcolor: palette.background, style: 'filled', fontname: 'Arial' },
      edgeAttributes: { color: palette.muted, fontcolor: palette.text, fontname: 'Arial' },
    })
    globalThis.postMessage(svg.length > MAX_SVG_CHARS ? { error: true } : { svg })
  } catch {
    globalThis.postMessage({ error: true })
  }
}
