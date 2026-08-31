import type { WorkbenchPanelDefinition } from '@/workbench/types'
import {
  workspaceFileFromWorkbenchItem,
} from '@/workbench/workspaceFileItems'
import WorkspaceFilePreviewPanel from './WorkspaceFilePreviewPanel.vue'

/**
 * Workspace file preview provider (kind 'file').
 *
 * No runtime is created: the panel component owns its own fetch/monaco
 * lifecycle, so the registry only needs the component + props mapping.
 */
export function createWorkspaceFileWorkbenchDefinition(): WorkbenchPanelDefinition {
  return {
    kind: 'file',
    component: WorkspaceFilePreviewPanel,
    supports: item => workspaceFileFromWorkbenchItem(item) !== null,
    getHeader: item => {
      const fileRef = workspaceFileFromWorkbenchItem(item)
      return {
        icon: 'fileText',
        title: item.title,
        subtitle: fileRef ? fileRef.path : undefined,
      }
    },
    getProps: item => {
      const fileRef = workspaceFileFromWorkbenchItem(item)
      if (!fileRef) return {}
      return {
        workspace: {
          id: fileRef.workspaceId,
          name: fileRef.workspaceName,
          path: fileRef.workspacePath,
        },
        path: fileRef.path,
        rootPath: fileRef.workspacePath,
        openNonce: fileRef.openNonce,
      }
    },
  }
}
