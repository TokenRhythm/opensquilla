import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import type { VisualizeReceipt } from './types'

/**
 * `show_widget` is the model-facing spelling used by generative-frame. The
 * OpenSquilla adapter returns the same immutable Artifact receipt as
 * `open_workspace_preview`; accepting both names keeps the renderer
 * independent from the tool that produced the receipt.
 */
const VISUALIZE_TOOL_NAMES = new Set(['open_workspace_preview', 'show_widget', 'edit_widget'])

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

/** Parse and validate the common immutable Artifact contract. */
export function parseVisualizeReceipt(call: ChatToolCall): VisualizeReceipt | null {
  if (!VISUALIZE_TOOL_NAMES.has(call.name) || call.isRunning || call.isError || call.status !== 'success' || !call.toolId) return null
  let value: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(call.result)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    value = parsed as Record<string, unknown>
  } catch {
    return null
  }
  if (value.error || value.is_error || value.isError) return null

  const directWidget = call.name === 'show_widget' || call.name === 'edit_widget'
  // The workspace-preview contract requires an explicit inline publication.
  // A show_widget adapter already denotes that presentation in its tool name,
  // but still rejects an explicit non-inline/error status.
  if (directWidget) {
    if (value.previewStatus !== undefined && value.previewStatus !== 'inline-ready') return null
    if (value.presentation !== undefined && value.presentation !== 'inline') return null
  } else if (value.previewStatus !== 'inline-ready' || value.presentation !== 'inline') {
    return null
  }

  const documentId = text(value.documentId)
  const revisionId = text(value.revisionId)
  const marker = text(value.marker)
  const sha256 = text(value.sha256)
  if (!documentId || !/^doc_[\w-]+$/.test(documentId)
    || !revisionId || !/^rev_[\w-]+$/.test(revisionId)
    || !sha256 || !/^[a-f0-9]{64}$/.test(sha256)
    || marker !== `visualize:${documentId}:${revisionId}`) return null

  // These defaults make the adapter tolerant of a minimal show_widget
  // response while preserving the existing renderer's integrity check: byte
  // length remains mandatory and must be supplied by the Artifact layer.
  const artifactId = text(value.artifactId) ?? (directWidget ? documentId : undefined)
  const name = text(value.name) ?? (directWidget ? text(value.title) ?? text(value.widgetId) ?? 'widget.html' : undefined)
  const mime = value.mime === undefined && directWidget ? 'text/html' : value.mime
  const size = value.size ?? value.byteLength ?? value.contentLength
  if (!artifactId || !name || name.length > 250 || mime !== 'text/html'
    || typeof size !== 'number' || !Number.isSafeInteger(size) || size < 1 || size > 1024 * 1024) return null

  const widgetId = text(value.widgetId) ?? text(value.widget_id)
  const rawAction = text(value.action)
  const action = rawAction === 'edit' || call.name === 'edit_widget' ? 'edit' : 'show'
  const rawVersion = value.version ?? value.widgetVersion ?? value.widget_version
  const version = typeof rawVersion === 'number' && Number.isSafeInteger(rawVersion) && rawVersion > 0 ? rawVersion : undefined
  const rawVisibility = text(value.visibility)
  const visibility = rawVisibility === 'internal' || rawVisibility === 'user' ? rawVisibility : undefined
  return { callId: call.toolId, ...(widgetId ? { widgetId } : {}), ...((rawAction || call.name === 'edit_widget') ? { action } : {}), ...(version ? { version } : {}), ...(visibility ? { visibility } : {}), documentId, revisionId, artifactId, name, mime, sha256, size, marker }
}

export function visualizeReceipts(calls: readonly ChatToolCall[]): VisualizeReceipt[] {
  // A widget edit creates a new immutable revision but must replace the same
  // inline canvas. Keep only the latest receipt per stable widget/document.
  const receipts = new Map<string, VisualizeReceipt>()
  for (const call of calls) {
    const receipt = parseVisualizeReceipt(call)
    if (!receipt) continue
    // The document is the durable identity and remains stable across the
    // legacy no-widgetId receipts as well as the new widget registry field.
    const identity = `document:${receipt.documentId}`
    const previous = receipts.get(identity)
    if (!previous || (receipt.version ?? 0) >= (previous.version ?? 0) || receipt.action === 'edit') receipts.set(identity, receipt)
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
  const byDocument = new Map(receipts.map(receipt => [receipt.documentId, receipt]))
  const used = new Set<string>()
  const result: VisualizeBodySegment[] = []
  let pending = ''
  const flush = () => { if (pending) { result.push({ key: `text-${result.length}`, html: pending }); pending = '' } }
  for (const node of doc.body.childNodes) {
    const element = node instanceof Element ? node : null
    const code = element?.tagName === 'P' && element.children.length === 1 && element.firstElementChild?.tagName === 'CODE'
      ? element.firstElementChild : null
    const marker = code?.textContent?.trim() ?? ''
    // An edit may be streamed after the assistant text was committed with the
    // old revision marker. Resolve that marker by document so the latest
    // canvas remains at the original inline position instead of disappearing.
    const markerDocumentId = marker.match(/^visualize:([^:]+):/)?.[1]
    const receipt = code && element?.textContent?.trim() === marker
      ? byMarker.get(marker) ?? (markerDocumentId ? byDocument.get(markerDocumentId) : undefined) : undefined
    if (receipt && !used.has(receipt.marker)) {
      flush(); used.add(receipt.marker); result.push({ key: `visualize:${receipt.widgetId ?? receipt.documentId}`, receipt })
    } else if (!receipt) pending += element?.outerHTML ?? (node.textContent ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  }
  flush()
  // Explicit placement chooses the published versions that belong in the
  // answer. A model may publish, inspect, and replace a draft in one turn;
  // appending every unreferenced receipt would resurrect those older drafts.
  // Retain the legacy fallback only when no authorized marker was placed.
  if (fallback && used.size === 0) for (const receipt of receipts) result.push({ key: `visualize:${receipt.widgetId ?? receipt.documentId}`, receipt })
  return result
}
