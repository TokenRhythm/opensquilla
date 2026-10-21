import type { WorkspaceFile, WorkspaceFiles } from '@/modules/workspaceFiles'
import type { WorkbenchItem, WorkbenchPanelDefinition } from '@/workbench/types'
import WorkspaceFilesPanel from './WorkspaceFilesPanel.vue'
import { workspaceFilesReadingState } from './workspaceReadingState'

export function createWorkspaceFilesWorkbenchDefinition(
  files: WorkspaceFiles | null,
  t: (key: string) => string,
  openDocument?: (file: WorkspaceFile, item: WorkbenchItem, isCurrent: () => boolean) => Promise<boolean>,
): WorkbenchPanelDefinition {
  return {
    kind: 'workspace-files',
    component: WorkspaceFilesPanel,
    supports: item => item.kind === 'workspace-files' && item.scope.type === 'session' && Boolean(files),
    getHeader: (_item: WorkbenchItem) => ({
      title: t('workbench.workspaceFiles.title'),
      subtitle: t('workbench.workspaceFiles.subtitle'),
      icon: 'folder',
    }),
    getProps: (item, state) => ({ ...state.runtimeState, runtimeReady: state.runtimeState.runtimeReady === true,
      ...(openDocument ? { openDocument: (file: WorkspaceFile, isCurrent: () => boolean) => openDocument(file, item, isCurrent) } : {}),
      sessionKey: item.scope.type === 'session' ? item.scope.id : '' }),
    createRuntime: (_item, context) => {
      context.updateRenderState({ runtimeReady: true, readingState: workspaceFilesReadingState(context.getViewState?.()?.reading), runRestore: context.runRestore })
      return {
        handleComponentEvent(event) {
          if (event.type !== 'workspace-files-reading-state') return
          const reading = workspaceFilesReadingState(event.payload)
          if (reading) context.saveViewState?.({ reading })
        },
      }
    },
  }
}
