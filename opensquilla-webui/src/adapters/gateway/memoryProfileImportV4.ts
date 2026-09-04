import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  MemoryProfileImportError,
  type MemoryImportInfo,
  type MemoryImportJob,
  type MemoryImportPreview,
  type MemoryImportProviderExpectation,
  type MemoryImportRecent,
  type MemoryImportTarget,
  type MemoryProfileImport,
} from '@/modules/memoryProfileImport'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { timeoutMs?: number }): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

const SCHEMA_VERSION = 1
const AGENT_ID = 'main'
const CLIENT_INPUT_LIMIT_BYTES = 256 * 1024
const METHODS = {
  info: 'memory.import.info',
  start: 'memory.import.start',
  status: 'memory.import.status',
  cancel: 'memory.import.cancel',
  retry: 'memory.import.retry',
  apply: 'memory.import.apply',
  undo: 'memory.import.undo',
  discard: 'memory.import.discard',
} as const

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function text(value: unknown): string { return typeof value === 'string' ? value : '' }
function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}
function target(value: unknown): MemoryImportTarget | null {
  return value === 'USER' || value === 'MEMORY' || value === 'IMPORT' ? value : null
}

function recent(value: unknown): MemoryImportRecent | null {
  const raw = object(value)
  if (!text(raw.receiptId)) return null
  return {
    receiptId: text(raw.receiptId),
    batchId: text(raw.batchId),
    appliedAt: text(raw.appliedAt),
    summary: strings(raw.summary),
    provider: text(raw.provider),
    model: text(raw.model),
    status: text(raw.status) || 'applied',
    indexStatus: text(raw.indexStatus),
    fileCount: number(raw.fileCount),
    targets: Array.isArray(raw.targets)
      ? raw.targets.map(target).filter((item): item is MemoryImportTarget => item !== null)
      : [],
  }
}

function preview(value: unknown): MemoryImportPreview | null {
  const raw = object(value)
  const counts = object(raw.decisionCounts)
  const files = Array.isArray(raw.files) ? raw.files.flatMap((item) => {
    const file = object(item)
    const logicalTarget = target(file.target ?? file.logicalTarget)
    const diff = text(file.diff)
    if (!logicalTarget || !diff) return []
    const status: 'created' | 'modified' | 'deleted' = file.status === 'created'
      || file.status === 'deleted'
      ? file.status
      : 'modified'
    return [{
      target: logicalTarget,
      displayName: text(file.displayName),
      relativePath: text(file.relativePath),
      status,
      additions: number(file.additions),
      deletions: number(file.deletions),
      diff,
    }]
  }) : []
  const result: MemoryImportPreview = {
    schemaVersion: number(raw.schemaVersion),
    previewId: text(raw.previewId),
    batchId: text(raw.batchId),
    candidateHash: text(raw.candidateHash),
    provider: text(raw.provider),
    model: text(raw.model),
    summary: strings(raw.summary),
    decisionCounts: {
      applied: number(counts.applied),
      duplicate: number(counts.duplicate),
      unresolved: number(counts.unresolved),
    },
    files,
  }
  return result.previewId && result.candidateHash ? result : null
}

function job(value: unknown): MemoryImportJob | null {
  const raw = object(value)
  const jobId = text(raw.jobId)
  const status = text(raw.status)
  if (!jobId || ![
    'queued', 'analyzing', 'cancelling', 'cancelled', 'interrupted',
    'ready', 'failed', 'applied', 'discarded',
  ].includes(status)) return null
  const stage = text(raw.stage)
  return {
    schemaVersion: number(raw.schemaVersion),
    jobId,
    batchId: text(raw.batchId),
    status: status as MemoryImportJob['status'],
    stage: stage === 'model' || stage === 'diff' ? stage : 'reading',
    provider: text(raw.provider),
    model: text(raw.model),
    startedAt: text(raw.startedAt),
    canRetry: raw.canRetry === true,
    errorCode: text(raw.errorCode),
    preview: preview(raw.preview),
  }
}

function info(value: unknown): MemoryImportInfo {
  const raw = object(value)
  return {
    schemaVersion: number(raw.schemaVersion),
    available: raw.available === true,
    provider: text(raw.provider),
    model: text(raw.model),
    isLocal: raw.isLocal === true || raw.isLoopback === true || raw.isLocalEndpoint === true,
    maxInputBytes: number(raw.maxInputBytes ?? raw.maxRawBytes) || CLIENT_INPUT_LIMIT_BYTES,
    promptVersion: text(raw.promptVersion),
    recentImport: recent(raw.recentImport),
    draftJob: job(raw.draftJob),
  }
}

function code(error: unknown): string {
  const value = error && typeof error === 'object' && 'code' in error
    ? String((error as { code?: unknown }).code || '')
    : ''
  return value
}

function methodMissing(error: unknown): boolean {
  return code(error) === 'METHOD_NOT_FOUND'
    || /method not found|unknown method|not registered/i.test(error instanceof Error ? error.message : String(error))
}

function invalid(message: string): MemoryProfileImportError {
  return new MemoryProfileImportError('invalid', 'MEMORY_IMPORT_INVALID_OUTPUT', message)
}

function mapError(error: unknown): MemoryProfileImportError {
  if (error instanceof MemoryProfileImportError) return error
  if (methodMissing(error)) {
    return new MemoryProfileImportError('unsupported', 'METHOD_NOT_FOUND', 'Memory profile import is unsupported.', error)
  }
  return new MemoryProfileImportError(
    'failed',
    code(error),
    error instanceof Error ? error.message : String(error),
    error,
  )
}

function expectedParams(expected: MemoryImportProviderExpectation): Record<string, unknown> {
  return {
    expectedProvider: expected.provider,
    expectedModel: expected.model,
    expectedIsLocal: expected.isLocal,
  }
}

export function createV4MemoryProfileImport(transport: RpcTransport): MemoryProfileImport {
  const request = async (method: string, params: Record<string, unknown>): Promise<unknown> => {
    if (!transport.supports(method)) throw new MemoryProfileImportError('unsupported', 'METHOD_NOT_FOUND', 'Memory profile import is unsupported.')
    await transport.ready({ timeoutMs: 8_000 })
    try {
      return await transport.request(method, { schemaVersion: SCHEMA_VERSION, agentId: AGENT_ID, ...params }, {
        timeoutMs: 15_000,
        timeoutAction: 'reject',
        abortAction: 'reject',
      })
    } catch (error) {
      if (methodMissing(error)) transport.markUnsupported(method)
      throw mapError(error)
    }
  }
  const validJob = async (method: string, params: Record<string, unknown>) => {
    const result = job(await request(method, params))
    if (!result || result.schemaVersion !== SCHEMA_VERSION) throw invalid('Invalid memory profile import job.')
    return result
  }

  return {
    async info() {
      const result = info(await request(METHODS.info, {}))
      if (result.schemaVersion !== SCHEMA_VERSION) throw invalid('Invalid memory profile import information.')
      return result
    },
    async start(input) {
      return await validJob(METHODS.start, {
        rawText: input.rawText,
        uiLocale: input.locale,
        exportPromptVersion: input.exportPromptVersion,
        clientRequestId: input.clientRequestId,
        ...expectedParams(input.expected),
      })
    },
    async status(jobId) { return await validJob(METHODS.status, { jobId }) },
    async cancel(jobId, clientRequestId) {
      return await validJob(METHODS.cancel, { jobId, clientRequestId })
    },
    async retry(jobId, clientRequestId, expected) {
      return await validJob(METHODS.retry, { jobId, clientRequestId, ...expectedParams(expected) })
    },
    async discard(target) { await request(METHODS.discard, target) },
    async apply(input) {
      const raw = object(await request(METHODS.apply, {
        previewId: input.preview.previewId,
        candidateHash: input.preview.candidateHash,
        idempotencyKey: input.idempotencyKey,
      }))
      if (number(raw.schemaVersion) !== SCHEMA_VERSION) throw invalid('Invalid memory profile import apply result.')
      return recent(raw.recentImport) || {
        receiptId: text(raw.receiptId),
        batchId: text(raw.batchId) || input.preview.batchId,
        appliedAt: text(raw.appliedAt) || new Date().toISOString(),
        summary: input.preview.summary,
        provider: input.preview.provider,
        model: input.preview.model,
        status: input.kind === 'undo' ? 'undone' : 'applied',
        indexStatus: text(raw.indexStatus),
        fileCount: input.preview.files.length,
        targets: [...new Set(input.preview.files.map(file => file.target))],
      }
    },
    async undo(input) {
      const raw = object(await request(METHODS.undo, {
        receiptId: input.recent.receiptId,
        clientRequestId: input.clientRequestId,
        ...expectedParams(input.expected),
      }))
      if (number(raw.schemaVersion) !== SCHEMA_VERSION) throw invalid('Invalid memory profile import undo result.')
      const status = text(raw.status)
      if (status === 'undone' || status === 'alreadyUndone') {
        return {
          kind: 'completed',
          recentImport: { ...input.recent, status: 'undone', indexStatus: text(raw.indexStatus) },
        }
      }
      if (status === 'reviewRequired') {
        const result = preview(raw.preview)
        if (!result) throw invalid('Invalid memory profile import undo preview.')
        if (result.provider !== input.expected.provider || result.model !== input.expected.model) {
          throw invalid('Memory profile import provider changed during undo.')
        }
        return { kind: 'review-required', preview: result }
      }
      throw invalid('Invalid memory profile import undo result.')
    },
  }
}
