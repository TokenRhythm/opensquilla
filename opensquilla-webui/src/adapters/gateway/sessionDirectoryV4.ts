import i18n from '@/i18n'
import type { RpcCallOptions } from '@/lib/rpc'
import {
  SESSIONS_LIST_METHOD,
  type SessionListEntry,
  type SessionRow,
  type SessionsListParams,
  type SessionsListResult,
} from '@/contracts/generated/v4/sessionsList'
import type {
  SessionCount,
  SessionDirectory,
  SessionItem,
  SessionPage,
} from '@/modules/sessionDirectory'
import {
  normalizeSessionRunStatus,
  resolveSessionRunStatus,
  sessionRunStatusLabel,
  summarizeSessionTask,
} from '@/modules/sessionRunStatus'
import type { RpcTransport } from './privateTransports'

const SESSION_LIST_VIEW = 'session-list-v1'
const SESSION_COUNT_VIEW = 'session-count-v1'
const SESSION_DIRECTORY_TIMEOUT_MS = 10_000
const SESSION_DIRECTORY_CALL_OPTIONS: RpcCallOptions = {
  timeoutMs: SESSION_DIRECTORY_TIMEOUT_MS,
  timeoutAction: 'reconnect',
  abortAction: 'reject',
}

type SessionDirectoryTransport = Pick<RpcTransport, 'request'>
  & Partial<Pick<RpcTransport, 'ready'>>

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

export function normalizeV4SessionItem(item: SessionListEntry | unknown): SessionItem | null {
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
  async function call(params: SessionsListParams, signal?: AbortSignal) {
    const options = signal
      ? { ...SESSION_DIRECTORY_CALL_OPTIONS, signal }
      : SESSION_DIRECTORY_CALL_OPTIONS
    await transport.ready?.(
      {
        timeoutMs: options.timeoutMs,
        signal: options.signal,
        timeoutAction: 'reject', abortAction: 'reject',
      },
    )
    if (signal?.aborted) throw signal.reason || new Error('Session directory request aborted')
    const result = await transport.request(SESSIONS_LIST_METHOD, params, options)
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

    async count(): Promise<SessionCount | null> {
      const result = await call({ limit: 200, view: SESSION_COUNT_VIEW })
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
  }
}
