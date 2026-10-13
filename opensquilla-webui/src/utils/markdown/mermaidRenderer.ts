import { copyTextWithFallback } from '@/utils/browser'
import { getManifest } from '@/themes/registry'
import { clampMermaidPreviewHeight, estimateMermaidPreviewHeight, formatMermaidParseError, renderMermaidWithRetry } from './mermaidUtils'
import { isInternalSvgReference, isSafeMarkdownImageSource } from './markdownUris'
import { createSvgDiagram, updateSvgDiagramLabels, type SvgDiagram, type SvgDiagramLabels } from './svgDiagram'

export interface MermaidRenderLabels extends SvgDiagramLabels {
  loadFailed: string
  tooLarge: string
  blockedResource: string
  viewSource: string
  syntaxErrorAt: (line: number, excerpt: string) => string
}

export interface MermaidRenderOptions {
  signal?: AbortSignal
  themeId?: string
  onRendered?: (diagram: SvgDiagram) => void
  budget?: { sourceChars: number, diagrams: number }
}

type MermaidApi = typeof import('mermaid').default
let mermaidLoading: Promise<MermaidApi> | null = null
let initializedTheme: string | null = null
let renderQueue: Promise<void> = Promise.resolve()
let mermaidSequence = 0
const MAX_SOURCE_CHARS = 50_000
const MAX_SURFACE_SOURCE_CHARS = 100_000
const MAX_SURFACE_DIAGRAMS = 32
const MIN_HOST_WIDTH = 280
const sources = new WeakMap<HTMLElement, { source: string, error?: unknown, reason?: 'loadFailed' | 'tooLarge' | 'blockedResource' }>()
const claims = new WeakMap<HTMLPreElement, symbol>()
class MermaidLoadError extends Error {}

export function resolvedDiagramThemeId(): string {
  return document.documentElement.getAttribute('data-theme')
    || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
}

export function preloadMermaid(): void {
  // Loading is shared, initialization belongs exclusively to an executing job.
  void getMermaid().catch(() => {})
}

async function getMermaid(): Promise<MermaidApi> {
  if (!mermaidLoading) {
    mermaidLoading = import('mermaid').then(mod => mod.default).catch(error => {
      mermaidLoading = null
      throw new MermaidLoadError(String(error))
    })
  }
  return mermaidLoading
}

function initializeTheme(mermaid: MermaidApi, themeId: string): void {
  const theme = getManifest(themeId)?.capabilities.colorScheme === 'light' ? 'default' : 'dark'
  if (initializedTheme === theme) return
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    theme,
    maxTextSize: MAX_SOURCE_CHARS,
    flowchart: { nodeSpacing: 30, rankSpacing: 40 },
    themeVariables: { fontSize: '16px' },
  })
  initializedTheme = theme
}

function isCurrent(root: HTMLElement, pre: HTMLElement, signal?: AbortSignal): boolean {
  return !signal?.aborted && root.isConnected && pre.isConnected && root.contains(pre)
}

// Mermaid owns global configuration. Serialize initialization and rendering
// together; the library's render queue alone cannot protect initialize(). Other
// future SVG engines can share the presentation without sharing this queue.
function enqueueRender<T>(run: () => Promise<T>): Promise<T> {
  const job = renderQueue.then(run)
  renderQueue = job.then(() => {}, () => {})
  return job
}

// Reject unsupported image requests before Mermaid attaches its measurement
// DOM. Final SVG sanitization remains independent of this source preflight.
export function hasUnsupportedMermaidImageSources(source: string): boolean {
  const template = document.createElement('template')
  template.innerHTML = source
  if (template.content.querySelector('audio, video, source, picture')) return true
  for (const image of template.content.querySelectorAll('img, image')) {
    if (image.hasAttribute('srcset')) return true
    const src = image.getAttribute('src') || image.getAttribute('href') || image.getAttribute('xlink:href') || ''
    if (!isSafeMarkdownImageSource(src)) return true
  }
  // Only the explicit flowchart shape attributes describe image requests.
  // Plain labels/sequence messages can legitimately contain "img: ./file".
  for (const shape of source.matchAll(/@\{([^}]*)\}/g)) {
    for (const match of shape[1].matchAll(/(?:^|,)\s*img\s*:\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^,}\s]+))/g)) {
      if (!isSafeMarkdownImageSource(match[1] ?? match[2] ?? match[3] ?? '')) return true
    }
  }
  const css = [...template.content.querySelectorAll('[style], style')]
    .map(element => element.getAttribute('style') || element.textContent || '')
  css.push(...(source.match(/^(?:\s*(?:classDef|style|linkStyle)\b[^\n]*|\s*%%\{[^\n]*)/gm) || []))
  const header = source.match(/^---\s*\r?\n([\s\S]*?)\r?\n---(?:\s|$)/)
  if (header) css.push(header[1])
  for (const entry of css) {
    if (/@import/i.test(entry)) return true
    for (const match of entry.matchAll(/url\s*\(\s*(['"]?)(.*?)\1\s*\)/gi)) {
      if (!isInternalSvgReference(match[2].trim())) return true
    }
  }
  return false
}

export async function renderMermaidBlocks(root: HTMLElement, labels: MermaidRenderLabels, options: MermaidRenderOptions = {}): Promise<void> {
  if (options.signal?.aborted || !root.isConnected) return
  const allFences = [...root.querySelectorAll<HTMLPreElement>('pre')]
    .filter(pre => pre.querySelector('code.language-mermaid'))
  const blocks = allFences.filter(pre => !claims.has(pre))
  if (!blocks.length) return
  const completed = options.budget ? [] : [...root.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')]
  const budget = options.budget ?? {
    sourceChars: completed.reduce((sum, element) => sum + Number(element.dataset.mermaidSourceLength || 0), 0),
    diagrams: completed.length,
  }
  budget.sourceChars += blocks.reduce((sum, pre) => sum + (pre.querySelector('code')?.textContent?.length || 0), 0)
  budget.diagrams += blocks.length
  const overSurfaceBudget = budget.sourceChars > MAX_SURFACE_SOURCE_CHARS || budget.diagrams > MAX_SURFACE_DIAGRAMS
  const token = Symbol('mermaid-render')
  for (const pre of blocks) {
    claims.set(pre, token)
    pre.dataset.mermaidState = 'rendering'
  }
  const release = () => {
    for (const pre of blocks) {
      if (claims.get(pre) !== token) continue
      claims.delete(pre)
      delete pre.dataset.mermaidState
      pre.style.minHeight = ''
    }
  }
  options.signal?.addEventListener('abort', release, { once: true })
  const current = (pre: HTMLPreElement) => claims.get(pre) === token && isCurrent(root, pre, options.signal)
  const themeId = options.themeId ?? resolvedDiagramThemeId()
  try {
    for (const pre of blocks) {
      if (!current(pre)) continue
      const source = pre.querySelector('code')?.textContent || ''
      let reason: 'tooLarge' | 'blockedResource' | undefined
      if (overSurfaceBudget || source.length > MAX_SOURCE_CHARS) reason = 'tooLarge'
      else if (hasUnsupportedMermaidImageSources(source)) reason = 'blockedResource'
      if (reason) {
        replaceError(pre, source, undefined, labels, reason)
        continue
      }
      pre.style.minHeight = `${clampMermaidPreviewHeight(estimateMermaidPreviewHeight(source))}px`
      try {
        const result = await enqueueRender(async () => {
          if (!current(pre)) return null
          const mermaid = await getMermaid()
          if (!current(pre)) return null
          initializeTheme(mermaid, themeId)
          const host = document.createElement('div')
          host.dataset.mermaidHost = '1'
          host.style.cssText = `position:fixed;left:-9999px;top:0;width:${Math.max(root.clientWidth, MIN_HOST_WIDTH)}px;opacity:0;pointer-events:none`
          document.body.appendChild(host)
          const removeHost = () => host.remove()
          options.signal?.addEventListener('abort', removeHost, { once: true })
          try {
            return await renderMermaidWithRetry(mermaid.render.bind(mermaid), `opensquilla-mermaid-${++mermaidSequence}`, source, host)
          } finally {
            options.signal?.removeEventListener('abort', removeHost)
            host.remove()
          }
        })
        if (!result || !current(pre)) continue
        const diagram = createSvgDiagram(result.svg, source, labels)
        diagram.element.dataset.mermaidSourceLength = String(source.length)
        sources.set(diagram.element, { source })
        pre.replaceWith(diagram.element)
        options.onRendered?.(diagram)
      } catch (error) {
        if (current(pre)) replaceError(pre, source, error, labels, error instanceof MermaidLoadError ? 'loadFailed' : undefined)
      }
    }
  } finally {
    // Aborted/removed fences can be retried by a later decoration generation.
    options.signal?.removeEventListener('abort', release)
    release()
  }
}

function errorMessage(error: unknown, labels: MermaidRenderLabels): string {
  const raw = String((error as Error)?.message || error)
  const parsed = formatMermaidParseError(raw)
  return parsed ? labels.syntaxErrorAt(parsed.line, parsed.excerpt.slice(0, 80)) : raw
}

function replaceError(pre: HTMLElement, source: string, error: unknown, labels: MermaidRenderLabels, reason?: 'loadFailed' | 'tooLarge' | 'blockedResource'): void {
  const card = mermaidErrorCard(reason ? labels[reason] : errorMessage(error, labels), source, labels)
  card.dataset.mermaidSourceLength = String(source.length)
  sources.set(card, { source, error, reason })
  pre.replaceWith(card)
}

export function restoreMermaidSources(root: HTMLElement): void {
  for (const element of root.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')) {
    const entry = sources.get(element)
    if (!entry) continue
    const pre = document.createElement('pre')
    const code = document.createElement('code')
    code.className = 'hljs language-mermaid'
    code.textContent = entry.source
    pre.appendChild(code)
    element.replaceWith(pre)
  }
}

export function updateMermaidLabels(root: HTMLElement, labels: MermaidRenderLabels): void {
  updateSvgDiagramLabels(root, labels)
  for (const card of root.querySelectorAll<HTMLElement>('.mermaid-error')) {
    const entry = sources.get(card)
    if (!entry) continue
    const next = mermaidErrorCard(entry.reason ? labels[entry.reason] : errorMessage(entry.error, labels), entry.source, labels)
    next.dataset.mermaidSourceLength = card.dataset.mermaidSourceLength || ''
    const oldDetails = card.querySelector('details')
    const newDetails = next.querySelector('details')
    if (oldDetails && newDetails) newDetails.open = oldDetails.open
    sources.set(next, entry)
    card.replaceWith(next)
  }
}

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
