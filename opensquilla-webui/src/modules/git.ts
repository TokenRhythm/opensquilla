import { hasInjectionContext, inject } from 'vue'
import type { InjectionKey } from 'vue'

/**
 * Read-only Git presentation model for the branch control and the workbench
 * source-control panel.
 *
 * These shapes mirror the `git.status` / `git.diff` wire Contracts, but the
 * generated Contract modules stay inside the Gateway Adapter. The Adapter
 * returns these types, so a wire change fails the build instead of silently
 * drifting from what the UI renders.
 */
export type GitFileStatus =
  | 'modified'
  | 'added'
  | 'deleted'
  | 'renamed'
  | 'untracked'
  | 'conflicted'

export interface GitChangedFile {
  path: string
  status: GitFileStatus
  staged: boolean
  previousPath?: string | null
}

export interface GitStatusPayload {
  /** Whether the directory is a usable git repository. */
  available: boolean
  workspacePath: string | null
  /** Branch name; null while detached. */
  branch: string | null
  detached: boolean
  /** Branch without any commit yet. */
  unborn: boolean
  headOid: string | null
  ahead: number
  behind: number
  clean: boolean
  files: GitChangedFile[]
  /** Why the repository is unavailable, such as `not_repository` or `git_unavailable`. */
  reason: string | null
}

export interface GitDiffPayload {
  path: string
  /** Unified diff text. */
  diff: string
  binary: boolean
  /** Whether an over-long diff was truncated. */
  truncated: boolean
  additions: number
  deletions: number
}

export interface GitStatusReadOptions {
  workspaceId?: string | null
  signal?: AbortSignal
}

export interface GitDiffReadInput {
  /** Path relative to the repository root. */
  path: string
  workspaceId?: string | null
  staged?: boolean
  signal?: AbortSignal
}

/** Owner-only read surface; every method is a query, never a mutation. */
export interface GitAccess {
  status(options?: GitStatusReadOptions): Promise<GitStatusPayload>
  diff(input: GitDiffReadInput): Promise<GitDiffPayload>
}

export const GIT_ACCESS_KEY: InjectionKey<GitAccess | null> = Symbol('GitAccess')

/** Absent outside the app shell (unit tests, isolated panels) rather than throwing. */
export function useGitAccess(): GitAccess | null {
  return hasInjectionContext() ? inject(GIT_ACCESS_KEY, null) : null
}

export interface GroupedChangedFiles {
  /** Index changes: `staged` is true and the file is tracked. */
  staged: GitChangedFile[]
  /** Working-tree changes that are not staged yet. */
  unstaged: GitChangedFile[]
  /** Files Git does not track yet; never reported as an unstaged change. */
  untracked: GitChangedFile[]
}

/** Split working-tree changes for the branch panel; `staged` alone is not enough. */
export function groupChangedFiles(files: readonly GitChangedFile[]): GroupedChangedFiles {
  const grouped: GroupedChangedFiles = { staged: [], unstaged: [], untracked: [] }
  for (const file of files) {
    if (file.status === 'untracked') grouped.untracked.push(file)
    else if (file.staged) grouped.staged.push(file)
    else grouped.unstaged.push(file)
  }
  return grouped
}
