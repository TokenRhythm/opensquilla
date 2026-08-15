// PetAdapter — internal core model → renderer contract.
//
// Ported from OpenSquilla pet backend/adapter.js. Maps core session states to the
// frontend's state words, overlays pending approvals as 'waiting'/'needsinput'
// sessions, synthesizes counts, and derives discrete pet:event(s) from the
// activity stream. OpenSquilla tool names are normalized to the same tool
// vocabulary the renderer already styles.

import path from 'node:path'
import { petT } from './i18n.js'
import { PetSnapshot, PetStats, PetEvent, PermChoice, PendingApproval, PetSession } from './types.js'

// OpenSquilla tool name → OpenSquilla pet tool word (icons + i18n keys key off the word).
const TOOL_MAP: Record<string, string> = {
  bash: 'Bash',
  shell: 'Bash',
  exec: 'Bash',
  edit: 'Edit',
  apply_patch: 'Edit',
  patch: 'Edit',
  write: 'Write',
  notebook_edit: 'NotebookEdit',
  read: 'Read',
  view: 'Read',
  grep: 'Grep',
  glob: 'Glob',
  web_search: 'WebSearch',
  websearch: 'WebSearch',
  web_fetch: 'WebFetch',
  webfetch: 'WebFetch',
  task: 'Task',
  agent: 'Task',
  spawn_agent: 'Task',
  todo_write: 'TodoWrite',
  todo: 'TodoWrite',
}
const TOOL_ICON: Record<string, string> = {
  Edit: '📝', MultiEdit: '📝', Write: '📝', NotebookEdit: '📝',
  Read: '📖', Bash: '⚙️', Grep: '🔍', Glob: '🔍',
  WebSearch: '🌐', WebFetch: '🌐', Task: '🤖', Agent: '🤖',
  TodoWrite: '✅', Js: '🧮', Wait: '⏳',
}
const TOOL_LABEL_KEY: Record<string, string> = {
  Edit: 'Edit', MultiEdit: 'Edit', Write: 'Write', NotebookEdit: 'NotebookEdit',
  Read: 'Read', Bash: 'Bash', Grep: 'Grep', Glob: 'Glob',
  WebSearch: 'WebSearch', WebFetch: 'WebFetch', Task: 'Task', Agent: 'Task',
  TodoWrite: 'TodoWrite', Js: 'Js', Wait: 'Wait',
}

export function agentOf(_entry: unknown): string {
  return 'squilla'
}

function normTool(name: string): string {
  if (!name) return 'Tool'
  const lower = String(name).toLowerCase()
  return TOOL_MAP[lower] || (lower.startsWith('mcp_') ? 'Tool' : String(name) || 'Tool')
}

export function toolIcon(tool: string): string {
  return TOOL_ICON[normTool(tool)] || '🔧'
}
export function toolLabel(tool: string): string {
  const word = normTool(tool)
  const key = TOOL_LABEL_KEY[word]
  if (key) return petT('tool.' + key)
  return word || petT('tool.default')
}

const TOOL_EVENTS = new Set<string>(['PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop'])
const LOAF_GAP_MS = 5000

function errorMessage(type: string | null): string {
  switch (type) {
    case 'rate_limit': return petT('err.rateLimit')
    case 'server_error':
    case 'overloaded_error':
    case 'overloaded':
    case 'api_error': return petT('err.server')
    case 'billing_error': return petT('err.billing')
    case 'authentication_failed': return petT('err.auth')
    case 'model_not_found': return petT('err.model')
    case 'max_output_tokens': return petT('err.maxTokens')
    default: return petT('err.default')
  }
}

export function projectName(entry: PetSession | null | undefined): string {
  if (!entry) return petT('sess.fallbackName')
  if (entry.sessionRole === 'travel') return petT('travel.sessionName', { who: 'OpenSquilla' })
  if (entry.sessionTitle) return entry.sessionTitle
  if (entry.cwd) return path.basename(entry.cwd) || entry.cwd
  return String(entry.id || '').slice(-6) || petT('sess.fallbackName')
}

function clip(s: string, n: number): string {
  const str = String(s || '').replace(/\s+/g, ' ').trim()
  return str.length > n ? str.slice(0, n - 1) + '…' : str
}

function plainText(s: string): string {
  return String(s || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|\s)[*_]([^*_]+)[*_]/g, '$1$2')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}[>\-*]\s+/gm, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
}

export function mapState(state: string): string {
  switch (state) {
    case 'working':
    case 'carrying':
      return 'working'
    case 'juggling':
      return 'juggling'
    case 'sweeping':
      return 'sweeping'
    case 'thinking':
      return 'thinking'
    case 'error':
      return 'error'
    case 'notification':
      return 'needsinput'
    case 'sleeping':
      return 'sleeping'
    case 'attention':
    case 'idle':
    case 'roam':
    default:
      return 'idle'
  }
}

function humanizeTool(toolName: string, input: Record<string, unknown> | null): string {
  const i = input && typeof input === 'object' ? input : {}
  const word = normTool(toolName)
  switch (word) {
    case 'Bash':
      return petT('perm.runCommand') + clip(String(i.command ?? i.cmd ?? ''), 80)
    case 'Edit':
    case 'MultiEdit':
    case 'Write':
    case 'NotebookEdit':
      return petT('perm.editFile') + clip(String(i.file_path ?? i.path ?? ''), 60)
    case 'Read':
      return petT('perm.readFile') + clip(String(i.file_path ?? i.path ?? ''), 60)
    case 'WebFetch':
      return petT('perm.fetchUrl') + clip(String(i.url ?? ''), 60)
    case 'WebSearch':
      return petT('perm.webSearch') + clip(String(i.query ?? ''), 60)
    default:
      return clip(toolName, 40) + petT('perm.needsApproval')
  }
}

export function buildPermChoice(perm: PendingApproval, entry: PetSession | null): PermChoice {
  const action = perm.command
    ? clip(perm.command, 90)
    : humanizeTool(perm.toolName, (perm.args as Record<string, unknown>) || null)
  return {
    kind: 'perm',
    sessionId: perm.sessionKey,
    permId: perm.id,
    project: entry ? projectName(entry) : perm.sessionKey || '?',
    header: perm.toolName || 'OpenSquilla',
    question: action,
    options: [
      { label: petT('perm.allow'), key: 'allow' },
      { label: petT('perm.deny'), key: 'deny' },
    ],
    multi: false,
    allowInput: false,
  }
}

export function buildContinueChoice(entry: PetSession): PermChoice {
  return {
    kind: 'continue',
    sessionId: entry.id,
    permId: '',
    project: projectName(entry),
    header: '',
    question: entry.assistantLastOutput ? clip(entry.assistantLastOutput, 120) : petT('perm.continueQuestion', { who: 'OpenSquilla' }),
    options: [],
    multi: false,
    allowInput: false,
  }
}

export interface BuildPetStatsOptions {
  lastOps?: Array<Record<string, unknown>>
}

export function buildPetStats(
  snapshot: PetSnapshot,
  pendingPermissions: PendingApproval[],
  metering: { today?: Record<string, number>; window5h?: Record<string, number>; byModel?: Record<string, unknown>; hourly?: number[]; hourlyTok?: number[]; daily?: Record<string, unknown>; diagnostics?: unknown } | null,
  opts: BuildPetStatsOptions = {},
): PetStats {
  const permsBySession = new Map<string, PendingApproval>()
  for (const p of pendingPermissions || []) {
    if (!permsBySession.has(p.sessionKey)) permsBySession.set(p.sessionKey, p)
  }

  const entries = snapshot.sessions || []
  const sessions: PetStats['sessions'] = entries.map((e) => {
    let state: PetStats['sessions'][number]['state'] = mapState(e.state) as PetStats['sessions'][number]['state']
    let reason: string | null = null
    let choice: PermChoice | null = null

    if (e.agentId !== 'codex' && state === 'working'
      && e.lastEvent && (e.lastEvent.rawEvent === 'PostToolUse' || e.lastEvent.rawEvent === 'SubagentStop')
      && e.idleMs > LOAF_GAP_MS) {
      state = 'loafing'
    }

    const perm = permsBySession.get(e.id)
    if (perm && !e.headless) {
      state = 'waiting'
      reason = 'perm'
      choice = buildPermChoice(perm, e)
    } else if (e.state === 'notification' && !e.headless) {
      state = 'needsinput'
      reason = 'reply'
      choice = buildContinueChoice(e)
    }

    return {
      project: projectName(e),
      agent: agentOf(e),
      state,
      reason,
      idleMs: e.idleMs,
      updatedAt: e.updatedAt || 0,
      op: (state === 'working' || state === 'juggling' || state === 'sweeping')
        && e.lastEvent && TOOL_EVENTS.has(e.lastEvent.rawEvent || '')
        ? toolLabel(e.lastEventTool || '')
        : null,
      sessionId: e.id,
      headless: e.headless,
      sessionRole: e.sessionRole || null,
      travelAgent: e.travelAgent || null,
      badge: e.badge,
      model: e.model || null,
      contextPercent: e.contextUsage && typeof e.contextUsage.percent === 'number' ? e.contextUsage.percent : null,
      choice,
      todos: [],
    }
  })

  const counted = sessions.filter((s) => !s.headless)
  const count = (pred: (s: PetStats['sessions'][number]) => boolean) => counted.filter(pred).length

  let context: { percent: number | null; used: number; limit: number | null } | null = null
  const active = snapshot.active
  if (active) {
    const ae = entries.find((e) => e.id === active.sessionId)
    if (ae && ae.contextUsage) {
      context = {
        percent: typeof ae.contextUsage.percent === 'number' ? ae.contextUsage.percent : null,
        used: ae.contextUsage.used || 0,
        limit: ae.contextUsage.limit || null,
      }
    }
  }

  const m = metering || {}
  const today = m.today || { input: 0, output: 0, tokens: 0, cost: 0, messages: 0 }
  const todayOut = {
    input: today.input || 0,
    output: today.output || 0,
    cacheCreate: today.cacheCreate || 0,
    cacheRead: today.cacheRead || 0,
    tokens: today.tokens || 0,
    cost: today.cost || 0,
    messages: today.messages != null ? today.messages : (today.msgs || 0),
  }

  let activeOut = snapshot.active
  if (activeOut && activeOut.project) {
    activeOut = { ...activeOut, project: path.basename(activeOut.project) || activeOut.project }
  }

  return {
    today: todayOut,
    window5h: (m.window5h as PetStats['window5h']) || { tokens: 0, cost: 0, startTs: 0, resetTs: 0 },
    byModel: (m.byModel as Record<string, { tokens: number; cost: number }>) || {},
    lastOps: (opts.lastOps as PetStats['lastOps']) || [],
    active: activeOut,
    sessions,
    waitingCount: count((s) => s.state === 'waiting'),
    needsinputCount: count((s) => s.state === 'needsinput'),
    workingCount: count((s) => s.state === 'working'),
    jugglingCount: count((s) => s.state === 'juggling'),
    sweepingCount: count((s) => s.state === 'sweeping'),
    thinkingCount: count((s) => s.state === 'thinking'),
    loafingCount: count((s) => s.state === 'loafing'),
    errorCount: count((s) => s.state === 'error'),
    todos: [],
    todosProject: '',
    hourly: m.hourly || new Array(24).fill(0),
    hourlyTok: m.hourlyTok || new Array(24).fill(0),
    daily: m.daily || {},
    diagnostics: m.diagnostics || null,
    lastActivityTs: snapshot.lastActivityTs || 0,
    idleMs: snapshot.idleMs,
    bg: { running: 0, zombie: 0, total: 0, items: [] },
    context,
    codexLimits: null,
    codexUsage: null,
    usageProvider: 'squilla',
    ts: snapshot.ts,
  }
}

const GREET_DEBOUNCE_MS = 30 * 60 * 1000
const lastGreetAt = new Map<string, number>()

export interface Activity {
  session: PetSession
  event: string | null
  prevState: string
  newState: string
  isNew: boolean
  realCompletion: boolean
  assistantChanged: boolean
}

export function activityToEvents(act: Activity): PetEvent[] {
  const { session, event, isNew, realCompletion, assistantChanged } = act
  if (!session || session.headless) return []
  const project = projectName(session)
  const out: PetEvent[] = []

  switch (event) {
    case 'SessionStart': {
      ;(session as any).greetPending = isNew ? Date.now() : null
      break
    }
    case 'UserPromptSubmit': {
      const pendingAt = (session as any).greetPending || 0
      const recentlyGreeted = (Date.now() - (lastGreetAt.get(project) || 0)) < GREET_DEBOUNCE_MS
      ;(session as any).greetPending = null
      if (pendingAt && Date.now() - pendingAt < 5 * 60 * 1000 && !recentlyGreeted) {
        lastGreetAt.set(project, Date.now())
        out.push({ kind: 'greet', project, ts: Date.now() })
        break
      }
      out.push({ kind: 'user-turn', project, ts: Date.now() })
      break
    }
    case 'PreToolUse': {
      const tool = session.lastEventTool || ''
      out.push({ kind: 'operation', tool, icon: toolIcon(tool), detail: toolLabel(tool), file: '', project, ts: Date.now() })
      break
    }
    case 'SubagentStart':
      out.push({ kind: 'operation', tool: 'Task', icon: toolIcon('Task'), detail: toolLabel('Task'), file: '', project, ts: Date.now() })
      break
    case 'PostToolUseFailure':
    case 'StopFailure':
    case 'ApiError': {
      const et = (session as any).errorType || null
      out.push({ kind: 'error', project, errorType: et, text: errorMessage(et), ts: Date.now() })
      break
    }
    case 'Stop':
      if (realCompletion) {
        const ops = countRecentOps(session)
        out.push({ kind: ops >= 5 ? 'big-done' : 'turn-done', project, ops, ts: Date.now() })
      }
      if (assistantChanged && session.assistantLastOutput) {
        out.push({ kind: 'say', text: clip(plainText(session.assistantLastOutput), 280), project, ts: Date.now() })
      }
      break
    case 'Notification':
      out.push({
        kind: 'needsinput',
        project,
        reason: 'reply',
        sessionId: session.id,
        choice: buildContinueChoice({ ...session, id: session.id }),
        ts: Date.now(),
      })
      break
    default:
      break
  }
  for (const ev of out) ev.agent = agentOf(session)
  return out
}

function countRecentOps(session: PetSession): number {
  const ev = (session as any).recentEvents || []
  let n = 0
  for (let i = ev.length - 1; i >= 0; i--) {
    const e = ev[i]
    if (e.event === 'UserPromptSubmit') break
    if (e.event === 'PreToolUse' || e.event === 'PostToolUse' || e.event === 'SubagentStart') n++
  }
  return n
}
