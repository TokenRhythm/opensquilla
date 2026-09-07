import type { InjectionKey } from 'vue'

export type MemoryImportTarget = 'USER' | 'MEMORY' | 'IMPORT'
export type MemoryImportAnalysisPhase = 'reading' | 'model' | 'diff'
export type MemoryImportJobStatus = 'queued' | 'analyzing' | 'cancelling' | 'cancelled'
  | 'interrupted' | 'ready' | 'failed' | 'applied' | 'discarded'

export interface MemoryImportRecent {
  receiptId: string
  batchId: string
  appliedAt: string
  summary: string[]
  provider: string
  model: string
  status: string
  indexStatus: string
  fileCount: number
  targets: MemoryImportTarget[]
}

export interface MemoryImportDiffFile {
  target: MemoryImportTarget
  displayName: string
  relativePath: string
  status: 'created' | 'modified' | 'deleted'
  additions: number
  deletions: number
  diff: string
}

export interface MemoryImportPreview {
  schemaVersion: number
  previewId: string
  batchId: string
  candidateHash: string
  provider: string
  model: string
  summary: string[]
  decisionCounts: { applied: number; duplicate: number; unresolved: number }
  files: MemoryImportDiffFile[]
}

export interface MemoryImportJob {
  schemaVersion: number
  jobId: string
  batchId: string
  status: MemoryImportJobStatus
  stage: MemoryImportAnalysisPhase
  provider: string
  model: string
  startedAt: string
  canRetry: boolean
  errorCode: string
  preview: MemoryImportPreview | null
}

export interface MemoryImportInfo {
  schemaVersion: number
  available: boolean
  provider: string
  model: string
  isLocal: boolean
  maxInputBytes: number
  promptVersion: string
  recentImport: MemoryImportRecent | null
  draftJob: MemoryImportJob | null
}

export interface MemoryImportProviderExpectation {
  provider: string
  model: string
  isLocal: boolean
}

export type MemoryImportUndoResult =
  | { kind: 'completed'; recentImport: MemoryImportRecent }
  | { kind: 'review-required'; preview: MemoryImportPreview }

export type MemoryProfileImportErrorKind = 'unsupported' | 'invalid' | 'failed'

export class MemoryProfileImportError extends Error {
  readonly name = 'MemoryProfileImportError'

  constructor(
    readonly kind: MemoryProfileImportErrorKind,
    readonly code: string,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message)
  }
}

export interface MemoryProfileImport {
  info(): Promise<MemoryImportInfo>
  start(input: {
    rawText: string
    locale: string
    exportPromptVersion: string
    clientRequestId: string
    expected: MemoryImportProviderExpectation
  }): Promise<MemoryImportJob>
  status(jobId: string): Promise<MemoryImportJob>
  cancel(jobId: string, clientRequestId: string): Promise<MemoryImportJob>
  retry(
    jobId: string,
    clientRequestId: string,
    expected: MemoryImportProviderExpectation,
  ): Promise<MemoryImportJob>
  discard(target: { jobId: string } | { previewId: string }): Promise<void>
  apply(input: {
    preview: MemoryImportPreview
    idempotencyKey: string
    kind: 'import' | 'undo'
  }): Promise<MemoryImportRecent>
  undo(input: {
    recent: MemoryImportRecent
    clientRequestId: string
    expected: MemoryImportProviderExpectation
  }): Promise<MemoryImportUndoResult>
}

export const MEMORY_PROFILE_IMPORT_KEY: InjectionKey<MemoryProfileImport> = Symbol('MemoryProfileImport')
