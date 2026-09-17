// Per-message timestamp formatting for the chat view.
//
// Chat message timestamps arrive either as UTC epoch-MILLISECONDS (history,
// from the backend's _now_ms) or as ISO-8601 strings carrying a 'Z' designator
// (live turns, via new Date().toISOString()). Both are absolute UTC instants,
// so `new Date(...)` renders them in the user's own browser timezone with no
// extra handling — there is no naive-local ambiguity to guard against.

function normalize(ts: string | number | null | undefined): number | string | null {
  if (ts == null || ts === '') return null
  // Defensive: a numeric value below 1e12 is epoch-SECONDS (some test fixtures
  // and seconds-based sources), not milliseconds. Promote it so we don't render
  // January 1970. Real ms-epoch values for any modern date are already > 1e12.
  if (typeof ts === 'number' && ts < 1e12) return ts * 1000
  return ts
}

export function messageDate(ts: string | number | null | undefined): Date | null {
  const value = normalize(ts)
  if (value == null) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

// Minimal translator shape shared by chat bubbles: resolves a named i18n key
// (e.g. "chat.time.minutesAgo") into a display string. vue-i18n's `t` satisfies
// it, and keeping this narrow structural type lets messageTime stay decoupled
// from the i18n runtime.
export type TimeTranslator = (key: string, named?: Record<string, string | number>) => string

// Compact English forms preserve existing non-UI callers and tests that don't
// carry a translator. Chat UI projections and bubbles inject vue-i18n's t, so
// their labels follow the active locale instead of being hardcoded.
const DEFAULT_TRANSLATOR: TimeTranslator = (key, named = {}) => {
  if (key === 'chat.time.justNow') return 'just now'
  if (key === 'chat.time.minutesAgo') return `${named.n}m ago`
  if (key === 'chat.time.hoursAgo') return `${named.n}h ago`
  return key
}

// Coarse relative label for messages less than one day old. Older messages keep
// their absolute timestamp without a redundant age suffix.
// Pass a ticking `now` (epoch ms) to keep the label live without per-component
// timers; it defaults to the current time for one-shot callers (e.g. export).
// An optional translator resolves the bucket labels through the active locale.
export function relativeTime(
  ts: string | number | null | undefined,
  now: number = Date.now(),
  t: TimeTranslator = DEFAULT_TRANSLATOR,
): string {
  const date = messageDate(ts)
  if (!date) return ''
  // Clamp future timestamps (client/server clock skew) to "just now" rather than
  // letting a negative diff fall through the buckets incidentally.
  const diff = Math.max(0, (now - date.getTime()) / 1000)
  if (diff < 60) return t('chat.time.justNow')
  if (diff < 3600) return t('chat.time.minutesAgo', { n: Math.floor(diff / 60) })
  if (diff < 86400) return t('chat.time.hoursAgo', { n: Math.floor(diff / 3600) })
  return ''
}

// Locale-aware coarse relative label for status readouts ("5 minutes ago",
// "5 分钟前"). Chat bubbles use the i18n-key `relativeTime` above; this Intl
// formatter is for readouts that pass a raw locale string.
export function localizedRelativeTime(
  ts: string | number | null | undefined,
  locale: string,
  now: number = Date.now(),
): string {
  const date = messageDate(ts)
  if (!date) return ''
  const diff = Math.max(0, (now - date.getTime()) / 1000)
  let formatter: Intl.RelativeTimeFormat
  try {
    formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'always' })
  } catch {
    formatter = new Intl.RelativeTimeFormat('en', { numeric: 'always' })
  }
  if (diff < 60) return formatter.format(-Math.max(1, Math.floor(diff)), 'second')
  if (diff < 3600) return formatter.format(-Math.floor(diff / 60), 'minute')
  if (diff < 86400) return formatter.format(-Math.floor(diff / 3600), 'hour')
  return formatter.format(-Math.floor(diff / 86400), 'day')
}

// Full local date and minute-level time. Keeping the year visible makes every
// message timestamp self-contained, regardless of how old the task is.
export function absoluteTime(ts: string | number | null | undefined): string {
  const date = messageDate(ts)
  if (!date) return ''
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const day = date.toLocaleDateString([], {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })
  return `${day}, ${time}`
}

// Machine-readable ISO instant for the <time datetime> attribute.
export function isoTime(ts: string | number | null | undefined): string {
  const date = messageDate(ts)
  return date ? date.toISOString() : ''
}

// Full, unabbreviated local date-time for the hover/title tooltip.
export function fullTime(ts: string | number | null | undefined): string {
  const date = messageDate(ts)
  return date ? date.toLocaleString() : ''
}
