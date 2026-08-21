import type { RpcCallOptions } from '@/lib/rpc'
import type {
  PromptAnnotation,
  PromptAnnotationCreateRequest,
  PromptAnnotationDiscardRequest,
  PromptAnnotationFreshness,
  PromptAnnotationFocusRequest,
  PromptAnnotationFocusResponse,
  PromptAnnotationFocusResult,
  PromptAnnotationResponse,
  PromptAnnotationSnapshot,
  PromptAnnotationsListResponse,
  PromptAnnotationStatus,
  PromptAnnotationUpdateRequest,
} from '@/types/promptAnnotations'

export const PROMPT_ANNOTATION_RPC_METHODS = {
  create: 'artifacts.prompt_annotations.create',
  list: 'artifacts.prompt_annotations.list',
  update: 'artifacts.prompt_annotations.update',
  discard: 'artifacts.prompt_annotations.discard',
  focus: 'artifacts.prompt_annotations.focus',
} as const

type PromptAnnotationRpc = {
  supportsMethod?: (method: string) => boolean
  markMethodUnavailable?: (method: string) => void
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<T>
}

export interface ArtifactPromptAnnotationProvider {
  list(sessionKey: string, signal?: AbortSignal): Promise<PromptAnnotation[]>
  create(request: PromptAnnotationCreateRequest): Promise<PromptAnnotation | null>
  update(request: PromptAnnotationUpdateRequest): Promise<PromptAnnotation | null>
  discard(request: PromptAnnotationDiscardRequest): Promise<PromptAnnotation | null>
  focus(request: PromptAnnotationFocusRequest): Promise<PromptAnnotationFocusResult | null>
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function valueAt(raw: Record<string, unknown>, ...keys: string[]): unknown {
  for (const key of keys) {
    if (raw[key] !== undefined) return raw[key]
  }
  return undefined
}

function stringAt(raw: Record<string, unknown>, ...keys: string[]): string {
  const value = valueAt(raw, ...keys)
  return typeof value === 'string' ? value : value == null ? '' : String(value)
}

function nullableStringAt(raw: Record<string, unknown>, ...keys: string[]): string | null {
  return stringAt(raw, ...keys).trim() || null
}

function numberAt(raw: Record<string, unknown>, fallback: number, ...keys: string[]): number {
  const value = Number(valueAt(raw, ...keys))
  return Number.isFinite(value) ? value : fallback
}

function timestampAt(raw: Record<string, unknown>, ...keys: string[]): number | string | null {
  const value = valueAt(raw, ...keys)
  return typeof value === 'number' || typeof value === 'string' ? value : null
}

function normalizedStatus(raw: Record<string, unknown>): PromptAnnotationStatus {
  const status = stringAt(raw, 'status').toLowerCase()
  return status === 'sent' || status === 'discarded' ? status : 'draft'
}

function normalizedFreshness(raw: Record<string, unknown>): PromptAnnotationFreshness {
  const value = stringAt(raw, 'freshness').toLowerCase()
  if (value === 'stale') return 'stale'
  if (value === 'fresh' || value === 'current') return 'fresh'
  return valueAt(raw, 'fresh', 'isFresh') === false || nullableStringAt(raw, 'staleReason', 'stale_reason')
    ? 'stale'
    : 'fresh'
}

function normalizedTargetStatus(raw: Record<string, unknown>): 'ready' | 'contextual' | undefined {
  const value = stringAt(raw, 'targetStatus', 'target_status').toLowerCase()
  return value === 'ready' || value === 'contextual' ? value : undefined
}

function normalizedTargetReason(raw: Record<string, unknown>): 'no_match' | 'ambiguous' | undefined {
  const value = stringAt(raw, 'targetReason', 'target_reason').toLowerCase()
  return value === 'no_match' || value === 'ambiguous' ? value : undefined
}

export function normalizePromptAnnotation(
  value: unknown,
  defaults: { sessionKey?: string } = {},
): PromptAnnotation | null {
  const raw = objectValue(value)
  if (!raw) return null
  const annotationId = stringAt(raw, 'annotationId', 'annotation_id', 'id').trim()
  const sessionKey = (stringAt(raw, 'sessionKey', 'session_key') || defaults.sessionKey || '').trim()
  const documentId = stringAt(raw, 'documentId', 'document_id').trim()
  const revisionId = stringAt(raw, 'revisionId', 'revision_id').trim()
  const anchorId = stringAt(raw, 'anchorId', 'anchor_id').trim()
  if (!annotationId || !sessionKey || !documentId || !revisionId || !anchorId) return null
  const anchor = objectValue(valueAt(raw, 'anchor'))
  const locator = objectValue(valueAt(raw, 'locator'))
    || (anchor ? objectValue(valueAt(anchor, 'locator')) : null)
    || {}
  const tagName = stringAt(raw, 'tagName', 'tag_name')
    || String(locator.tagName || locator.tag_name || '')
  return {
    annotationId,
    sessionKey,
    sessionId: nullableStringAt(raw, 'sessionId', 'session_id'),
    sessionEpoch: valueAt(raw, 'sessionEpoch', 'session_epoch') == null
      ? null
      : Math.max(0, numberAt(raw, 0, 'sessionEpoch', 'session_epoch')),
    documentId,
    documentName: stringAt(raw, 'documentName', 'document_name', 'name') || 'artifact',
    revisionId,
    generation: valueAt(raw, 'generation') == null
      ? null
      : Math.max(1, numberAt(raw, 1, 'generation')),
    anchorId,
    body: stringAt(raw, 'body'),
    status: normalizedStatus(raw),
    freshness: normalizedFreshness(raw),
    staleReason: nullableStringAt(raw, 'staleReason', 'stale_reason'),
    stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    tagName: tagName.toLowerCase(),
    ...(normalizedTargetStatus(raw) ? { targetStatus: normalizedTargetStatus(raw) } : {}),
    ...(normalizedTargetReason(raw) ? { targetReason: normalizedTargetReason(raw) } : {}),
    ...(stringAt(raw, 'targetKind', 'target_kind').trim()
      ? { targetKind: stringAt(raw, 'targetKind', 'target_kind').trim().toLowerCase() }
      : {}),
    ...(stringAt(raw, 'targetText', 'target_text').trim()
      ? { targetText: stringAt(raw, 'targetText', 'target_text').trim().slice(0, 160) }
      : {}),
    locator,
    quote: nullableStringAt(raw, 'quote')
      || (anchor ? nullableStringAt(anchor, 'quote') : null),
    sourceExcerpt: nullableStringAt(raw, 'sourceExcerpt', 'source_excerpt'),
    sentMessageId: nullableStringAt(raw, 'sentMessageId', 'sent_message_id'),
    sentTurnId: nullableStringAt(raw, 'sentTurnId', 'sent_turn_id'),
    sentOrder: valueAt(raw, 'sentOrder', 'sent_order') == null
      ? null
      : Math.max(0, numberAt(raw, 0, 'sentOrder', 'sent_order')),
    createdAt: timestampAt(raw, 'createdAt', 'created_at'),
    updatedAt: timestampAt(raw, 'updatedAt', 'updated_at'),
    schemaVersion: Math.max(1, numberAt(raw, 1, 'schemaVersion', 'schema_version')),
  }
}

export function normalizePromptAnnotationSnapshot(
  value: unknown,
  fallbackOrder = 0,
): PromptAnnotationSnapshot | null {
  const raw = objectValue(value)
  if (!raw) return null
  const document = objectValue(valueAt(raw, 'document'))
  const revision = objectValue(valueAt(raw, 'revision'))
  const anchor = objectValue(valueAt(raw, 'anchor'))
  const annotationId = stringAt(raw, 'annotationId', 'annotation_id', 'id').trim()
  const documentId = (stringAt(raw, 'documentId', 'document_id')
    || (document ? stringAt(document, 'id', 'documentId', 'document_id') : '')).trim()
  const revisionId = (stringAt(raw, 'revisionId', 'revision_id')
    || (revision ? stringAt(revision, 'id', 'revisionId', 'revision_id') : '')).trim()
  const anchorId = (stringAt(raw, 'anchorId', 'anchor_id')
    || (anchor ? stringAt(anchor, 'id', 'anchorId', 'anchor_id') : '')).trim()
  if (!annotationId || !documentId || !revisionId || !anchorId) return null
  const locator = objectValue(valueAt(raw, 'locator'))
    || (anchor ? objectValue(valueAt(anchor, 'locator')) : null)
    || {}
  return {
    annotationId,
    documentId,
    documentName: stringAt(raw, 'documentName', 'document_name', 'name')
      || (document ? stringAt(document, 'name') : '')
      || 'artifact',
    revisionId,
    generation: valueAt(raw, 'generation') == null
      && (!revision || valueAt(revision, 'generation') == null)
      ? null
      : Math.max(1, revision && valueAt(raw, 'generation') == null
        ? numberAt(revision, 1, 'generation')
        : numberAt(raw, 1, 'generation')),
    anchorId,
    body: stringAt(raw, 'body'),
    tagName: (stringAt(raw, 'tagName', 'tag_name')
      || (anchor ? stringAt(anchor, 'tagName', 'tag_name') : '')
      || String(locator.tagName || locator.tag_name || '')).toLowerCase(),
    ...(normalizedTargetStatus(raw) ? { targetStatus: normalizedTargetStatus(raw) } : {}),
    ...(normalizedTargetReason(raw) ? { targetReason: normalizedTargetReason(raw) } : {}),
    ...(stringAt(raw, 'targetKind', 'target_kind').trim()
      ? { targetKind: stringAt(raw, 'targetKind', 'target_kind').trim().toLowerCase() }
      : {}),
    ...(stringAt(raw, 'targetText', 'target_text').trim()
      ? { targetText: stringAt(raw, 'targetText', 'target_text').trim().slice(0, 160) }
      : {}),
    locator,
    quote: nullableStringAt(raw, 'quote')
      || (anchor ? nullableStringAt(anchor, 'quote') : null),
    sourceExcerpt: nullableStringAt(raw, 'sourceExcerpt', 'source_excerpt'),
    sentOrder: Math.max(0, numberAt(
      raw,
      fallbackOrder,
      'sentOrder',
      'sent_order',
      'order',
    )),
  }
}

function methodNotFound(error: unknown): boolean {
  const raw = objectValue(error)
  const message = error instanceof Error ? error.message : String(error)
  return raw?.code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

function signalOptions(signal?: AbortSignal): RpcCallOptions {
  return {
    timeoutMs: 10_000,
    timeoutAction: 'reject',
    abortAction: 'reject',
    ...(signal ? { signal } : {}),
  }
}

export function createRpcArtifactPromptAnnotationProvider(
  rpc: PromptAnnotationRpc,
): ArtifactPromptAnnotationProvider {
  async function call<T>(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T | null> {
    if (rpc.supportsMethod?.(method) === false) return null
    try {
      return await rpc.call<T>(method, params, signalOptions(signal))
    } catch (error) {
      if (!methodNotFound(error)) throw error
      rpc.markMethodUnavailable?.(method)
      return null
    }
  }

  return {
    async list(sessionKey, signal) {
      const response = await call<PromptAnnotationsListResponse>(
        PROMPT_ANNOTATION_RPC_METHODS.list,
        { sessionKey, status: 'draft' },
        signal,
      )
      return Array.isArray(response?.annotations)
        ? response.annotations
            .map(value => normalizePromptAnnotation(value, { sessionKey }))
            .filter((item): item is PromptAnnotation => item !== null)
        : []
    },
    async create(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.create, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        documentId: request.documentId,
        revisionId: request.revisionId,
        selection: {
          selectionId: request.selection.selectionId,
          tagName: request.selection.tagName,
          elementPath: request.selection.elementPath,
          elementProofSha256: request.selection.elementProofSha256,
          ...(request.selection.domSha256
            ? { domSha256: request.selection.domSha256 }
            : {}),
        },
        ...(request.body !== undefined ? { body: request.body } : {}),
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async update(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.update, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        body: request.body,
        expectedStateRevision: request.expectedStateRevision,
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async discard(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.discard, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        expectedStateRevision: request.expectedStateRevision,
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async focus(request) {
      const response = await call<PromptAnnotationFocusResponse>(
        PROMPT_ANNOTATION_RPC_METHODS.focus,
        {
          sessionKey: request.sessionKey,
          annotationId: request.annotationId,
        },
      )
      const annotationId = typeof response?.annotationId === 'string'
        ? response.annotationId.trim()
        : ''
      const documentId = typeof response?.documentId === 'string'
        ? response.documentId.trim()
        : ''
      return response?.focused === true
        && annotationId === request.annotationId
        && Boolean(documentId)
        ? { focused: true, annotationId, documentId }
        : null
    },
  }
}
