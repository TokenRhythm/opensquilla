import type { WorkbenchItem } from './types'

export const WORKSPACE_CHANGES_OPEN_EVENT = 'opensquilla:open-workspace-changes'

export interface WorkspaceChangesOpenEventDetail {
  workspaceId: string
  workspaceName: string
}

/**
 * One panel per project workspace. The `diff` panel kind and the `workspace`
 * scope are reserved by the Workbench for exactly this consumer, so the item id
 * only has to stay stable across re-opens.
 */
export function workspaceChangesWorkbenchItemId(workspaceId: string): string {
  return `workspace-changes:${workspaceId}`
}

export function createWorkspaceChangesWorkbenchItem(options: {
  workspaceId: string
  workspaceName?: string
}): WorkbenchItem | null {
  const workspaceId = options.workspaceId.trim()
  if (!workspaceId) return null
  const workspaceName = (options.workspaceName || '').trim() || workspaceId
  return {
    id: workspaceChangesWorkbenchItemId(workspaceId),
    kind: 'diff',
    title: workspaceName,
    // Workspace scope: switching sessions must not close a workspace review.
    scope: { type: 'workspace', id: workspaceId },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: { workspaceId, workspaceName },
  }
}

export function workspaceIdFromWorkbenchItem(item: WorkbenchItem): string {
  if (item.kind !== 'diff') return ''
  const value = item.payload.workspaceId
  return typeof value === 'string' ? value.trim() : ''
}

export function requestWorkspaceChangesOpen(
  detail: WorkspaceChangesOpenEventDetail,
): boolean {
  if (!detail.workspaceId.trim() || typeof window === 'undefined') return false
  window.dispatchEvent(new CustomEvent<WorkspaceChangesOpenEventDetail>(
    WORKSPACE_CHANGES_OPEN_EVENT,
    { detail: { ...detail, workspaceId: detail.workspaceId.trim() } },
  ))
  return true
}
