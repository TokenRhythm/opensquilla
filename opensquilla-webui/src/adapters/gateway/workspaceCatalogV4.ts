import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { RpcRequester as WorkspaceTransport } from './privateTransports'
import {
  WORKSPACES_LIST_METHOD,
  type Result as WorkspacesListResult,
} from '@/contracts/generated/v4/workspacesList'
import { validateResult as validateWorkspacesListResult } from '@/contracts/generated/v4/workspacesListValidators.mjs'
import {
  WORKSPACES_OPEN_METHOD,
  type Params as WorkspacesOpenParams,
  type Result as WorkspacesOpenResult,
} from '@/contracts/generated/v4/workspacesOpen'
import { validateResult as validateWorkspacesOpenResult } from '@/contracts/generated/v4/workspacesOpenValidators.mjs'
import {
  WORKSPACES_UPDATE_METHOD,
  type Params as WorkspacesUpdateParams,
  type Result as WorkspacesUpdateResult,
} from '@/contracts/generated/v4/workspacesUpdate'
import { validateResult as validateWorkspacesUpdateResult } from '@/contracts/generated/v4/workspacesUpdateValidators.mjs'
import {
  WORKSPACES_PIN_METHOD,
  type Params as WorkspacesPinParams,
  type Result as WorkspacesPinResult,
} from '@/contracts/generated/v4/workspacesPin'
import { validateResult as validateWorkspacesPinResult } from '@/contracts/generated/v4/workspacesPinValidators.mjs'
import {
  WORKSPACES_REMOVE_METHOD,
  type Params as WorkspacesRemoveParams,
  type Result as WorkspacesRemoveResult,
} from '@/contracts/generated/v4/workspacesRemove'
import { validateResult as validateWorkspacesRemoveResult } from '@/contracts/generated/v4/workspacesRemoveValidators.mjs'
import {
  WORKSPACES_HISTORY_DELETE_METHOD,
  type Params as WorkspacesHistoryDeleteParams,
  type Result as WorkspacesHistoryDeleteResult,
} from '@/contracts/generated/v4/workspacesHistoryDelete'
import { validateResult as validateWorkspacesHistoryDeleteResult } from '@/contracts/generated/v4/workspacesHistoryDeleteValidators.mjs'
import {
  SANDBOX_PATH_LIST_METHOD,
  type Params as SandboxPathListParams,
  type Result as SandboxPathListResult,
} from '@/contracts/generated/v4/sandboxPathList'
import { validateResult as validateSandboxPathListResult } from '@/contracts/generated/v4/sandboxPathListValidators.mjs'
import {
  SANDBOX_PATH_CREATE_DIRECTORY_METHOD,
  type SandboxPathCreateDirectoryParams,
  type SandboxPathCreateDirectoryResult,
} from '@/contracts/generated/v4/sandboxPathCreateDirectory'
import {
  SANDBOX_PATH_PICK_METHOD,
  type SandboxPathPickParams,
  type SandboxPathPickResult,
} from '@/contracts/generated/v4/sandboxPathPick'
import type {
  WorkspaceCatalog,
  WorkspaceHistoryDeletion,
  WorkspaceItem,
  WorkspacePathListing,
  WorkspacePathSelection,
} from '@/modules/workspaceCatalog'

function validCreateDirectory(value: unknown): value is SandboxPathCreateDirectoryResult {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const raw = value as Record<string, unknown>
  return typeof raw.path === 'string' && typeof raw.name === 'string' && raw.kind === 'directory'
}

function validPick(value: unknown): value is SandboxPathPickResult {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const raw = value as Record<string, unknown>
  return (typeof raw.path === 'string' || raw.path === null)
    && (raw.kind === 'workspace' || raw.kind === 'mount')
}

function optionsFor(signal?: AbortSignal): RpcCallOptions | undefined {
  return signal ? { signal, abortAction: 'reject', timeoutAction: 'reject' } : undefined
}

function errorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const value = error as { code?: unknown; data?: { code?: unknown } }
  return String(value.code ?? value.data?.code ?? '').toUpperCase()
}

function normalizeWorkspace(value: unknown): WorkspaceItem | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const raw = value as Record<string, unknown>
  const id = typeof raw.id === 'string' ? raw.id.trim() : ''
  const name = typeof raw.name === 'string' ? raw.name.trim() : ''
  const path = typeof raw.path === 'string' ? raw.path : ''
  if (!id || !name || !path) return null
  const taskCount = Number(raw.taskCount)
  return {
    id,
    name,
    path,
    taskCount: Number.isFinite(taskCount) ? Math.max(0, Math.floor(taskCount)) : 0,
    pinned: raw.pinned === true,
    available: raw.available !== false,
    ...(typeof raw.availabilityReason === 'string' && raw.availabilityReason
      ? { availabilityReason: raw.availabilityReason }
      : {}),
  }
}

function requireResult<T>(value: unknown, valid: (candidate: unknown) => boolean, method: string): T {
  if (!valid(value)) throw new Error(`${method} returned an invalid response`)
  return value as T
}

function normalizeMutation(value: unknown): WorkspaceItem | null {
  if (!value || typeof value !== 'object') return null
  return normalizeWorkspace((value as Record<string, unknown>).workspace)
}

export function createV4WorkspaceCatalog(transport: WorkspaceTransport): WorkspaceCatalog {
  const mutate = async <T>(method: string, params: Record<string, unknown>, signal: AbortSignal | undefined, valid: (value: unknown) => boolean): Promise<T> => {
    try {
      return requireResult<T>(
        await transport.request(method, params, optionsFor(signal)),
        valid,
        method,
      )
    } catch (error) {
      if (errorCode(error) === 'METHOD_NOT_FOUND') throw new Error(`${method} is unsupported`)
      throw error
    }
  }

  return {
    async list(options): Promise<readonly WorkspaceItem[]> {
      const result = await mutate<WorkspacesListResult>(
        WORKSPACES_LIST_METHOD,
        {},
        options?.signal,
        validateWorkspacesListResult,
      )
      return result.workspaces.map(normalizeWorkspace).filter((item): item is WorkspaceItem => item !== null)
    },

    async open(path, options): Promise<WorkspaceItem | null> {
      const params: WorkspacesOpenParams = { path, trusted: true }
      const result = await mutate<WorkspacesOpenResult>(
        WORKSPACES_OPEN_METHOD,
        params as unknown as Record<string, unknown>,
        options?.signal,
        validateWorkspacesOpenResult,
      )
      return normalizeMutation(result)
    },

    async rename(id, name, options): Promise<WorkspaceItem | null> {
      const params: WorkspacesUpdateParams = { workspaceId: id, name }
      const result = await mutate<WorkspacesUpdateResult>(WORKSPACES_UPDATE_METHOD, params as unknown as Record<string, unknown>, options?.signal, validateWorkspacesUpdateResult)
      return normalizeMutation(result)
    },

    async setPinned(id, pinned, options): Promise<WorkspaceItem | null> {
      const params: WorkspacesPinParams = { workspaceId: id, pinned }
      const result = await mutate<WorkspacesPinResult>(WORKSPACES_PIN_METHOD, params as unknown as Record<string, unknown>, options?.signal, validateWorkspacesPinResult)
      return normalizeMutation(result)
    },

    async remove(id, options): Promise<void> {
      const params: WorkspacesRemoveParams = { workspaceId: id }
      await mutate<WorkspacesRemoveResult>(WORKSPACES_REMOVE_METHOD, params as unknown as Record<string, unknown>, options?.signal, validateWorkspacesRemoveResult)
    },

    async deleteHistory(id, options): Promise<WorkspaceHistoryDeletion> {
      const params: WorkspacesHistoryDeleteParams = { workspaceId: id }
      const result = await mutate<WorkspacesHistoryDeleteResult>(WORKSPACES_HISTORY_DELETE_METHOD, params as unknown as Record<string, unknown>, options?.signal, validateWorkspacesHistoryDeleteResult)
      return {
        workspaceId: result.workspaceId,
        deletedTaskCount: result.deletedTaskCount,
        deletedSessionKeys: [...result.deletedSessionKeys],
      }
    },

    async listPath(request, options): Promise<WorkspacePathListing> {
      const params: SandboxPathListParams = {
        sessionKey: request.sessionKey,
        ...(request.kind ? { kind: request.kind } : {}),
        ...(request.path ? { path: request.path } : {}),
      }
      const result = await mutate<SandboxPathListResult>(SANDBOX_PATH_LIST_METHOD, params as unknown as Record<string, unknown>, options?.signal, validateSandboxPathListResult)
      return {
        currentPath: result.currentPath,
        path: result.path,
        parentPath: result.parentPath,
        systemPickerAvailable: result.systemPickerAvailable,
        entries: result.entries.map(entry => ({ ...entry })),
      }
    },

    async createDirectory(request, options) {
      const params: SandboxPathCreateDirectoryParams = {
        sessionKey: request.sessionKey,
        parentPath: request.parentPath,
        name: request.name,
        ...(request.kind ? { kind: request.kind } : {}),
      }
      const result = await mutate<SandboxPathCreateDirectoryResult>(SANDBOX_PATH_CREATE_DIRECTORY_METHOD, params as unknown as Record<string, unknown>, options?.signal, validCreateDirectory)
      return { path: result.path, name: result.name, kind: 'directory' as const }
    },

    async pickPath(request, options): Promise<WorkspacePathSelection> {
      const params: SandboxPathPickParams = {
        sessionKey: request.sessionKey,
        ...(request.initialPath ? { initialPath: request.initialPath } : {}),
        ...(request.kind ? { kind: request.kind } : {}),
        ...(request.access ? { access: request.access } : {}),
      }
      const result = await mutate<SandboxPathPickResult>(SANDBOX_PATH_PICK_METHOD, params as unknown as Record<string, unknown>, options?.signal, validPick)
      return { path: result.path, kind: result.kind }
    },
  }
}
