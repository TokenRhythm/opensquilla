import type { PromptAnnotationSnapshot } from './promptAnnotations'
import { normalizeOfficeSelection, type OfficeSelection } from './officeSelection'

export interface PageAnnotationInput {
  text: string
  selectionText?: string
  locatorHint?: string
  selection?: OfficeSelection
}

/** Ordinary user input. Neither a page reference nor an annotation grants tool access. */
export interface ChatPageContext {
  targetRef?: string
  resourceId?: string
  pagePath?: string
  revisionId?: string
  sha256?: string
  annotations?: PageAnnotationInput[]
}

export function normalizePageContext(value: unknown): ChatPageContext | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const result: ChatPageContext = {}
  for (const key of ['targetRef', 'resourceId', 'pagePath', 'revisionId', 'sha256'] as const) {
    if (typeof raw[key] === 'string' && raw[key].trim()) result[key] = raw[key].trim()
  }
  if (Array.isArray(raw.annotations)) {
    result.annotations = raw.annotations.flatMap((value): PageAnnotationInput[] => {
      if (!value || typeof value !== 'object') return []
      const item = value as Record<string, unknown>
      if (typeof item.text !== 'string' || !item.text.trim()) return []
      const selection = normalizeOfficeSelection(item.selection)
      return [{
        text: item.text,
        ...(typeof item.selectionText === 'string' ? { selectionText: item.selectionText } : {}),
        ...(typeof item.locatorHint === 'string' ? { locatorHint: item.locatorHint } : {}),
        ...(selection ? { selection } : {}),
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
  if (snapshots.some(item => item.targetRef !== first.targetRef
    || item.resourceId !== first.resourceId || item.pagePath !== first.pagePath
    || item.revisionId !== first.revisionId || item.sha256 !== first.sha256)) {
    throw new Error('Send annotations for one page at a time.')
  }
  return {
    ...(first.targetRef ? { targetRef: first.targetRef } : {}),
    ...(first.resourceId ? { resourceId: first.resourceId } : {}),
    ...(first.pagePath ? { pagePath: first.pagePath } : {}),
    ...(first.revisionId ? { revisionId: first.revisionId } : {}),
    ...(first.sha256 ? { sha256: first.sha256 } : {}),
    annotations: snapshots.map(item => ({
      text: item.body,
      ...(item.quote ? { selectionText: item.quote } : {}),
      ...(item.locatorHint ? { locatorHint: item.locatorHint } : {}),
      ...(item.selection ? { selection: normalizeOfficeSelection(item.selection) || undefined } : {}),
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
    revisionId: context.revisionId,
    sha256: context.sha256,
    selection: annotation.selection,
    pagePath: context.pagePath,
    locatorHint: annotation.locatorHint,
    tagName: annotation.selection?.kind || '',
    targetKind: annotation.selection?.kind === 'text' ? 'text' : undefined,
    targetText: annotation.selection?.text,
    quote: annotation.selectionText || null,
    sentOrder: index,
  }))
}
