import type { WorkspaceFiles } from '@/modules/workspaceFiles'
import type { WorkbenchItem, WorkbenchPanelDefinition } from '@/workbench/types'
import WorkspaceFilesPanel from './WorkspaceFilesPanel.vue'

export function createWorkspaceFilesWorkbenchDefinition(
  files: WorkspaceFiles | null,
  t: (key: string) => string,
): WorkbenchPanelDefinition {
  return {
    kind: 'workspace-files',
    component: WorkspaceFilesPanel,
    supports: item => item.kind === 'workspace-files' && item.scope.type === 'session' && Boolean(files),
    getHeader: (_item: WorkbenchItem) => ({
      title: t('workspaceFiles.title'),
      subtitle: t('workspaceFiles.subtitle'),
      icon: 'folder',
    }),
    getProps: item => ({ sessionKey: item.scope.type === 'session' ? item.scope.id : '' }),
  }
}
