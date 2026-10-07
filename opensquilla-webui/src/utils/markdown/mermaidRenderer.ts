import { copyTextWithFallback } from '@/utils/browser'
import { copyIconSvg } from '@/utils/chat/codeBlockChrome'
import {
  clampMermaidPreviewHeight,
  estimateMermaidPreviewHeight,
  formatMermaidParseError,
  renderMermaidWithRetry,
} from '@/utils/markdown/mermaidUtils'
import { getManifest } from '@/themes/registry'

// Live rendering for ```mermaid fences in assistant markdown, ported from
// browsa's sidepanel render pipeline. The mermaid bundle is multi-megabyte, so
// it is only ever reached through a dynamic import (Vite emits it as its own
// chunk); call preloadMermaid() when a stream sniffs a mermaid fence so the
// chunk is usually in flight long before the fence commits.
//
// Security posture: securityLevel 'loose' is mermaid's proven configuration for
// HTML labels ('strict' emits empty label shells), which leaves label markup
// unescaped — so every rendered SVG passes sanitizeMermaidSvg before insertion:
// a namespace-agnostic DOM walk that drops script-family elements, event
// handler attributes, and script-scheme URIs everywhere in the tree (including
// inside foreignObject labels). DOMPurify is deliberately not used here: its
// HTML parse path strips foreignObject content wholesale, destroying labels.

export interface MermaidRenderLabels {
  zoomIn: string
  zoomOut: string
  reset: string
  copyCode: string
  copied: string
  copyFailed: string
  exportPng: string
  loadFailed: string
  viewSource: string
  syntaxErrorAt: (line: number, excerpt: string) => string
}

type MermaidApi = typeof import('mermaid').default

let mermaidLoading: Promise<MermaidApi> | null = null
let initializedTheme: string | null = null
let interactionsWired = false

const ZOOM_STEP = 0.2
const ZOOM_WHEEL_STEP = 0.1
const ZOOM_MIN = 0.2
const ZOOM_MAX = 4

// Mermaid needs a DOM-attached container while rendering, and its layout is
// computed from that container's width — a hardcoded width gets squashed by the
// `max-width:100%` downscale and distorts proportions (browsa bug history).
// Measure the real container instead, with a sane floor.
const MIN_HOST_WIDTH = 280

interface MermaidZoomState {
  scale: number
  tx: number
  ty: number
  origViewBox: { x: number, y: number, w: number, h: number } | null
  svgWidth: number
  svgHeight: number
}

const zoomStates = new WeakMap<HTMLElement, MermaidZoomState>()

export function preloadMermaid(): void {
  void getMermaid().catch(() => { /* the render path surfaces load failures */ })
}

function resolvedAppThemeId(): string {
  const attr = document.documentElement.getAttribute('data-theme')
  if (attr) return attr
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function mermaidThemeFor(themeId: string): 'dark' | 'default' {
  return getManifest(themeId)?.capabilities.colorScheme === 'light' ? 'default' : 'dark'
}

// The theme snapshot must be re-checked per render (not once at initialize):
// a live theme switch would otherwise leave every later diagram on the stale
// palette. Unchanged theme = one attribute read; mermaid v11 tolerates
// re-initialization and already-rendered SVGs are untouched.
function initMermaidTheme(mermaid: MermaidApi): void {
  const theme = mermaidThemeFor(resolvedAppThemeId())
  if (initializedTheme === theme) return
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    theme,
    flowchart: { nodeSpacing: 30, rankSpacing: 40 },
    themeVariables: { fontSize: '16px' },
  })
  initializedTheme = theme
}

async function getMermaid(): Promise<MermaidApi> {
  if (!mermaidLoading) mermaidLoading = import('mermaid').then(mod => mod.default)
  const mermaid = await mermaidLoading
  initMermaidTheme(mermaid)
  return mermaid
}

// Active content that must never survive diagram rendering, wherever it
// appears in the tree (mermaid puts label markup inside foreignObject, a
// namespace DOMPurify's HTML parse path destroys — hence this walker).
const MERMAID_STRIP_TAGS = new Set(['script', 'iframe', 'object', 'embed', 'link', 'base', 'meta'])
const MERMAID_URI_ATTRS = new Set(['href', 'xlink:href', 'src', 'background', 'formaction', 'action', 'poster'])
const SCRIPT_SCHEME_RE = /^\s*(?:javascript|vbscript|data:text\/html)/i

function sanitizeMermaidSvg(svg: string): string {
  if (typeof document === 'undefined') return ''
  const template = document.createElement('template')
  template.innerHTML = svg
  for (const element of [...template.content.querySelectorAll('*')]) {
    if (MERMAID_STRIP_TAGS.has(element.tagName.toLowerCase())) {
      element.remove()
      continue
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.name.toLowerCase()
      // Inline event handlers (onclick=… and friends) never survive.
      if (name.startsWith('on')) {
        element.removeAttribute(attribute.name)
        continue
      }
      const value = attribute.value
      if (MERMAID_URI_ATTRS.has(name) && SCRIPT_SCHEME_RE.test(value)) {
        element.removeAttribute(attribute.name)
        continue
      }
      // style="…url(javascript:…)" / -moz-binding style vectors.
      if (name === 'style' && /(?:url\s*\(\s*['"]?\s*|:\s*)(?:javascript|vbscript|data:text\/html)/i.test(value)) {
        element.removeAttribute(attribute.name)
      }
    }
  }
  return template.innerHTML
}

export async function renderMermaidBlocks(root: HTMLElement, labels: MermaidRenderLabels): Promise<void> {
  // `:has()` is avoided on purpose: lightweight DOM test runtimes do not fully
  // support it, and a plain child check is just as fast at these sizes. The
  // mermaidState marker is claimed synchronously (before the module-load await
  // below) so a decoration pass racing an in-flight render cannot start a
  // second render for the same fence; a pass that bails before replacing
  // always releases its claims so a later pass can retry.
  const blocks = [...root.querySelectorAll<HTMLPreElement>('pre')]
    .filter(pre => !pre.dataset.mermaidState && pre.querySelector('code.language-mermaid'))
  if (blocks.length === 0) return
  for (const pre of blocks) pre.dataset.mermaidState = 'rendering'

  let mermaid: MermaidApi
  try {
    mermaid = await getMermaid()
  } catch {
    for (const pre of blocks) {
      pre.replaceWith(mermaidErrorCard(
        labels.loadFailed,
        pre.querySelector('code')?.textContent || '',
        labels,
      ))
    }
    return
  }

  // One shared offscreen measurement container for the whole pass.
  const host = document.createElement('div')
  host.style.cssText = `position:fixed;left:-9999px;top:0;width:${Math.max(root.clientWidth || 0, MIN_HOST_WIDTH)}px;opacity:0;pointer-events:none`
  document.body.appendChild(host)
  try {
    for (const pre of blocks) {
      const source = pre.querySelector('code')?.textContent || ''
      // Hold the temporary code fence at roughly the expected diagram height so
      // the swap from raw code to rendered diagram jumps less. The estimate can
      // overshoot — it must never reach the final wrapper, which sizes to its
      // real SVG (that is why it is set on the pre being replaced).
      pre.style.minHeight = `${clampMermaidPreviewHeight(estimateMermaidPreviewHeight(source))}px`
      try {
        const { svg } = await renderMermaidWithRetry(mermaid.render.bind(mermaid), mermaidId(), source, host)
        pre.replaceWith(buildDiagram(svg, source, labels))
      } catch (error) {
        pre.replaceWith(mermaidErrorCard(errorMessage(error, labels), source, labels))
      }
    }
  } finally {
    host.remove()
  }
}

let mermaidSequence = 0
function mermaidId(): string {
  mermaidSequence += 1
  return `opensquilla-mermaid-${mermaidSequence}`
}

function errorMessage(error: unknown, labels: MermaidRenderLabels): string {
  const raw = String((error as Error)?.message || error)
  const parsed = formatMermaidParseError(raw)
  return parsed ? labels.syntaxErrorAt(parsed.line, parsed.excerpt.slice(0, 80)) : raw
}

function buildDiagram(svg: string, source: string, labels: MermaidRenderLabels): HTMLElement {
  const wrapper = document.createElement('div')
  wrapper.className = 'mermaid-diagram'
  const svgWrap = document.createElement('div')
  svgWrap.className = 'mermaid-svg-wrap'
  // When the sanitizer rejects the svg it returns '' — render nothing rather
  // than injecting unsanitized markup on the reject path.
  svgWrap.innerHTML = sanitizeMermaidSvg(svg)
  if (svgWrap.querySelector('svg')) {
    wrapper.appendChild(svgWrap)
    wrapper.appendChild(buildToolbar(svgWrap, source, labels))
    wireDiagramInteractions(wrapper, svgWrap)
  }
  return wrapper
}

function buildToolbar(svgWrap: HTMLElement, source: string, labels: MermaidRenderLabels): HTMLElement {
  const bar = document.createElement('div')
  bar.className = 'mermaid-toolbar'
  const specs: Array<{ title: string, content: string | Node, action: (button: HTMLButtonElement) => void }> = [
    { title: labels.zoomIn, content: '+', action: () => applyZoom(svgWrap, ZOOM_STEP) },
    { title: labels.zoomOut, content: '−', action: () => applyZoom(svgWrap, -ZOOM_STEP) },
    { title: labels.reset, content: '⊙', action: () => resetZoom(svgWrap) },
    {
      title: labels.copyCode,
      content: copyIconSvg('idle'),
      action: (button) => {
        void copyTextWithFallback(source).then(() => {
          button.replaceChildren(copyIconSvg('copied'))
          button.title = labels.copied
          button.setAttribute('aria-label', labels.copied)
          window.setTimeout(() => {
            if (!button.isConnected) return
            button.replaceChildren(copyIconSvg('idle'))
            button.title = labels.copyCode
            button.setAttribute('aria-label', labels.copyCode)
          }, 1500)
        }).catch(() => { /* copy failures stay silent on diagram chrome */ })
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
        }).catch(() => { /* export failures stay silent on diagram chrome */ })
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

// ─── Error card ─────────────────────────────────────────────────────────────
// A failed block must never destroy the model's source: the short message is
// shown as a chip, and the full source (plus the raw parser dump for parse
// errors) stays readable and copyable under a collapsed "view source".
function mermaidErrorCard(message: string, source: string, labels: MermaidRenderLabels): HTMLElement {
  const card = document.createElement('div')
  card.className = 'mermaid-error'
  const chip = document.createElement('span')
  chip.textContent = `⚠ Mermaid: ${message}`
  card.appendChild(chip)
  if (source) {
    const copyButton = document.createElement('button')
    copyButton.type = 'button'
    copyButton.className = 'mermaid-err-copy'
    copyButton.textContent = labels.copyCode
    copyButton.addEventListener('click', () => {
      void copyTextWithFallback(source).then(() => {
        copyButton.textContent = '✓'
        window.setTimeout(() => {
          if (copyButton.isConnected) copyButton.textContent = labels.copyCode
        }, 1500)
      }).catch(() => {
        copyButton.textContent = labels.copyFailed
      })
    })
    card.appendChild(copyButton)
    const details = document.createElement('details')
    const summary = document.createElement('summary')
    summary.textContent = labels.viewSource
    const pre = document.createElement('pre')
    pre.className = 'mermaid-err-src'
    pre.textContent = source
    details.appendChild(summary)
    details.appendChild(pre)
    card.appendChild(details)
  }
  return card
}

// ─── Zoom & pan (crisp vector zoom via viewBox manipulation) ────────────────
// CSS transforms would rasterize the SVG (blurry at non-1x), and mermaid pins
// its own max-width style, so the original viewBox is re-windowed instead: the
// SVG keeps its natural screen size and re-renders as vectors at any zoom.

function zoomState(svgWrap: HTMLElement): MermaidZoomState {
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
  state.origViewBox = null
  state.svgWidth = 0
  state.svgHeight = 0
}

interface MermaidDrag {
  svgWrap: HTMLElement
  startX: number
  startY: number
  startTx: number
  startTy: number
}

let mermaidDrag: MermaidDrag | null = null

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
  const dataUrl = await rasterizeSvg(svg, { bg })
  const anchor = document.createElement('a')
  anchor.href = dataUrl
  anchor.download = 'diagram.png'
  anchor.click()
}

async function rasterizeSvg(svg: SVGSVGElement, opts: { bg?: string }): Promise<string> {
  const vb = svg.viewBox?.baseVal
  const w = Math.max((vb && vb.width) || svg.clientWidth || 600, 1)
  const h = Math.max((vb && vb.height) || svg.clientHeight || 400, 1)
  const scale = Math.min(2, 1600 / w)
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(w * scale)
  canvas.height = Math.round(h * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('canvas unavailable')
  if (opts.bg) {
    ctx.fillStyle = opts.bg
    ctx.fillRect(0, 0, canvas.width, canvas.height)
  }
  const xml = new XMLSerializer().serializeToString(svg)
  const img = new Image()
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve()
    img.onerror = () => reject(new Error('svg rasterize failed'))
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`
  })
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  return canvas.toDataURL('image/png')
}
