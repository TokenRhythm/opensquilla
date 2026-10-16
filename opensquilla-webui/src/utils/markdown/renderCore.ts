import { Marked, type Token, type Tokens, type MarkedExtension } from 'marked'
import createDOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import { strictStrikethrough } from '@/utils/markdown/strikethrough'
import { workspaceFilePath } from '@/utils/chat/workspaceFiles'
import { isSafeMarkdownImageSource, isSafeMarkdownLinkSource } from '@/utils/markdown/markdownUris'

// Parsing and sanitization are shared; chat transforms and DOM interactions
// stay with their callers. Each parse owns its options and work budget.
const HIGHLIGHT_MAX_CHARS = 30_000
const MATH_MAX_CHARS = 16_000
const CODE_CLASS_RE = /^(?:hljs|hljs-[\w-]+|language-[\w#+.-]+|code-lang|function_|class_|inherited__)$/

export interface MarkdownRenderBudget {
  highlightCharsRemaining: number
  highlightBlocksRemaining: number
  mathCharsRemaining: number
  mathExpressionsRemaining: number
  mathScansRemaining: number
}

export function createMarkdownRenderBudget(): MarkdownRenderBudget {
  return {
    highlightCharsRemaining: 100_000,
    highlightBlocksRemaining: 32,
    mathCharsRemaining: 64_000,
    mathExpressionsRemaining: 256,
    mathScansRemaining: 2048,
  }
}

export interface MarkdownCoreOptions {
  highlight?: boolean
  math?: 'full' | 'defer'
  diagrams?: 'full' | 'defer'
  /** Resource adapters resolve these inert placeholders with session authorization. */
  images?: 'placeholder'
  /** A streaming message shares this budget across its committed blocks. */
  budget?: MarkdownRenderBudget
}

export function escapeMarkdownHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

type Purifier = ReturnType<typeof createDOMPurify>
let sanitizerWindow: Window | undefined
let purifier: Purifier | undefined
let mathPurifier: Purifier | undefined

// Private, lazy instances avoid changing other DOMPurify users and keep
// imports usable by Node-only resource adapters.
function ensureSanitizers(): boolean {
  if (typeof window === 'undefined') return false
  if (sanitizerWindow === window && purifier && mathPurifier) return true
  sanitizerWindow = window
  purifier = createDOMPurify(window)
  mathPurifier = createDOMPurify(window)
  purifier.addHook('uponSanitizeElement', (node, data) => {
    if (data.tagName === 'input' && (node as Element).getAttribute('type') !== 'checkbox') node.parentNode?.removeChild(node)
  })
  purifier.addHook('uponSanitizeAttribute', (node, data) => {
    const tag = node.nodeName.toLowerCase()
    if (tag === 'img' && data.attrName === 'src') {
      data.keepAttr = isSafeMarkdownImageSource(data.attrValue)
    } else if (tag === 'a' && data.attrName === 'href') {
      data.keepAttr = isSafeMarkdownLinkSource(data.attrValue)
    } else if (data.attrName === 'data-workspace-path') {
      // An inert candidate, never an href or authorization. TextPart resolves
      // it against the current session before installing a file action.
      data.keepAttr = tag === 'a' && workspaceFilePath(data.attrValue) !== null
    } else if (data.attrName === 'align') {
      data.keepAttr = (tag === 'th' || tag === 'td') && /^(left|center|right)$/.test(data.attrValue)
    } else if (data.attrName === 'start') {
      data.keepAttr = tag === 'ol' && /^-?\d{1,9}$/.test(data.attrValue)
    } else if (data.attrName === 'data-markdown-image' || data.attrName === 'data-markdown-image-alt') {
      data.keepAttr = tag === 'span'
    } else if (/^data-markdown-footnote(?:-ref|-back)?$/.test(data.attrName)) {
      data.keepAttr = (tag === 'a' || tag === 'li') && /^\d+$/.test(data.attrValue)
    } else if (data.attrName === 'data-markdown-footnotes') {
      data.keepAttr = tag === 'ol'
    } else if (data.attrName === 'tabindex') {
      data.keepAttr = tag === 'a' && data.attrValue === '0'
    } else if (data.attrName === 'role') {
      data.keepAttr = tag === 'a' && data.attrValue === 'link'
    } else if (data.attrName === 'type') {
      data.keepAttr = tag === 'input' && data.attrValue === 'checkbox'
    } else if (data.attrName === 'class') {
      if (tag !== 'code' && tag !== 'span') { data.keepAttr = false; return }
      data.attrValue = data.attrValue.split(/\s+/).filter(cls => CODE_CLASS_RE.test(cls)).join(' ')
      data.keepAttr = Boolean(data.attrValue)
    }
  })
  purifier.addHook('afterSanitizeAttributes', node => {
    if (node.nodeName === 'A' && /^https?:/i.test(node.getAttribute('href') || '')) {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    } else if (node.nodeName === 'IMG') {
      if (!node.getAttribute('src')) { node.parentNode?.removeChild(node); return }
      node.setAttribute('loading', 'lazy')
      node.setAttribute('decoding', 'async')
      node.setAttribute('referrerpolicy', 'no-referrer')
    } else if (node.nodeName === 'INPUT') {
      node.setAttribute('disabled', '')
    }
  })
  return true
}

function sanitizeMarkdown(html: string): string {
  const sanitized = purifier!.sanitize(html, {
    ALLOWED_TAGS: ['p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'strong', 'em', 'del', 'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div', 'span', 'sup', 'input'],
    ALLOWED_ATTR: ['href', 'title', 'alt', 'src', 'loading', 'decoding', 'referrerpolicy', 'target', 'rel', 'class', 'align', 'type', 'data-workspace-path', 'checked', 'disabled', 'start', 'role', 'tabindex', 'data-markdown-image', 'data-markdown-image-alt', 'data-markdown-footnote', 'data-markdown-footnote-ref', 'data-markdown-footnote-back', 'data-markdown-footnotes'],
    ALLOW_DATA_ATTR: false,
    ADD_URI_SAFE_ATTR: ['align', 'type', 'start', 'tabindex', 'data-workspace-path', 'data-markdown-image', 'data-markdown-image-alt', 'data-markdown-footnote', 'data-markdown-footnote-ref', 'data-markdown-footnote-back', 'data-markdown-footnotes'],
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i,
  })
  // A malfunctioning DOM must not cause another raw-HTML render. Validate
  // output without rewriting it; failure preserves complete escaped source.
  const template = document.createElement('template')
  template.innerHTML = sanitized
  if (template.content.querySelector('script,iframe,object,embed,form,style,svg,math,link,base,meta')) throw new Error('Unsafe Markdown sanitizer output')
  for (const element of template.content.querySelectorAll('*')) {
    for (const attr of element.attributes) {
      if (/^on/i.test(attr.name) || attr.name === 'style') throw new Error('Unsafe Markdown sanitizer attribute')
    }
    if (element.tagName === 'IMG' && !isSafeMarkdownImageSource(element.getAttribute('src') || '')) throw new Error('Unsafe Markdown image')
    if (element.tagName === 'A' && element.hasAttribute('href') && !isSafeMarkdownLinkSource(element.getAttribute('href') || '')) throw new Error('Unsafe Markdown link')
  }
  return sanitized
}

function renderMath(content: string, display: boolean): string {
  try {
    // Only library-generated math gets this wider policy. Raw Markdown never
    // gains SVG, MathML, arbitrary classes or inline styles.
    const generated = katex.renderToString(content, {
      displayMode: display, output: 'htmlAndMathml', throwOnError: false,
      trust: false, maxExpand: 1000, maxSize: 100,
    })
    // An inert outer container keeps the formula subtree intact in DOM
    // implementations that discard the first fragment element on sanitize.
    return mathPurifier!.sanitize(`<span>${generated}</span>`, {
      ALLOWED_TAGS: ['span', 'svg', 'path', 'line', 'math', 'semantics', 'annotation', 'mrow', 'mi', 'mn', 'mo', 'mtext', 'mspace', 'msup', 'msub', 'msubsup', 'mfrac', 'msqrt', 'mroot', 'mover', 'munder', 'munderover', 'mtable', 'mtr', 'mtd', 'mpadded', 'mphantom', 'menclose', 'mstyle', 'mmultiscripts', 'mprescripts', 'none'],
      ALLOWED_ATTR: ['class', 'style', 'aria-hidden', 'xmlns', 'width', 'height', 'viewBox', 'preserveAspectRatio', 'd', 'x1', 'x2', 'y1', 'y2', 'stroke', 'stroke-width', 'display', 'encoding', 'mathvariant', 'mathsize', 'mathcolor', 'mathbackground', 'stretchy', 'fence', 'separator', 'accent', 'accentunder', 'lspace', 'rspace', 'minsize', 'maxsize', 'movablelimits', 'columnalign', 'columnspacing', 'rowspacing', 'rowlines', 'columnlines', 'linethickness', 'scriptlevel', 'displaystyle', 'depth', 'voffset'],
      ALLOW_DATA_ATTR: false,
    })
  } catch {
    return `<code>${escapeMarkdownHtml(content)}</code>`
  }
}

function readMath(src: string, block: boolean): { raw: string; content: string; display: boolean } | undefined {
  const opening = src.startsWith('$$') ? '$$' : src.startsWith('\\[') ? '\\[' : src.startsWith('\\(') ? '\\(' : src.startsWith('$') ? '$' : ''
  if (!opening || (block && opening !== '$$' && opening !== '\\[')) return
  const closing = opening === '\\[' ? '\\]' : opening === '\\(' ? '\\)' : opening
  if (opening === '$' && /[\s$]/.test(src[1] || ' ')) return
  for (let index = opening.length; index < Math.min(src.length, MATH_MAX_CHARS); index += 1) {
    if ((opening === '$' || opening === '\\(') && src[index] === '\n') return
    if (src[index] === '\\' && !src.startsWith(closing, index)) { index += 1; continue }
    if (!src.startsWith(closing, index)) continue
    if (opening === '$' && (/\s/.test(src[index - 1]) || /[\d$]/.test(src[index + 1] || ''))) continue
    const content = src.slice(opening.length, index).trim()
    if (!content || (opening === '$' && /^\d/.test(content) && /(^|[^\\])\$/.test(content))) return
    return { raw: src.slice(0, index + closing.length), content, display: opening === '$$' || opening === '\\[' }
  }
  if (src.length >= MATH_MAX_CHARS) {
    // A lone dollar in long prose is not evidence of an oversized formula.
    // Reject only a possible closed formula beyond the bounded math scan.
    const end = src.indexOf(closing, MATH_MAX_CHARS)
    const newline = (opening === '$' || opening === '\\(') ? src.indexOf('\n') : -1
    if (end >= 0 && (newline < 0 || end < newline)) throw new RangeError('Formula exceeds the render budget')
  }
}

interface Footnote { raw: string; tokens: Token[]; number?: number }

function renderContent(text: string, opts: MarkdownCoreOptions): string {
  const notes = new Map<string, Footnote>()
  const usedNotes: Footnote[] = []
  const budget = opts.budget ?? createMarkdownRenderBudget()
  const generated: string[] = []
  // Unpredictable markers cannot be forged using literal or entity-encoded
  // private-use characters in assistant text.
  let marker = `\uE000MARKDOWN${Array.from(crypto.getRandomValues(new Uint32Array(4)), value => value.toString(16).padStart(8, '0')).join('')}`
  while (text.includes(marker)) marker += '_'
  const mathToken = (src: string, block: boolean) => {
    if (!/^(?:\$|\\[([])/.test(src)) return
    if (budget.mathScansRemaining <= 0) throw new RangeError('Math scan budget exhausted')
    budget.mathScansRemaining -= 1
    return readMath(src, block)
  }
  const mathHtml = (token: Tokens.Generic): string => {
    const content = String(token.content)
    if (budget.mathExpressionsRemaining <= 0 || content.length > budget.mathCharsRemaining) return escapeMarkdownHtml(token.raw)
    budget.mathCharsRemaining -= content.length
    budget.mathExpressionsRemaining -= 1
    return `${marker}${generated.push(renderMath(content, Boolean(token.display))) - 1}\uE001`
  }
  const mathExtensions: MarkedExtension = opts.math === 'defer' ? {} : { extensions: [
    {
      name: 'blockMath', level: 'block',
      start(src) { const match = /(?:^|\n)(?:\$\$|\\\[)/.exec(src); if (match && budget.mathScansRemaining <= 0) throw new RangeError('Math scan budget exhausted'); return match ? match.index + (match[0].startsWith('\n') ? 1 : 0) : undefined },
      tokenizer(src) { const result = mathToken(src, true); return result ? { type: 'blockMath', ...result } : undefined },
      renderer: mathHtml,
    },
    {
      name: 'inlineMath', level: 'inline',
      start(src) { const index = src.search(/\$|\\[([]/); if (index >= 0 && budget.mathScansRemaining <= 0) throw new RangeError('Math scan budget exhausted'); return index < 0 ? undefined : index },
      tokenizer(src) { if (this.lexer.state.inRawBlock) return; const result = mathToken(src, false); return result ? { type: 'inlineMath', ...result } : undefined },
      renderer: mathHtml,
    },
  ] }
  const parser = new Marked(strictStrikethrough, mathExtensions, {
    breaks: true,
    extensions: [
      {
        name: 'footnoteDefinition', level: 'block',
        start(src) { const match = /(?:^|\n) {0,3}\[\^[^\]\s]+\]:/.exec(src); return match ? match.index + (match[0].startsWith('\n') ? 1 : 0) : undefined },
        tokenizer(src) {
          const match = /^ {0,3}\[\^([^\]\s]+)\]:[ \t]*([^\n]*)(?:\n|$)/.exec(src)
          if (!match) return
          let raw = match[0]
          let body = match[2]
          const continuation = /^(?:(?:\n(?= {4}|\t))?(?: {4}|\t)[^\n]*(?:\n|$))/
          let next: RegExpExecArray | null
          while ((next = continuation.exec(src.slice(raw.length)))) {
            raw += next[0]
            body += `\n${next[0].replace(/^( {4}|\t)/gm, '').replace(/\n$/, '')}`
          }
          const key = match[1].toLowerCase()
          // Duplicate definitions remain readable instead of silently losing text.
          if (notes.has(key)) return { type: 'footnoteDefinition', raw, duplicate: true }
          const note: Footnote = { raw, tokens: [] }
          notes.set(key, note)
          note.tokens = this.lexer.blockTokens(body)
          return { type: 'footnoteDefinition', raw, key }
        },
        renderer(token) { return token.duplicate ? `<p>${escapeMarkdownHtml(token.raw)}</p>` : '' },
      },
      {
        name: 'footnoteReference', level: 'inline',
        start(src) { const index = src.indexOf('[^'); return index < 0 ? undefined : index },
        tokenizer(src) { if (this.lexer.state.inRawBlock || this.lexer.state.inLink) return; const match = /^\[\^([^\]\s]+)\]/.exec(src); return match ? { type: 'footnoteReference', raw: match[0], key: match[1].toLowerCase() } : undefined },
        renderer(token) {
          const note = notes.get(token.key)
          if (!note) return escapeMarkdownHtml(token.raw)
          note.number ??= usedNotes.push(note)
          return `<sup><a role="link" tabindex="0" data-markdown-footnote-ref="${note.number}">${note.number}</a></sup>`
        },
      },
    ],
    renderer: {
      link(token: Tokens.Link): string | false {
        const path = workspaceFilePath(token.href, true)
        return path ? `<a data-workspace-path="${escapeMarkdownHtml(path)}">${this.parser.parseInline(token.tokens)}</a>` : false
      },
      image(token: Tokens.Image): string | false {
        return opts.images === 'placeholder' && !isSafeMarkdownImageSource(token.href)
          ? `<span data-markdown-image="${escapeMarkdownHtml(token.href)}" data-markdown-image-alt="${escapeMarkdownHtml(token.text)}">${escapeMarkdownHtml(token.text)}</span>` : false
      },
      code({ text: code, lang, raw }: Tokens.Code): string {
        const language = (lang || '').trim().split(/\s+/)[0].toLowerCase()
        const highlight = opts.highlight !== false && Boolean(language) && code.length <= HIGHLIGHT_MAX_CHARS
          && code.length <= budget.highlightCharsRemaining && budget.highlightBlocksRemaining > 0 && Boolean(hljs.getLanguage(language))
        let body = ''
        if (highlight) {
          budget.highlightCharsRemaining -= code.length
          budget.highlightBlocksRemaining -= 1
          try { body = hljs.highlight(code, { language, ignoreIllegals: true }).value } catch { /* Plain source follows. */ }
        }
        const label = language ? `<span class="code-lang">${escapeMarkdownHtml(language)}</span>` : ''
        const fence = raw.match(/^ {0,3}(`{3,}|~{3,})/)?.[1]
        const lines = raw.trimEnd().split('\n')
        const closing = lines[lines.length - 1]?.trim() || ''
        const closed = Boolean(fence && new RegExp(`^${fence[0]}{${fence.length},}$`).test(closing))
        const diagram = /^(mermaid|dot|graphviz)$/.test(language)
        const deferred = diagram && (opts.diagrams === 'defer' || !closed)
        const langClass = language && !deferred ? ` language-${escapeMarkdownHtml(language)}` : ''
        return `<pre>${label}<code class="hljs${langClass}">${body || escapeMarkdownHtml(code)}</code></pre>\n`
      },
    },
  })
  let rawHtml = parser.parse(text, { async: false }) as string
  const renderedNotes: string[] = []
  for (const note of usedNotes) {
    renderedNotes.push(`<li data-markdown-footnote="${note.number}">${parser.parser(note.tokens)}<a role="link" tabindex="0" data-markdown-footnote-back="${note.number}">↩</a></li>`)
  }
  if (renderedNotes.length) rawHtml += `<hr><ol data-markdown-footnotes="">${renderedNotes.join('')}</ol>`
  for (const note of notes.values()) if (!note.number) rawHtml += `<p>${escapeMarkdownHtml(note.raw)}</p>`
  const html = sanitizeMarkdown(rawHtml)
  return html.replace(new RegExp(`${marker}(\\d+)\uE001`, 'g'), (_, index) => generated[Number(index)] || '')
}

/** Preserve complete, inert source on parse or sanitizer failure. */
export function renderMarkdownCore(text: string, opts: MarkdownCoreOptions = {}): string {
  if (!text) return ''
  try {
    if (!ensureSanitizers()) throw new Error('Markdown rendering requires a DOM sanitizer')
    return renderContent(text, opts)
  } catch {
    return `<div class="chat-markdown-plain">${escapeMarkdownHtml(text)}</div>`
  }
}
