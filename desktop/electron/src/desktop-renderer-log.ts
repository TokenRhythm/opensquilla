// Pure helpers for persisting renderer-side observability to desktop.log.
//
// The Control UI runs in the renderer process, so a purely front-end failure
// (a thrown error or an unhandled promise rejection) otherwise leaves no trace:
// it never reaches the gateway log, and
// DevTools is disabled on Windows. Forwarding bounded renderer errors and
// render-process state to desktop.log makes those problems diagnosable
// from a user's log folder without needing a reproduction.
//
// The decision + shaping logic lives here (not inline in main.ts) so it can be
// unit-tested without spinning up Electron.

/** Console severities Electron reports for `console-message`. */
export type RendererConsoleLevel = 'info' | 'warning' | 'error' | 'debug'

/** The subset of `console-message` params we care about. */
export interface RendererConsoleMessage {
  level: RendererConsoleLevel
  message: string
  sourceId: string
  lineNumber: number
}

/** A structured log entry ready to hand to `desktopLog(event, detail)`. */
export interface RendererLogEntry {
  event: string
  detail: Record<string, unknown>
}

// Only errors are persisted. Warnings are commonly used for routine application
// state and are too easy for a hot loop to emit; process hangs have their own
// structured breadcrumb.
const FORWARDED_LEVELS: ReadonlySet<RendererConsoleLevel> = new Set<RendererConsoleLevel>([
  'error',
])

const MAX_MESSAGE_BYTES = 2048
const MAX_SOURCE_BYTES = 512
const MAX_SCAN_CHARS = MAX_MESSAGE_BYTES * 4
const REDACTED = '[redacted]'

const SENSITIVE_KEY = [
  '(?:[a-z0-9]+[_-])*',
  '(?:',
  'api[_-]?key',
  '|access[_-]?key',
  '|private[_-]?key',
  '|signing[_-]?key',
  '|client[_-]?secret',
  '|token',
  '|password',
  '|passwd',
  '|secret',
  '|auth(?:orization)?',
  '|cookie',
  '|webhook',
  '|credential',
  ')',
].join('')

export interface RendererConsoleLogOptions {
  homeDir?: string
}

export interface RendererConsoleLogLimiterOptions {
  maxEntries?: number
  windowMs?: number
}

/** Whether a given console level should be persisted to desktop.log. */
export function shouldForwardConsoleLevel(level: RendererConsoleLevel): boolean {
  return FORWARDED_LEVELS.has(level)
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function redactHomePath(value: string, homeDir?: string): string {
  const trimmed = homeDir?.replace(/[\\/]+$/, '')
  if (!trimmed) return value
  const variants = new Set([trimmed, trimmed.replace(/\\/g, '/'), trimmed.replace(/\//g, '\\')])
  let redacted = value
  for (const variant of variants) {
    if (variant) redacted = redacted.replace(new RegExp(escapeRegExp(variant), 'gi'), '~')
  }
  return redacted
}

/** Desktop-boundary defense in depth for data that will persist on disk. */
export function redactRendererLogText(value: string, homeDir?: string): string {
  return redactHomePath(String(value || '').slice(0, MAX_SCAN_CHARS), homeDir)
    .replace(/([a-z][a-z0-9+.-]*:\/\/)([^/\s:@]+):([^@/\s]+)@/gi, `$1${REDACTED}@`)
    .replace(/(authorization[ \t]*:[ \t]*(?:bearer|basic)[ \t]+)[^\s"',;]+/gi, `$1${REDACTED}`)
    .replace(/\b((?:bearer|basic)[ \t]+)[a-z0-9._~+/=-]{8,}/gi, `$1${REDACTED}`)
    .replace(
      new RegExp(`((?:--?|\\/)${SENSITIVE_KEY})(?:[ \\t]+|=)(?:"[^"]*"|'[^']*'|[^\\s,;]+)`, 'gi'),
      `$1 ${REDACTED}`,
    )
    .replace(
      new RegExp(`("(?:${SENSITIVE_KEY})"\\s*:\\s*)"(?:[^"\\\\]|\\\\.)*"`, 'gi'),
      `$1"${REDACTED}"`,
    )
    .replace(
      new RegExp(`(^|[^a-z0-9])(${SENSITIVE_KEY}[ \\t]*[:=][ \\t]*)(?!(?:bearer|basic)\\b)(?:"[^"]*"|'[^']*'|[^\\s,;]+)`, 'gim'),
      `$1$2${REDACTED}`,
    )
    .replace(new RegExp(`([?&](?:${SENSITIVE_KEY})=)[^&#\\s]+`, 'gi'), `$1${REDACTED}`)
    .replace(
      /\b(?:sk-[a-z0-9_-]{8,}|sk_(?:live|test|proj)_[a-z0-9_]{8,}|gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]{12,}|xox[baprs]-[a-z0-9-]{12,}|AKIA[A-Z0-9]{12,}|AIza[a-z0-9_-]{12,}|eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})\b/gi,
      REDACTED,
    )
    // Unknown providers still tend to use long, unbroken credential-shaped
    // values. Prefer losing a hash-like diagnostic to persisting a secret.
    .replace(/[a-z0-9+/=_-]{32,}/gi, REDACTED)
}

function truncateUtf8(value: string, maxBytes: number): string {
  if (Buffer.byteLength(value, 'utf8') <= maxBytes) return value
  const suffix = '… [truncated]'
  const available = Math.max(0, maxBytes - Buffer.byteLength(suffix, 'utf8'))
  let head = ''
  let used = 0
  for (const character of value) {
    const size = Buffer.byteLength(character, 'utf8')
    if (used + size > available) break
    head += character
    used += size
  }
  return head.trimEnd() + suffix
}

function sanitizeSource(sourceId: string, homeDir?: string): string {
  const raw = String(sourceId || '')
  if (/^(?:data|javascript):/i.test(raw)) return '[inline]'
  let withoutCredentials = raw
  try {
    const parsed = new URL(raw)
    parsed.username = ''
    parsed.password = ''
    parsed.search = ''
    parsed.hash = ''
    withoutCredentials = parsed.protocol === 'file:'
      ? decodeURIComponent(parsed.pathname)
      : parsed.toString()
  } catch {
    withoutCredentials = raw.split(/[?#]/, 1)[0] || ''
  }
  const redacted = redactRendererLogText(withoutCredentials, homeDir).replace(/^\/~\//, '~/')
  return truncateUtf8(redacted, MAX_SOURCE_BYTES)
}

/**
 * Build a log entry for a renderer console message, or `null` when the level
 * should not be persisted. Returning the entry (rather than logging directly)
 * keeps this unit-testable and leaves the actual sink to the caller.
 */
export function buildRendererConsoleLogEntry(
  params: RendererConsoleMessage,
  options: RendererConsoleLogOptions = {},
): RendererLogEntry | null {
  if (!shouldForwardConsoleLevel(params.level)) return null
  return {
    event: 'renderer_console',
    detail: {
      level: params.level,
      message: truncateUtf8(
        redactRendererLogText(params.message, options.homeDir),
        MAX_MESSAGE_BYTES,
      ),
      source: sanitizeSource(params.sourceId, options.homeDir),
      line: Number.isFinite(params.lineNumber) && params.lineNumber >= 0
        ? Math.floor(params.lineNumber)
        : 0,
    },
  }
}

/** Build a low-volume breadcrumb for renderer responsiveness transitions. */
export function buildRendererStateLogEntry(
  state: 'unresponsive' | 'responsive',
  durationMs?: number,
): RendererLogEntry {
  return {
    event: `renderer_${state}`,
    detail: state === 'responsive' && durationMs !== undefined
      ? { duration_ms: Math.max(0, Math.floor(durationMs)) }
      : {},
  }
}

/** Bound synchronous main-process writes when renderer errors occur in a loop. */
export class RendererConsoleLogLimiter {
  private readonly maxEntries: number
  private readonly windowMs: number
  private windowStartedAt: number | null = null
  private emitted = 0
  private suppressed = 0

  constructor(options: RendererConsoleLogLimiterOptions = {}) {
    this.maxEntries = Math.max(1, options.maxEntries ?? 10)
    this.windowMs = Math.max(1, options.windowMs ?? 60_000)
  }

  accept(entry: RendererLogEntry, now = Date.now()): RendererLogEntry[] {
    const output: RendererLogEntry[] = []
    if (this.windowStartedAt === null || now - this.windowStartedAt >= this.windowMs) {
      output.push(...this.flush())
      this.windowStartedAt = now
      this.emitted = 0
    }
    if (this.emitted < this.maxEntries) {
      this.emitted += 1
      output.push(entry)
    } else {
      this.suppressed += 1
    }
    return output
  }

  flush(): RendererLogEntry[] {
    if (this.suppressed === 0) return []
    const count = this.suppressed
    this.suppressed = 0
    return [{
      event: 'renderer_console_suppressed',
      detail: { count, window_ms: this.windowMs },
    }]
  }
}

/** Build a log entry for a gone render process (crash / hang / oom). */
export function buildRendererGoneLogEntry(details: {
  reason: string
  exitCode: number
}): RendererLogEntry {
  return {
    event: 'renderer_process_gone',
    detail: {
      reason: details.reason,
      exitCode: details.exitCode,
    },
  }
}
