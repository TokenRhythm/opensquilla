import { copyTextWithFallback } from '@/utils/browser'
import { parseFragment, type DefaultTreeAdapterTypes } from 'parse5'
import { getManifest } from '@/themes/registry'
import { clampMermaidPreviewHeight, estimateMermaidPreviewHeight, formatMermaidParseError, renderMermaidWithRetry } from './mermaidUtils'
import { isInternalSvgReference, isSafeMarkdownImageSource } from './markdownUris'
import { DiagramTaskQueue } from './diagrams/queue'
import { DiagramCache } from './diagrams/cache'
import { renderGraphviz } from './diagrams/graphviz'
import { checkGraphvizBudget } from './diagrams/budget'
import { diagramPalette } from './diagrams/palette'
import { sanitizeDiagramSvg as sanitizeGraphvizSvg } from './diagrams/sanitize'
import { DiagramRenderError } from './diagrams/types'
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
  budget?: { sourceChars: number, diagrams: number, charged?: WeakSet<HTMLElement> }
  /** Limit discovery without adding another rendering implementation. */
  languages?: readonly string[]
}

type MermaidApi = typeof import('mermaid').default
let mermaidLoading: Promise<MermaidApi> | null = null
let initializedTheme: string | null = null
const renderQueue = new DiagramTaskQueue()
const renderCache = new DiagramCache()
let mermaidSequence = 0
const MAX_SOURCE_CHARS = 50_000
const MAX_SURFACE_SOURCE_CHARS = 100_000
const MAX_SURFACE_DIAGRAMS = 32
const MIN_HOST_WIDTH = 280
const sources = new WeakMap<HTMLElement, { source: string, language: string, error?: unknown, reason?: 'loadFailed' | 'tooLarge' | 'blockedResource' }>()
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
async function enqueueRender(run: () => Promise<string>, signal?: AbortSignal): Promise<string> {
  for (;;) {
    try { return await renderQueue.add(run, signal) }
    catch (error) {
      if (!(error instanceof DiagramRenderError) || error.code !== 'busy') throw error
      await renderQueue.waitForCapacity(signal)
    }
  }
}

// Reject unsupported image requests before Mermaid attaches its measurement
// DOM. Final SVG sanitization remains independent of this source preflight.
const UNSUPPORTED_SOURCE_TAGS = new Set([
  'script', 'iframe', 'object', 'embed', 'link', 'base', 'meta', 'form', 'input',
  'audio', 'video', 'source', 'picture',
])

function sourceText(node: DefaultTreeAdapterTypes.ParentNode): string {
  const pending: DefaultTreeAdapterTypes.Node[] = [...node.childNodes].reverse()
  const text: string[] = []
  while (pending.length) {
    const child = pending.pop()!
    if ('value' in child) text.push(child.value)
    if ('childNodes' in child) pending.push(...[...child.childNodes].reverse())
  }
  return text.join('')
}

export function hasUnsupportedMermaidImageSources(source: string): boolean {
  // Decode HTML entities and apply HTML parsing rules without constructing
  // browser DOM or sanitizing away the attributes this preflight must reject.
  const pending: DefaultTreeAdapterTypes.Node[] = [parseFragment(source)]
  const css: string[] = []
  while (pending.length) {
    const node = pending.pop()!
    if ('childNodes' in node) pending.push(...node.childNodes)
    if (!('tagName' in node)) continue
    const tag = node.tagName.toLowerCase()
    if (UNSUPPORTED_SOURCE_TAGS.has(tag)) return true
    const attributes = new Map(node.attrs.map(attribute => [
      attribute.prefix ? `${attribute.prefix}:${attribute.name}` : attribute.name,
      attribute.value,
    ]))
    if ([...attributes.keys()].some(name => /^on/i.test(name))) return true
    if (tag === 'img' || tag === 'image') {
      if (attributes.has('srcset')) return true
      const src = attributes.get('src') || attributes.get('href') || attributes.get('xlink:href') || ''
      if (!isSafeMarkdownImageSource(src)) return true
    }
    if (attributes.has('style') || tag === 'style') {
      css.push(attributes.get('style') || sourceText(node))
    }
    if (tag === 'template' && node.namespaceURI === 'http://www.w3.org/1999/xhtml') {
      pending.push((node as DefaultTreeAdapterTypes.Template).content)
    }
  }
  // Only the explicit flowchart shape attributes describe image requests.
  // Plain labels/sequence messages can legitimately contain "img: ./file".
  for (const shape of source.matchAll(/@\{([^}]*)\}/g)) {
    for (const match of shape[1].matchAll(/(?:^|,)\s*img\s*:\s*(?:"([^"\n]*)"|'([^'\n]*)'|([^,}\s]+))/g)) {
      if (!isSafeMarkdownImageSource(match[1] ?? match[2] ?? match[3] ?? '')) return true
    }
  }
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

export function renderMermaidBlocks(root: HTMLElement, labels: MermaidRenderLabels, options: MermaidRenderOptions = {}): Promise<void> {
  return renderMarkdownDiagrams(root, labels, { ...options, languages: ['mermaid'] })
}

/** Both diagram engines share admission, presentation and mounted ownership. */
export async function renderMarkdownDiagrams(root: HTMLElement, labels: MermaidRenderLabels, options: MermaidRenderOptions = {}): Promise<void> {
  if (options.signal?.aborted || !root.isConnected) return
  const candidates = root.matches('pre') ? [root as HTMLPreElement] : [...root.querySelectorAll<HTMLPreElement>('pre')]
  const languageOf = (pre: HTMLElement) => ['mermaid', 'dot', 'graphviz'].find(language => pre.querySelector(`code.language-${language}`))
  const allFences = candidates.filter(pre => { const language = languageOf(pre); return language && (!options.languages || options.languages.includes(language)) })
  const blocks = allFences.filter(pre => !claims.has(pre))
  if (!blocks.length) return
  const completed = options.budget ? [] : [...root.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')]
  const budget = options.budget ?? {
    sourceChars: completed.reduce((sum, element) => sum + Number(element.dataset.mermaidSourceLength || 0), 0),
    diagrams: completed.length,
    charged: new WeakSet<HTMLElement>(),
  }
  const charged = budget.charged ??= new WeakSet<HTMLElement>()
  for (const pre of blocks) {
    if (charged.has(pre)) continue
    charged.add(pre)
    budget.sourceChars += pre.querySelector('code')?.textContent?.length || 0
    budget.diagrams += 1
  }
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
      const language = languageOf(pre)!
      let reason: 'tooLarge' | 'blockedResource' | undefined
      if (overSurfaceBudget || source.length > MAX_SOURCE_CHARS || (language !== 'mermaid' && !checkGraphvizBudget(source))) reason = 'tooLarge'
      else if (language === 'mermaid' && hasUnsupportedMermaidImageSources(source)) reason = 'blockedResource'
      if (reason) {
        replaceError(pre, source, undefined, labels, reason, language)
        continue
      }
      if (language === 'mermaid') pre.style.minHeight = `${clampMermaidPreviewHeight(estimateMermaidPreviewHeight(source))}px`
      try {
        const palette = language === 'mermaid' ? undefined : diagramPalette(root)
        const width = Math.max(root.clientWidth, MIN_HOST_WIDTH)
        const key = JSON.stringify([language === 'graphviz' ? 'dot' : language, source, themeId, width, palette])
        const result = await enqueueRender(async () => {
          if (!current(pre)) return ''
          const cached = renderCache.get(key)
          if (cached !== undefined) return cached
          let svg: string
          if (language !== 'mermaid') {
            svg = sanitizeGraphvizSvg(await renderGraphviz(source, palette!, options.signal))
          } else {
            const mermaid = await getMermaid()
            if (!current(pre)) return ''
            initializeTheme(mermaid, themeId)
            const host = document.createElement('div')
            host.dataset.mermaidHost = '1'
            host.style.cssText = `position:fixed;left:-9999px;top:0;width:${width}px;opacity:0;pointer-events:none`
            document.body.appendChild(host)
            const removeHost = () => host.remove()
            options.signal?.addEventListener('abort', removeHost, { once: true })
            try {
              svg = (await renderMermaidWithRetry(mermaid.render.bind(mermaid), `opensquilla-mermaid-${++mermaidSequence}`, source, host)).svg
            } finally {
              options.signal?.removeEventListener('abort', removeHost)
              host.remove()
            }
          }
          return svg
        }, options.signal)
        if (!result || !current(pre)) continue
        const diagram = createSvgDiagram(result, source, labels)
        // Only cache results accepted by the shared SVG boundary. Each mount
        // namespaces IDs independently so cached definitions cannot collide.
        renderCache.set(key, result)
        diagram.element.dataset.mermaidSourceLength = String(source.length)
        diagram.element.dataset.diagramLanguage = language
        sources.set(diagram.element, { source, language })
        pre.replaceWith(diagram.element)
        options.onRendered?.(diagram)
      } catch (error) {
        if (current(pre)) replaceError(pre, source, error, labels, error instanceof MermaidLoadError ? 'loadFailed' : error instanceof DiagramRenderError ? (error.code === 'budget' ? 'tooLarge' : 'loadFailed') : undefined, language)
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

function replaceError(pre: HTMLElement, source: string, error: unknown, labels: MermaidRenderLabels, reason?: 'loadFailed' | 'tooLarge' | 'blockedResource', language = 'mermaid'): void {
  const card = mermaidErrorCard(reason ? labels[reason] : errorMessage(error, labels), source, labels, language)
  card.dataset.mermaidSourceLength = String(source.length)
  sources.set(card, { source, language, error, reason })
  card.dataset.diagramLanguage = language
  pre.replaceWith(card)
}

export function restoreMermaidSources(root: HTMLElement): void {
  for (const element of root.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')) {
    const entry = sources.get(element)
    if (!entry) continue
    const pre = document.createElement('pre')
    const code = document.createElement('code')
    code.className = `hljs language-${entry.language}`
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
    const next = mermaidErrorCard(entry.reason ? labels[entry.reason] : errorMessage(entry.error, labels), entry.source, labels, entry.language)
    next.dataset.mermaidSourceLength = card.dataset.mermaidSourceLength || ''
    next.dataset.diagramLanguage = entry.language
    const oldDetails = card.querySelector('details')
    const newDetails = next.querySelector('details')
    if (oldDetails && newDetails) newDetails.open = oldDetails.open
    sources.set(next, entry)
    card.replaceWith(next)
  }
}

function mermaidErrorCard(message: string, source: string, labels: MermaidRenderLabels, language = 'mermaid'): HTMLElement {
  const card = document.createElement('div')
  card.className = 'mermaid-error'
  const chip = document.createElement('span')
  chip.textContent = `⚠ ${language === 'mermaid' ? 'Mermaid' : 'Graphviz'}: ${message}`
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

/** Share clones cannot inherit WeakMap ownership. Restore only the clone. */
export function restoreClonedDiagramSources(original: HTMLElement, clone: HTMLElement): void {
  const originals = original.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')
  const copies = clone.querySelectorAll<HTMLElement>('[data-mermaid-source-length]')
  originals.forEach((element, index) => {
    const entry = sources.get(element)
    const target = copies[index]
    if (!entry || !target) return
    const pre = document.createElement('pre')
    const code = document.createElement('code')
    code.className = `hljs language-${entry.language}`
    code.textContent = entry.source
    pre.appendChild(code)
    target.replaceWith(pre)
  })
}
