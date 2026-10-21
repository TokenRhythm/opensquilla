import type { InjectionKey } from 'vue'

export interface WorkspaceItem {
  readonly id: string
  readonly name: string
  readonly path: string
  readonly taskCount: number
  readonly pinned: boolean
  readonly available: boolean
  readonly availabilityReason?: string
}

export interface WorkspaceHistoryDeletion {
  readonly workspaceId: string
  readonly deletedTaskCount: number
  readonly deletedSessionKeys: readonly string[]
}

export interface WorkspaceCreationDefaults {
  readonly parentPath: string | null
  readonly hostName: string
  readonly platform: 'windows' | 'posix'
}

export type WorkspacePathKind = 'workspace' | 'mount'
export type WorkspacePathEntryKind = 'directory' | 'file'

export interface WorkspacePathEntry {
  readonly name: string
  readonly path: string
  readonly kind: WorkspacePathEntryKind
  readonly selectable: boolean
  readonly hidden?: boolean
}

export interface WorkspacePathListing {
  readonly currentPath: string
  readonly path: string
  readonly parentPath: string | null
  readonly systemPickerAvailable: boolean
  readonly entries: readonly WorkspacePathEntry[]
}

export interface WorkspacePathSelection {
  readonly path: string | null
  readonly kind: WorkspacePathKind
}

export interface WorkspaceCatalog {
  /** Defaults from the most recently read Gateway/profile catalog. */
  readonly creationDefaults?: WorkspaceCreationDefaults | null
  list(options?: { signal?: AbortSignal; timeoutMs?: number }): Promise<readonly WorkspaceItem[]>
  create?(parentPath: string, name: string, options?: { signal?: AbortSignal }): Promise<WorkspaceItem | null>
  open(path: string, options?: { signal?: AbortSignal }): Promise<WorkspaceItem | null>
  rename(id: string, name: string, options?: { signal?: AbortSignal }): Promise<WorkspaceItem | null>
  setPinned(id: string, pinned: boolean, options?: { signal?: AbortSignal }): Promise<WorkspaceItem | null>
  remove(id: string, options?: { signal?: AbortSignal }): Promise<void>
  deleteHistory(id: string, options?: { signal?: AbortSignal }): Promise<WorkspaceHistoryDeletion>
  listPath(
    request: { sessionKey: string; kind?: WorkspacePathKind; path?: string | null },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspacePathListing>
  createDirectory(
    request: { sessionKey: string; parentPath: string; name: string; kind?: WorkspacePathKind },
    options?: { signal?: AbortSignal },
  ): Promise<{ path: string; name: string; kind: 'directory' }>
  pickPath(
    request: { sessionKey: string; initialPath?: string; kind?: WorkspacePathKind; access?: 'ro' | 'rw' },
    options?: { signal?: AbortSignal },
  ): Promise<WorkspacePathSelection>
}

export const WORKSPACE_CATALOG_KEY: InjectionKey<WorkspaceCatalog> = Symbol('WorkspaceCatalog')
