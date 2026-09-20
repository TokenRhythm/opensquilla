import { SESSIONS_PROCESSES_LIST_METHOD, type SessionProcessSnapshot, type SessionsProcessesListParams, type SessionsProcessesListResult } from '@/contracts/generated/v4/sessionsProcessesList'
import { validateSessionsProcessesListParams, validateSessionsProcessesListResult } from '@/contracts/generated/v4/sessionsProcessesListValidators.mjs'
import { SESSIONS_PROCESSES_LOG_METHOD, type SessionsProcessesLogParams, type SessionsProcessesLogResult } from '@/contracts/generated/v4/sessionsProcessesLog'
import { validateSessionsProcessesLogParams, validateSessionsProcessesLogResult } from '@/contracts/generated/v4/sessionsProcessesLogValidators.mjs'
import { SESSIONS_PROCESSES_STOP_METHOD, type SessionsProcessesStopParams, type SessionsProcessesStopResult } from '@/contracts/generated/v4/sessionsProcessesStop'
import { validateSessionsProcessesStopParams, validateSessionsProcessesStopResult } from '@/contracts/generated/v4/sessionsProcessesStopValidators.mjs'
import type { SessionProcess, SessionProcessIdentity, SessionProcesses } from '@/modules/sessionProcesses'
interface ProcessTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
  supports(method: string): boolean
}

function identity(raw: SessionsProcessesListResult | SessionsProcessesLogResult | SessionsProcessesStopResult): SessionProcessIdentity {
  return { sessionKey: raw.session_key, sessionId: raw.session_id, sessionEpoch: raw.session_epoch }
}

function process(raw: SessionProcessSnapshot): SessionProcess {
  return { executionId: raw.execution_id, taskId: raw.task_id, command: raw.command, status: raw.status,
    returncode: raw.returncode, startedAt: raw.started_at, endedAt: raw.ended_at }
}

export function createV4SessionProcesses(
  transport: ProcessTransport,
  options: { getAuth(): Record<string, unknown> | null },
): SessionProcesses {
  function hasScope(write: boolean) {
    const auth = options.getAuth()
    const principal = auth?.principal && typeof auth.principal === 'object'
      ? auth.principal as Record<string, unknown> : null
    if (!principal || principal.authState === 'guest'
      || (Array.isArray(principal.capabilities) && principal.capabilities.includes('guest.safe'))) return false
    const scopes = Array.isArray(principal.scopes) ? principal.scopes : []
    return scopes.includes('operator.admin') || scopes.includes('operator.write')
      || (!write && scopes.includes('operator.read'))
  }
  return {
    get available() { return transport.supports(SESSIONS_PROCESSES_LIST_METHOD) && hasScope(false) },
    get canStop() { return transport.supports(SESSIONS_PROCESSES_STOP_METHOD) && hasScope(true) },
    async list(sessionKey) {
      const params: SessionsProcessesListParams = { sessionKey }
      if (!validateSessionsProcessesListParams(params)) throw new Error('Invalid process list request')
      const raw = await transport.request<SessionsProcessesListResult>(SESSIONS_PROCESSES_LIST_METHOD, { ...params })
      if (!validateSessionsProcessesListResult(raw) || raw.session_key !== sessionKey) throw new Error('Invalid process snapshot')
      return { ...identity(raw), processes: raw.processes.map(process) }
    },
    async log(sessionKey, executionId) {
      const params: SessionsProcessesLogParams = { sessionKey, executionId, limit: 12000 }
      if (!validateSessionsProcessesLogParams(params)) throw new Error('Invalid process log request')
      const raw = await transport.request<SessionsProcessesLogResult>(SESSIONS_PROCESSES_LOG_METHOD, { ...params })
      if (!validateSessionsProcessesLogResult(raw) || raw.session_key !== sessionKey || raw.execution_id !== executionId) throw new Error('Invalid process output')
      return { ...identity(raw), executionId: raw.execution_id, status: raw.status, output: raw.output, truncated: raw.truncated }
    },
    async stop(sessionKey, executionId) {
      if (!hasScope(true)) throw new Error('Stopping processes is not permitted')
      const params: SessionsProcessesStopParams = { sessionKey, executionId }
      if (!validateSessionsProcessesStopParams(params)) throw new Error('Invalid process stop request')
      const raw = await transport.request<SessionsProcessesStopResult>(SESSIONS_PROCESSES_STOP_METHOD, { ...params })
      if (!validateSessionsProcessesStopResult(raw) || raw.session_key !== sessionKey || raw.process.execution_id !== executionId) throw new Error('Invalid process stop receipt')
      return { ...identity(raw), process: process(raw.process) }
    },
  }
}
