import type { InjectionKey } from 'vue'

/**
 * Domain projection of one changed path in a project workspace. Staged and
 * unstaged state stay separate because a file can be in both: a staged edit
 * followed by a further worktree edit must not collapse into one badge.
 */
export interface WorkspaceChangeEntry {
  readonly path: string
  readonly previousPath: string | null
  readonly changeType: WorkspaceChangeType
  readonly staged: boolean
  readonly unstaged: boolean
  /** Null when lines are not countable (binary, or no stats for the path). */
  readonly addedLines: number | null
  readonly removedLines: number | null
}

export type WorkspaceChangeType =
  | 'added'
  | 'modified'
  | 'deleted'
  | 'renamed'
  | 'copied'
  | 'typeChanged'
  | 'unmerged'
  | 'untracked'
  | 'unknown'

/**
 * Why a workspace has no readable working-tree state. `available: false` is
 * separate from an empty change list so the panel can say "cannot tell"
 * instead of "nothing changed".
 */
export type WorkspaceChangesAvailability =
  | 'git_unavailable'
  | 'not_repository'
  | 'timed_out'
  | 'failed'

export interface WorkspaceChanges {
  readonly available: boolean
  readonly availabilityReason: WorkspaceChangesAvailability | null
  readonly branch: string | null
  readonly detached: boolean
  readonly upstream: string | null
  readonly ahead: number
  readonly behind: number
  readonly totalCount: number
  readonly truncated: boolean
  readonly addedLines: number
  readonly removedLines: number
  readonly entries: readonly WorkspaceChangeEntry[]
}

export interface WorkspaceFileDiff {
  readonly path: string
  readonly staged: boolean
  readonly text: string
  readonly truncated: boolean
  readonly binary: boolean
}

export interface WorkspaceIndexChangeRequest {
  readonly workspaceId: string
  /** Repository-relative paths exactly as `WorkspaceChanges.entries` reported them. */
  readonly paths: readonly string[]
  /** True stages; false unstages. Worktree content is never modified. */
  readonly staged: boolean
}

/**
 * The index-only acknowledgement. It deliberately does not echo the refreshed
 * change list: the caller re-reads `readChanges` so the list it renders always
 * comes from one source rather than from a write response that could drift.
 */
export interface WorkspaceIndexChange {
  readonly staged: boolean
  readonly affectedPaths: readonly string[]
}

export interface WorkspacePathListRequest {
  readonly workspaceId: string
  readonly paths: readonly string[]
}

/**
 * A commit was created. `sha` is empty when Git committed but could not report
 * the object, so callers must render `subject` rather than assume a sha.
 */
export interface WorkspaceCommit {
  readonly sha: string
  readonly subject: string
}

export interface WorkspacePush {
  readonly upstream: string
  readonly output: string
}

/**
 * A drafted commit message, split the way Git reads it. It is a proposal for
 * the operator to edit: nothing is committed by drafting one.
 */
export interface WorkspaceCommitMessageDraft {
  readonly subject: string
  /** Empty when the model returned a subject alone. */
  readonly body: string
}

export interface WorkspaceChangesReader {
  readChanges(
    workspaceId: string,
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceChanges>
  readDiff(
    request: { workspaceId: string; path: string; staged?: boolean },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceFileDiff>
  stagePaths(
    request: WorkspaceIndexChangeRequest,
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceIndexChange>
  /** Restores tracked paths' worktree from the index. Loses uncommitted edits. */
  discardPaths(
    request: WorkspacePathListRequest,
    options?: { signal?: AbortSignal },
  ): Promise<readonly string[]>
  /**
   * Drafts a commit message from the staged index. Read-only on the repository:
   * the result is text, and what the message should say beyond the diff comes
   * from the application setting rather than from a field in this panel.
   */
  draftCommitMessage(
    request: { workspaceId: string },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceCommitMessageDraft>
  /** Commits the index. Nothing is staged implicitly. */
  commitIndex(
    request: { workspaceId: string; message: string },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceCommit>
  /** Pushes the current branch to the upstream it already tracks. */
  pushBranch(
    request: { workspaceId: string },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspacePush>
  /**
   * Moves the branch back one commit, leaving its content staged. The Gateway
   * refuses a tip the upstream already has, so callers only have to decide
   * whether to offer it, not whether it is safe.
   */
  undoCommit(
    request: { workspaceId: string },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspaceCommit>
}

export const WORKSPACE_CHANGES_KEY: InjectionKey<WorkspaceChangesReader> = Symbol('WorkspaceChanges')
