import { getCurrentScope, onScopeDispose, watch } from 'vue'
import type { Attachment } from '@/types/chat'
import { copySelectedSkills, sameSelectedSkills, type SelectedSkillRef } from '@/types/selectedSkills'
import type { VisualizeDraftSnapshot } from '@/visualize/useVisualizeDraft'
import type { PromptAnnotationSnapshot } from '@/types/promptAnnotations'

export interface AutoSendDraftState {
  text: string
  composerText: string
  localPaths: readonly string[]
  attachments: readonly Attachment[]
  skills: readonly SelectedSkillRef[]
  annotationIds?: readonly string[]
  annotationDrafts?: readonly Pick<PromptAnnotationSnapshot,
    'annotationId' | 'documentId' | 'body' | 'targetRef' | 'resourceId' | 'pagePath'
    | 'revisionId' | 'sha256' | 'selection' | 'quote' | 'locatorHint'>[]
  visualization: VisualizeDraftSnapshot | null
  sessionKey: string
  authority: string | null
  projectKey: string
}

function capture(state: AutoSendDraftState) {
  return {
    ...state,
    localPaths: [...state.localPaths],
    skills: copySelectedSkills(state.skills),
    annotationIds: [...(state.annotationIds ?? [])],
    annotationContent: JSON.stringify((state.annotationDrafts ?? []).map(item => [
      item.annotationId, item.documentId, item.body, item.targetRef, item.resourceId,
      item.pagePath, item.revisionId, item.sha256, item.selection, item.quote, item.locatorHint,
    ])),
    attachments: state.attachments.map(attachment => ({
      // File is immutable and survives upload completion/token refresh. Those
      // transport changes do not change the material the operator selected.
      file: attachment.file,
      content: JSON.stringify([
        attachment.local_id, attachment.name, attachment.mime, attachment.size,
        attachment.origin, attachment.durable_material,
        attachment.workspaceFile ? [attachment.workspaceFile.workspaceId,
          attachment.workspaceFile.relativePath, attachment.workspaceFile.name,
          attachment.workspaceFile.mime, attachment.workspaceFile.size] : null,
        attachment.file ? null : [attachment.data, attachment.dataUrl],
        attachment.file ? null : attachment.file_uuid,
      ]),
    })),
  }
}

function sameDraft(left: ReturnType<typeof capture>, right: ReturnType<typeof capture>): boolean {
  return left.text === right.text && left.composerText === right.composerText
    && left.sessionKey === right.sessionKey && left.authority === right.authority
    && left.projectKey === right.projectKey && left.visualization === right.visualization
    && left.localPaths.length === right.localPaths.length
    && left.localPaths.every((path, index) => path === right.localPaths[index])
    && sameSelectedSkills(left.skills, right.skills)
    && left.annotationIds.length === right.annotationIds.length
    && left.annotationIds.every((id, index) => id === right.annotationIds[index])
    && left.annotationContent === right.annotationContent
    && left.attachments.length === right.attachments.length
    && left.attachments.every((attachment, index) => (
      attachment.file === right.attachments[index]?.file
      && attachment.content === right.attachments[index]?.content
    ))
}

/** One handoff may survive equivalent restoration, never an edit then undo. */
export function useChatAutoSendDraftGuard(
  readDraft: () => AutoSendDraftState,
  options: { isAuthorityCurrent?: () => boolean } = {},
) {
  const initial = capture(readDraft())
  let changed = false
  let disposed = false
  function unchanged(current: AutoSendDraftState): boolean {
    const next = capture(current)
    if (initial.authority === null && next.authority !== null && options.isAuthorityCurrent?.()) {
      // The coordinator may bind its first identity after Hello admitted this
      // same draft/read. Adopt that proof once; later changes still cancel.
      initial.authority = next.authority
    }
    return (options.isAuthorityCurrent?.() ?? true) && sameDraft(initial, next)
  }
  const stop = watch(readDraft, current => {
    if (!unchanged(current)) changed = true
  }, { deep: true, flush: 'sync' })
  const dispose = () => {
    if (disposed) return
    disposed = true
    stop()
  }
  if (getCurrentScope()) onScopeDispose(dispose)
  return {
    isUnchanged: () => !disposed && !changed && unchanged(readDraft()),
    dispose,
  }
}
