import { onScopeDispose, watch, type Ref } from 'vue'
import type { ConversationToolContent } from '@/modules/conversationEventContent'
import type { WorkbenchResource, WorkbenchResourceOpenResponse, WorkbenchResourceRef } from '@/types/workbenchResources'
import { createWorkbenchResourceRef, workbenchResourceRefId } from '@/types/workbenchResources'
import { artifactProductClientError } from '@/utils/artifactProductErrors'
import { toolResultIsError } from '@/utils/chat/toolDisplay'
import { workspacePreviewFromToolCall } from '@/utils/chat/workspacePreviews'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'

type CurrentDocument = Extract<WorkbenchResourceOpenResponse, { disposition: 'document' }>

/** Opens the existing canonical Document surface; never falls back to a source path. */
export function useWorkspacePreviewOpening(options: {
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  enabled: Ref<boolean>
  resolve: (key: string, ref: WorkbenchResourceRef) => Promise<WorkbenchResource | null>
  openCurrent: (key: string, resource: WorkbenchResource) => Promise<WorkbenchResourceOpenResponse | null>
  show: (current: CurrentDocument, key: string, previewPagePath?: string) => void
  onError: (error: unknown) => void
}) {
  let lifetime = 0
  let disposed = false
  let openRequest = 0
  const openedCalls = new Set<string>()
  watch([options.sessionKey, options.currentEpoch], () => {
    lifetime += 1
    openedCalls.clear()
  }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; lifetime += 1 })

  async function open(documentId: string, expectedSessionKey = options.sessionKey.value, previewPagePath?: string) {
    const key = options.sessionKey.value
    const started = lifetime
    if (disposed || !key || expectedSessionKey !== key) return
    const request = ++openRequest
    const isCurrent = () => lifetime === started && options.sessionKey.value === key && request === openRequest
    const hasPage = (resource: WorkbenchResource) => previewPagePath === undefined
      || (isPreviewPagePath(previewPagePath) && resource.previewPages?.includes(previewPagePath))
    try {
      if (!options.enabled.value || !/^doc_[\w-]+$/.test(documentId)
        || (previewPagePath !== undefined && !isPreviewPagePath(previewPagePath))) {
        throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
      }
      const resource = await options.resolve(key, createWorkbenchResourceRef('document', documentId))
      if (!isCurrent()) return
      if (!resource || resource.resource.type !== 'document'
        || workbenchResourceRefId(resource.resource) !== documentId
        || !resource.capabilities.preview || !hasPage(resource) || !options.enabled.value) {
        throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
      }
      const current = await options.openCurrent(key, resource)
      if (!isCurrent()) return
      if (!options.enabled.value || current?.disposition !== 'document'
        || current.document.documentId !== documentId
        || current.resource.resource.type !== 'document'
        || workbenchResourceRefId(current.resource.resource) !== documentId
        || !current.resource.capabilities.preview || !hasPage(current.resource)) {
        throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
      }
      if (previewPagePath === undefined) options.show(current, key)
      else options.show(current, key, previewPagePath)
    } catch (error) {
      if (isCurrent()) options.onError(error)
    }
  }

  // Called only after the conversation handler accepts a fresh live result.
  function acceptLiveResult(payload: ConversationToolContent) {
    if (!options.enabled.value || (payload.key && payload.key !== options.sessionKey.value)) return
    const failed = toolResultIsError(payload) || Boolean(payload.is_error || payload.error)
    const link = workspacePreviewFromToolCall({
      toolId: typeof payload.id === 'string' ? payload.id : '',
      name: typeof payload.name === 'string' ? payload.name : '',
      isRunning: false,
      status: failed ? 'error' : 'success',
      isError: failed,
      result: typeof payload.result === 'string' ? payload.result : JSON.stringify(payload.result) || '',
    })
    if (!link) return
    const identity = JSON.stringify([payload.epoch, payload.task_id || payload.turn_id, link.callId])
    if (openedCalls.has(identity)) return
    openedCalls.add(identity)
    void open(link.documentId)
  }

  return { open, acceptLiveResult }
}
