import type { WorkbenchItem } from './types'
import { createClientRequestId } from '@/utils/chat/messageIdentity'

export function createAssistantChatWorkbenchItem(parentKey: string, workspaceId?: string, title = 'Assistant chat'): WorkbenchItem {
  const ownerId = createClientRequestId()
  return {
    id: `assistant-chat:${ownerId}`,
    kind: 'assistant-chat', title, scope: { type: 'session', id: parentKey },
    hostKind: 'dom', retention: 'keep-alive', payload: { parentSessionKey: parentKey, workspaceId, ownerId },
  }
}
