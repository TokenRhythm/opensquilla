// Pure helpers for persisting renderer-side observability to desktop.log.
//
// The Control UI runs in the renderer process, so a purely front-end failure
// (a thrown error, an unhandled promise rejection, or a stuck UI that only logs
// a warning) otherwise leaves no trace: it never reaches the gateway log, and
// DevTools is disabled on Windows. Forwarding renderer console errors/warnings
// and render-process crashes to desktop.log makes those problems diagnosable
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

// Only error/warning are persisted. info/debug are dropped so routine renderer
// chatter never floods the lifecycle log.
const FORWARDED_LEVELS: ReadonlySet<RendererConsoleLevel> = new Set<RendererConsoleLevel>([
  'error',
  'warning',
])

// Guard against a runaway single message (e.g. a giant serialized object) bloating
// the log line. The head is the most useful part for diagnosis.
const MAX_MESSAGE_CHARS = 4000

/** Whether a given console level should be persisted to desktop.log. */
export function shouldForwardConsoleLevel(level: RendererConsoleLevel): boolean {
  return FORWARDED_LEVELS.has(level)
}

function truncateMessage(message: string): string {
  if (message.length <= MAX_MESSAGE_CHARS) return message
  return `${message.slice(0, MAX_MESSAGE_CHARS)}… [truncated ${message.length - MAX_MESSAGE_CHARS} chars]`
}

/**
 * Build a log entry for a renderer console message, or `null` when the level
 * should not be persisted. Returning the entry (rather than logging directly)
 * keeps this unit-testable and leaves the actual sink to the caller.
 */
export function buildRendererConsoleLogEntry(
  params: RendererConsoleMessage,
): RendererLogEntry | null {
  if (!shouldForwardConsoleLevel(params.level)) return null
  return {
    event: 'renderer_console',
    detail: {
      level: params.level,
      message: truncateMessage(params.message),
      source: params.sourceId,
      line: params.lineNumber,
    },
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
