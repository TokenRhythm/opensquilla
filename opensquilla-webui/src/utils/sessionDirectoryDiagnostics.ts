export interface StorageBusyDiagnostic {
  code: 'STORAGE_BUSY'
  operation?: string
  stage?: string
  resource?: string
  waited_ms?: number
  retry_after_ms?: number
}

// Inspect only own data properties: diagnostics must not execute error getters.
function ownValue(value: unknown, key: string): unknown {
  if (value === null || typeof value !== 'object') return undefined
  try {
    const descriptor = Object.getOwnPropertyDescriptor(value, key)
    return descriptor && 'value' in descriptor ? descriptor.value : undefined
  } catch {
    return undefined
  }
}

/** Bounded, redacted diagnostics for wire errors and domain-error wrappers. */
export function storageBusyDiagnostic(error: unknown): StorageBusyDiagnostic | null {
  const pending = [{ value: error, depth: 0 }]
  const seen = new Set<object>()
  for (let index = 0; index < pending.length && index < 16; index++) {
    const { value, depth } = pending[index]!
    if (value === null || typeof value !== 'object' || seen.has(value)) continue
    seen.add(value)
    if (ownValue(value, 'code') === 'STORAGE_BUSY') {
      const data = ownValue(value, 'data')
      const sources = [value, ownValue(value, 'details'), data, ownValue(data, 'details')]
      const diagnostic: StorageBusyDiagnostic = { code: 'STORAGE_BUSY' }
      for (const field of ['operation', 'stage', 'resource'] as const) {
        for (const source of sources) {
          const candidate = ownValue(source, field)
          // Server diagnostic identifiers only; omit paths, session keys,
          // messages, control characters and unbounded arbitrary strings.
          if (typeof candidate === 'string' && candidate.length <= 96
            && /^[a-z][a-z0-9_]*(?::(?:recovery|candidates|content))?$/.test(candidate)) {
            diagnostic[field] = candidate
            break
          }
        }
      }
      for (const field of ['waited_ms', 'retry_after_ms'] as const) {
        for (const source of sources) {
          const candidate = ownValue(source, field)
          if (typeof candidate === 'number' && Number.isSafeInteger(candidate) && candidate >= 0) {
            diagnostic[field] = candidate
            break
          }
        }
      }
      return diagnostic
    }
    if (depth < 4) {
      for (const key of ['cause', 'data', 'details']) {
        pending.push({ value: ownValue(value, key), depth: depth + 1 })
      }
    }
  }
  return null
}

export function sessionDirectoryErrorLogValue(error: unknown): unknown {
  const diagnostic = storageBusyDiagnostic(error)
  if (diagnostic) return diagnostic
  // Preserve existing non-busy logging, while hostile Error.message getters
  // must not interrupt the caller's normal failure/retry handling.
  try {
    if (!(error instanceof Error)) return error
    const message = Object.getOwnPropertyDescriptor(error, 'message')
    if (!message) return ''
    return 'value' in message ? message.value : 'Session directory request failed'
  } catch {
    return 'Session directory request failed'
  }
}
