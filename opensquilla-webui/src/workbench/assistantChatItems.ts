import type { WorkbenchItem } from './types'
import { assistantChatBindingKey } from '@/modules/assistantChat'

export function createAssistantChatWorkbenchItem(parentKey: string, workspaceId?: string, title = 'Assistant chat'): WorkbenchItem {
  return {
    id: `assistant-chat:${assistantChatBindingKey('', parentKey).split(':').pop()}`,
    kind: 'assistant-chat', title, scope: { type: 'session', id: parentKey },
    hostKind: 'dom', retention: 'keep-alive', payload: { parentSessionKey: parentKey, workspaceId },
  }
}
