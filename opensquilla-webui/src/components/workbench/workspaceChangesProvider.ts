import type { WorkbenchPanelDefinition } from '@/workbench/types'
import { workspaceIdFromWorkbenchItem } from '@/workbench/workspaceChangesItems'
import WorkspaceChangesPanel from './WorkspaceChangesPanel.vue'

export interface WorkspaceChangesProviderOptions {
  t(key: string, params?: Record<string, unknown>): string
}

/**
 * Panel definition for the working-tree review surface.
 *
 * `kind: 'diff'` is the panel kind the Workbench registry reserves for this
 * provider (see `src/workbench/registry.ts`), so no host switch is added.
 */
export function createWorkspaceChangesWorkbenchDefinition(
  options: WorkspaceChangesProviderOptions,
): WorkbenchPanelDefinition {
  return {
    kind: 'diff',
    component: WorkspaceChangesPanel,
    supports: item => item.kind === 'diff' && Boolean(workspaceIdFromWorkbenchItem(item)),
    getHeader: item => ({
      icon: 'fileCode',
      title: item.title,
      subtitle: options.t('workbench.changes.subtitle'),
    }),
    getProps: item => ({
      workspaceId: workspaceIdFromWorkbenchItem(item),
      workspaceName: item.title,
    }),
  }
}
