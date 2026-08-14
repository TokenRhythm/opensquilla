// Pure helpers for the ErrorBoundary fallback. Caught values are untrusted:
// proxies and custom Error subclasses can throw from property access,
// serialization, or string conversion. Keep every conversion fail-closed so
// the fallback itself cannot crash while trying to describe the first error.

const MAX_MESSAGE_CHARS = 500
const MAX_DETAILS_CHARS = 8000

function safeString(value: unknown): string {
  if (value === undefined) return ''
  if (typeof value === 'string') return value
  try {
    const json = JSON.stringify(value)
    if (typeof json === 'string') return json
  } catch {
    // Fall through to String() for circular and otherwise non-JSON values.
  }
  try {
    return String(value)
  } catch {
    return ''
  }
}

function safeProperty(value: object, key: 'message' | 'name' | 'stack'): string {
  try {
    return safeString(Reflect.get(value, key))
  } catch {
    return ''
  }
}

function isError(value: unknown): value is Error {
  try {
    return value instanceof Error
  } catch {
    return false
  }
}

/**
 * Remove high-risk local identifiers and credentials before details reach the
 * UI, clipboard, or renderer console. This intentionally targets common error
 * shapes rather than claiming to be a complete secret scanner.
 */
export function redactErrorBoundaryText(value: string): string {
  return value
    .replace(/\/(Users|home)\/[^/\s]+\//g, '/$1/[user]/')
    .replace(/([A-Za-z]:[\\/]Users[\\/])[^\\/\s]+([\\/])/g, '$1[user]$2')
    .replace(/([a-z][a-z0-9+.-]*:\/\/)[^/\s:@]+:[^@/\s]+@/gi, '$1[redacted]@')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]')
    .replace(
      /((?:--?|\/)\s*(?:api[_-]?key|token|password|secret|auth(?:orization)?))(?:\s+|=)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      '$1 [redacted]',
    )
    .replace(
      /\b(access[_-]?token|token|api[_-]?key|apikey|secret|password|authorization|auth)\b(\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&#]+)/gi,
      '$1$2[redacted]',
    )
    .replace(
      /\b(?:sk-[a-z0-9_-]{8,}|sk_(?:live|test|proj)_[a-z0-9_]{8,}|gh[pousr]_[a-z0-9_]{12,}|xox[baprs]-[a-z0-9-]{12,}|AKIA[A-Z0-9]{12,}|eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})\b/gi,
      '[redacted]',
    )
}

function truncate(value: string, maxChars: number): string {
  if (value.length <= maxChars) return value
  const marker = '\n…'
  return `${value.slice(0, maxChars - marker.length)}${marker}`
}

/** Short, sanitized, single-line message shown in the fallback heading area. */
export function errorBoundaryMessage(err: unknown): string {
  const raw = isError(err) ? safeProperty(err, 'message') : safeString(err)
  const singleLine = redactErrorBoundaryText(raw).replace(/[\r\n]+/g, ' ').trim()
  return truncate(singleLine, MAX_MESSAGE_CHARS).replace(/\n/g, '')
}

/**
 * Sanitized copyable detail block. When a native stack already starts with the
 * standard error heading, do not add that heading a second time.
 */
export function errorBoundaryDetails(err: unknown): string {
  let raw = ''
  if (isError(err)) {
    const name = safeProperty(err, 'name').trim() || 'Error'
    const message = safeProperty(err, 'message').trim()
    const heading = message ? `${name}: ${message}` : name
    const stack = safeProperty(err, 'stack').trim()
    const stackIncludesHeading = stack === heading || stack.startsWith(`${heading}\n`)
    raw = stack ? (stackIncludesHeading ? stack : `${heading}\n\n${stack}`) : heading
  } else {
    raw = safeString(err)
  }

  const sanitized = redactErrorBoundaryText(raw).trim()
  return truncate(sanitized, MAX_DETAILS_CHARS)
}
