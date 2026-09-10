import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { Attachment } from '@/types/chat'
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
    )).map(item => [item.annotationId, item]))
  } catch {
    return {}
  }
}

function snapshotOf(annotation: PromptAnnotation, sentOrder: number): PromptAnnotationSnapshot {
  return {
    annotationId: annotation.annotationId,
    documentId: annotation.documentId,
    documentName: annotation.documentName,
    body: annotation.body,
    tagName: annotation.tagName,
    quote: annotation.quote,
    targetRef: annotation.targetRef,
    resourceId: annotation.resourceId,
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
  const drafts = computed(() => Object.values(annotations.value).filter(item => item.status === 'draft'))

  function commit(next: Record<string, PromptAnnotation>) {
    const storage = draftStorage()
    if (!storage) throw new Error('Local annotation storage is unavailable.')
    storage.setItem(STORAGE_KEY, JSON.stringify(Object.values(next), (key, value) => (
      key === 'file' ? undefined : value
    )))
    annotations.value = next
  }
  function draftsForSession(sessionKey: string) {
    return drafts.value.filter(item => item.sessionKey === sessionKey)
  }
  function setActiveDocument(sessionKey: string, documentId: string) {
    activeDocumentBySession.value[sessionKey] = documentId
  }
  function activeDraftsForSession(sessionKey: string) {
    const items = draftsForSession(sessionKey)
    const documentId = activeDocumentBySession.value[sessionKey] || items[items.length - 1]?.documentId
    return items.filter(item => (
      item.documentId === documentId && !overlayOwnerSessions.value[item.annotationId]
    ))
  }
  function sendableDraftsForSession(sessionKey: string) {
    return activeDraftsForSession(sessionKey).filter(item => (
      item.body.trim() && promptAnnotationBodyWithinLimit(item.body)
    ))
  }
  function sendBlockedReason(sessionKey: string): 'editing' | 'empty' | 'too-long' | null {
    if (Object.values(overlayOwnerSessions.value).includes(sessionKey)) return 'editing'
    const items = activeDraftsForSession(sessionKey)
    if (items.some(item => !item.body.trim())) return 'empty'
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
  async function create(request: PromptAnnotationCreateRequest): Promise<PromptAnnotation> {
    if (!request.selection.targetRef) throw new Error('The selected page is unavailable.')
    if (!promptAnnotationBodyWithinLimit(request.body || '')) throw new Error('The annotation is too long.')
    if (draftsForSession(request.sessionKey).length >= PROMPT_ANNOTATION_MAX_COUNT) {
      throw new Error('The annotation limit has been reached.')
    }
    const item: PromptAnnotation = {
      annotationId: request.annotationId, sessionKey: request.sessionKey,
      documentId: request.documentId, documentName: request.documentName || 'artifact',
      resourceId: request.resourceId || request.selection.resourceId,
      targetRef: request.selection.targetRef, locatorHint: request.selection.locatorHint,
      body: request.body || '', status: 'draft', tagName: request.selection.tagName,
      quote: request.selection.selectionText || null,
      targetText: request.selection.selectionText || '',
      createdAt: Date.now(), updatedAt: Date.now(),
    }
    commit({ ...annotations.value, [item.annotationId]: item })
    setActiveDocument(item.sessionKey, item.documentId)
    return item
  }
  function setScreenshot(annotationId: string, screenshotAttachment: Attachment) {
    const current = annotations.value[annotationId]
    if (!current) return
    commit({ ...annotations.value, [annotationId]: { ...current, screenshotAttachment } })
  }
  function attachmentsForIds(ids: readonly string[]): Attachment[] {
    // A batch describes one page. Its latest capture accompanies all selected areas.
    const screenshot = [...ids].reverse()
      .map(id => annotations.value[id]?.screenshotAttachment)
      .find((item): item is Attachment => Boolean(item))
    return screenshot ? [screenshot] : []
  }
  async function update(annotationId: string, body: string) {
    if (!promptAnnotationBodyWithinLimit(body)) throw new Error('The annotation is too long.')
    const current = annotations.value[annotationId]
    if (!current) return null
    const updated = { ...current, body, updatedAt: Date.now() }
    commit({ ...annotations.value, [annotationId]: updated })
    return updated
  }
  async function discard(annotationId: string) {
    if (!annotations.value[annotationId]) return false
    const next = { ...annotations.value }
    delete next[annotationId]
    commit(next)
    releaseOverlayEdit(annotationId)
    return true
  }
  async function prepareForSend(ids: readonly string[]) {
    return ids.length > 0 && ids.length <= PROMPT_ANNOTATION_MAX_COUNT && ids.every(id => {
      const item = annotations.value[id]
      return item && !overlayOwnerSessions.value[id]
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
  function acknowledgeSent(snapshots: readonly PromptAnnotationSnapshot[]) {
    const next = { ...annotations.value }
    const removedIds: string[] = []
    for (const snapshot of snapshots) {
      const current = next[snapshot.annotationId]
      // An acknowledgement for an older message cannot erase a newer composer edit.
      if (current?.body === snapshot.body
        && current.targetRef === snapshot.targetRef
        && current.resourceId === snapshot.resourceId) {
        delete next[snapshot.annotationId]
        removedIds.push(snapshot.annotationId)
      }
    }
    commit(next)
    removedIds.forEach(releaseOverlayEdit)
    return removedIds
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
  }
  return { annotations, drafts, overlayOwnerSessions, activeDocumentBySession, draftsForSession,
    setActiveDocument, activeDraftsForSession, sendableDraftsForSession, sendBlockedReason,
    beginOverlayEdit, releaseOverlayEdit, completeOverlayEdit, load, create, update, discard,
    prepareForSend, snapshotsForIds, attachmentsForIds, setScreenshot, acknowledgeSent, clearSession, reset }
})
