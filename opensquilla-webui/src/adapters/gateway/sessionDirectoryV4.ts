import i18n from '@/i18n'
import type { RpcCallOptions } from '@/lib/rpc'
import {
  SESSIONS_LIST_METHOD,
  type SessionRow,
  type SessionsListParams,
  type SessionsListResult,
} from '@/contracts/generated/v4/sessionsList'
import {
  SESSIONS_RESOLVE_METHOD,
  type SessionsResolveParams,
  type SessionsResolveResult,
} from '@/contracts/generated/v4/sessionsResolve'
import { validateSessionsResolveResult } from '@/contracts/generated/v4/sessionsResolveValidators.mjs'
import {
  SESSIONS_SEARCH_METHOD,
  type SessionsSearchParams,
  type SessionsSearchResult as SessionsSearchWireResult,
} from '@/contracts/generated/v4/sessionsSearch'
import { validateSessionsSearchResult } from '@/contracts/generated/v4/sessionsSearchValidators.mjs'
import type {
  ResolvedSession,
  SessionCount,
  SessionDirectory,
  SessionItem,
  SessionPage,
  SessionSearchRequest,
  SessionSearchResult,
} from '@/modules/sessionDirectory'
import { SessionDirectoryError } from '@/modules/sessionDirectory'
import {
  normalizeSessionRunStatus,
  resolveSessionRunStatus,
  sessionRunStatusLabel,
  summarizeSessionTask,
} from '@/modules/sessionRunStatus'

const SESSION_LIST_VIEW = 'session-list-v1'
const SESSION_COUNT_VIEW = 'session-count-v1'
const SESSION_DIRECTORY_TIMEOUT_MS = 10_000
const SESSION_DIRECTORY_CALL_OPTIONS: RpcCallOptions = {
  timeoutMs: SESSION_DIRECTORY_TIMEOUT_MS,
  timeoutAction: 'reject',
  abortAction: 'reject',
}

// Keep the Adapter's public factory signature independent of the private
// transport implementation.  The composition root passes the richer F2
// transport structurally; exposing RpcTransport here would leak a private
// Gateway symbol through an exported declaration and violate the boundary
// Gate.  This narrow port is intentionally limited to the operations this
// domain Adapter owns.
interface SessionDirectoryTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ): Promise<T>
  ready?(options?: {
    timeoutMs?: number
    signal?: AbortSignal
    timeoutAction?: 'reject' | 'reconnect'
    abortAction?: 'reject' | 'reconnect'
  }): Promise<void>
}

function rpcErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const candidate = error as {
    code?: unknown
    data?: { code?: unknown }
  }
  const code = candidate.code ?? candidate.data?.code
  return typeof code === 'string' ? code.toUpperCase() : ''
}

function sessionDirectoryError(error: unknown): SessionDirectoryError {
  if (error instanceof SessionDirectoryError) return error
  const code = rpcErrorCode(error)
  const message = error instanceof Error
    ? error.message
    : 'Session directory request failed'
  if (code === 'NOT_FOUND' || code === 'SESSION_NOT_FOUND') {
    return new SessionDirectoryError('not-found', message, { cause: error })
  }
  if (code === 'METHOD_NOT_FOUND' || code === 'UNSUPPORTED') {
    return new SessionDirectoryError('unsupported', message, { cause: error })
  }
  if (code === 'UNAUTHORIZED' || code === 'FORBIDDEN') {
    return new SessionDirectoryError('forbidden', message, { cause: error })
  }
  if (code === 'CONFLICT') {
    return new SessionDirectoryError('conflict', message, { cause: error })
  }
  if (code === 'INVALID_REQUEST' || code === 'INVALID_PARAMS') {
    return new SessionDirectoryError('invalid', message, { cause: error })
  }
  return new SessionDirectoryError('unavailable', message, { cause: error })
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || (error instanceof Error && error.name === 'AbortError')
}

const hasOwn = (obj: unknown, field: string) =>
  !!obj && Object.prototype.hasOwnProperty.call(obj, field)

function textValue(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

function numberValue(...values: unknown[]): number | null {
  for (const value of values) {
    if (value == null || value === '') continue
    const number = Number(value)
    if (Number.isFinite(number)) return number
  }
  return null
}

function objectValue(...values: unknown[]): Record<string, unknown> | null {
  const found = values.find(value => value && typeof value === 'object' && !Array.isArray(value))
  return (found as Record<string, unknown> | undefined) || null
}

function keyAgentId(key: string): string {
  const [kind, agentId] = key.split(':')
  return kind === 'agent' && agentId ? agentId : ''
}

function channelKind(row: SessionRow): string {
  return textValue(row.channelKind, row.channel_kind, row.lastChannel, row.last_channel)
}

function classifySession(row: SessionRow, key: string) {
  const source = textValue(row.sourceKind, row.source_kind).toLowerCase()
  const channel = channelKind(row).toLowerCase()
  const chatType = textValue(row.chatType, row.chat_type).toLowerCase()
  const web = key.includes(':webchat:') || chatType === 'webchat' || source === 'webui' || channel === 'webchat'
  const cron = key.startsWith('cron:') || key.includes(':cron:') || source === 'cron' || channel === 'cron'
  const task = key.includes(':subagent:') || source === 'subagent' || channel === 'subagent'
  const cli = key.includes(':cli:') || key.includes(':standalone:') || source === 'cli' || channel === 'cli'
  const external = source === 'channel' || (!!channel && !['cli', 'subagent', 'standalone'].includes(channel))
  const sessionKind = textValue(row.sessionKind)
    || (web ? 'chat' : cron ? 'cron' : external ? 'channel' : task ? 'task' : cli ? 'chat' : 'unknown')
  const conversationKind = textValue(row.conversationKind)
    || (web ? 'direct' : cron ? 'unknown' : external ? chatType || 'group' : task || cli ? 'internal' : 'unknown')
  const fallbackSurface = sessionKind === 'chat' && key.includes(':webchat:') ? 'webchat'
    : sessionKind === 'cron' ? channel || source || 'cron'
      : sessionKind === 'channel' ? channel || source || 'channel'
        : task ? 'subagent' : cli ? 'cli' : source || channel || 'unknown'
  return { sessionKind, conversationKind, surface: textValue(row.surface) || fallbackSurface }
}

function deriveGroupLabel(row: SessionRow, sessionKind: string, agentId: string): string {
  const explicit = textValue(row.groupLabel)
  if (explicit) return explicit
  if (sessionKind === 'chat') return agentId || 'main'
  if (sessionKind === 'cron') {
    const cron = objectValue(row.cron)
    return textValue(cron?.name, cron?.jobId, cron?.id, row.subject)
      || i18n.global.t('sessions.kindLabel.cron')
  }
  if (sessionKind === 'channel') {
    const channel = channelKind(row) || i18n.global.t('sessions.kindLabel.channel')
    const context = objectValue(row.deliveryContext, row.delivery_context)
    const target = textValue(
      row.lastTo, row.last_to, row.channelId, row.channel_id,
      context?.channel_id, context?.thread_id,
      row.groupId, row.group_id,
    )
    return target ? `${channel} / ${target}` : channel
  }
  return i18n.global.t('sessions.group.operational')
}

function fallbackSessionTitle(row: SessionRow, key: string, sessionKind: string): string {
  const semantic = textValue(
    row.display_name, row.displayName, row.subject, row.derived_title, row.derivedTitle,
  )
  if (semantic) return semantic
  if (sessionKind === 'chat') return i18n.global.t('sessions.fallbackTitle.chat')
  if (sessionKind === 'cron') return textValue(row.subject) || i18n.global.t('sessions.fallbackTitle.cron')
  if (sessionKind === 'channel') return textValue(row.subject) || i18n.global.t('sessions.fallbackTitle.channel')
  if (sessionKind === 'task') return i18n.global.t('sessions.fallbackTitle.task')
  return key || i18n.global.t('sessions.fallbackTitle.untitled')
}

function normalizeParent(row: SessionRow): SessionItem['parent'] {
  const parent = objectValue(row.parent)
  const key = textValue(parent?.key, row.parentSessionKey, row.parent_session_key)
  const spawnDepth = numberValue(
    parent?.spawnDepth, row.spawnDepth, row.spawn_depth,
  ) ?? 0
  return key || spawnDepth > 0 ? { key, spawnDepth } : null
}

export function normalizeV4SessionItem(item: unknown): SessionItem | null {
  const candidate = typeof item === 'string' ? { key: item } : objectValue(item)
  if (!candidate) return null
  const row = candidate as SessionRow
  const key = typeof item === 'string'
    ? item
    : textValue(candidate.key, candidate.session, candidate.sessionKey)
  if (!key || key === 'unknown') return null

  const derivedAgentId = textValue(row.effectiveAgentId, row.agentId, row.agent_id)
    || keyAgentId(key)
    || 'unknown'
  const { sessionKind, conversationKind, surface } = classifySession(row, key)
  const groupLabel = deriveGroupLabel(row, sessionKind, derivedAgentId)
  const workspace = textValue(row.workspace)
  const workspaceId = textValue(row.workspaceId, row.workspace_id)
  let title = textValue(row.title)
  if (!title) title = fallbackSessionTitle(row, key, sessionKind)
  if (
    sessionKind === 'task'
    && (/^you are a subagent\b/i.test(title) || title.trim().toLowerCase() === 'subagent task')
  ) title = i18n.global.t('sessions.fallbackTitle.task')

  const subtitle = hasOwn(row, 'subtitle') ? textValue(row.subtitle) : ''
  const messageCount = numberValue(row.messageCount, row.message_count, row.entry_count)
  const updatedAt = numberValue(row.lastActivityAt, row.last_activity_at, row.updatedAt, row.updated_at) ?? 0

  const activeTask = objectValue(row.active_task)
  const lastTaskSource = objectValue(row.last_task)
  const lastTask = summarizeSessionTask(lastTaskSource)
  const runStatus = resolveSessionRunStatus(
    textValue(row.runStatus, row.run_status),
    textValue(activeTask?.status),
    textValue(row.terminal_status, row.terminalStatus, lastTaskSource?.status),
  )
  const status = textValue(row.status) || 'unknown'

  return {
    key,
    title,
    subtitle,
    groupLabel,
    workspace: workspace || undefined,
    workspaceId: workspaceId || undefined,
    workspaceLabel: textValue(row.workspaceLabel) || undefined,
    workspaceDisplayPath: textValue(row.workspaceDisplayPath) || undefined,
    effectiveAgentId: derivedAgentId,
    sessionKind,
    surface,
    conversationKind,
    status,
    runStatus: normalizeSessionRunStatus(runStatus),
    runLabel: sessionRunStatusLabel(runStatus, lastTask),
    messageCount,
    updatedAt,
    model: textValue(row.model),
    parent: normalizeParent(row),
    forkedFromParent: row.forkedFromParent === true || row.forked_from_parent === true,
    hasContractGaps: [
      'title', 'subtitle', 'effectiveAgentId', 'sessionKind', 'surface',
      'conversationKind', 'messageCount', 'updatedAt', 'runStatus',
    ].some(field => !hasOwn(row, field)),
  }
}

export function createV4SessionDirectory(
  transport: SessionDirectoryTransport,
): SessionDirectory {
  async function requestWithPolicy<T>(
    method: string,
    params: Record<string, unknown>,
    signal: AbortSignal | undefined,
    abortMessage: string,
  ): Promise<T> {
    const options = signal ? { ...SESSION_DIRECTORY_CALL_OPTIONS, signal } : SESSION_DIRECTORY_CALL_OPTIONS
    await transport.ready?.({ ...options, timeoutAction: 'reject', abortAction: 'reject' })
    if (signal?.aborted) throw signal.reason || new Error(abortMessage)
    return transport.request<T>(method, params, options)
  }

  async function call(params: SessionsListParams, signal?: AbortSignal) {
    const result = await requestWithPolicy<Partial<SessionsListResult>>(
      SESSIONS_LIST_METHOD,
      params,
      signal,
      'Session directory request aborted',
    )
    return objectValue(result) as Partial<SessionsListResult> || {}
  }

  return {
    async listPage(request): Promise<SessionPage> {
      const params: SessionsListParams = {
        limit: request.limit,
        view: SESSION_LIST_VIEW,
      }
      if (request.cursor) params.cursor = request.cursor
      const result = await call(params, request.signal)
      const entries = Array.isArray(result.sessions)
        ? result.sessions
        : Array.isArray(result.keys) ? result.keys : []
      const items = entries
        .map(normalizeV4SessionItem)
        .filter((item): item is SessionItem => item !== null)
      const nextCursor = textValue(result.nextCursor, result.next_cursor) || null
      return {
        items,
        hasMore: (result.hasMore ?? result.has_more) === true,
        nextCursor,
      }
    },

    async count(options = {}): Promise<SessionCount | null> {
      const result = await call(
        { limit: 200, view: SESSION_COUNT_VIEW },
        options.signal,
      )
      const exact = numberValue(result.totalCount, result.total_count)
      if (exact != null && Number.isInteger(exact) && exact >= 0) return { value: exact, exact: true }
      const entries = Array.isArray(result.sessions)
        ? result.sessions
        : Array.isArray(result.keys) ? result.keys : null
      if (!entries) return null
      const value = entries.reduce(
        (count, entry) => count + (normalizeV4SessionItem(entry) ? 1 : 0),
        0,
      )
      return { value, exact: false }
    },

    async resolve(request): Promise<ResolvedSession> {
      const params: SessionsResolveParams = { key: request.key }
      try {
        const raw = await requestWithPolicy<SessionsResolveResult>(
          SESSIONS_RESOLVE_METHOD,
          params,
          request.signal,
          'Session resolution request aborted',
        )
        if (!validateSessionsResolveResult(raw)) throw new SessionDirectoryError(
          'unavailable', 'sessions.resolve returned an invalid response',
        )
        const result = raw
        if (
          typeof result.session_key !== 'string'
          || typeof result.session_id !== 'string'
        ) {
          throw new SessionDirectoryError(
            'unavailable',
            'sessions.resolve returned an invalid response',
          )
        }
        return {
          key: result.session_key,
          id: result.session_id,
        }
      } catch (error) {
        if (isAbort(error, request.signal)) throw error
        throw sessionDirectoryError(error)
      }
    },

    async search(request: SessionSearchRequest): Promise<SessionSearchResult> {
      try {
        const params: SessionsSearchParams = { query: request.query }
        if (request.limit !== undefined) params.limit = request.limit
        const raw = await requestWithPolicy<SessionsSearchWireResult>(
          SESSIONS_SEARCH_METHOD,
          params,
          request.signal,
          'Session search request aborted',
        )
        if (!validateSessionsSearchResult(raw)) throw new SessionDirectoryError(
          'unavailable', 'sessions.search returned an invalid response',
        )
        const wire = raw
        return {
          sessions: wire.sessions.map(({ key, title, surface }) => ({ key, title, surface })),
          messages: wire.messages.map(({ key, title, snippet, createdAt }) => ({
            key, title, snippet, createdAt: numberValue(createdAt),
          })),
        }
      } catch (error) {
        if (isAbort(error, request.signal)) throw error
        throw sessionDirectoryError(error)
      }
    },
  }
}
