import { onBeforeUnmount, onMounted, shallowRef, watch, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { decorateCodeBlocks } from '@/utils/chat/codeBlockChrome'
import { renderMermaidBlocks, resolvedDiagramThemeId, restoreMermaidSources, updateMermaidLabels } from '@/utils/markdown/mermaidRenderer'
import type { SvgDiagram } from '@/utils/markdown/svgDiagram'
import { useMarkdownImageLightbox } from './useMarkdownImageLightbox'

export interface MarkdownEnhancementOptions {
  version: () => unknown
  // Streaming appends keep committed nodes and their jobs alive. A retract
  // supplies resetVersion so an earlier stream generation can never commit.
  incremental?: boolean
  resetVersion?: () => unknown
}

// One lifecycle per mounted Markdown surface; parsing remains synchronous and
// framework-free. Diagrams, images and code chrome share the same mounted root.
export function useMarkdownEnhancements(root: Ref<HTMLElement | null>, options: MarkdownEnhancementOptions) {
  const { t, locale } = useI18n()
  const images = useMarkdownImageLightbox()
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
      decorateBlock(element)
    }
  }

  function decorateBlock(element: HTMLElement): void {
    decorateCodeBlocks(element, { copy: t('chat.copy'), copied: t('chat.copied'), copyFailed: t('chat.toast.copyFailed') })
    images.decorate(element)
    const signal = controller.signal
    const pass = renderMermaidBlocks(element, labels(), {
      signal,
      budget,
      themeId: resolvedDiagramThemeId(),
      onRendered(diagram) {
        if (signal.aborted || disposed) { diagram.dispose(); return }
        diagrams.add(diagram)
      },
    })
    // Incremental passes can overlap while the Mermaid engine is queued. The
    // exposed promise covers all work submitted by this owner generation.
    // A platform/DOM failure can reject outside the per-fence error path.
    // Keep the escaped source and prevent an unhandled background rejection.
    ready.value = Promise.all([ready.value, pass]).then(() => {}, () => {})
  }

  function invalidate(restore = false): void {
    controller.abort()
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
    for (const button of element.querySelectorAll('pre > .code-copy-btn')) button.remove()
    decorateCodeBlocks(element, { copy: t('chat.copy'), copied: t('chat.copied'), copyFailed: t('chat.toast.copyFailed') })
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
  }
  onBeforeUnmount(dispose)
  return { ready, refresh, dispose }
}
