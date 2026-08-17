import { defineStore } from 'pinia'
import { shallowRef } from 'vue'

import type { ChatDocumentContext } from '@/types/rpc'

export interface ActiveWorkbenchDocumentContext extends ChatDocumentContext {
  activeItemId: string
  sessionKey: string
}

export interface WorkbenchDocumentContextPrepareRequest {
  activeItemId: string
  documentId: string
  isCurrent?: () => boolean
  sessionKey: string
}

type WorkbenchDocumentContextPreparer = (
  request: WorkbenchDocumentContextPrepareRequest,
) => Promise<ChatDocumentContext | false>

export interface WorkbenchDocumentContextController {
  clear(): void
  detach(): void
  setActive(context: ActiveWorkbenchDocumentContext | null): void
}

export async function flushActiveWorkbenchDocumentContext(options: {
  beforeClose: () => Promise<boolean>
  isCurrent: () => boolean
  readLatest: () => Promise<ChatDocumentContext | null>
}): Promise<ChatDocumentContext | false> {
  if (!options.isCurrent()) return false
  try {
    if (!await options.beforeClose() || !options.isCurrent()) return false
    const latest = await options.readLatest()
    if (!latest || !options.isCurrent()) return false
    return latest
  } catch {
    return false
  }
}

function sameActiveDocument(
  left: ActiveWorkbenchDocumentContext | null,
  right: ActiveWorkbenchDocumentContext | null,
): boolean {
  return left?.sessionKey === right?.sessionKey
    && left?.activeItemId === right?.activeItemId
    && left?.documentId === right?.documentId
    && left?.headRevisionId === right?.headRevisionId
}

export const useWorkbenchDocumentContextStore = defineStore(
  'workbenchDocumentContext',
  () => {
    const active = shallowRef<ActiveWorkbenchDocumentContext | null>(null)
    let controller: {
      prepare: WorkbenchDocumentContextPreparer
      token: symbol
    } | null = null

    function attachController(
      prepare: WorkbenchDocumentContextPreparer,
    ): WorkbenchDocumentContextController {
      const token = Symbol('workbench-document-context-controller')
      controller = { prepare, token }
      active.value = null

      const ownsBridge = () => controller?.token === token
      return {
        clear() {
          if (ownsBridge()) active.value = null
        },
        detach() {
          if (!ownsBridge()) return
          active.value = null
          controller = null
        },
        setActive(context) {
          if (!ownsBridge()) return
          const next = context ? { ...context } : null
          if (!sameActiveDocument(active.value, next)) active.value = next
        },
      }
    }

    function currentDocumentContext(sessionKey: string): ChatDocumentContext | null {
      const current = active.value
      if (!current || !sessionKey || current.sessionKey !== sessionKey) return null
      return {
        documentId: current.documentId,
        headRevisionId: current.headRevisionId,
      }
    }

    async function prepareDocumentContextForSend(
      sessionKey: string,
      options: { isCurrent?: () => boolean } = {},
    ): Promise<ChatDocumentContext | null | false> {
      const before = active.value
      if (!before || !sessionKey || before.sessionKey !== sessionKey) return null
      const attached = controller
      if (!attached || options.isCurrent?.() === false) return false

      const result = await attached.prepare({
        activeItemId: before.activeItemId,
        documentId: before.documentId,
        isCurrent: options.isCurrent,
        sessionKey,
      })
      if (result === false || options.isCurrent?.() === false) return false

      const after = active.value
      if (
        controller !== attached
        || !after
        || after.sessionKey !== before.sessionKey
        || after.activeItemId !== before.activeItemId
        || after.documentId !== before.documentId
        || result.documentId !== before.documentId
        || !result.headRevisionId
      ) return false

      active.value = { ...after, headRevisionId: result.headRevisionId }
      return { ...result }
    }

    function reset() {
      active.value = null
      controller = null
    }

    return {
      active,
      attachController,
      currentDocumentContext,
      prepareDocumentContextForSend,
      reset,
    }
  },
)
