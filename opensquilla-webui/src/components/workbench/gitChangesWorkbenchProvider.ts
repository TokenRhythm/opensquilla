import type { GitAccess } from '@/modules/git'
import type { WorkbenchItem, WorkbenchPanelDefinition } from '@/workbench/types'
import GitChangesPanel from './GitChangesPanel.vue'

/**
 * The workspace a Git workbench tab reads. App.vue writes it into the item
 * payload when the branch chip opens the tab, because a panel renders outside
 * the chat route that owns the project selection.
 */
export function gitChangesWorkspaceId(item: WorkbenchItem): string | null {
  const value = item.payload.workspaceId
  return typeof value === 'string' && value ? value : null
}

export function createGitChangesWorkbenchDefinition(
  access: GitAccess | null,
  t: (key: string) => string,
): WorkbenchPanelDefinition {
  return {
    kind: 'git-changes',
    component: GitChangesPanel,
    // Without a Git owner the tab has nothing truthful to show, so the host
    // falls back to its empty state instead of an always-empty panel.
    supports: item => item.kind === 'git-changes' && Boolean(access),
    getHeader: () => ({ title: t('workbench.gitChanges.title'), subtitle: t('workbench.gitChanges.subtitle'), icon: 'fork' }),
    getProps: (item: WorkbenchItem, state) => ({
      workspaceId: gitChangesWorkspaceId(item),
      visible: state.visible ?? (state.active && state.hostAvailable),
    }),
  }
}
