import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  SESSIONS_RESET_METHOD,
  type SessionsResetParams,
  type SessionsResetResult,
} from '@/contracts/generated/v4/sessionsReset'
import { validateSessionsResetResult } from '@/contracts/generated/v4/sessionsResetValidators.mjs'
import {
  SESSIONS_CONTEXT_COMPACT_METHOD,
  type SessionsContextCompactParams,
  type SessionsContextCompactResult,
} from '@/contracts/generated/v4/sessionsContextCompact'
import { validateSessionsContextCompactResult } from '@/contracts/generated/v4/sessionsContextCompactValidators.mjs'
import type {
  CompactSessionCommand,
  ResetSessionCommand,
  ResetSessionResult,
  SessionCompactionResult,
  SessionMaintenance,
  SessionMaintenanceRequestOptions,
} from '@/modules/sessionMaintenance'

interface SessionMaintenanceTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
}

function callOptions(
  options?: SessionMaintenanceRequestOptions,
): RpcCallOptions | undefined {
  if (!options) return undefined
  return {
    signal: options.signal,
    timeoutMs: options.timeoutMs,
    timeoutAction: options.timeoutAction,
    abortAction: options.abortAction,
  }
}

async function request<T>(
  transport: SessionMaintenanceTransport,
  method: string,
  params: Record<string, unknown>,
  options?: SessionMaintenanceRequestOptions,
): Promise<T> {
  const mapped = callOptions(options)
  return mapped
    ? transport.request<T>(method, params, mapped)
    : transport.request<T>(method, params)
}

function invalidResponse(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

function resetResult(raw: SessionsResetResult): ResetSessionResult {
  return {
    key: raw.key,
    reset: true,
    rotated: raw.rotated,
    previousSessionId: raw.previous_session_id,
    sessionId: raw.session_id,
    epoch: raw.epoch,
    ...(raw.flush_receipt !== undefined ? { flushReceipt: raw.flush_receipt } : {}),
  }
}

function compactionResult(raw: SessionsContextCompactResult): SessionCompactionResult {
  return {
    key: raw.key,
    compactionId: raw.compaction_id,
    status: raw.status,
    compacted: raw.compacted,
    applied: raw.applied,
    durability: raw.durability,
    userVisible: raw.user_visible,
    ...(raw.mode !== undefined ? { mode: raw.mode } : {}),
    ...(raw.summary_len !== undefined ? { summaryLength: raw.summary_len } : {}),
    ...(raw.summary_source !== undefined ? { summarySource: raw.summary_source } : {}),
    ...(raw.context_window_tokens !== undefined
      ? { contextWindowTokens: raw.context_window_tokens }
      : {}),
    ...(raw.tokens_before !== undefined ? { tokensBefore: raw.tokens_before } : {}),
    ...(raw.tokens_after !== undefined ? { tokensAfter: raw.tokens_after } : {}),
    ...(raw.remaining_budget_tokens !== undefined
      ? { remainingBudgetTokens: raw.remaining_budget_tokens }
      : {}),
    ...(raw.removed_count !== undefined ? { removedCount: raw.removed_count } : {}),
    ...(raw.kept_count !== undefined ? { keptCount: raw.kept_count } : {}),
    ...(raw.chunk_count !== undefined ? { chunkCount: raw.chunk_count } : {}),
    ...(raw.coverage_status !== undefined ? { coverageStatus: raw.coverage_status } : {}),
    ...(raw.missing_obligation_count !== undefined
      ? { missingObligationCount: raw.missing_obligation_count }
      : {}),
    ...(raw.critical_carry_forward_count !== undefined
      ? { criticalCarryForwardCount: raw.critical_carry_forward_count }
      : {}),
    ...(raw.state_kind !== undefined ? { stateKind: raw.state_kind } : {}),
    ...(raw.quality_report !== undefined ? { qualityReport: raw.quality_report } : {}),
    ...(raw.skip_reason !== undefined ? { skipReason: raw.skip_reason } : {}),
    ...(raw.reason !== undefined ? { reason: raw.reason } : {}),
    ...(raw.flush_receipt !== undefined ? { flushReceipt: raw.flush_receipt } : {}),
    ...(raw.flush_receipt_status !== undefined
      ? { flushReceiptStatus: raw.flush_receipt_status }
      : {}),
  }
}

function resetParams(command: ResetSessionCommand): SessionsResetParams {
  return {
    key: command.key,
    ...(command.force !== undefined ? { force: command.force } : {}),
  }
}

function compactParams(command: CompactSessionCommand): SessionsContextCompactParams {
  return {
    key: command.key,
    ...(command.wait !== undefined ? { wait: command.wait } : {}),
    ...(command.contextWindowTokens !== undefined
      ? { contextWindowTokens: command.contextWindowTokens }
      : {}),
    ...(command.instructions !== undefined ? { instructions: command.instructions } : {}),
  }
}

export function createV4SessionMaintenance(
  transport: SessionMaintenanceTransport,
): SessionMaintenance {
  return {
    async reset(command): Promise<ResetSessionResult> {
      const raw = await request<SessionsResetResult>(
        transport,
        SESSIONS_RESET_METHOD,
        resetParams(command),
        command.options,
      )
      if (!validateSessionsResetResult(raw)) throw invalidResponse(SESSIONS_RESET_METHOD)
      return resetResult(raw)
    },

    async compact(command): Promise<SessionCompactionResult> {
      const raw = await request<SessionsContextCompactResult>(
        transport,
        SESSIONS_CONTEXT_COMPACT_METHOD,
        compactParams(command),
        command.options,
      )
      if (!validateSessionsContextCompactResult(raw)) {
        throw invalidResponse(SESSIONS_CONTEXT_COMPACT_METHOD)
      }
      return compactionResult(raw)
    },
  }
}
