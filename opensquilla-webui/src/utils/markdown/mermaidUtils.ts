// Pure helpers for the mermaid fence renderer, ported from browsa's
// lib/sidepanel/mermaid-utils.js (which itself credits markstream-vue).
// Kept framework-free and mermaid-free so they unit-test without loading the
// multi-megabyte mermaid bundle.

const MERMAID_PREVIEW_MIN_HEIGHT = 60
const MERMAID_PREVIEW_MAX_HEIGHT = 500

export function getMermaidDiagramKind(code: string): string {
  for (const rawLine of code.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('%%')) continue
    const match = line.match(/^([A-Z][\w-]*)\b/i)
    return match?.[1]?.toLowerCase() || ''
  }
  return ''
}

// Reduces the layout jump between the raw code-fence placeholder and the real
// diagram once rendered: the temporary <pre> is held at roughly the expected
// diagram height. The estimate can overshoot, so it must never leak onto the
// final rendered wrapper (which sizes to its real SVG).
export function estimateMermaidPreviewHeight(code: string): number {
  const meaningfulLines = code
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line && !line.startsWith('%%'))
  const lineCount = Math.max(1, meaningfulLines.length)
  const kind = getMermaidDiagramKind(code)

  if (kind === 'gantt') return 220 + lineCount * 28
  if (kind === 'sequencediagram') return 180 + lineCount * 26
  if (kind === 'classdiagram' || kind === 'statediagram' || kind === 'erdiagram') return 180 + lineCount * 24
  if (kind === 'flowchart' || kind === 'graph') return 170 + lineCount * 28
  return 200 + lineCount * 22
}

export function clampMermaidPreviewHeight(
  height: number,
  minHeight: number = MERMAID_PREVIEW_MIN_HEIGHT,
  maxHeight: number | null = MERMAID_PREVIEW_MAX_HEIGHT,
): number {
  return maxHeight == null
    ? Math.max(minHeight, height)
    : Math.min(Math.max(minHeight, height), maxHeight)
}

// ─── Sequence-diagram semicolon escaping ────────────────────────────────────
// Works around a mermaid parser quirk: a bare `;` inside sequence-diagram
// message/Note text (e.g. dialogue quoting a SQL snippet "BEGIN; SELECT ...")
// breaks the parser, since mermaid treats `;` as a statement terminator. Only
// message/Note text is rewritten, and only on the retry path after a plain
// render already failed.

const SEMICOLON_ENTITY = '#59;'

function isEscapedEntityBefore(text: string, index: number): boolean {
  return /(?:&#\d+|#\d+|&[a-z]+)$/i.test(text.slice(Math.max(0, index - 12), index))
}

function hasSequenceArrow(text: string): boolean {
  return text.includes('->') || text.includes('-->') || text.includes('->>') || text.includes('-->>')
    || text.includes('-x') || text.includes('--x') || text.includes('-)') || text.includes('--)')
    || text.includes('-+') || text.includes('--+')
}

function startsSequenceMessage(text: string): boolean {
  const segment = text.split(';', 1)[0]
  const colonIndex = segment.indexOf(':')
  return colonIndex > 0 && hasSequenceArrow(segment.slice(0, colonIndex))
}

function startsSequenceStatement(text: string): boolean {
  const source = text.trimStart()
  return /^(?:accDescr|accTitle|activate|actor|and|alt|autonumber|box|break|critical|create\s+(?:actor|participant)|deactivate|destroy|else|end|link|links|loop|Note|opt|option|par|participant|properties|rect)\b/i.test(source)
    || startsSequenceMessage(source)
}

function isSequenceTextLine(line: string, colonIndex: number): boolean {
  const prefix = line.slice(0, colonIndex)
  return /^\s*Note\b/i.test(prefix) || hasSequenceArrow(prefix)
}

function escapeTextSemicolons(text: string): string {
  let escaped = ''
  let changed = false
  for (let index = 0; index < text.length; index++) {
    const char = text[index]
    if (char !== ';' || isEscapedEntityBefore(text, index)) {
      escaped += char
      continue
    }
    if (startsSequenceStatement(text.slice(index + 1))) {
      escaped += char
      continue
    }
    escaped += SEMICOLON_ENTITY
    changed = true
  }
  return changed ? escaped : text
}

function escapeLine(line: string): string {
  if (!line.includes(';')) return line
  const colonIndex = line.indexOf(':')
  if (colonIndex === -1 || !isSequenceTextLine(line, colonIndex)) return line
  const beforeText = line.slice(0, colonIndex + 1)
  const text = line.slice(colonIndex + 1)
  const escapedText = escapeTextSemicolons(text)
  return escapedText === text ? line : `${beforeText}${escapedText}`
}

export function escapeSequenceTextSemicolons(code: string): string {
  if (getMermaidDiagramKind(code) !== 'sequencediagram') return code
  const parts = code.split(/(\r\n|\n|\r)/)
  let changed = false
  for (let index = 0; index < parts.length; index += 2) {
    const line = parts[index]
    const escapedLine = escapeLine(line)
    if (escapedLine !== line) {
      parts[index] = escapedLine
      changed = true
    }
  }
  return changed ? parts.join('') : code
}

export interface MermaidRenderFn {
  render(id: string, source: string, container?: HTMLElement): Promise<{ svg: string }>
}

// Retries a mermaid render once with sequence-diagram semicolons escaped, if
// the first attempt failed for a reason the escaping could plausibly fix.
export async function renderMermaidWithRetry(
  render: MermaidRenderFn['render'],
  id: string,
  source: string,
  host?: HTMLElement,
): Promise<{ svg: string }> {
  try {
    return await render(id, source, host)
  } catch (error) {
    const escaped = escapeSequenceTextSemicolons(source)
    if (escaped === source) throw error
    return await render(id, escaped, host)
  }
}

// ─── Parse-error short message ──────────────────────────────────────────────
// Mermaid parse errors look like four lines:
//   Parse error on line 12:
//   <excerpt>
//   <caret ruler>
//   Expecting 'X', 'Y', ... got 'Z'
// The error card shows line + excerpt; the full dump stays under "view source".
export function formatMermaidParseError(message: string): { line: number, excerpt: string, expecting: string } | null {
  const m = String(message ?? '')
    .match(/Parse error on line (\d+):\s*\n([^\n]*)\n[^\n]*\^[^\n]*\n?\s*(Expecting[^\n]*)/)
  if (!m) return null
  return {
    line: Number(m[1]),
    excerpt: m[2].trim(),
    expecting: m[3].trim(),
  }
}
