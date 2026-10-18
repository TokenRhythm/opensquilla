import createDOMPurify from 'dompurify'
import { copyTextWithFallback } from '@/utils/browser'
import { copyIconSvg } from '@/utils/chat/codeBlockChrome'
import { isInternalSvgReference, isSafeMarkdownImageSource, isSafeMarkdownLinkSource } from './markdownUris'

export interface SvgDiagramLabels {
  zoomIn: string
  zoomOut: string
  reset: string
  copyCode: string
  copyFailed: string
  exportPng: string
  exportFailed: string
}

let interactionsWired = false

const ZOOM_STEP = 0.2
const ZOOM_WHEEL_STEP = 0.1
const ZOOM_MIN = 0.2
const ZOOM_MAX = 4

interface DiagramZoomState {
  scale: number
  tx: number
  ty: number
  origViewBox: { x: number, y: number, w: number, h: number } | null
  svgWidth: number
  svgHeight: number
}

const zoomStates = new WeakMap<HTMLElement, DiagramZoomState>()

const MAX_SVG_AXIS = 100_000
const MAX_EXPORT_AXIS = 4096
const MAX_EXPORT_PIXELS = 4_000_000
const diagramInstances = new WeakMap<HTMLElement, SvgDiagram>()

export interface SvgDiagram {
  element: HTMLElement
  updateLabels(labels: SvgDiagramLabels): void
  dispose(): void
}

// Use an isolated purifier: Markdown's HTML/class hooks must not mutate the
// SVG engine's HTML labels. foreignObject is an explicit HTML integration
// point, so ordinary labels survive without bypassing namespace sanitization.
export function sanitizeDiagramSvg(source: string): string {
  const purifier = createDOMPurify(window)
  const cleaned = purifier.sanitize(source, {
    ADD_TAGS: ['foreignObject'],
    ADD_ATTR: ['dominant-baseline'],
    HTML_INTEGRATION_POINTS: { foreignobject: true },
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'link', 'base', 'meta', 'form', 'input', 'audio', 'video', 'source', 'picture'],
    FORBID_ATTR: ['srcset', 'background', 'poster', 'action', 'formaction'],
  })
  const template = document.createElement('template')
  template.innerHTML = cleaned
  // The value gates also cover lightweight DOM runtimes where DOMPurify's
  // namespace support is incomplete; final insertion never trusts a URL merely
  // because the diagram engine produced it.
  for (const element of template.content.querySelectorAll('*')) {
    const tag = element.localName.toLowerCase()
    if (['script', 'iframe', 'object', 'embed', 'link', 'base', 'meta', 'form', 'input', 'animate', 'set', 'audio', 'video', 'source', 'picture'].includes(tag)) {
      element.remove()
      continue
    }
    for (const attr of [...element.attributes]) {
      const name = attr.name.toLowerCase()
      if (name.startsWith('on') || ['srcset', 'background', 'poster', 'action', 'formaction', 'xml:base'].includes(name)) element.removeAttribute(attr.name)
      else if (name === 'href' || name === 'xlink:href' || name === 'src') {
        const image = tag === 'img' || tag === 'image'
        const allowed = image
          ? isSafeMarkdownImageSource(attr.value)
          : isInternalSvgReference(attr.value) || (tag === 'a' && isSafeMarkdownLinkSource(attr.value))
        if (!allowed) element.removeAttribute(attr.name)
      } else if ((name === 'style' || /url\s*\(/i.test(attr.value)) && !hasOnlyInternalCssUrls(attr.value)) {
        element.removeAttribute(attr.name)
      }
    }
    if (tag === 'style' && !hasOnlyInternalCssUrls(element.textContent || '')) element.remove()
    if (tag === 'a' && (element.hasAttribute('href') || element.hasAttribute('xlink:href'))) {
      const href = element.getAttribute('href') || element.getAttribute('xlink:href') || ''
      if (!isInternalSvgReference(href)) {
        element.setAttribute('target', '_blank')
        element.setAttribute('rel', 'noopener noreferrer')
      }
    }
    if ((tag === 'img' || tag === 'image') && !element.hasAttribute('src')
      && !element.hasAttribute('href') && !element.hasAttribute('xlink:href')) element.remove()
  }
  return template.innerHTML
}

function hasOnlyInternalCssUrls(css: string): boolean {
  if (/@import|expression\s*\(|-moz-binding|[\\]/i.test(css)) return false
  const urls = css.matchAll(/url\s*\(\s*(['"]?)(.*?)\1\s*\)/gi)
  return [...urls].every(match => isInternalSvgReference(match[2].trim()))
}

export function readDiagramViewBox(svg: SVGSVGElement): { x: number, y: number, w: number, h: number } {
  const values = (svg.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number)
  if (values.length !== 4 || !values.every(Number.isFinite) || values[2] <= 0 || values[3] <= 0
    || values[2] > MAX_SVG_AXIS || values[3] > MAX_SVG_AXIS) throw new Error('Invalid diagram dimensions')
  return { x: values[0], y: values[1], w: values[2], h: values[3] }
}

export function diagramExportSize(w: number, h: number): { width: number, height: number } {
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0
    || w > MAX_SVG_AXIS || h > MAX_SVG_AXIS) throw new Error('Invalid diagram dimensions')
  const scale = Math.min(2, 1600 / w, MAX_EXPORT_AXIS / h, Math.sqrt(MAX_EXPORT_PIXELS / (w * h)))
  return { width: Math.max(1, Math.floor(w * scale)), height: Math.max(1, Math.floor(h * scale)) }
}

export function createSvgDiagram(svg: string, source: string, labels: SvgDiagramLabels): SvgDiagram {
  const element = buildDiagramElement(svg, source, labels)
  const svgWrap = element.querySelector<HTMLElement>('.mermaid-svg-wrap')
  const renderedSvg = svgWrap?.querySelector('svg')
  if (!svgWrap || !renderedSvg) throw new Error('Diagram did not produce an SVG')
  const state = zoomState(svgWrap)
  state.origViewBox = readDiagramViewBox(renderedSvg)
  let disposed = false
  const instance: SvgDiagram = {
    element,
    updateLabels(nextLabels) {
      if (disposed) return
      element.querySelector('.mermaid-toolbar')?.replaceWith(buildToolbar(svgWrap, source, nextLabels))
    },
    dispose() {
      if (disposed) return
      disposed = true
      if (mermaidDrag?.svgWrap === svgWrap) mermaidDrag = null
      zoomStates.delete(svgWrap)
      diagramInstances.delete(element)
    },
  }
  diagramInstances.set(element, instance)
  return instance
}

export function updateSvgDiagramLabels(root: HTMLElement, labels: SvgDiagramLabels): void {
  for (const element of root.querySelectorAll<HTMLElement>('.mermaid-diagram')) {
    diagramInstances.get(element)?.updateLabels(labels)
  }
}

function buildDiagramElement(svg: string, source: string, labels: SvgDiagramLabels): HTMLElement {
  const wrapper = document.createElement('div')
  wrapper.className = 'mermaid-diagram'
  const svgWrap = document.createElement('div')
  svgWrap.className = 'mermaid-svg-wrap'
  // When the sanitizer rejects the svg it returns '' — render nothing rather
  // than injecting unsanitized markup on the reject path.
  svgWrap.innerHTML = sanitizeDiagramSvg(svg)
  if (svgWrap.querySelector('svg')) {
    namespaceDiagramIds(svgWrap)
    wrapper.appendChild(svgWrap)
    wrapper.appendChild(buildToolbar(svgWrap, source, labels))
    wireDiagramInteractions(wrapper, svgWrap)
  }
  return wrapper
}

let diagramIdSequence = 0

// Cached SVGs and repeated DOT output reuse engine IDs. Keep all fragment and
// accessibility references local to one mounted diagram, including its CSS.
function namespaceDiagramIds(root: HTMLElement): void {
  const prefix = `diagram-${++diagramIdSequence}-`
  const ids = new Map<string, string>()
  for (const element of root.querySelectorAll<HTMLElement>('[id]')) {
    const original = element.id
    if (!ids.has(original)) ids.set(original, `${prefix}${ids.size}`)
    element.id = ids.get(original)!
  }
  if (!ids.size) return
  const selectorIds = [...ids].map(([id, value]) => [new RegExp(`#${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`, 'g'), `#${value}`] as const)
  const references = (value: string) => value.replace(/url\(\s*(['"]?)#([^'"\s)]+)\1\s*\)/gi,
    (original, quote: string, id: string) => ids.has(id) ? `url(${quote}#${ids.get(id)}${quote})` : original)
  for (const element of root.querySelectorAll('*')) {
    for (const attribute of [...element.attributes]) {
      if (attribute.name === 'id') continue
      let value = references(attribute.value)
      if (attribute.name === 'href' || attribute.name === 'xlink:href') {
        if (value.startsWith('#') && ids.has(value.slice(1))) value = `#${ids.get(value.slice(1))}`
      } else if (attribute.name === 'aria-labelledby' || attribute.name === 'aria-describedby') {
        value = value.split(/\s+/).map(id => ids.get(id) || id).join(' ')
      }
      if (value !== attribute.value) element.setAttribute(attribute.name, value)
    }
    if (element.localName === 'style') {
      // Rewrite selectors only in rule preludes, leaving declaration colors
      // such as #fff intact even if a source happens to contain id="fff".
      element.textContent = references(element.textContent || '').replace(/([^{}]+)(?=\{)/g,
        prelude => selectorIds.reduce((value, [pattern, replacement]) => value.replace(pattern, replacement), prelude))
    }
  }
}

function buildToolbar(svgWrap: HTMLElement, source: string, labels: SvgDiagramLabels): HTMLElement {
  const bar = document.createElement('div')
  bar.className = 'mermaid-toolbar'
  let copyRequest = 0
  let copyTimer: number | undefined
  const specs: Array<{ title: string, content: string | Node, action: (button: HTMLButtonElement) => void }> = [
    { title: labels.zoomIn, content: '+', action: () => applyZoom(svgWrap, ZOOM_STEP) },
    { title: labels.zoomOut, content: '−', action: () => applyZoom(svgWrap, -ZOOM_STEP) },
    { title: labels.reset, content: '⊙', action: () => resetZoom(svgWrap) },
    {
      title: labels.copyCode,
      content: copyIconSvg('idle'),
      action: (button) => {
        const request = ++copyRequest
        window.clearTimeout(copyTimer)
        void copyTextWithFallback(source).then(() => {
          if (!button.isConnected || request !== copyRequest) return
          button.replaceChildren(copyIconSvg('copied'))
          button.title = labels.copyCode
          button.setAttribute('aria-label', labels.copyCode)
          copyTimer = window.setTimeout(() => {
            if (!button.isConnected || request !== copyRequest) return
            button.replaceChildren(copyIconSvg('idle'))
            copyTimer = undefined
          }, 1500)
        }).catch(() => {
          if (!button.isConnected || request !== copyRequest) return
          button.replaceChildren(copyIconSvg('error'))
          button.title = labels.copyFailed
          button.setAttribute('aria-label', labels.copyFailed)
        })
      },
    },
    {
      title: labels.exportPng,
      content: '↓',
      action: (button) => {
        void exportSvgWrapAsPng(svgWrap).then(() => {
          button.textContent = '✓'
          window.setTimeout(() => {
            if (button.isConnected) button.textContent = '↓'
          }, 1500)
        }).catch(() => {
          if (!button.isConnected) return
          button.textContent = '!'
          button.title = labels.exportFailed
          button.setAttribute('aria-label', labels.exportFailed)
        })
      },
    },
  ]
  for (const spec of specs) {
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'mermaid-btn'
    button.title = spec.title
    button.setAttribute('aria-label', spec.title)
    if (typeof spec.content === 'string') button.textContent = spec.content
    else button.appendChild(spec.content)
    button.addEventListener('click', event => {
      event.stopPropagation()
      spec.action(button)
    })
    bar.appendChild(button)
  }
  return bar
}

// ─── Zoom & pan (crisp vector zoom via viewBox manipulation) ────────────────
// CSS transforms would rasterize the SVG (blurry at non-1x), and mermaid pins
// its own max-width style, so the original viewBox is re-windowed instead: the
// SVG keeps its natural screen size and re-renders as vectors at any zoom.

function zoomState(svgWrap: HTMLElement): DiagramZoomState {
  let state = zoomStates.get(svgWrap)
  if (!state) {
    state = { scale: 1, tx: 0, ty: 0, origViewBox: null, svgWidth: 0, svgHeight: 0 }
    zoomStates.set(svgWrap, state)
  }
  return state
}

function applyZoomViewBox(svgWrap: HTMLElement): void {
  const state = zoomState(svgWrap)
  const svg = svgWrap.querySelector('svg')
  if (!svg) return

  if (!state.origViewBox) {
    const vb = svg.viewBox?.baseVal
    if (vb && vb.width > 0) {
      state.origViewBox = { x: vb.x, y: vb.y, w: vb.width, h: vb.height }
    } else {
      const rect = svg.getBoundingClientRect()
      const w = rect.width || parseFloat(svg.getAttribute('width') || '') || 600
      const h = rect.height || parseFloat(svg.getAttribute('height') || '') || 400
      state.origViewBox = { x: 0, y: 0, w, h }
      svg.setAttribute('viewBox', `0 0 ${w} ${h}`)
    }
    const r = svg.getBoundingClientRect()
    state.svgWidth = r.width || state.origViewBox.w
    state.svgHeight = r.height || state.origViewBox.h
  }
  // The SVG is created before it is attached. Measure at first interaction,
  // and again after a responsive-width change, rather than caching a zero rect.
  const rect = svg.getBoundingClientRect()
  state.svgWidth = rect.width || state.origViewBox.w
  state.svgHeight = rect.height || state.origViewBox.h

  const { x: ox, y: oy, w: ow, h: oh } = state.origViewBox
  if (state.scale === 1 && !state.tx && !state.ty) {
    svg.setAttribute('viewBox', `${ox} ${oy} ${ow} ${oh}`)
    return
  }

  const vbW = ow / state.scale
  const vbH = oh / state.scale
  // Clamp pan so the viewport can never fully separate from the diagram.
  const maxTx = state.svgWidth * (state.scale + 1) / 2
  const maxTy = state.svgHeight * (state.scale + 1) / 2
  state.tx = Math.min(maxTx, Math.max(-maxTx, state.tx))
  state.ty = Math.min(maxTy, Math.max(-maxTy, state.ty))

  const panX = -state.tx * vbW / state.svgWidth
  const panY = -state.ty * vbH / state.svgHeight
  const vbX = ox + (ow - vbW) / 2 + panX
  const vbY = oy + (oh - vbH) / 2 + panY
  svg.setAttribute('viewBox', `${vbX} ${vbY} ${vbW} ${vbH}`)
}

function applyZoom(svgWrap: HTMLElement, delta: number): void {
  const state = zoomState(svgWrap)
  state.scale = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, state.scale + delta))
  applyZoomViewBox(svgWrap)
}

function resetZoom(svgWrap: HTMLElement): void {
  const state = zoomState(svgWrap)
  const svg = svgWrap.querySelector('svg')
  if (svg && state.origViewBox) {
    const { x, y, w, h } = state.origViewBox
    svg.setAttribute('viewBox', `${x} ${y} ${w} ${h}`)
  }
  state.scale = 1
  state.tx = 0
  state.ty = 0
  state.svgWidth = 0
  state.svgHeight = 0
}

interface DiagramDrag {
  svgWrap: HTMLElement
  startX: number
  startY: number
  startTx: number
  startTy: number
}

let mermaidDrag: DiagramDrag | null = null

// Diagram-level listeners attach per diagram; the window-level drag listeners
// attach exactly once (lazily, on the first diagram) and key off a single drag
// slot — per-diagram window listeners would leak on every re-render (browsa
// leak fix).
function wireDiagramInteractions(wrapper: HTMLElement, svgWrap: HTMLElement): void {
  wrapper.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return
    event.preventDefault()
    applyZoom(svgWrap, event.deltaY < 0 ? ZOOM_WHEEL_STEP : -ZOOM_WHEEL_STEP)
  }, { passive: false })
  svgWrap.addEventListener('mousedown', event => {
    if (event.button !== 0) return
    const state = zoomState(svgWrap)
    mermaidDrag = {
      svgWrap,
      startX: event.clientX,
      startY: event.clientY,
      startTx: state.tx,
      startTy: state.ty,
    }
    svgWrap.style.cursor = 'grabbing'
    event.preventDefault()
  })
  svgWrap.style.cursor = 'grab'

  if (interactionsWired || typeof window === 'undefined') return
  interactionsWired = true
  window.addEventListener('mousemove', event => {
    if (!mermaidDrag) return
    const { svgWrap: dragWrap, startX, startY, startTx, startTy } = mermaidDrag
    if (!dragWrap.isConnected) { mermaidDrag = null; return }
    const state = zoomState(dragWrap)
    state.tx = startTx + (event.clientX - startX)
    state.ty = startTy + (event.clientY - startY)
    applyZoomViewBox(dragWrap)
  })
  window.addEventListener('mouseup', () => {
    if (!mermaidDrag) return
    mermaidDrag.svgWrap.style.cursor = 'grab'
    mermaidDrag = null
  })
  window.addEventListener('blur', () => {
    if (mermaidDrag) mermaidDrag.svgWrap.style.cursor = 'grab'
    mermaidDrag = null
  })
}

// ─── PNG export (2x, current theme backdrop baked in) ───────────────────────
// A transparent-background SVG exports dark-on-transparent and is unreadable in
// external viewers, so the backdrop comes from the active theme's surface token
// (read at export time — no color literals here, per the theme contract).

async function exportSvgWrapAsPng(svgWrap: HTMLElement): Promise<void> {
  const svg = svgWrap.querySelector('svg')
  if (!svg) throw new Error('no svg')
  const styles = getComputedStyle(document.documentElement)
  const bg = styles.getPropertyValue('--bg-surface').trim() || undefined
  // Export the source diagram, independent of the current viewport zoom/pan.
  const clone = svg.cloneNode(true) as SVGSVGElement
  const original = zoomState(svgWrap).origViewBox || readDiagramViewBox(svg)
  clone.setAttribute('viewBox', `${original.x} ${original.y} ${original.w} ${original.h}`)
  clone.setAttribute('width', String(original.w))
  clone.setAttribute('height', String(original.h))
  const dataUrl = await rasterizeSvg(clone, { bg })
  if (!svgWrap.isConnected) return
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = 'diagram.png'
  anchor.click()
}

async function rasterizeSvg(svg: SVGSVGElement, opts: { bg?: string }): Promise<string> {
  const { w, h } = readDiagramViewBox(svg)
  const { width, height } = diagramExportSize(w, h)
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('canvas unavailable')
  if (opts.bg) {
    ctx.fillStyle = opts.bg
    ctx.fillRect(0, 0, canvas.width, canvas.height)
  }
  const xml = new XMLSerializer().serializeToString(svg)
  const img = new Image()
  await new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => finish(new Error('svg rasterize timed out')), 10_000)
    function finish(error?: Error) {
      window.clearTimeout(timer)
      img.onload = null
      img.onerror = null
      if (error) reject(error)
      else resolve()
    }
    img.onload = () => finish()
    img.onerror = () => finish(new Error('svg rasterize failed'))
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`
  })
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  return canvas.toDataURL('image/png')
}
