import type { InjectionKey } from 'vue'

/**
 * Semantic reasons exposed by the session directory.  The Gateway has more
 * historical reason strings than the UI needs to understand. Known generic
 * invalidations are projected to `updated`; values introduced by a newer
 * Gateway are deliberately projected to `unknown` at the Adapter boundary.
 */
export type SessionDirectoryChangeReason =
  | 'created'
  | 'deleted'
  | 'renamed'
  | 'forked'
  | 'taskQueued'
  | 'taskRunning'
  | 'taskTerminal'
  | 'autoTitled'
  | 'cronStaticMessage'
  | 'updated'
  | 'unknown'

export interface SessionDirectoryTask {
  id: string
  status?: string
}

/** A wire-independent invalidation notice for the session directory. */
export interface SessionDirectoryChange {
  key: string
  reason: SessionDirectoryChangeReason
  runStatus?: string
  changedTask?: SessionDirectoryTask
  lastTask?: SessionDirectoryTask
}

export interface SessionDirectoryChangeSubscription {
  close(): void
}

export interface SessionDirectoryChanges {
  /** Add a local listener; listeners do not own the physical WebSocket. */
  subscribe(listener: (change: SessionDirectoryChange) => void): SessionDirectoryChangeSubscription
  /**
   * Ask the Gateway Adapter to bind the logical directory lease on the current
   * connection. The operation is idempotent and never recycles a shared
   * transport. It resolves after the attempt, including unavailable/forbidden
   * Gateways, so callers can keep their polling fallback.
   */
  resume(): Promise<void>
  /** Release local listeners and the logical lease owned by this Module. */
  dispose(): void
}

export const SESSION_DIRECTORY_CHANGES_KEY: InjectionKey<SessionDirectoryChanges> =
  Symbol('SessionDirectoryChanges')
