import { marked, type Tokens } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import { strictStrikethrough } from '@/utils/markdown/strikethrough'
import { workspaceFilePath } from '@/utils/chat/workspaceFiles'

// The single markdown parse pipeline for every assistant-authored surface:
// chat bubbles (via useChatTextRendering's cache), streaming committed blocks,
// and workbench artifact previews (renderArtifactMarkdown). Owns the shared
// `marked` renderer configuration, the DOMPurify hooks, math stashing, and the
// highlight toggle. Chat-message-specific text transforms (directive tags,
// artifact markers) stay OUT of here — they belong to the chat callers.

const MATH_SCAN_RE = /(```[\s\S]*?```|`[^`\n]+?`|\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([^)\n]+?\\\)|\$(?![\s\d])(?:\\\$|[^$\n])+?(?<![\s])\$)/g
const MATH_SENTINEL_RE = /\uE000M(\d+)\uE001/g
// Highlighting is synchronous inside the streaming render path; past this
// size a block renders as plain mono text so it cannot stall a flush.
const HIGHLIGHT_MAX_CHARS = 30_000
// The only class names allowed through sanitization: highlighter token
// classes (incl. sub-scope suffixes like `function_`) and the code chrome.
const CODE_CLASS_RE = /^(?:hljs|hljs-[\w-]+|language-[\w#+.-]+|code-lang|function_|class_|inherited__)$/
const KATEX_CLASS_RE = /^[A-Za-z][\w-]*$/

type MathEntry = {
  type: 'inline' | 'display'
  content: string
}

export interface MarkdownCoreOptions {
  highlight?: boolean
  math?: 'full' | 'defer'
}

// Syntax highlighting is the heaviest part of the render and re-runs over the
// whole code block on every flush during streaming. While a turn is streaming
// we render code as plain (escaped) monospace and defer highlighting to the
// committed message — a one-time recolor at the end, no reflow. renderMarkdown
// toggles this around each parse; it is synchronous so the flag never leaks.
let codeHighlightEnabled = true
let katexSanitizeEnabled = false

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

marked.use(strictStrikethrough, {
  renderer: {
    link(token: Tokens.Link): string | false {
      const path = workspaceFilePath(token.href, true)
      if (!path) return false
      // Local references remain inert until the session file API confirms access.
      return `<a data-workspace-path="${escapeHtml(path)}">${this.parser.parseInline(token.tokens)}</a>`
    },
    code({ text, lang }: Tokens.Code): string {
      const language = (lang || '').trim().split(/\s+/)[0].toLowerCase()
      const canHighlight =
        codeHighlightEnabled && language.length > 0 && text.length <= HIGHLIGHT_MAX_CHARS && Boolean(hljs.getLanguage(language))
      let body = ''
      if (canHighlight) {
        try {
          body = hljs.highlight(text, { language, ignoreIllegals: true }).value
        } catch {
          body = ''
        }
      }
      if (!body) body = escapeHtml(text)
      const label = language ? `<span class="code-lang">${escapeHtml(language)}</span>` : ''
      // The language class is emitted even when this pass renders plain (stream
      // fallback, unknown language): downstream DOM decoration finds mermaid
      // fences and code chrome by `language-*`, not by highlight state.
      const langClass = language ? ` language-${language}` : ''
      return `<pre>${label}<code class="hljs${langClass}">${body}</code></pre>\n`
    },
  },
})

// Hooks are registered lazily on the first sanitize call: importing this
// module must stay side-effect-free so non-DOM test environments (and future
// SSR-adjacent uses) can pull the artifact-preview chain in without a window.
let sanitizerConfigured = false

function ensureSanitizerConfigured(): void {
  if (sanitizerConfigured) return
  sanitizerConfigured = true

  // Markdown only ever emits <input> as a disabled task-list checkbox. Drop any
  // other raw <input> outright so assistant text cannot render editable fields.
  DOMPurify.addHook('uponSanitizeElement', (node, data) => {
    if (data.tagName !== 'input') return
    if ((node as Element).getAttribute('type') !== 'checkbox') {
      node.parentNode?.removeChild(node)
    }
  })

  // GFM table `align` and the task-list checkbox `type` are allow-listed and
  // marked URI-safe (see ADD_URI_SAFE_ATTR below) so the sanitizer keeps them
  // through its normal pipeline; here they are additionally constrained to the
  // exact tags and values markdown emits, so nothing else can ride in on those
  // attribute names. `class` is only allowed where the code renderer above emits
  // it; markdown cannot smuggle arbitrary classes onto other elements.
  DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
    const tag = node.nodeName.toLowerCase()

    // Markdown images: http(s) and inline raster data URIs only. <img> sits in
    // DOMPurify's default DATA_URI_TAGS allow-list, so ALLOWED_URI_REGEXP never
    // governs data: values on it (browsa hit this: restricting the regex did not
    // stop data:image/svg+xml srcs) — every src is therefore gated here, and
    // svg+xml is rejected outright as an active document.
    if (tag === 'img' && data.attrName === 'src') {
      const value = String(data.attrValue || '').trim()
      const ok = /^https?:\/\//i.test(value)
        || /^data:image\/(?:png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=]+$/.test(value)
      if (!ok) data.keepAttr = false
      return
    }

    // Table column alignment — only the enum values, and only on table cells.
    if (data.attrName === 'align') {
      const ok = (tag === 'th' || tag === 'td')
        && (data.attrValue === 'left' || data.attrValue === 'center' || data.attrValue === 'right')
      if (!ok) data.keepAttr = false
      return
    }

    // The only inputs markdown emits are disabled task-list checkboxes.
    if (data.attrName === 'type') {
      if (!(tag === 'input' && data.attrValue === 'checkbox')) data.keepAttr = false
      return
    }

    if (katexSanitizeEnabled && tag === 'span' && data.attrName === 'style') return
    if (katexSanitizeEnabled && tag === 'span' && data.attrName === 'aria-hidden') return

    if (data.attrName !== 'class') return
    if (tag !== 'code' && tag !== 'span') {
      data.keepAttr = false
      return
    }
    const safe = String(data.attrValue || '')
      .split(/\s+/)
      .filter(cls => CODE_CLASS_RE.test(cls) || (katexSanitizeEnabled && KATEX_CLASS_RE.test(cls)))
    if (safe.length === 0) {
      data.keepAttr = false
      return
    }
    data.attrValue = safe.join(' ')
  })

  // External links open in a new tab without leaking the opener (only http(s)
  // anchors become cross-document). Task-list checkboxes are forced inert so a
  // raw `<input type="checkbox">` cannot render as an interactive control.
  DOMPurify.addHook('afterSanitizeAttributes', node => {
    if (node.nodeName === 'A') {
      const href = node.getAttribute('href') || ''
      if (/^https?:/i.test(href)) {
        node.setAttribute('target', '_blank')
        node.setAttribute('rel', 'noopener noreferrer')
      }
      return
    }
    if (node.nodeName === 'IMG') {
      // The src gate above may have stripped the only source a blocked image
      // had (active scheme, relative path, svg data URI). Drop the element
      // outright — an attribute-less <img> renders as a broken-image box.
      if (!node.getAttribute('src')) {
        node.parentNode?.removeChild(node)
        return
      }
      // Lazy fetch + off-main-thread decode keep inline images from stuttering
      // the stream while large screenshots load.
      node.setAttribute('loading', 'lazy')
      node.setAttribute('decoding', 'async')
      return
    }
    if (node.nodeName === 'INPUT') {
      node.setAttribute('disabled', '')
    }
  })
}

function makeMathEntry(raw: string): MathEntry | null {
  if (raw.startsWith('$$') && raw.endsWith('$$')) {
    return { type: 'display', content: raw.slice(2, -2).trim() }
  }
  if (raw.startsWith('\\[') && raw.endsWith('\\]')) {
    return { type: 'display', content: raw.slice(2, -2).trim() }
  }
  if (raw.startsWith('\\(') && raw.endsWith('\\)')) {
    return { type: 'inline', content: raw.slice(2, -2).trim() }
  }
  if (raw.startsWith('$') && raw.endsWith('$')) {
    return { type: 'inline', content: raw.slice(1, -1).trim() }
  }
  return null
}

function stashMath(text: string): { text: string, stash: MathEntry[] } {
  const stash: MathEntry[] = []
  const stashedText = text.replace(MATH_SCAN_RE, raw => {
    if (raw.startsWith('```') || raw.startsWith('`')) return raw
    const entry = makeMathEntry(raw)
    if (!entry) return raw
    const idx = stash.length
    stash.push(entry)
    return `\uE000M${idx}\uE001`
  })
  return { text: stashedText, stash }
}

function renderMath(entry: MathEntry): string {
  try {
    return katex.renderToString(entry.content, {
      displayMode: entry.type === 'display',
      throwOnError: false,
      output: 'html',
    })
  } catch {
    return `<code class="math-raw" title="LaTeX formula (parse error)">${escapeHtml(entry.content)}</code>`
  }
}

function restoreMath(html: string, stash: MathEntry[]): string {
  if (stash.length === 0) return html
  return html.replace(MATH_SENTINEL_RE, (_, i) => {
    const entry = stash[Number(i)]
    return entry ? renderMath(entry) : ''
  })
}

function sanitizeMarkdownHtml(rawHtml: string, allowKatex = false): string {
  ensureSanitizerConfigured()
  katexSanitizeEnabled = allowKatex
  try {
    return DOMPurify.sanitize(rawHtml, {
      ALLOWED_TAGS: [
        'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
        'strong', 'em', 'del', 'a', 'img', 'table', 'thead',
        'tbody', 'tr', 'th', 'td', 'div', 'span', 'sup', 'input',
      ],
      // `align` carries GFM table column alignment; `type`/`checked`/`disabled`
      // are the (disabled) task-list checkbox attributes; `src`/`loading`/
      // `decoding` are the markdown image attributes (src is value-gated by the
      // hook above). No script vectors.
      ALLOWED_ATTR: [
        'href', 'title', 'alt', 'src', 'loading', 'decoding', 'target', 'rel', 'class', 'align', 'type',
        'data-workspace-path', 'checked', 'disabled', ...(allowKatex ? ['style', 'aria-hidden'] : []),
      ],
      // `align`/`type` carry inert presentational values, not URIs; mark them
      // safe so the value gate keeps them (the hook above constrains the values).
      ADD_URI_SAFE_ATTR: ['align', 'type'],
      ALLOWED_URI_REGEXP: /^(?:https?|mailto|#):/i,
    })
  } finally {
    katexSanitizeEnabled = false
  }
}

// marked parse without sanitization — exclusively for renderArtifactMarkdown's
// degraded-mode fallback (when DOMPurify output fails the active-content
// sniff, the raw parse is re-filtered with native DOM traversal). Never insert
// its output directly.
export function parseMarkdownRaw(text: string): string {
  return marked.parse(text, { async: false, breaks: true }) as string
}

// Full parse pipeline with no cache and no chat-message-specific text
// transforms. `math: 'defer'` skips math during streaming; 'full' renders it.
export function renderMarkdownCore(text: string, opts: MarkdownCoreOptions = {}): string {  const highlight = opts.highlight !== false
  const mathMode = opts.math ?? 'full'
  let rawHtml: string
  const { text: stashedText, stash } = mathMode === 'defer'
    ? { text, stash: [] as MathEntry[] }
    : stashMath(text)
  // Toggle the shared code-highlight flag only across the synchronous parse;
  // try/finally guarantees it is restored even if marked.parse throws, so a
  // later highlighted render can never inherit a stale "plain" flag.
  codeHighlightEnabled = highlight
  try {
    rawHtml = marked.parse(stashedText, { async: false, breaks: true }) as string
  } finally {
    codeHighlightEnabled = true
  }
  const sanitizedHtml = sanitizeMarkdownHtml(rawHtml)
  return stash.length > 0
    ? sanitizeMarkdownHtml(restoreMath(sanitizedHtml, stash), true)
    : sanitizedHtml
}
