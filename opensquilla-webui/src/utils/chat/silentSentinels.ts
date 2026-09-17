const SILENT_SENTINELS = new Set(['NO_REPLY', 'HEARTBEAT_OK'])

type AssistantBoundarySelection = {
  leading?: boolean
  trailing?: boolean
}

/**
 * Trusted turn provenance used only for the presentation compatibility shim.
 * Direct user turns intentionally omit (or carry non-internal) values so a
 * literal boundary example such as `NO_REPLY\nExplanation` stays visible.
 */
export interface AssistantPresentationProvenance {
  inputMode?: string | null
  runKind?: string | null
}

export function allowsMixedAssistantSentinelCleanup(
  provenance?: AssistantPresentationProvenance,
): boolean {
  const inputMode = String(provenance?.inputMode || '').trim().toLowerCase()
  const runKind = String(provenance?.runKind || '').trim().toLowerCase()
  return inputMode === 'system_event' || runKind === 'goal' || runKind === 'heartbeat'
}

function isSilentSentinelLine(line: string): boolean {
  // Four leading spaces, or a tab within the first Markdown indentation stop,
  // makes this an indented code line. Literal examples must never be treated
  // as legacy delivery control markers.
  if (/^(?: {4}| {0,3}\t)/.test(line)) return false
  return SILENT_SENTINELS.has(line.trim())
}

function firstNonBlankLine(lines: readonly string[]): number {
  return lines.findIndex(line => line.trim() !== '')
}

function lastNonBlankLine(lines: readonly string[]): number {
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    if (lines[index]!.trim() !== '') return index
  }
  return -1
}

/**
 * Whether `lineIndex` is inside a Markdown fenced-code block opened earlier in
 * the same text segment. A trailing `NO_REPLY` example inside a fence is user
 * content, not a legacy delivery marker.
 */
function isInsideFence(lines: readonly string[], lineIndex: number): boolean {
  let fence: { marker: '`' | '~', length: number } | null = null

  for (let index = 0; index < lineIndex; index += 1) {
    const match = lines[index]!.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
    if (!match) continue
    const run = match[1]!
    const marker = run[0] as '`' | '~'
    if (!fence) {
      fence = { marker, length: run.length }
      continue
    }
    if (
      marker === fence.marker
      && run.length >= fence.length
      && match[2]!.trim() === ''
    ) {
      fence = null
    }
  }

  return fence !== null
}

function sanitizeSelectedBoundaries(
  value: string,
  selection: AssistantBoundarySelection,
): string {
  const text = String(value || '')
  if (!text || (!text.includes('NO_REPLY') && !text.includes('HEARTBEAT_OK'))) return text

  const newline = text.match(/\r\n|\r|\n/)?.[0] ?? '\n'
  const lines = text.split(/\r\n|\r|\n/)
  let changed = false

  if (selection.leading !== false) {
    while (true) {
      const index = firstNonBlankLine(lines)
      if (index < 0 || !isSilentSentinelLine(lines[index]!)) break
      lines.splice(0, index + 1)
      changed = true
    }
    if (changed) {
      while (lines[0]?.trim() === '') lines.shift()
    }
  }

  if (selection.trailing !== false) {
    let removedTrailing = false
    while (true) {
      const index = lastNonBlankLine(lines)
      if (
        index < 0
        || !isSilentSentinelLine(lines[index]!)
        || isInsideFence(lines, index)
      ) break
      lines.splice(index)
      changed = true
      removedTrailing = true
    }
    if (removedTrailing) {
      while (lines[lines.length - 1]?.trim() === '') lines.pop()
    }
  }

  return changed ? lines.join(newline) : text
}

/**
 * Compatibility-only presentation projection for legacy gateways that could
 * persist a silent delivery sentinel alongside assistant text. Only standalone
 * sentinel lines at the outer assistant-text boundaries are removed. Raw
 * transcript/history objects must retain their canonical text.
 */
export function sanitizeAssistantPresentationText(
  value: string,
  provenance?: AssistantPresentationProvenance,
): string {
  const text = String(value || '')
  const projected = sanitizeSelectedBoundaries(text, { leading: true, trailing: true })
  if (projected === text) return text

  // Sentinel-only legacy rows are globally safe to hide. Mixed text requires
  // positive internal-turn provenance; otherwise the marker may be user-visible
  // content in an ordinary assistant answer.
  if (projected.trim() === '' || allowsMixedAssistantSentinelCleanup(provenance)) {
    return projected
  }
  return text
}

/**
 * Apply the same boundary rule across chronologically separated assistant text
 * segments without treating a middle segment as a message boundary. Returned
 * strings are copies; callers can project them without mutating history rows.
 */
export function sanitizeAssistantPresentationSegments(
  values: readonly string[],
  provenance?: AssistantPresentationProvenance,
): string[] {
  const originals = values.map(value => String(value || ''))
  const projected = [...originals]

  while (true) {
    const index = projected.findIndex(value => value.trim() !== '')
    if (index < 0) break
    const current = projected[index]!
    const next = sanitizeSelectedBoundaries(current, { leading: true, trailing: false })
    projected[index] = next
    if (next === current || next.trim() !== '') break
  }

  while (true) {
    const index = lastNonBlankLine(projected)
    if (index < 0) break
    const current = projected[index]!
    const next = sanitizeSelectedBoundaries(current, { leading: false, trailing: true })
    projected[index] = next
    if (next === current || next.trim() !== '') break
  }

  const changed = projected.some((value, index) => value !== originals[index])
  if (!changed) return projected
  if (
    projected.every(value => value.trim() === '')
    || allowsMixedAssistantSentinelCleanup(provenance)
  ) {
    return projected
  }
  return originals
}

export function isLegacySilentSentinelOnly(value: string): boolean {
  const text = String(value || '')
  return text.trim() !== '' && sanitizeAssistantPresentationText(text).trim() === ''
}
