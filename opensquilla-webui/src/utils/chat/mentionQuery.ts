export interface MentionQuery {
  /** Offset of the `@` that opens the token. */
  start: number
  /** Caret offset closing the token (exclusive). */
  end: number
  /** Text typed after `@`, without the sigil. */
  query: string
}

/** A mention never continues a word, so e-mail addresses and handles stay prose. */
const MENTION_BOUNDARY = /[\s()[\]{}"'`<>，。；：、！？「」『』（）【】《》]/
const MAX_MENTION_QUERY = 200

/**
 * Only a standalone `@` token ending at the caret opens the mention palette.
 * A caret inside an e-mail address, a handle, or a token that already contains
 * whitespace returns null so typing prose never reopens the palette.
 */
export function mentionQueryAt(text: string, caret = text.length): MentionQuery | null {
  if (typeof text !== 'string') return null
  const end = Math.max(0, Math.min(Number.isFinite(caret) ? Math.trunc(caret) : text.length, text.length))
  let start = -1
  for (let index = end - 1; index >= 0; index -= 1) {
    const char = text[index]!
    if (char === '@') {
      start = index
      break
    }
    if (/\s/.test(char)) return null
  }
  if (start < 0) return null
  if (start > 0 && !MENTION_BOUNDARY.test(text[start - 1]!)) return null
  const query = text.slice(start + 1, end)
  if (query.length > MAX_MENTION_QUERY) return null
  if (/[\u0000-\u001f\u007f]/.test(query)) return null
  return { start, end, query }
}

/** Split a typed query into the directory to list and the fragment to match. */
export function mentionTarget(raw: string): { directory: string; fragment: string } {
  const normalized = raw.replace(/\\/g, '/')
  const separator = normalized.lastIndexOf('/')
  if (separator < 0) return { directory: '', fragment: normalized }
  return {
    directory: normalized.slice(0, separator),
    fragment: normalized.slice(separator + 1),
  }
}

/** Rank a candidate name against the typed fragment: prefix beats substring. */
export function mentionMatchRank(name: string, fragment: string): number {
  const needle = fragment.trim().toLocaleLowerCase()
  if (!needle) return 0
  const value = name.toLocaleLowerCase()
  if (value === needle) return 0
  if (value.startsWith(needle)) return 1
  return value.includes(needle) ? 2 : -1
}

/** Replace a mention token with its resolved insertion text. */
export function replaceMentionQuery(
  text: string,
  range: MentionQuery,
  replacement: string,
): { text: string; caret: number } {
  const next = text.slice(0, range.start) + replacement + text.slice(range.end)
  return { text: next, caret: range.start + replacement.length }
}
