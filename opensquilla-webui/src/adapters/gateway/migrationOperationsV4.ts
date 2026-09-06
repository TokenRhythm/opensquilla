import {
  readTransportFailure,
} from './transportTypes'
import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { RpcRequester as RpcTransport } from './privateTransports'
import type {
  GatewayMigrationCandidate,
  GatewayMigrationPreview,
  GatewayMigrationSources,
  MigrationOperations,
} from '@/modules/migrationOperations'
import { MigrationOperationsError } from '@/modules/migrationOperations'
import { MIGRATION_SOURCES_LIST_METHOD } from '@/contracts/generated/v4/migrationSourcesList'
import { validateResult as validateMigrationSourcesListResult } from '@/contracts/generated/v4/migrationSourcesListValidators.mjs'
import { MIGRATION_SOURCES_PREVIEW_METHOD } from '@/contracts/generated/v4/migrationSourcesPreview'
import { validateParams as validateMigrationSourcesPreviewParams, validateResult as validateMigrationSourcesPreviewResult } from '@/contracts/generated/v4/migrationSourcesPreviewValidators.mjs'

const options = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 15_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

function candidate(value: unknown): GatewayMigrationCandidate {
  const source = value as Record<string, unknown>
  return {
    id: String(source.candidateId),
    sourceKind: String(source.sourceKind),
    version: typeof source.version === 'string' ? source.version : null,
    estimatedActivityAt: typeof source.estimatedActivityAt === 'string'
      ? source.estimatedActivityAt
      : null,
    sessionCount: typeof source.sessionCount === 'number' ? source.sessionCount : null,
    sizeBytes: typeof source.sizeBytes === 'number' ? source.sizeBytes : null,
    previouslyImported: source.previouslyImported === true,
  }
}

function sources(value: unknown): GatewayMigrationSources {
  const raw = value as Record<string, unknown>
  const capabilities = raw.capabilities as Record<string, unknown>
  const candidates = Array.isArray(raw.candidates) ? raw.candidates.map(candidate) : []
  return {
    schemaVersion: 1,
    mode: 'preview_only',
    capabilities: {
      discover: capabilities.discover === true,
      preview: capabilities.preview === true,
      apply: capabilities.apply === true,
      manualSource: capabilities.manualSource === true,
    },
    candidates,
  }
}

function preview(value: unknown): GatewayMigrationPreview {
  const raw = value as Record<string, unknown>
  const summary = raw.summary as Record<string, unknown>
  const itemCounts = summary.itemCounts as Record<string, unknown>
  const execution = raw.execution as Record<string, unknown>
  return {
    schemaVersion: 1,
    mode: 'preview_only',
    candidate: candidate(raw.candidate),
    previewStatus: raw.previewStatus === 'blocked' ? 'blocked' : 'available',
    targetAction: raw.targetAction === 'replace' ? 'replace' : 'copy',
    summary: {
      sessionCount: typeof summary.sessionCount === 'number' ? summary.sessionCount : null,
      itemCounts: {
        planned: Number(itemCounts.planned),
        skipped: Number(itemCounts.skipped),
        error: Number(itemCounts.error),
      },
      pausedJobCount: Number(summary.pausedJobCount),
      diskRequiredBytes: Number(summary.diskRequiredBytes),
      diskFreeBytes: Number(summary.diskFreeBytes),
    },
    blockers: Array.isArray(raw.blockers) ? raw.blockers.filter(item => typeof item === 'string') : [],
    notices: Array.isArray(raw.notices) ? raw.notices.filter(item => typeof item === 'string') : [],
    execution: {
      canApply: false,
      supportedBy: Array.isArray(execution.supportedBy)
        ? execution.supportedBy.filter(item => typeof item === 'string')
        : [],
    },
  }
}

function mapMigrationError(error: unknown): MigrationOperationsError {
  if (error instanceof MigrationOperationsError) return error
  const failure = readTransportFailure(error)
  const code = failure.code?.toUpperCase()
  const kind = code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED'
    ? 'unsupported'
    : code === 'UNAUTHORIZED' || code === 'FORBIDDEN'
      ? 'forbidden'
      : code === 'INVALID_PARAMS' || code === 'INVALID_REQUEST'
        ? 'invalid'
        : 'unavailable'
  return new MigrationOperationsError(kind, failure.message, error)
}

async function requestMigration<T>(
  rpc: RpcTransport,
  method: string,
  params: Record<string, unknown>,
  requestOptions: RpcCallOptions,
): Promise<T> {
  try {
    return await rpc.request<T>(method, params, requestOptions)
  } catch (error) {
    throw mapMigrationError(error)
  }
}

export function createV4MigrationOperations(rpc: RpcTransport): MigrationOperations {
  return {
    async listSources(request) {
      const result = await requestMigration(
        rpc,
        MIGRATION_SOURCES_LIST_METHOD,
        {},
        options(request?.signal),
      )
      if (!validateMigrationSourcesListResult(result)) {
        throw new MigrationOperationsError(
          'invalid',
          `${MIGRATION_SOURCES_LIST_METHOD} returned an invalid response`,
        )
      }
      return sources(result)
    },
    async preview(candidateId, request) {
      const params = { candidateId: candidateId.trim() }
      if (!validateMigrationSourcesPreviewParams(params)) {
        throw new MigrationOperationsError(
          'invalid',
          `${MIGRATION_SOURCES_PREVIEW_METHOD} params are invalid`,
        )
      }
      const result = await requestMigration(
        rpc,
        MIGRATION_SOURCES_PREVIEW_METHOD,
        params,
        options(request?.signal),
      )
      if (!validateMigrationSourcesPreviewResult(result)) {
        throw new MigrationOperationsError(
          'invalid',
          `${MIGRATION_SOURCES_PREVIEW_METHOD} returned an invalid response`,
        )
      }
      return preview(result)
    },
  }
}
