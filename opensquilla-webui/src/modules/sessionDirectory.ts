import type { InjectionKey } from 'vue'

export interface SessionItem {
  key: string
  title: string
  subtitle: string
  groupLabel: string
  workspace?: string
  workspaceId?: string
  workspaceLabel?: string
  workspaceDisplayPath?: string
  effectiveAgentId: string
  sessionKind: string
  surface: string
  conversationKind: string
  status: string
  runStatus: string
  runLabel: string
  messageCount: number | null
  updatedAt: number
  model: string
  parent: { key: string; spawnDepth: number } | null
  provisional?: boolean
  forkedFromParent: boolean
  hasContractGaps: boolean
}

export interface SessionPage {
  items: SessionItem[]
  hasMore: boolean
  nextCursor: string | null
}

export interface SessionCount { value: number; exact: boolean }

export interface SessionDirectoryQueryOptions {
  signal?: AbortSignal
}

export interface SessionDirectory {
  listPage(
    request: { limit: number; cursor?: string } & SessionDirectoryQueryOptions,
  ): Promise<SessionPage>
  count(options?: SessionDirectoryQueryOptions): Promise<SessionCount | null>
}

export const SESSION_DIRECTORY_KEY: InjectionKey<SessionDirectory> = Symbol('SessionDirectory')
