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

// Coarse relative label: "just now" / "5m ago" / "2h ago" / "3d ago".
// Pass a ticking `now` (epoch ms) to keep the label live without per-component
// timers; it defaults to the current time for one-shot callers (e.g. export).
export function relativeTime(
  ts: string | number | null | undefined,
  now: number = Date.now(),
): string {
  const date = messageDate(ts)
  if (!date) return ''
  // Clamp future timestamps (client/server clock skew) to "just now" rather than
  // letting a negative diff fall through the buckets incidentally.
  const diff = Math.max(0, (now - date.getTime()) / 1000)
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// Locale-aware coarse relative label for status readouts ("5 minutes ago",
// "5 分钟前"). Chat rendering keeps the compact English `relativeTime` above.
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

// Compact absolute local time: time-only for today, "Mon DD, HH:MM" within the
// current year, and a full date otherwise. Always in the browser locale + tz.
export function absoluteTime(ts: string | number | null | undefined): string {
  const date = messageDate(ts)
  if (!date) return ''
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const startOfToday = new Date()
  startOfToday.setHours(0, 0, 0, 0)
  if (date.getTime() >= startOfToday.getTime()) return time
  const sameYear = date.getFullYear() === startOfToday.getFullYear()
  const day = date.toLocaleDateString(
    [],
    sameYear
      ? { month: 'short', day: 'numeric' }
      : { year: 'numeric', month: 'short', day: 'numeric' },
  )
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
