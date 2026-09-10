import type { PromptAnnotationSnapshot } from './promptAnnotations'

export interface PageAnnotationInput {
  text: string
  selectionText?: string
  locatorHint?: string
}

/** Ordinary user input. Neither a page reference nor an annotation grants tool access. */
export interface ChatPageContext {
  targetRef?: string
  resourceId?: string
  annotations?: PageAnnotationInput[]
}

export function normalizePageContext(value: unknown): ChatPageContext | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const result: ChatPageContext = {}
  for (const key of ['targetRef', 'resourceId'] as const) {
    if (typeof raw[key] === 'string' && raw[key].trim()) result[key] = raw[key].trim()
  }
  if (Array.isArray(raw.annotations)) {
    result.annotations = raw.annotations.flatMap((value): PageAnnotationInput[] => {
      if (!value || typeof value !== 'object') return []
      const item = value as Record<string, unknown>
      if (typeof item.text !== 'string' || !item.text.trim()) return []
      return [{
        text: item.text,
        ...(typeof item.selectionText === 'string' ? { selectionText: item.selectionText } : {}),
        ...(typeof item.locatorHint === 'string' ? { locatorHint: item.locatorHint } : {}),
      }]
    }).slice(0, 16)
  }
  return Object.keys(result).length > 0 ? result : null
}

export function pageContextForAnnotations(
  snapshots: readonly PromptAnnotationSnapshot[],
): ChatPageContext | null {
  const first = snapshots[0]
  if (!first) return null
  if (snapshots.some(item => item.targetRef !== first.targetRef || item.resourceId !== first.resourceId)) {
    throw new Error('Send annotations for one page at a time.')
  }
  return {
    ...(first.targetRef ? { targetRef: first.targetRef } : {}),
    ...(first.resourceId ? { resourceId: first.resourceId } : {}),
    annotations: snapshots.map(item => ({
      text: item.body,
      ...(item.quote ? { selectionText: item.quote } : {}),
      ...(item.locatorHint ? { locatorHint: item.locatorHint } : {}),
    })),
  }
}

export function pageAnnotationSnapshots(value: unknown): PromptAnnotationSnapshot[] {
  const context = normalizePageContext(value)
  if (!context) return []
  const documentId = context.resourceId?.startsWith('document:')
    ? context.resourceId.slice('document:'.length) : context.resourceId || ''
  return (context.annotations || []).map((annotation, index) => ({
    annotationId: `page-annotation-${index}`,
    documentId,
    documentName: '',
    body: annotation.text,
    targetRef: context.targetRef,
    resourceId: context.resourceId,
    locatorHint: annotation.locatorHint,
    tagName: '',
    quote: annotation.selectionText || null,
    sentOrder: index,
  }))
}
