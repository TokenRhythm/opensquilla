import { onBeforeUnmount, onMounted, shallowRef, watch, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { createCodeBlockChrome } from '@/utils/chat/codeBlockChrome'
import { renderMarkdownDiagrams, resolvedDiagramThemeId, restoreMermaidSources, updateMermaidLabels } from '@/utils/markdown/mermaidRenderer'
import { bindMarkdownFootnotes } from '@/utils/markdown/footnotes'
import type { SvgDiagram } from '@/utils/markdown/svgDiagram'
import { useMarkdownImageLightbox } from './useMarkdownImageLightbox'

export interface MarkdownEnhancementOptions {
  version: () => unknown
  // Streaming appends keep committed nodes and their jobs alive. A retract
  // supplies resetVersion so an earlier stream generation can never commit.
  incremental?: boolean
  resetVersion?: () => unknown
  /** A trace owns multiple independent Markdown bodies. */
  selector?: string
}

// One lifecycle per mounted Markdown surface; parsing remains synchronous and
// framework-free. Diagrams, images and code chrome share the same mounted root.
export function useMarkdownEnhancements(root: Ref<HTMLElement | null>, options: MarkdownEnhancementOptions) {
  const { t, locale } = useI18n()
  const images = useMarkdownImageLightbox()
  const chrome = createCodeBlockChrome()
  const notes = new Map<HTMLElement, () => void>()
  let visibility: IntersectionObserver | null = null
  const visibleJobs = new Map<HTMLElement, AbortController>()
  const ready = shallowRef<Promise<void>>(Promise.resolve())
  const diagrams = new Set<SvgDiagram>()
  let controller = new AbortController()
  let observer: MutationObserver | null = null
  let themeId = ''
  let disposed = false
  let lastProcessedNode: Element | null = null
  let budget = { sourceChars: 0, diagrams: 0 }
  let ownedRoot: HTMLElement | null = null
  let renderedVersion: unknown

  function labels() {
    return {
      zoomIn: t('chat.mermaid.zoomIn'), zoomOut: t('chat.mermaid.zoomOut'), reset: t('chat.mermaid.reset'),
      copyCode: t('chat.mermaid.copyCode'), copied: t('chat.copied'), copyFailed: t('chat.toast.copyFailed'),
      exportPng: t('chat.mermaid.exportPng'), exportFailed: t('chat.mermaid.exportFailed'),
      loadFailed: t('chat.mermaid.loadFailed'), tooLarge: t('chat.mermaid.tooLarge'),
      blockedResource: t('chat.mermaid.blockedResource'), viewSource: t('chat.mermaid.viewSource'),
      syntaxErrorAt: (line: number, excerpt: string) => t('chat.mermaid.syntaxErrorAt', { line, excerpt }),
    }
  }

  function decorate(): void {
    const element = root.value
    if (disposed || !element?.isConnected) return
    ownedRoot = element
    renderedVersion = options.version()
    if (options.incremental) {
      // The live stream is a stable append root. Its tail node is reused while
      // new committed blocks are inserted before it, so never advance onto it.
      let child = lastProcessedNode?.nextElementSibling ?? (lastProcessedNode ? null : element.firstElementChild)
      while (child && !child.classList.contains('streaming-tail')) {
        if (child instanceof HTMLElement && child.classList.contains('streaming-rich-block')) decorateBlock(child)
        lastProcessedNode = child
        child = child.nextElementSibling
      }
    } else {
      const bodies = options.selector ? [...element.querySelectorAll<HTMLElement>(options.selector)] : [element]
      for (const body of bodies) decorateBlock(body)
    }
  }

  function track(pass: Promise<void>): void {
    ready.value = Promise.all([ready.value, pass]).then(() => {}, () => {})
  }

  function render(element: HTMLElement, signal: AbortSignal): Promise<void> {
    return renderMarkdownDiagrams(element, labels(), {
      signal, budget, themeId: resolvedDiagramThemeId(),
      onRendered(diagram) {
        if (signal.aborted || disposed) { diagram.dispose(); return }
        diagrams.add(diagram)
      },
    })
  }

  function decorateBlock(element: HTMLElement): void {
    chrome.decorate(element, { copy: t('chat.copy'), copied: t('chat.copied'), copyFailed: t('chat.toast.copyFailed') })
    images.decorate(element)
    if (!notes.has(element)) notes.set(element, bindMarkdownFootnotes(element))
    const fences = [...element.querySelectorAll<HTMLPreElement>('pre')]
      .filter(pre => pre.querySelector('code.language-mermaid, code.language-dot, code.language-graphviz'))
    if (typeof IntersectionObserver === 'undefined' || !fences.length) {
      track(render(element, controller.signal))
      return
    }
    if (!visibility) {
      const ownerSignal = controller.signal
      visibility = new IntersectionObserver(entries => {
        if (ownerSignal.aborted || disposed) return
        for (const entry of entries) {
          const pre = entry.target as HTMLElement
          if (!entry.isIntersecting) {
            visibleJobs.get(pre)?.abort()
            visibleJobs.delete(pre)
            continue
          }
          if (visibleJobs.has(pre)) continue
          const request = new AbortController()
          visibleJobs.set(pre, request)
          track(render(pre, request.signal).finally(() => {
            if (visibleJobs.get(pre) !== request) return
            visibleJobs.delete(pre)
            if (!request.signal.aborted) visibility?.unobserve(pre)
          }))
        }
      })
    }
    for (const pre of fences) visibility.observe(pre)
  }

  function invalidate(restore = false): void {
    controller.abort()
    for (const request of visibleJobs.values()) request.abort()
    visibleJobs.clear()
    visibility?.disconnect()
    visibility = null
    for (const release of notes.values()) release()
    notes.clear()
    chrome.reset()
    controller = new AbortController()
    for (const diagram of diagrams) diagram.dispose()
    diagrams.clear()
    images.reset()
    lastProcessedNode = null
    budget = { sourceChars: 0, diagrams: 0 }
    if (restore && root.value?.isConnected) restoreMermaidSources(root.value)
    ready.value = Promise.resolve()
  }

  function refresh(): void {
    if (disposed) return
    invalidate(true)
    decorate()
  }

  watch([root, options.version], ([element, version]) => {
    if (element === ownedRoot && Object.is(version, renderedVersion)) return
    if (!options.incremental || element !== ownedRoot) invalidate()
    ownedRoot = element
    renderedVersion = version
    decorate()
  }, { flush: 'post' })
  if (options.resetVersion) watch(options.resetVersion, () => { invalidate(); decorate() }, { flush: 'post' })
  watch(locale, () => {
    const element = root.value
    if (!element?.isConnected || disposed) return
    // Rebuild only button chrome; locale changes preserve diagram pan/zoom.
    chrome.updateLabels({ copy: t('chat.copy'), copied: t('chat.copied'), copyFailed: t('chat.toast.copyFailed') })
    updateMermaidLabels(element, labels())
  }, { flush: 'post' })

  onMounted(() => {
    themeId = resolvedDiagramThemeId()
    observer = new MutationObserver(() => {
      const nextTheme = resolvedDiagramThemeId()
      if (nextTheme === themeId) return
      themeId = nextTheme
      refresh()
    })
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    decorate()
  })
  function dispose(): void {
    if (disposed) return
    disposed = true
    invalidate()
    observer?.disconnect()
    observer = null
    images.dispose()
    chrome.dispose()
  }
  onBeforeUnmount(dispose)
  return { ready, refresh, dispose }
}
