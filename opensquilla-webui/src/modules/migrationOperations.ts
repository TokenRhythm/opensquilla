import type { InjectionKey } from 'vue'

export interface GatewayMigrationCandidate {
  readonly id: string
  readonly sourceKind: string
  readonly version: string | null
  readonly estimatedActivityAt: string | null
  readonly sessionCount: number | null
  readonly sizeBytes: number | null
  readonly previouslyImported: boolean
}

export interface GatewayMigrationCapabilities {
  readonly discover: boolean
  readonly preview: boolean
  readonly apply: boolean
  readonly manualSource: boolean
}

export interface GatewayMigrationSources {
  readonly schemaVersion: 1
  readonly mode: 'preview_only'
  readonly capabilities: GatewayMigrationCapabilities
  readonly candidates: readonly GatewayMigrationCandidate[]
}

export interface GatewayMigrationItemCounts {
  readonly planned: number
  readonly skipped: number
  readonly error: number
}

export interface GatewayMigrationPreviewSummary {
  readonly sessionCount: number | null
  readonly itemCounts: GatewayMigrationItemCounts
  readonly pausedJobCount: number
  readonly diskRequiredBytes: number
  readonly diskFreeBytes: number
}

export interface GatewayMigrationPreview {
  readonly schemaVersion: 1
  readonly mode: 'preview_only'
  readonly candidate: GatewayMigrationCandidate
  readonly previewStatus: 'available' | 'blocked'
  readonly targetAction: 'copy' | 'replace'
  readonly summary: GatewayMigrationPreviewSummary
  readonly blockers: readonly string[]
  readonly notices: readonly string[]
  readonly execution: {
    readonly canApply: false
    readonly supportedBy: readonly string[]
  }
}

export type MigrationOperationsErrorKind =
  | 'unsupported'
  | 'forbidden'
  | 'unavailable'
  | 'invalid'

/** Migration discovery failure projected by the Gateway Adapter. */
export class MigrationOperationsError extends Error {
  constructor(
    readonly kind: MigrationOperationsErrorKind,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
    this.name = 'MigrationOperationsError'
  }
}

export interface MigrationOperations {
  listSources(options?: { signal?: AbortSignal }): Promise<GatewayMigrationSources>
  preview(candidateId: string, options?: { signal?: AbortSignal }): Promise<GatewayMigrationPreview>
}

export const MIGRATION_OPERATIONS_KEY: InjectionKey<MigrationOperations> = Symbol('MigrationOperations')
