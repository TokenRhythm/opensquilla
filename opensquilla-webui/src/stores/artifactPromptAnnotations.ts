import { defineStore } from 'pinia'
import { computed, markRaw, ref, shallowRef } from 'vue'

import type {
  PromptAnnotation,
  PromptAnnotationCreateRequest,
  PromptAnnotationSelection,
  PromptAnnotationSnapshot,
} from '@/types/promptAnnotations'
import {
  PROMPT_ANNOTATION_MAX_COUNT,
  promptAnnotationBodyWithinLimit,
} from '@/types/promptAnnotations'
import type { ArtifactPromptAnnotationProvider } from '@/workbench/artifactPromptAnnotationProvider'

export const PROMPT_ANNOTATION_CREATE_AMBIGUOUS = 'ARTIFACT_ANNOTATION_CREATE_AMBIGUOUS'
export const PROMPT_ANNOTATION_CREATE_CONFLICT = 'ARTIFACT_ANNOTATION_CREATE_CONFLICT'

class PromptAnnotationCreateReconciliationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly originalError: unknown,
  ) {
    super(message)
    this.name = 'PromptAnnotationCreateReconciliationError'
  }
}

function byCreationOrder(left: PromptAnnotation, right: PromptAnnotation): number {
  const leftTime = new Date(left.createdAt || 0).getTime() || 0
  const rightTime = new Date(right.createdAt || 0).getTime() || 0
  return leftTime - rightTime || left.annotationId.localeCompare(right.annotationId)
}

function snapshotOf(annotation: PromptAnnotation, order: number): PromptAnnotationSnapshot {
  return {
    annotationId: annotation.annotationId,
    documentId: annotation.documentId,
    documentName: annotation.documentName,
    revisionId: annotation.revisionId,
    generation: annotation.generation,
    anchorId: annotation.anchorId,
    body: annotation.body,
    tagName: annotation.tagName,
    locator: annotation.locator,
    quote: annotation.quote,
    sourceExcerpt: annotation.sourceExcerpt,
    ...(annotation.targetStatus ? { targetStatus: annotation.targetStatus } : {}),
    ...(annotation.targetReason ? { targetReason: annotation.targetReason } : {}),
    ...(annotation.targetKind ? { targetKind: annotation.targetKind } : {}),
    ...(annotation.targetText ? { targetText: annotation.targetText } : {}),
    sentOrder: order,
  }
}

export const useArtifactPromptAnnotationsStore = defineStore(
  'artifactPromptAnnotations',
  () => {
    const provider = shallowRef<ArtifactPromptAnnotationProvider | null>(null)
    const annotations = ref<Record<string, PromptAnnotation>>({})
    const loadedSessions = ref<Record<string, boolean>>({})
    const loadingSessions = ref<Record<string, boolean>>({})
    const sessionErrors = ref<Record<string, string | null>>({})
    const activeDocumentBySession = ref<Record<string, string>>({})
    // Drafts are persisted before the trusted editor opens so autosave and
    // crash recovery remain durable. While that editor owns a draft, however,
    // it is not yet an operator-confirmed instruction and must stay out of the
    // Composer and every chat.send batch.
    const overlayOwnerSessions = ref<Record<string, string>>({})
    const requests = new Map<string, AbortController>()
    const mutationTails = new Map<string, Promise<unknown>>()
    const sessionMutationVersions = new Map<string, number>()

    const drafts = computed(() => Object.values(annotations.value)
      .filter(item => item.status === 'draft')
      .sort(byCreationOrder))

    function setProvider(next: ArtifactPromptAnnotationProvider | null) {
      if (provider.value === next) return
      abortAll()
      provider.value = next ? markRaw(next) : null
    }

    function sessionMutationVersion(sessionKey: string): number {
      return sessionMutationVersions.get(sessionKey) || 0
    }

    function recordSessionMutation(sessionKey: string): void {
      if (!sessionKey) return
      sessionMutationVersions.set(sessionKey, sessionMutationVersion(sessionKey) + 1)
    }

    function draftsForSession(sessionKey: string): PromptAnnotation[] {
      return drafts.value.filter(item => item.sessionKey === sessionKey)
    }

    function activeDocumentId(sessionKey: string): string {
      const current = activeDocumentBySession.value[sessionKey]
      if (current && draftsForSession(sessionKey).some(item => item.documentId === current)) {
        return current
      }
      const sessionDrafts = draftsForSession(sessionKey)
      return sessionDrafts[sessionDrafts.length - 1]?.documentId || ''
    }

    function activeDraftsForSession(sessionKey: string): PromptAnnotation[] {
      const documentId = activeDocumentId(sessionKey)
      return documentId
        ? draftsForSession(sessionKey).filter(item => (
            item.documentId === documentId
            && overlayOwnerSessions.value[item.annotationId] === undefined
          ))
        : []
    }

    function beginOverlayEdit(annotationId: string, sessionKey: string): void {
      const id = String(annotationId || '').trim()
      const session = String(sessionKey || '').trim()
      if (!id || !session) return
      overlayOwnerSessions.value = {
        ...overlayOwnerSessions.value,
        [id]: session,
      }
    }

    function clearOverlayEdit(annotationId: string): void {
      const id = String(annotationId || '').trim()
      if (!id || overlayOwnerSessions.value[id] === undefined) return
      const next = { ...overlayOwnerSessions.value }
      delete next[id]
      overlayOwnerSessions.value = next
    }

    /** The trusted editor closed successfully after an explicit submit. */
    function completeOverlayEdit(annotationId: string): void {
      clearOverlayEdit(annotationId)
    }

    /** An abnormal surface release restores the durable draft as unfinished. */
    function releaseOverlayEdit(annotationId: string): void {
      clearOverlayEdit(annotationId)
    }

    function sendableDraftsForSession(sessionKey: string): PromptAnnotation[] {
      return activeDraftsForSession(sessionKey).filter(item => (
        item.body.trim().length > 0
        && promptAnnotationBodyWithinLimit(item.body)
      ))
    }

    function sendBlockedReason(
      sessionKey: string,
    ): 'editing' | 'empty' | 'too-long' | null {
      const editing = Object.entries(overlayOwnerSessions.value).some(
        ([annotationId, ownerSessionKey]) => (
          ownerSessionKey === sessionKey
          || annotations.value[annotationId]?.sessionKey === sessionKey
        ),
      )
      if (editing) return 'editing'
      const active = activeDraftsForSession(sessionKey)
      if (active.some(item => item.body.trim().length === 0)) return 'empty'
      if (active.some(item => !promptAnnotationBodyWithinLimit(item.body))) return 'too-long'
      return null
    }

    function setActiveDocument(sessionKey: string, documentId: string) {
      if (!sessionKey || !documentId) return
      activeDocumentBySession.value = {
        ...activeDocumentBySession.value,
        [sessionKey]: documentId,
      }
    }

    function replaceSession(sessionKey: string, items: PromptAnnotation[]) {
      const next: Record<string, PromptAnnotation> = {}
      for (const [id, item] of Object.entries(annotations.value)) {
        if (item.sessionKey !== sessionKey) next[id] = item
      }
      for (const item of items) {
        if (item.status === 'draft') next[item.annotationId] = item
      }
      annotations.value = next
      loadedSessions.value = { ...loadedSessions.value, [sessionKey]: true }
    }

    async function load(sessionKey: string, options: { force?: boolean } = {}) {
      if (!sessionKey || !provider.value) return draftsForSession(sessionKey)
      if (!options.force && loadedSessions.value[sessionKey]) return draftsForSession(sessionKey)
      requests.get(sessionKey)?.abort()
      const controller = new AbortController()
      requests.set(sessionKey, controller)
      const mutationVersionAtStart = sessionMutationVersion(sessionKey)
      loadingSessions.value = { ...loadingSessions.value, [sessionKey]: true }
      sessionErrors.value = { ...sessionErrors.value, [sessionKey]: null }
      try {
        const items = await provider.value.list(sessionKey, controller.signal)
        if (!controller.signal.aborted) {
          // A list started before create/update/discard/accept is stale with
          // respect to the visible composer. Never let its late response erase
          // or resurrect a draft the operator just changed. A later forced
          // refresh can reconcile from an authoritative post-mutation read.
          if (sessionMutationVersion(sessionKey) === mutationVersionAtStart) {
            replaceSession(sessionKey, items)
          } else {
            loadedSessions.value = { ...loadedSessions.value, [sessionKey]: true }
          }
        }
        return sessionMutationVersion(sessionKey) === mutationVersionAtStart
          ? items
          : draftsForSession(sessionKey)
      } catch (error) {
        if (!controller.signal.aborted) {
          sessionErrors.value = {
            ...sessionErrors.value,
            [sessionKey]: error instanceof Error ? error.message : String(error),
          }
        }
        return draftsForSession(sessionKey)
      } finally {
        if (requests.get(sessionKey) === controller) requests.delete(sessionKey)
        loadingSessions.value = { ...loadingSessions.value, [sessionKey]: false }
      }
    }

    async function create(request: PromptAnnotationCreateRequest) {
      if (!promptAnnotationBodyWithinLimit(request.body || '')) {
        throw new Error('The artifact annotation is too long.')
      }
      const current = draftsForSession(request.sessionKey)
        .filter(item => item.documentId === request.documentId)
      if (current.length >= PROMPT_ANNOTATION_MAX_COUNT) {
        throw new Error('The artifact annotation limit has been reached.')
      }
      const createProvider = provider.value
      if (!createProvider) throw new Error('Artifact annotation creation is unavailable.')
      let created: PromptAnnotation | null = null
      try {
        created = await createProvider.create(request)
      } catch (originalError) {
        let refreshWasAuthoritative = false
        const refresh = async (): Promise<PromptAnnotation | null> => {
          const items = await createProvider.list(request.sessionKey)
          refreshWasAuthoritative = true
          if (provider.value === createProvider) replaceSession(request.sessionKey, items)
          const recovered = items.find(item => item.annotationId === request.annotationId) || null
          if (!recovered) return null
          if (
            recovered.status !== 'draft'
            || recovered.sessionKey !== request.sessionKey
            || recovered.documentId !== request.documentId
            || recovered.revisionId !== request.revisionId
            || recovered.body !== (request.body || '')
          ) {
            throw new PromptAnnotationCreateReconciliationError(
              PROMPT_ANNOTATION_CREATE_CONFLICT,
              'The artifact annotation id is already in use.',
              originalError,
            )
          }
          return recovered
        }

        try {
          created = await refresh()
        } catch (refreshError) {
          if (refreshError instanceof PromptAnnotationCreateReconciliationError) {
            throw refreshError
          }
          // The first create may have committed while its response and this
          // refetch were lost. Replay the exact client-owned ID once. Gateway
          // resolves committed IDs before consulting the one-shot candidate.
          try {
            created = await createProvider.create(request)
          } catch {
            try {
              created = await refresh()
            } catch (finalRefreshError) {
              if (finalRefreshError instanceof PromptAnnotationCreateReconciliationError) {
                throw finalRefreshError
              }
              throw new PromptAnnotationCreateReconciliationError(
                PROMPT_ANNOTATION_CREATE_AMBIGUOUS,
                'Artifact annotation creation could not be reconciled.',
                originalError,
              )
            }
          }
        }
        if (!created && refreshWasAuthoritative) throw originalError
        if (!created) {
          throw new PromptAnnotationCreateReconciliationError(
            PROMPT_ANNOTATION_CREATE_AMBIGUOUS,
            'Artifact annotation creation could not be reconciled.',
            originalError,
          )
        }
      }
      if (!created) throw new Error('Artifact annotation creation is unavailable.')
      annotations.value = { ...annotations.value, [created.annotationId]: created }
      recordSessionMutation(created.sessionKey)
      setActiveDocument(created.sessionKey, created.documentId)
      return created
    }

    async function serializeMutation<T>(
      annotationId: string,
      mutation: () => Promise<T>,
    ): Promise<T> {
      const prior = mutationTails.get(annotationId) || Promise.resolve()
      const pending = prior.catch(() => undefined).then(mutation)
      mutationTails.set(annotationId, pending)
      try {
        return await pending
      } finally {
        if (mutationTails.get(annotationId) === pending) mutationTails.delete(annotationId)
      }
    }

    async function update(annotationId: string, body: string) {
      if (!promptAnnotationBodyWithinLimit(body)) {
        throw new Error('The artifact annotation is too long.')
      }
      return serializeMutation(annotationId, async () => {
        const current = annotations.value[annotationId]
        if (!current || current.status !== 'draft') return null
        const updated = await provider.value?.update({
          annotationId,
          sessionKey: current.sessionKey,
          body,
          expectedStateRevision: current.stateRevision,
        })
        if (!updated) throw new Error('Artifact annotation update is unavailable.')
        annotations.value = { ...annotations.value, [annotationId]: updated }
        recordSessionMutation(updated.sessionKey)
        return updated
      })
    }

    async function discard(annotationId: string) {
      return serializeMutation(annotationId, async () => {
        const current = annotations.value[annotationId]
        if (!current || current.status !== 'draft') return false
        const discarded = await provider.value?.discard({
          annotationId,
          sessionKey: current.sessionKey,
          expectedStateRevision: current.stateRevision,
        })
        if (!discarded) throw new Error('Artifact annotation discard is unavailable.')
        const next = { ...annotations.value }
        delete next[annotationId]
        annotations.value = next
        clearOverlayEdit(annotationId)
        recordSessionMutation(current.sessionKey)
        return true
      })
    }

    async function focus(annotationId: string) {
      const current = annotations.value[annotationId]
      if (!current || current.status !== 'draft') return null
      const focused = await provider.value?.focus({
        annotationId,
        sessionKey: current.sessionKey,
      })
      if (!focused || focused.documentId !== current.documentId) {
        throw new Error('Artifact annotation focus is unavailable.')
      }
      return focused
    }

    /** Wait for every queued autosave before snapshotting a chat.send batch. */
    async function prepareForSend(ids: readonly string[]): Promise<boolean> {
      const ordered = ids
        .map(id => String(id || '').trim())
        .filter((id, index, values) => Boolean(id) && values.indexOf(id) === index)
      if (ordered.length === 0 || ordered.length > PROMPT_ANNOTATION_MAX_COUNT) return false
      for (;;) {
        const pending = ordered
          .map(id => mutationTails.get(id))
          .filter((item): item is Promise<unknown> => Boolean(item))
        if (pending.length === 0) break
        const settled = await Promise.allSettled(pending)
        if (settled.some(item => item.status === 'rejected')) return false
        await Promise.resolve()
      }
      return ordered.every((id) => {
        const item = annotations.value[id]
        return Boolean(
          item
          && item.status === 'draft'
          && overlayOwnerSessions.value[id] === undefined
          && item.body.trim()
          && promptAnnotationBodyWithinLimit(item.body),
        )
      })
    }

    function snapshotsForIds(ids: readonly string[]): PromptAnnotationSnapshot[] {
      return ids.map(id => annotations.value[id])
        .filter((item): item is PromptAnnotation => Boolean(
          item && overlayOwnerSessions.value[item.annotationId] === undefined,
        ))
        .map(snapshotOf)
    }

    /** Clear only IDs explicitly proven accepted by chat.send. */
    function acknowledgeAccepted(
      requestedIds: readonly string[],
      acceptedIds: readonly string[],
    ) {
      const requested = new Set(requestedIds)
      const accepted = new Set(acceptedIds.filter(id => requested.has(id)))
      if (accepted.size === 0) return
      const next = { ...annotations.value }
      const changedSessions = new Set<string>()
      for (const id of accepted) {
        const current = next[id]
        if (current) changedSessions.add(current.sessionKey)
        delete next[id]
        clearOverlayEdit(id)
      }
      annotations.value = next
      for (const sessionKey of changedSessions) recordSessionMutation(sessionKey)
    }

    function clearSession(sessionKey: string) {
      requests.get(sessionKey)?.abort()
      requests.delete(sessionKey)
      const sessionAnnotationIds = new Set(
        draftsForSession(sessionKey).map(item => item.annotationId),
      )
      replaceSession(sessionKey, [])
      const loaded = { ...loadedSessions.value }
      delete loaded[sessionKey]
      loadedSessions.value = loaded
      const overlayOwners = { ...overlayOwnerSessions.value }
      for (const [annotationId, ownerSessionKey] of Object.entries(overlayOwners)) {
        if (ownerSessionKey === sessionKey || sessionAnnotationIds.has(annotationId)) {
          delete overlayOwners[annotationId]
        }
      }
      overlayOwnerSessions.value = overlayOwners
      sessionMutationVersions.delete(sessionKey)
    }

    function abortAll() {
      for (const request of requests.values()) request.abort()
      requests.clear()
      mutationTails.clear()
    }

    function reset() {
      abortAll()
      annotations.value = {}
      loadedSessions.value = {}
      loadingSessions.value = {}
      sessionErrors.value = {}
      activeDocumentBySession.value = {}
      overlayOwnerSessions.value = {}
      sessionMutationVersions.clear()
    }

    return {
      provider,
      annotations,
      drafts,
      loadedSessions,
      loadingSessions,
      sessionErrors,
      activeDocumentBySession,
      overlayOwnerSessions,
      setProvider,
      draftsForSession,
      activeDraftsForSession,
      sendableDraftsForSession,
      sendBlockedReason,
      setActiveDocument,
      beginOverlayEdit,
      completeOverlayEdit,
      releaseOverlayEdit,
      load,
      create,
      update,
      discard,
      focus,
      prepareForSend,
      snapshotsForIds,
      acknowledgeAccepted,
      clearSession,
      reset,
    }
  },
)

export type { PromptAnnotationSelection }
