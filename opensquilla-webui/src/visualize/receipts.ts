import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import type { VisualizeReceipt } from './types'

export function visualizeReceipts(calls: readonly ChatToolCall[]): VisualizeReceipt[] {
  const receipts = new Map<string, VisualizeReceipt>()
  for (const call of calls) {
    if (call.name !== 'open_workspace_preview' || call.isRunning || call.isError || call.status !== 'success' || !call.toolId) continue
    let value: Record<string, unknown>
    try { value = JSON.parse(call.result) } catch { continue }
    if (!value || value.previewStatus !== 'inline-ready' || value.presentation !== 'inline'
      || value.error || value.is_error || value.isError) continue
    const { documentId, revisionId, artifactId, name, mime, sha256, size, marker } = value
    if (typeof documentId !== 'string' || !/^doc_[\w-]+$/.test(documentId)
      || typeof revisionId !== 'string' || !/^rev_[\w-]+$/.test(revisionId)
      || typeof artifactId !== 'string' || typeof name !== 'string' || name.length > 250
      || mime !== 'text/html' || typeof sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(sha256)
      || typeof size !== 'number' || !Number.isSafeInteger(size) || size < 1 || size > 1024 * 1024
      || marker !== `visualize:${documentId}:${revisionId}`) continue
    receipts.set(marker, { callId: call.toolId, documentId, revisionId, artifactId, name, mime, sha256, size, marker })
  }
  return [...receipts.values()]
}

export function visualizeReceiptsFromMessage(message: ChatRenderedMessage): VisualizeReceipt[] {
  return visualizeReceipts([
    ...(message.timelineItems ?? []).flatMap(item => item.type === 'tool-group' ? item.group.calls : []),
    ...(message.toolCalls ?? []),
  ])
}

export type VisualizeBodySegment = { key: string; html: string; receipt?: never } | { key: string; receipt: VisualizeReceipt; html?: never }

/** Only an entire top-level inline-code paragraph can place an authorized canvas. */
export function splitVisualizeBody(html: string, receipts: readonly VisualizeReceipt[], fallback = true): VisualizeBodySegment[] {
  if (!receipts.length) return [{ key: 'text-0', html }]
  const doc = new DOMParser().parseFromString(html, 'text/html')
  const byMarker = new Map(receipts.map(receipt => [receipt.marker, receipt]))
  const used = new Set<string>()
  const result: VisualizeBodySegment[] = []
  let pending = ''
  const flush = () => { if (pending) { result.push({ key: `text-${result.length}`, html: pending }); pending = '' } }
  for (const node of doc.body.childNodes) {
    const element = node instanceof Element ? node : null
    const code = element?.tagName === 'P' && element.children.length === 1 && element.firstElementChild?.tagName === 'CODE'
      ? element.firstElementChild : null
    const receipt = code && element?.textContent?.trim() === code.textContent?.trim()
      ? byMarker.get(code.textContent?.trim() ?? '') : undefined
    if (receipt && !used.has(receipt.marker)) {
      flush(); used.add(receipt.marker); result.push({ key: receipt.marker, receipt })
    } else if (!receipt) pending += element?.outerHTML ?? (node.textContent ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  }
  flush()
  if (fallback) for (const receipt of receipts) if (!used.has(receipt.marker)) result.push({ key: receipt.marker, receipt })
  return result
}
