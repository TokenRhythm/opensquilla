import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { normalizeOfficeSelection } from '@/types/officeSelection'
import type {
  PromptAnnotation,
  PromptAnnotationCreateRequest,
  PromptAnnotationSnapshot,
} from '@/types/promptAnnotations'
import {
  PROMPT_ANNOTATION_MAX_COUNT,
  promptAnnotationBodyWithinLimit,
} from '@/types/promptAnnotations'

const STORAGE_KEY = 'opensquilla.page-annotation-drafts.v1'

function draftStorage(): Storage | null {
  return typeof localStorage === 'undefined' ? null : localStorage
}

function readDrafts(): Record<string, PromptAnnotation> {
  try {
    const values: unknown = JSON.parse(draftStorage()?.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(values)) return {}
    return Object.fromEntries(values.filter((item): item is PromptAnnotation => Boolean(
      item && typeof item === 'object' && typeof item.annotationId === 'string'
      && typeof item.sessionKey === 'string' && typeof item.documentId === 'string'
      && typeof item.body === 'string' && promptAnnotationBodyWithinLimit(item.body)
      && item.status === 'draft',
    )).map(item => {
      // Older drafts can contain automatic captures whose staged uploads have expired.
      const { screenshotAttachment: _capture, ...annotation } = item as PromptAnnotation & {
        screenshotAttachment?: unknown
      }
      return [annotation.annotationId, {
        ...annotation,
        ...(!annotation.revisionId || !annotation.sha256 ? { freshness: 'stale' as const } : {}),
      }]
    }))
  } catch {
    return {}
  }
}

function snapshotOf(annotation: PromptAnnotation, sentOrder: number): PromptAnnotationSnapshot {
  return {
    annotationId: annotation.annotationId,
    documentId: annotation.documentId,
    documentName: annotation.documentName,
    revisionId: annotation.revisionId,
    sha256: annotation.sha256,
    // Recorded evidence is immutable send input. Renderer projection belongs
    // only at creation; repeating it here can silently truncate restored input.
    selection: annotation.selection === undefined ? undefined : JSON.parse(JSON.stringify(annotation.selection)),
    body: annotation.body,
    tagName: annotation.tagName,
    quote: annotation.quote,
    targetRef: annotation.targetRef,
    resourceId: annotation.resourceId,
    pagePath: annotation.pagePath,
    locatorHint: annotation.locatorHint,
    targetText: annotation.targetText,
    targetKind: annotation.targetKind,
    sentOrder,
  }
}

/** Local composer drafts use the normal chat acknowledgement, never a separate mutation RPC. */
export const useArtifactPromptAnnotationsStore = defineStore('artifactPromptAnnotations', () => {
  const annotations = ref<Record<string, PromptAnnotation>>(readDrafts())
  const overlayOwnerSessions = ref<Record<string, string>>({})
  const activeDocumentBySession = ref<Record<string, string>>({})
  const activePageBySession = ref<Record<string, string | undefined>>({})
  const queuedDraftIdsBySession = ref<Record<string, string[]>>({})
  function isQueued(annotationId: string) {
    return Object.values(queuedDraftIdsBySession.value).some(ids => ids.includes(annotationId))
  }
  function setQueuedDraftIds(sessionKey: string, ids: readonly string[]) {
    queuedDraftIdsBySession.value[sessionKey] = [...new Set(ids)]
  }
  const drafts = computed(() => Object.values(annotations.value).filter(item => item.status === 'draft'))

  function commit(next: Record<string, PromptAnnotation>) {
    const storage = draftStorage()
    if (!storage) throw new Error('Local annotation storage is unavailable.')
    storage.setItem(STORAGE_KEY, JSON.stringify(Object.values(next)))
    annotations.value = next
  }
  function draftsForSession(sessionKey: string) {
    return drafts.value.filter(item => item.sessionKey === sessionKey)
  }
  function setActiveDocument(sessionKey: string, documentId: string) {
    if (activeDocumentBySession.value[sessionKey] !== documentId) delete activePageBySession.value[sessionKey]
    activeDocumentBySession.value[sessionKey] = documentId
  }
  function setActivePage(sessionKey: string, pagePath?: string) {
    activePageBySession.value[sessionKey] = pagePath || ''
  }
  function validateDocumentVersion(sessionKey: string, documentId: string, revisionId: string, sha256: string) {
    const next = { ...annotations.value }
    let changed = false
    for (const draft of draftsForSession(sessionKey)) {
      if (draft.documentId !== documentId || isQueued(draft.annotationId)) continue
      const freshness = draft.revisionId === revisionId && draft.sha256 === sha256 ? 'fresh' : 'stale'
      if (draft.freshness !== freshness) { next[draft.annotationId] = { ...draft, freshness }; changed = true }
    }
    if (changed) commit(next)
  }
  function activeDraftsForSession(sessionKey: string, allowed?: (item: PromptAnnotation) => boolean) {
    const items = draftsForSession(sessionKey).filter(item => !allowed || allowed(item))
    const activeId = activeDocumentBySession.value[sessionKey]
    const documentId = (allowed && !items.some(item => item.documentId === activeId)
      ? undefined : activeId) || items[items.length - 1]?.documentId
    const documentItems = items.filter(item => (
      item.documentId === documentId && !overlayOwnerSessions.value[item.annotationId] && !isQueued(item.annotationId)
    ))
    const page = activePageBySession.value[sessionKey] ?? documentItems[documentItems.length - 1]?.pagePath ?? ''
    return documentItems.filter(item => item.selection || (item.pagePath || '') === page)
  }
  function sendableDraftsForSession(sessionKey: string, allowed?: (item: PromptAnnotation) => boolean) {
    return activeDraftsForSession(sessionKey, allowed).filter(item => (
      item.freshness !== 'stale' && item.body.trim() && promptAnnotationBodyWithinLimit(item.body)
    ))
  }
  function sendBlockedReason(sessionKey: string, allowed?: (item: PromptAnnotation) => boolean): 'editing' | 'too-long' | 'stale' | null {
    const documentId = activeDocumentBySession.value[sessionKey]
    const pagePath = activePageBySession.value[sessionKey]
    if (Object.entries(overlayOwnerSessions.value).some(([id, owner]) => (
      owner === sessionKey && (!allowed || !annotations.value[id] || allowed(annotations.value[id]))
      && (!documentId || !annotations.value[id] || annotations.value[id].documentId === documentId)
      && (pagePath === undefined || !annotations.value[id] || annotations.value[id].selection
        || (annotations.value[id].pagePath || '') === pagePath)
    ))) return 'editing'
    const items = activeDraftsForSession(sessionKey, allowed)
    if (items.some(item => item.freshness === 'stale' && item.body.trim())) return 'stale'
    return items.some(item => !promptAnnotationBodyWithinLimit(item.body)) ? 'too-long' : null
  }
  function beginOverlayEdit(annotationId: string, sessionKey: string) {
    overlayOwnerSessions.value[annotationId] = sessionKey
  }
  function releaseOverlayEdit(annotationId: string) {
    delete overlayOwnerSessions.value[annotationId]
  }
  function completeOverlayEdit(annotationId: string) {
    releaseOverlayEdit(annotationId)
  }
  async function load(sessionKey: string, _options: { force?: boolean } = {}) {
    return draftsForSession(sessionKey)
  }
  function buildDraft(request: PromptAnnotationCreateRequest): PromptAnnotation {
    if (!request.selection.targetRef && !request.resourceId && !request.selection.resourceId) {
      throw new Error('The selected page is unavailable.')
    }
    const selection = request.officeSelection ? normalizeOfficeSelection(request.officeSelection) : null
    if (request.officeSelection && (!selection || !request.revisionId || !/^[a-f0-9]{64}$/i.test(request.sha256 || ''))) {
      throw new Error('The selected document version is unavailable.')
    }
    if (!promptAnnotationBodyWithinLimit(request.body || '')) throw new Error('The annotation is too long.')
    const item: PromptAnnotation = {
      annotationId: request.annotationId, sessionKey: request.sessionKey,
      documentId: request.documentId, documentName: request.documentName || 'artifact',
      resourceId: request.resourceId || request.selection.resourceId,
      revisionId: request.revisionId,
      sha256: request.sha256,
      ...(selection ? { selection } : {}),
      pagePath: request.pagePath,
      targetRef: request.selection.targetRef, locatorHint: request.selection.locatorHint,
      body: request.body || '', status: 'draft', tagName: request.selection.tagName,
      quote: request.selection.selectionText || null,
      targetText: request.selection.selectionText || '',
      createdAt: Date.now(), updatedAt: Date.now(),
    }
    return item
  }
  async function create(request: PromptAnnotationCreateRequest): Promise<PromptAnnotation> {
    if (draftsForSession(request.sessionKey).filter(item => !isQueued(item.annotationId)
      && item.documentId === request.documentId
      && (request.officeSelection || (item.pagePath || '') === (request.pagePath || ''))).length >= PROMPT_ANNOTATION_MAX_COUNT) {
      throw new Error('The annotation limit has been reached.')
    }
    const item = buildDraft(request)
    commit({ ...annotations.value, [item.annotationId]: item })
    setActiveDocument(item.sessionKey, item.documentId)
    setActivePage(item.sessionKey, item.pagePath)
    return item
  }
  async function update(annotationId: string, body: string) {
    if (isQueued(annotationId)) throw new Error('Cancel the queued annotation before editing it.')
    if (!promptAnnotationBodyWithinLimit(body)) throw new Error('The annotation is too long.')
    const current = annotations.value[annotationId]
    if (!current) return null
    const updated = { ...current, body, updatedAt: Date.now() }
    commit({ ...annotations.value, [annotationId]: updated })
    return updated
  }
  async function discard(annotationId: string) {
    if (isQueued(annotationId)) throw new Error('Cancel the queued annotation before editing it.')
    if (!annotations.value[annotationId]) return false
    const next = { ...annotations.value }
    delete next[annotationId]
    commit(next)
    releaseOverlayEdit(annotationId)
    return true
  }
  async function replaceSelection(annotationId: string, request: PromptAnnotationCreateRequest) {
    const current = annotations.value[annotationId]
    if (!current || isQueued(annotationId) || current.sessionKey !== request.sessionKey
      || current.documentId !== request.documentId || annotationId === request.annotationId) {
      throw new Error('The annotation is unavailable for reselection.')
    }
    const replacement = { ...buildDraft({ ...request, body: current.body }), createdAt: current.createdAt }
    const next = Object.fromEntries(Object.entries(annotations.value).map(([id, value]) =>
      id === annotationId ? [replacement.annotationId, replacement] : [id, value]))
    commit(next)
    releaseOverlayEdit(annotationId)
    return replacement
  }
  function restoreReplacement(original: PromptAnnotation, replacementId: string) {
    const replacement = annotations.value[replacementId]
    if (!replacement || annotations.value[original.annotationId] || isQueued(replacementId)
      || replacement.sessionKey !== original.sessionKey || replacement.documentId !== original.documentId
      || replacement.body !== original.body) return false
    const next = { ...annotations.value }
    delete next[replacementId]
    next[original.annotationId] = original
    commit(next)
    releaseOverlayEdit(replacementId)
    return true
  }
  function restoreSnapshots(sessionKey: string, snapshots: readonly PromptAnnotationSnapshot[], retiredIds: readonly string[] = []) {
    const next = { ...annotations.value }
    for (const id of retiredIds) {
      if (next[id]?.sessionKey === sessionKey) delete next[id]
    }
    const ids: string[] = []
    for (const snapshot of snapshots) {
      const id = crypto.randomUUID()
      next[id] = {
        ...snapshot, annotationId: id, sessionKey, status: 'draft',
        createdAt: Date.now(), updatedAt: Date.now(),
      }
      ids.push(id)
    }
    commit(next)
    retiredIds.forEach(releaseOverlayEdit)
    if (snapshots[0]) {
      setActiveDocument(sessionKey, snapshots[0].documentId)
      setActivePage(sessionKey, snapshots[0].pagePath)
    }
    return ids
  }
  async function prepareForSend(ids: readonly string[]) {
    return ids.length > 0 && ids.length <= PROMPT_ANNOTATION_MAX_COUNT && ids.every(id => {
      const item = annotations.value[id]
      return item && item.freshness !== 'stale' && !overlayOwnerSessions.value[id]
        && item.body.trim() && promptAnnotationBodyWithinLimit(item.body)
    })
  }
  function snapshotsForIds(ids: readonly string[]): PromptAnnotationSnapshot[] {
    return ids.map(id => annotations.value[id])
      .filter((item): item is PromptAnnotation => Boolean(
        item && !overlayOwnerSessions.value[item.annotationId],
      ))
      .map(snapshotOf)
  }
  function invalidateSnapshots(snapshots: readonly PromptAnnotationSnapshot[]) {
    const next = { ...annotations.value }
    for (const snapshot of snapshots) {
      const current = next[snapshot.annotationId]
      if (current && current.documentId === snapshot.documentId && current.revisionId === snapshot.revisionId
        && current.sha256 === snapshot.sha256) next[snapshot.annotationId] = { ...current, freshness: 'stale' }
    }
    commit(next)
  }
  function acknowledgeSent(snapshots: readonly PromptAnnotationSnapshot[]) {
    const next = { ...annotations.value }
    const removedIds: string[] = []
    for (const snapshot of snapshots) {
      const current = next[snapshot.annotationId]
      // An acknowledgement for an older message cannot erase a newer composer edit.
      if (current?.body === snapshot.body
        && current.targetRef === snapshot.targetRef
        && current.resourceId === snapshot.resourceId
        && current.revisionId === snapshot.revisionId
        && current.sha256 === snapshot.sha256
        && current.documentId === snapshot.documentId
        && current.locatorHint === snapshot.locatorHint
        && current.quote === snapshot.quote
        && current.targetText === snapshot.targetText
        && JSON.stringify(current.selection) === JSON.stringify(snapshot.selection)
        && current.pagePath === snapshot.pagePath) {
        delete next[snapshot.annotationId]
        removedIds.push(snapshot.annotationId)
      }
    }
    commit(next)
    removedIds.forEach(releaseOverlayEdit)
    return removedIds
  }
  function restoreUnsent(snapshots: readonly PromptAnnotationSnapshot[], sessionKey: string): boolean {
    if (!snapshots.length) return true
    if (snapshots.length > PROMPT_ANNOTATION_MAX_COUNT
      || snapshots.some(item => !item.body.trim() || !promptAnnotationBodyWithinLimit(item.body))) return false
    const ids = new Set(snapshots.map(item => item.annotationId))
    if (ids.size !== snapshots.length || draftsForSession(sessionKey).some(item => !ids.has(item.annotationId))) return false
    // A retired send can restore missing local copies, never replace a newer edit.
    if (snapshots.some((item, index) => {
      const current = annotations.value[item.annotationId]
      return overlayOwnerSessions.value[item.annotationId] || isQueued(item.annotationId) || (current && (
        current.sessionKey !== sessionKey
        || JSON.stringify(snapshotOf(current, index)) !== JSON.stringify(snapshotOf({
          ...item, sessionKey, status: 'draft', createdAt: null, updatedAt: null,
        }, index))
      ))
    })) return false
    const next = { ...annotations.value }
    for (const item of snapshots) {
      if (!next[item.annotationId]) next[item.annotationId] = {
        ...item, sessionKey, status: 'draft', createdAt: Date.now(), updatedAt: Date.now(),
      }
    }
    commit(next)
    setActiveDocument(sessionKey, snapshots[0]!.documentId)
    setActivePage(sessionKey, snapshots[0]!.pagePath)
    return true
  }
  function clearSession(sessionKey: string) {
    commit(Object.fromEntries(Object.entries(annotations.value).filter(([, item]) => (
      item.sessionKey !== sessionKey
    ))))
    for (const [id, owner] of Object.entries(overlayOwnerSessions.value)) {
      if (owner === sessionKey) releaseOverlayEdit(id)
    }
  }
  function reset() {
    annotations.value = readDrafts()
    overlayOwnerSessions.value = {}
    activeDocumentBySession.value = {}
    activePageBySession.value = {}
    queuedDraftIdsBySession.value = {}
  }
  return { annotations, drafts, overlayOwnerSessions, activeDocumentBySession, draftsForSession,
    setActiveDocument, validateDocumentVersion, invalidateSnapshots, activeDraftsForSession, sendableDraftsForSession, sendBlockedReason,
    beginOverlayEdit, releaseOverlayEdit, completeOverlayEdit, load, create, update, discard,
    prepareForSend, snapshotsForIds, acknowledgeSent, restoreUnsent, clearSession, reset,
    replaceSelection, restoreSnapshots, isQueued, setQueuedDraftIds, queuedDraftIdsBySession,
    setActivePage, activePageBySession, restoreReplacement }
})
