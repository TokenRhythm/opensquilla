import type { WorkbenchPanelDefinition } from '@/workbench/types'
import AssistantChatPanel from './AssistantChatPanel.vue'

export function createAssistantChatWorkbenchDefinition(t: (key: string) => string): WorkbenchPanelDefinition {
  return {
    kind: 'assistant-chat', component: AssistantChatPanel,
    supports: item => item.kind === 'assistant-chat' && item.scope.type === 'session',
    getHeader: () => ({ title: t('workbench.newPanel.assistantChat'), icon: 'chat' }),
    getProps: item => ({ parentSessionKey: item.scope.type === 'session' ? item.scope.id : '', workspaceId: item.payload.workspaceId }),
  }
}
