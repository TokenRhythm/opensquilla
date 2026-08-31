export const WORKSPACE_FILE_ATTACH_EVENT = 'opensquilla:workspace-file-attach'

export interface WorkspaceFileAttachDetail {
  workspaceId: string
  workspacePath: string
  path: string
  name: string
  /** Directory-listing size in bytes; used to refuse files that would be
   *  truncated by the bounded content API before they become attachments. */
  size?: number
  /** Pre-fetched text (e.g. an editor selection snippet). When present the
   *  receiver stages this content directly instead of calling the content
   *  API, which would return the whole file rather than the selection. */
  content?: string
}

export function requestWorkspaceFileAttach(detail: WorkspaceFileAttachDetail) {
  window.dispatchEvent(
    new CustomEvent<WorkspaceFileAttachDetail>(WORKSPACE_FILE_ATTACH_EVENT, {
      detail,
    }),
  )
}
