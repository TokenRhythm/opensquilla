interface SessionWorkspaceReference {
  key: string
  workspaceId?: string
}

export interface ProjectHistoryDeletionContext {
  workspaceId: string
  currentSessionKey: string
  sessions: readonly SessionWorkspaceReference[]
  deletedSessionKeys: readonly string[]
}

/**
 * Whether deleting a project's history removed the task currently on screen.
 *
 * The backend deletion result is authoritative during the short interval
 * before sessions.list includes a just-created task.
 */
export function activeTaskWasDeletedWithProjectHistory(
  context: ProjectHistoryDeletionContext,
): boolean {
  if (!context.currentSessionKey) return false
  if (context.deletedSessionKeys.includes(context.currentSessionKey)) return true
  return context.sessions.some(session =>
    session.key === context.currentSessionKey
    && session.workspaceId === context.workspaceId)
}
