import { renderMarkdownCore, type MarkdownCoreOptions } from '@/utils/markdown/renderCore'
import { sanitizeAssistantPresentationText } from '@/utils/chat/silentSentinels'
import type { AssistantPresentationProvenance } from '@/utils/chat/silentSentinels'

// Chat-bubble facade over the shared markdown pipeline (utils/markdown/
// renderCore.ts): adds the chat-message-specific presentation transforms
// (directive tags, generated-artifact markers), the LRU HTML cache, and the
// copy-text sanitizer. Streaming committed blocks and artifact previews call
// renderMarkdownCore directly.

const DIRECTIVE_TAG_RE = /\[\[\s*(?:reply_to_current|reply_to\s*:\s*[^\]\n]+)\s*\]\]\s*/g
const GENERATED_ARTIFACT_MARKER_RE = /(?:^|\s*)\[generated artifact omitted:\s*[^\]\n]+?\]\s*/gi
const TIME_PREFIX_RE = /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[+\-]\d{2}:\d{2} (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Za-z0-9_+\-/]+\]\n/

const MARKDOWN_CACHE_MAX_BYTES = 8 * 1024 * 1024
const MARKDOWN_CACHE_MAX_ITEM_BYTES = 256 * 1024

export interface RenderMarkdownOptions extends MarkdownCoreOptions {
  cache?: 'settled' | 'none'
}

export function useChatTextRendering() {
  const markdownCache = new Map<string, { html: string, bytes: number }>()
  let markdownCacheBytes = 0

  function stripDirectiveTags(text: string): string {
    return text.replace(DIRECTIVE_TAG_RE, '').replace(/^\n+/, '')
  }

  function stripGeneratedArtifactMarkers(text: string): string {
    text = String(text || '')
    if (!text.includes('[generated artifact omitted:')) return text
    return text.replace(/\r\n/g, '\n').replace(GENERATED_ARTIFACT_MARKER_RE, '').replace(/[ \t]{2,}/g, ' ').replace(/\n{3,}/g, '\n\n').trim()
  }

  function stripTimePrefix(text: string): string {
    return typeof text === 'string' ? text.replace(TIME_PREFIX_RE, '') : text
  }

  function renderMarkdown(text: string, opts?: RenderMarkdownOptions): string {
    // Tool-protocol compatibility belongs to the shared backend stream. The UI
    // cannot infer intent from user-visible Markdown: `<tool_calls>` may be
    // documentation inside inline/fenced code, and cutting at that marker loses
    // the rest of an otherwise valid answer. Keep canonical text here and apply
    // only the established directive/artifact presentation transforms.
    text = stripDirectiveTags(stripGeneratedArtifactMarkers(text))
    if (!text) return ''

    // Cache key is namespaced by highlight mode so a plain streaming render is
    // never served where a highlighted one is expected (and vice versa).
    const highlight = opts?.highlight !== false
    const cacheMode = opts?.cache ?? 'settled'
    const mathMode = opts?.math ?? 'full'
    const cacheKey = `${highlight ? 'H' : 'P'}${mathMode === 'full' ? 'M' : 'D'}\n${text}`
    if (cacheMode === 'settled') {
      const cached = markdownCache.get(cacheKey)
      if (cached !== undefined) {
        // Map insertion order is the LRU order. Refresh a hit without changing
        // the retained-byte accounting.
        markdownCache.delete(cacheKey)
        markdownCache.set(cacheKey, cached)
        return cached.html
      }
    }

    const html = renderMarkdownCore(text, { highlight, math: mathMode })

    if (cacheMode === 'settled') {
      // UTF-16 code units are a conservative and deterministic approximation
      // of retained JS string storage. Count both the key (which embeds the
      // source) and the sanitized HTML value.
      const bytes = (cacheKey.length + html.length) * 2
      if (bytes <= MARKDOWN_CACHE_MAX_ITEM_BYTES) {
        while (markdownCacheBytes + bytes > MARKDOWN_CACHE_MAX_BYTES) {
          const firstKey = markdownCache.keys().next().value
          if (firstKey === undefined) break
          const evicted = markdownCache.get(firstKey)
          markdownCache.delete(firstKey)
          markdownCacheBytes -= evicted?.bytes ?? 0
        }
        markdownCache.set(cacheKey, { html, bytes })
        markdownCacheBytes += bytes
      }
    }
    return html
  }

  function clearMarkdownCache(): void {
    markdownCache.clear()
    markdownCacheBytes = 0
  }

  function markdownCacheStats(): { entries: number, bytes: number } {
    return { entries: markdownCache.size, bytes: markdownCacheBytes }
  }

  function sanitizeCopyText(
    text: string,
    opts?: {
      assistantBoundary?: boolean
      provenance?: AssistantPresentationProvenance
    },
  ): string {
    const sanitized = stripDirectiveTags(
      stripGeneratedArtifactMarkers(stripTimePrefix(String(text || ''))),
    )
    return (opts?.assistantBoundary === false
      ? sanitized
      : sanitizeAssistantPresentationText(sanitized, opts?.provenance)).trim()
  }

  return {
    renderMarkdown,
    clearMarkdownCache,
    markdownCacheStats,
    sanitizeCopyText,
    stripDirectiveTags,
    stripGeneratedArtifactMarkers,
    stripTimePrefix,
  }
}
