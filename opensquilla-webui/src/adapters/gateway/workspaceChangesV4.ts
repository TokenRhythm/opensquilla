import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { RpcRequester as WorkspaceChangesTransport } from './privateTransports'
import {
  WORKSPACES_GIT_DIFF_METHOD,
  type WorkspacesGitDiffParams,
  type WorkspacesGitDiffResult,
} from '@/contracts/generated/v4/workspacesGitDiff'
import { validateWorkspacesGitDiffResult } from '@/contracts/generated/v4/workspacesGitDiffValidators.mjs'
import {
  WORKSPACES_GIT_STATUS_METHOD,
  type WorkspacesGitStatusParams,
  type WorkspacesGitStatusResult,
} from '@/contracts/generated/v4/workspacesGitStatus'
import { validateWorkspacesGitStatusResult } from '@/contracts/generated/v4/workspacesGitStatusValidators.mjs'
import {
  WORKSPACES_GIT_STAGE_METHOD,
  type WorkspacesGitStageParams,
  type WorkspacesGitStageResult,
} from '@/contracts/generated/v4/workspacesGitStage'
import {
  validateWorkspacesGitStageParams,
  validateWorkspacesGitStageResult,
} from '@/contracts/generated/v4/workspacesGitStageValidators.mjs'
import {
  WORKSPACES_GIT_DISCARD_METHOD,
  type WorkspacesGitDiscardParams,
  type WorkspacesGitDiscardResult,
} from '@/contracts/generated/v4/workspacesGitDiscard'
import {
  validateWorkspacesGitDiscardParams,
  validateWorkspacesGitDiscardResult,
} from '@/contracts/generated/v4/workspacesGitDiscardValidators.mjs'
import {
  WORKSPACES_GIT_COMMIT_METHOD,
  type WorkspacesGitCommitParams,
  type WorkspacesGitCommitResult,
} from '@/contracts/generated/v4/workspacesGitCommit'
import {
  validateWorkspacesGitCommitParams,
  validateWorkspacesGitCommitResult,
} from '@/contracts/generated/v4/workspacesGitCommitValidators.mjs'
import {
  WORKSPACES_GIT_COMMIT_MESSAGE_DRAFT_METHOD,
  type WorkspacesGitCommitMessageDraftParams,
  type WorkspacesGitCommitMessageDraftResult,
} from '@/contracts/generated/v4/workspacesGitCommitMessage'
import {
  validateWorkspacesGitCommitMessageDraftParams,
  validateWorkspacesGitCommitMessageDraftResult,
} from '@/contracts/generated/v4/workspacesGitCommitMessageValidators.mjs'
import {
  WORKSPACES_GIT_PUSH_METHOD,
  type WorkspacesGitPushParams,
  type WorkspacesGitPushResult,
} from '@/contracts/generated/v4/workspacesGitPush'
import {
  validateWorkspacesGitPushParams,
  validateWorkspacesGitPushResult,
} from '@/contracts/generated/v4/workspacesGitPushValidators.mjs'
import {
  WORKSPACES_GIT_UNDO_COMMIT_METHOD,
  type WorkspacesGitUndoCommitParams,
  type WorkspacesGitUndoCommitResult,
} from '@/contracts/generated/v4/workspacesGitUndoCommit'
import {
  validateWorkspacesGitUndoCommitParams,
  validateWorkspacesGitUndoCommitResult,
} from '@/contracts/generated/v4/workspacesGitUndoCommitValidators.mjs'
import type {
  WorkspaceChanges,
  WorkspaceChangesReader,
  WorkspaceCommit,
  WorkspaceCommitMessageDraft,
  WorkspaceIndexChange,
  WorkspacePush,
} from '@/modules/workspaceChanges'

function optionsFor(signal?: AbortSignal): RpcCallOptions | undefined {
  return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined
}

function requireResult<T>(value: unknown, valid: (candidate: unknown) => boolean, method: string): T {
  if (!valid(value)) throw new Error(`${method} returned an invalid response`)
  return value as T
}

/** The outgoing direction is validated too: a request that cannot satisfy its
 * Contract (an empty path list, say) should not reach the Gateway at all. */
function requireParams<T>(value: unknown, valid: (candidate: unknown) => boolean, method: string): T {
  if (!valid(value)) throw new Error(`${method} received params that violate its contract`)
  return value as T
}

/**
 * Gateway-backed reader for the read-only working-tree surface.
 *
 * The generated validator runs at this boundary, so a mixed-version Gateway
 * that answers with an unexpected shape fails here instead of reaching the
 * panel as a half-populated model.
 */
export function createV4WorkspaceChanges(
  transport: WorkspaceChangesTransport,
): WorkspaceChangesReader {
  return {
    async readChanges(workspaceId, options): Promise<WorkspaceChanges> {
      const params: WorkspacesGitStatusParams = { workspaceId }
      const result = requireResult<WorkspacesGitStatusResult>(
        await transport.request(
          WORKSPACES_GIT_STATUS_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitStatusResult,
        WORKSPACES_GIT_STATUS_METHOD,
      )
      return {
        available: result.available,
        availabilityReason: result.availabilityReason,
        branch: result.branch,
        detached: result.detached,
        upstream: result.upstream,
        ahead: result.ahead,
        behind: result.behind,
        totalCount: result.totalCount,
        truncated: result.truncated,
        addedLines: result.addedLines,
        removedLines: result.removedLines,
        entries: result.entries.map(entry => ({
          path: entry.path,
          previousPath: entry.previousPath,
          changeType: entry.changeType,
          staged: entry.staged,
          unstaged: entry.unstaged,
          addedLines: entry.addedLines,
          removedLines: entry.removedLines,
        })),
      }
    },

    async readDiff(request, options) {
      const params: WorkspacesGitDiffParams = {
        workspaceId: request.workspaceId,
        path: request.path,
        ...(request.staged === undefined ? {} : { staged: request.staged }),
      }
      const result = requireResult<WorkspacesGitDiffResult>(
        await transport.request(
          WORKSPACES_GIT_DIFF_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitDiffResult,
        WORKSPACES_GIT_DIFF_METHOD,
      )
      return {
        path: result.path,
        staged: result.staged,
        text: result.text,
        truncated: result.truncated,
        binary: result.binary,
      }
    },

    async stagePaths(request, options): Promise<WorkspaceIndexChange> {
      const params = requireParams<WorkspacesGitStageParams>(
        {
          workspaceId: request.workspaceId,
          staged: request.staged,
          paths: [...request.paths],
        },
        validateWorkspacesGitStageParams,
        WORKSPACES_GIT_STAGE_METHOD,
      )
      const result = requireResult<WorkspacesGitStageResult>(
        await transport.request(
          WORKSPACES_GIT_STAGE_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitStageResult,
        WORKSPACES_GIT_STAGE_METHOD,
      )
      return {
        staged: result.staged,
        affectedPaths: result.affectedPaths,
      }
    },

    async discardPaths(request, options): Promise<readonly string[]> {
      const params = requireParams<WorkspacesGitDiscardParams>(
        { workspaceId: request.workspaceId, paths: [...request.paths] },
        validateWorkspacesGitDiscardParams,
        WORKSPACES_GIT_DISCARD_METHOD,
      )
      const result = requireResult<WorkspacesGitDiscardResult>(
        await transport.request(
          WORKSPACES_GIT_DISCARD_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitDiscardResult,
        WORKSPACES_GIT_DISCARD_METHOD,
      )
      return result.discardedPaths
    },

    async draftCommitMessage(request, options): Promise<WorkspaceCommitMessageDraft> {
      const params = requireParams<WorkspacesGitCommitMessageDraftParams>(
        { workspaceId: request.workspaceId },
        validateWorkspacesGitCommitMessageDraftParams,
        WORKSPACES_GIT_COMMIT_MESSAGE_DRAFT_METHOD,
      )
      const result = requireResult<WorkspacesGitCommitMessageDraftResult>(
        await transport.request(
          WORKSPACES_GIT_COMMIT_MESSAGE_DRAFT_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitCommitMessageDraftResult,
        WORKSPACES_GIT_COMMIT_MESSAGE_DRAFT_METHOD,
      )
      return { subject: result.subject, body: result.body }
    },

    async commitIndex(request, options): Promise<WorkspaceCommit> {
      const params = requireParams<WorkspacesGitCommitParams>(
        { workspaceId: request.workspaceId, message: request.message },
        validateWorkspacesGitCommitParams,
        WORKSPACES_GIT_COMMIT_METHOD,
      )
      const result = requireResult<WorkspacesGitCommitResult>(
        await transport.request(
          WORKSPACES_GIT_COMMIT_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitCommitResult,
        WORKSPACES_GIT_COMMIT_METHOD,
      )
      return { sha: result.sha, subject: result.subject }
    },

    async pushBranch(request, options): Promise<WorkspacePush> {
      const params = requireParams<WorkspacesGitPushParams>(
        { workspaceId: request.workspaceId },
        validateWorkspacesGitPushParams,
        WORKSPACES_GIT_PUSH_METHOD,
      )
      const result = requireResult<WorkspacesGitPushResult>(
        await transport.request(
          WORKSPACES_GIT_PUSH_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitPushResult,
        WORKSPACES_GIT_PUSH_METHOD,
      )
      return { upstream: result.upstream, output: result.output }
    },

    async undoCommit(request, options): Promise<WorkspaceCommit> {
      const params = requireParams<WorkspacesGitUndoCommitParams>(
        { workspaceId: request.workspaceId },
        validateWorkspacesGitUndoCommitParams,
        WORKSPACES_GIT_UNDO_COMMIT_METHOD,
      )
      const result = requireResult<WorkspacesGitUndoCommitResult>(
        await transport.request(
          WORKSPACES_GIT_UNDO_COMMIT_METHOD,
          params as unknown as Record<string, unknown>,
          optionsFor(options?.signal),
        ),
        validateWorkspacesGitUndoCommitResult,
        WORKSPACES_GIT_UNDO_COMMIT_METHOD,
      )
      return { sha: result.sha, subject: result.subject }
    },
  }
}
