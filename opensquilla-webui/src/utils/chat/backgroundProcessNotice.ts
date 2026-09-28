import type { ChatStreamTimelineItem } from '@/types/chat'

interface BackgroundToolReceipt {
  name?: unknown
  tool_name?: unknown
  result?: unknown
  is_error?: unknown
  isError?: unknown
}

const TOOL_NAMES = new Set(['exec_command', 'background_process', 'process'])
const NOTICE_SUFFIX = 'A running process was reported; no exit result was recorded in this turn.'
const DESCRIPTION = /^(exec_command|background_process|process) \(execution_id=([A-Za-z0-9_-]+)\)$/

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function runningReceipts(calls: readonly BackgroundToolReceipt[]): Set<string> {
  const receipts = new Set<string>()
  for (const call of calls) {
    const name = String(call.name || call.tool_name || '')
    if (!TOOL_NAMES.has(name) || call.is_error === true || call.isError === true) continue
    if (typeof call.result !== 'string') continue
    if (name === 'background_process') {
      const id = call.result.match(/^session_id=([A-Za-z0-9_-]+)(?:\r?\n|$)/)?.[1]
      if (id && /^status: running\r?$/m.test(call.result)) receipts.add(`${name}:${id}`)
      continue
    }
    let payload: Record<string, unknown> | undefined
    try { payload = record(JSON.parse(call.result)) } catch { continue }
    if (!payload || payload.exited === true || payload.is_error === true || payload.isError === true) continue
    const sessions = record(payload.session)
      ? [payload.session]
      : name === 'process' && payload.action === 'wait' && Array.isArray(payload.sessions)
        ? payload.sessions
        : []
    for (const value of sessions) {
      const session = record(value)
      if (!session || session.status !== 'running' || session.returncode != null) continue
      const id = record(payload.session) ? payload.execution_id || session.session_id : session.session_id
      if (typeof id === 'string' && /^[A-Za-z0-9_-]+$/.test(id)) receipts.add(`${name}:${id}`)
    }
  }
  return receipts
}

function insideFence(text: string): boolean {
  let fence: { marker: string, length: number } | undefined
  for (const line of text.split(/\r\n|\r|\n/)) {
    const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
    if (!match) continue
    const marker = match[1]![0]!
    if (!fence) fence = { marker, length: match[1]!.length }
    else if (marker === fence.marker && match[1]!.length >= fence.length && !match[2]!.trim()) {
      fence = undefined
    }
  }
  return fence !== undefined
}

/** Hide only the legacy runtime footer corroborated by this assistant turn's tools. */
export function stripBackgroundProcessNotice(
  text: string,
  calls: readonly BackgroundToolReceipt[],
): string {
  if (!text.includes(NOTICE_SUFFIX)) return text
  const match = /(^|(?:\r?\n|\r(?!\n)){2})Background process status: ([^\r\n]+)\. A running process was reported; no exit result was recorded in this turn\.[ \t\r\n]*$/.exec(text)
  if (!match || insideFence(text.slice(0, match.index))) return text
  const receipts = runningReceipts(calls)
  const descriptions = match[2]!.split(', ')
  if (!descriptions.every(description => {
    const parts = DESCRIPTION.exec(description)
    return parts !== null && receipts.has(`${parts[1]}:${parts[2]}`)
  })) return text
  return text.slice(0, match.index).trimEnd()
}

/** Preserve segment identities and canonical source strings, including fence context. */
export function stripBackgroundProcessNoticeSegments(
  values: readonly string[],
  calls: readonly BackgroundToolReceipt[],
): string[] {
  const joined = values.join('\n\n')
  const projected = stripBackgroundProcessNotice(joined, calls)
  if (projected === joined) return [...values]
  let offset = 0
  return values.map(value => {
    const result = value.slice(0, Math.max(0, projected.length - offset))
    offset += value.length + 2
    return result
  })
}

export function stripBackgroundProcessNoticeTimeline(
  items: ChatStreamTimelineItem[],
  calls: readonly BackgroundToolReceipt[],
  renderMarkdown: (text: string) => string,
): ChatStreamTimelineItem[] {
  const texts = items.flatMap(item => item.type === 'text' ? [item.rawText || ''] : [])
  const projected = stripBackgroundProcessNoticeSegments(texts, calls)
  if (projected.every((value, index) => value === texts[index])) return items
  let index = 0
  return items.flatMap((item): ChatStreamTimelineItem[] => {
    if (item.type !== 'text') return [item]
    const rawText = projected[index++] || ''
    if (!rawText) return []
    return rawText === item.rawText ? [item] : [{ ...item, rawText, html: renderMarkdown(rawText) }]
  })
}
