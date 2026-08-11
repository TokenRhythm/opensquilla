import createDOMPurify from 'dompurify'
import { marked } from 'marked'
import type { ArtifactPayload } from '@/types/rpc'
import { artifactExtension, artifactMime, artifactName } from '@/utils/chat/artifacts'
import { strictStrikethrough } from '@/utils/markdown/strikethrough'

// `marked` is a shared singleton, so declare the rule here too: a markdown
// artifact renders the same text as the chat bubble it came from, and must not
// depend on whether the chat renderer happens to have been imported first.
marked.use(strictStrikethrough)

export const ARTIFACT_TEXT_PREVIEW_LIMIT = 5 * 1024 * 1024
export const ARTIFACT_BINARY_PREVIEW_LIMIT = 30 * 1024 * 1024
export const ARTIFACT_HTML_REFERENCE_SCAN_LIMIT = ARTIFACT_TEXT_PREVIEW_LIMIT
export const ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT = 64

export type ArtifactWorkbenchPreviewKind =
  | 'html'
  | 'image'
  | 'markdown'
  | 'pdf'
  | 'text'
  | 'unsupported'

const HTML_EXTENSIONS = new Set(['htm', 'html', 'xhtml'])
const MARKDOWN_EXTENSIONS = new Set(['markdown', 'md', 'mdown', 'mkd'])
const TEXT_EXTENSIONS = new Set(['log', 'text', 'txt'])
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'ico', 'jpeg', 'jpg', 'png', 'svg', 'webp'])
const MARKDOWN_ALLOWED_TAGS = new Set([
  'A', 'BLOCKQUOTE', 'BR', 'CODE', 'DEL', 'EM', 'H1', 'H2', 'H3', 'H4',
  'H5', 'H6', 'HR', 'LI', 'OL', 'P', 'PRE', 'STRONG', 'TABLE', 'TBODY',
  'TD', 'TH', 'THEAD', 'TR', 'UL',
])
const MARKDOWN_DROP_WITH_CONTENT = new Set([
  'AUDIO', 'BASE', 'FORM', 'IFRAME', 'OBJECT', 'SCRIPT', 'STYLE', 'SVG', 'VIDEO',
])

function normalizedMime(value: unknown): string {
  return typeof value === 'string'
    ? value.split(';', 1)[0].trim().toLowerCase()
    : ''
}

export function artifactWorkbenchPreviewKind(
  artifact: ArtifactPayload,
): ArtifactWorkbenchPreviewKind {
  const mime = artifactMime(artifact).split(';', 1)[0].trim()
  const extension = artifactExtension(artifactName(artifact))

  if (mime === 'text/html' || mime === 'application/xhtml+xml') return 'html'
  if (mime === 'application/pdf') return 'pdf'
  if (mime === 'text/markdown') return 'markdown'
  if (mime === 'text/plain' || mime === 'text/x-log') return 'text'
  if (mime.startsWith('image/')) return 'image'

  if (!mime || mime === 'application/octet-stream') {
    if (HTML_EXTENSIONS.has(extension)) return 'html'
    if (extension === 'pdf') return 'pdf'
    if (MARKDOWN_EXTENSIONS.has(extension)) return 'markdown'
    if (TEXT_EXTENSIONS.has(extension)) return 'text'
    if (IMAGE_EXTENSIONS.has(extension)) return 'image'
  }
  return 'unsupported'
}

export function artifactUsesWorkbenchPreview(
  artifact: ArtifactPayload,
): boolean {
  const kind = artifactWorkbenchPreviewKind(artifact)
  return kind !== 'unsupported' && kind !== 'image'
}

export function artifactPreviewLimit(kind: ArtifactWorkbenchPreviewKind): number {
  return kind === 'html' || kind === 'markdown' || kind === 'text'
    ? ARTIFACT_TEXT_PREVIEW_LIMIT
    : ARTIFACT_BINARY_PREVIEW_LIMIT
}

export function responseMatchesArtifactPreviewKind(
  kind: ArtifactWorkbenchPreviewKind,
  responseMime: string,
): boolean {
  const mime = normalizedMime(responseMime)
  if (!mime || mime === 'application/octet-stream') return true
  if (kind === 'html') return mime === 'text/html' || mime === 'application/xhtml+xml'
  if (kind === 'pdf') return mime === 'application/pdf'
  if (kind === 'markdown') return mime === 'text/markdown' || mime === 'text/plain'
  if (kind === 'text') return mime === 'text/plain' || mime === 'text/x-log'
  if (kind === 'image') return mime.startsWith('image/')
  return false
}

export function renderArtifactMarkdown(markdown: string): string {
  const raw = marked.parse(markdown, {
    async: false,
    breaks: true,
    gfm: true,
  }) as string
  if (typeof window === 'undefined') return ''
  const purifier = createDOMPurify(window)
  const purified = purifier.sanitize(raw, {
    ALLOWED_TAGS: [
      'a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3',
      'h4', 'h5', 'h6', 'hr', 'li', 'ol', 'p', 'pre', 'strong', 'table',
      'tbody', 'td', 'th', 'thead', 'tr', 'ul',
    ],
    ALLOWED_ATTR: ['href', 'title'],
    ALLOWED_URI_REGEXP: /^(?:https?|mailto|#):/i,
  })
  // DOMPurify is the primary sanitizer. Some lightweight DOM test runtimes do
  // not fully implement the browser APIs it relies on, so validate its output
  // and apply the same allow-list with native DOM traversal as defense-in-depth.
  const candidate = /<(?:script|style|iframe|object|svg|form)\b|javascript:/i.test(purified)
    ? raw
    : purified
  return enforceArtifactMarkdownAllowList(candidate)
}

function enforceArtifactMarkdownAllowList(html: string): string {
  const template = document.createElement('template')
  template.innerHTML = html
  const elements = [...template.content.querySelectorAll('*')]
  for (const element of elements) {
    if (!MARKDOWN_ALLOWED_TAGS.has(element.tagName)) {
      if (MARKDOWN_DROP_WITH_CONTENT.has(element.tagName)) element.remove()
      else element.replaceWith(document.createTextNode(element.textContent || ''))
      continue
    }
    for (const attribute of [...element.attributes]) {
      const allowed = attribute.name === 'title'
        || (element.tagName === 'A' && attribute.name === 'href')
      if (!allowed) element.removeAttribute(attribute.name)
    }
    if (element.tagName === 'A') {
      const href = element.getAttribute('href') || ''
      if (!/^(?:https?|mailto|#):/i.test(href)) {
        element.removeAttribute('href')
      } else if (/^https?:/i.test(href)) {
        element.setAttribute('target', '_blank')
        element.setAttribute('rel', 'noopener noreferrer')
      }
    }
  }
  return template.innerHTML
}

const RESOURCE_ATTRIBUTE_PATTERN =
  /\b(action|href|poster|src|srcset)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/gi
const CSS_URL_PATTERN = /\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^'")\s]+))\s*\)/gi
const CSS_IMPORT_PATTERN =
  /@import\s+(?:url\(\s*)?(?:"([^"]*)"|'([^']*)'|([^'")\s;]+))/gi
const JS_MODULE_DECLARATION_SPAN_LIMIT = 32 * 1024

function isLocalResourceReference(value: string): boolean {
  const normalized = value.trim()
  if (!normalized || normalized.startsWith('#')) return false
  if (normalized.startsWith('//') || normalized.startsWith('/')) return true
  if (/^(?:data|blob|https?|mailto|tel|javascript):/i.test(normalized)) return false
  return true
}

function addRelativeResourceReference(
  value: string,
  destination: Set<string>,
): boolean {
  if (destination.size >= ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT) return true
  const normalized = value.trim()
  if (isLocalResourceReference(normalized)) destination.add(normalized)
  return destination.size >= ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT
}

function collectMatches(
  source: string,
  pattern: RegExp,
  destination: Set<string>,
) {
  pattern.lastIndex = 0
  for (const match of source.matchAll(pattern)) {
    const candidate = String(match[1] || match[2] || match[3] || '').trim()
    if (!candidate) continue
    if (addRelativeResourceReference(candidate, destination)) break
  }
}

function collectResourceAttributeMatches(
  source: string,
  destination: Set<string>,
) {
  RESOURCE_ATTRIBUTE_PATTERN.lastIndex = 0
  for (const match of source.matchAll(RESOURCE_ATTRIBUTE_PATTERN)) {
    const attribute = String(match[1] || '').toLowerCase()
    const candidate = String(match[2] || match[3] || match[4] || '').trim()
    if (!candidate) continue
    if (attribute === 'srcset') collectSrcsetResourceReferences(candidate, destination)
    else addRelativeResourceReference(candidate, destination)
    if (destination.size >= ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT) break
  }
}

function isHtmlSpace(code: number): boolean {
  return code === 9 || code === 10 || code === 12 || code === 13 || code === 32
}

function collectSrcsetResourceReferences(
  value: string,
  destination: Set<string>,
) {
  let index = 0
  while (
    index < value.length
    && destination.size < ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT
  ) {
    while (
      index < value.length
      && (isHtmlSpace(value.charCodeAt(index)) || value.charCodeAt(index) === 44)
    ) {
      index += 1
    }
    if (index >= value.length) break

    const urlStart = index
    while (index < value.length && !isHtmlSpace(value.charCodeAt(index))) {
      index += 1
    }
    let url = value.slice(urlStart, index)
    while (url.endsWith(',')) url = url.slice(0, -1)
    if (addRelativeResourceReference(url, destination)) return

    while (index < value.length) {
      while (index < value.length && isHtmlSpace(value.charCodeAt(index))) index += 1
      if (value.charCodeAt(index) === 44) {
        index += 1
        break
      }
      while (
        index < value.length
        && !isHtmlSpace(value.charCodeAt(index))
        && value.charCodeAt(index) !== 44
      ) {
        index += 1
      }
      if (value.charCodeAt(index) === 44) {
        index += 1
        break
      }
    }
  }
}

type JavaScriptTokenKind = 'identifier' | 'punctuation' | 'string'

interface JavaScriptToken {
  end: number
  kind: JavaScriptTokenKind
  start: number
  value: string
}

interface JavaScriptModuleScanState {
  kind: 'export' | 'import'
  phase: 'after-keyword' | 'await-specifier' | 'seek-from'
  start: number
}

function isJavaScriptIdentifierStart(code: number): boolean {
  return (code >= 65 && code <= 90)
    || (code >= 97 && code <= 122)
    || code === 36
    || code === 95
}

function isJavaScriptIdentifierPart(code: number): boolean {
  return isJavaScriptIdentifierStart(code) || (code >= 48 && code <= 57)
}

/**
 * Returns the next significant JavaScript-like token without backtracking.
 * The artifact is HTML rather than a standalone module, so this intentionally
 * recognizes only the ASCII syntax needed by literal resource references.
 */
function nextJavaScriptToken(
  source: string,
  offset: number,
  limit: number,
): JavaScriptToken | null {
  let index = offset
  while (index < limit) {
    const code = source.charCodeAt(index)
    if (code <= 32 || code === 160) {
      index += 1
      continue
    }

    if (code === 47 && source.charCodeAt(index + 1) === 47) {
      index += 2
      while (index < limit) {
        const next = source.charCodeAt(index)
        index += 1
        if (next === 10 || next === 13) break
      }
      continue
    }
    if (code === 47 && source.charCodeAt(index + 1) === 42) {
      const close = source.indexOf('*/', index + 2)
      index = close < 0 || close + 2 > limit ? limit : close + 2
      continue
    }

    if (code === 34 || code === 39 || code === 96) {
      const quote = code
      const start = index
      let hasInterpolation = false
      index += 1
      const valueStart = index
      while (index < limit) {
        const next = source.charCodeAt(index)
        if (next === 92) {
          index = Math.min(limit, index + 2)
          continue
        }
        if (quote === 96 && next === 36 && source.charCodeAt(index + 1) === 123) {
          hasInterpolation = true
        }
        if (next === quote) {
          const value = source.slice(valueStart, index)
          index += 1
          return {
            end: index,
            kind: hasInterpolation ? 'punctuation' : 'string',
            start,
            value: hasInterpolation ? '`' : value,
          }
        }
        index += 1
      }
      return {
        end: limit,
        kind: 'punctuation',
        start,
        value: String.fromCharCode(quote),
      }
    }

    if (isJavaScriptIdentifierStart(code)) {
      const start = index
      index += 1
      while (index < limit && isJavaScriptIdentifierPart(source.charCodeAt(index))) {
        index += 1
      }
      return {
        end: index,
        kind: 'identifier',
        start,
        value: source.slice(start, index),
      }
    }

    return {
      end: index + 1,
      kind: 'punctuation',
      start: index,
      value: source[index] || '',
    }
  }
  return null
}

function addJavaScriptResource(value: string, destination: Set<string>) {
  addRelativeResourceReference(value, destination)
}

function tokenIs(
  token: JavaScriptToken | null,
  kind: JavaScriptTokenKind,
  value: string,
): boolean {
  return token?.kind === kind && token.value === value
}

/**
 * Scans once from left to right. Module declarations are tracked as state
 * instead of running a lazy regex from every `import`/`export` occurrence.
 */
function collectJavaScriptResourceReferences(
  source: string,
  destination: Set<string>,
) {
  let offset = 0
  let previous: JavaScriptToken | null = null
  let beforePrevious: JavaScriptToken | null = null
  let thirdPrevious: JavaScriptToken | null = null
  let moduleState: JavaScriptModuleScanState | null = null

  while (
    offset < source.length
    && destination.size < ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT
  ) {
    const token = nextJavaScriptToken(source, offset, source.length)
    if (!token) break
    offset = token.end

    if (
      moduleState
      && token.start - moduleState.start > JS_MODULE_DECLARATION_SPAN_LIMIT
    ) {
      moduleState = null
    }

    if (moduleState) {
      if (moduleState.phase === 'after-keyword') {
        if (moduleState.kind === 'import' && token.kind === 'string') {
          addJavaScriptResource(token.value, destination)
          moduleState = null
        } else if (
          tokenIs(token, 'punctuation', '(')
          || tokenIs(token, 'punctuation', '.')
          || tokenIs(token, 'punctuation', ';')
        ) {
          moduleState = null
        } else if (
          moduleState.kind === 'export'
          && !tokenIs(token, 'punctuation', '{')
          && !tokenIs(token, 'punctuation', '*')
        ) {
          moduleState = null
        } else {
          moduleState.phase = 'seek-from'
        }
      } else if (moduleState.phase === 'seek-from') {
        if (tokenIs(token, 'punctuation', ';')) {
          moduleState = null
        } else if (tokenIs(token, 'identifier', 'from')) {
          moduleState.phase = 'await-specifier'
        }
      } else if (token.kind === 'string') {
        addJavaScriptResource(token.value, destination)
        moduleState = null
      } else {
        moduleState = null
      }
    }

    if (token.kind === 'string' && tokenIs(previous, 'punctuation', '(')) {
      if (
        tokenIs(beforePrevious, 'identifier', 'import')
        || tokenIs(beforePrevious, 'identifier', 'fetch')
      ) {
        addJavaScriptResource(token.value, destination)
      } else if (
        tokenIs(thirdPrevious, 'identifier', 'new')
        && beforePrevious?.kind === 'identifier'
        && (
          beforePrevious.value === 'Worker'
          || beforePrevious.value === 'SharedWorker'
          || beforePrevious.value === 'URL'
        )
      ) {
        addJavaScriptResource(token.value, destination)
      }
    }

    if (tokenIs(token, 'identifier', 'import')) {
      moduleState = {
        kind: 'import',
        phase: 'after-keyword',
        start: token.start,
      }
    } else if (tokenIs(token, 'identifier', 'export')) {
      moduleState = {
        kind: 'export',
        phase: 'after-keyword',
        start: token.start,
      }
    }

    thirdPrevious = beforePrevious
    beforePrevious = previous
    previous = token
  }
}

/**
 * Finds references a single-file artifact cannot resolve. The returned strings
 * are for diagnostics only and should not be persisted or included in exports.
 */
export function detectArtifactHtmlRelativeResources(source: string): string[] {
  const scannedSource = source.length > ARTIFACT_HTML_REFERENCE_SCAN_LIMIT
    ? source.slice(0, ARTIFACT_HTML_REFERENCE_SCAN_LIMIT)
    : source
  const resources = new Set<string>()
  collectResourceAttributeMatches(scannedSource, resources)
  if (resources.size < ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT) {
    collectMatches(scannedSource, CSS_URL_PATTERN, resources)
  }
  if (resources.size < ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT) {
    collectMatches(scannedSource, CSS_IMPORT_PATTERN, resources)
  }
  if (resources.size < ARTIFACT_HTML_RELATIVE_RESOURCE_LIMIT) {
    collectJavaScriptResourceReferences(scannedSource, resources)
  }
  return [...resources].sort()
}

export const ARTIFACT_HTML_OFFLINE_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline' blob: data:",
  "style-src 'unsafe-inline' blob: data:",
  'img-src blob: data:',
  'font-src blob: data:',
  'media-src blob: data:',
  "connect-src 'none'",
  "frame-src 'none'",
  "child-src 'none'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join('; ')

export const ARTIFACT_PREVIEW_ESCAPE_MESSAGE =
  'opensquilla:artifact-preview:escape'

function stripUnsafeNavigationMarkup(source: string): string {
  return source
    .replace(/<base\b[^>]*>/gi, '')
    .replace(
      /<meta\b(?=[^>]*\bhttp-equiv\s*=\s*(?:"\s*refresh\s*"|'\s*refresh\s*'|refresh\b))[^>]*>/gi,
      '',
    )
    .replace(/<!doctype\b[^>]*>/gi, '')
}

/**
 * Makes a self-contained HTML artifact safe to load in a sandboxed iframe.
 * The iframe must still omit `allow-same-origin`, forms, popups and navigation.
 */
export function buildOfflineArtifactHtml(source: string): string {
  const sanitizedNavigation = stripUnsafeNavigationMarkup(source)
  const csp = `<meta http-equiv="Content-Security-Policy" content="${ARTIFACT_HTML_OFFLINE_CSP}">`
  const referrer = '<meta name="referrer" content="no-referrer">'
  const keyboardBridge = `<script>addEventListener("keydown",function(event){if(event.key==="Escape")parent.postMessage(${JSON.stringify(ARTIFACT_PREVIEW_ESCAPE_MESSAGE)},"*")})</script>`
  // Keep the policy ahead of every byte of untrusted markup. Parsing first can
  // start image/frame fetches in some DOM implementations before the CSP node
  // is inserted. A top-level metadata prelude is placed in the implicit head by
  // the HTML parser and preserves the artifact's original html/body attributes.
  return `<!doctype html>${csp}${referrer}${keyboardBridge}${sanitizedNavigation}`
}
