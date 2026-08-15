// Shared types for the OpenSquilla desktop pet backend.
//
// The pet renderer (pet/pet.html) consumes OpenSquilla pet's frontend contract
// (pet:stats / pet:event / pet:config). These types mirror that contract so
// the TypeScript backend can build payloads the renderer already understands.

/** Core session state words (from shared/states.js). */
export type CoreState =
  | 'working'
  | 'thinking'
  | 'juggling'
  | 'sweeping'
  | 'carrying'
  | 'error'
  | 'notification'
  | 'attention'
  | 'idle'
  | 'roam'
  | 'sleeping'

/** Frontend state word (adapter-mapped superset). */
export type PetState =
  | CoreState
  | 'loafing'
  | 'happy'
  | 'waiting'
  | 'needsinput'
  | 'greet'
  | 'talking'
  | 'loved'
  | 'sad'
  | 'sorry'
  | 'excited'
  | 'puzzled'

/** A session row in the core store. */
export interface PetSession {
  id: string
  agentId: string
  state: CoreState
  badge: string
  cwd: string
  headless: boolean
  sessionTitle: string | null
  model: string | null
  sessionRole: string | null
  travelAgent: string | null
  contextUsage: { used: number; limit?: number; percent?: number } | null
  assistantLastOutput: string | null
  assistantLastOutputTruncated: boolean
  requiresCompletionAck: boolean
  lastEvent: { rawEvent: string | null; at: number } | null
  lastEventTool: string | null
  updatedAt: number
  idleMs: number
  sourcePid: number | null
}

/** Full snapshot from the core store. */
export interface PetSnapshot {
  sessions: PetSession[]
  active: { sessionId: string; project: string; model: string | null; lastActivity: number } | null
  idleMs: number | null
  lastActivityTs: number
  ts: number
}

/** One approval pending card (from exec.approval.* events). */
export interface PendingApproval {
  id: string
  namespace: string
  sessionKey: string
  toolName: string
  command: string
  approvalKind: string
  agent: string
  args: Record<string, unknown> | null
  warning: string
  createdAt: number
  deadline: number | null
}

/** Permission card the renderer renders (built by adapter). */
export interface PermChoice {
  kind: 'perm' | 'plan' | 'ask' | 'continue'
  sessionId: string
  permId: string
  project: string
  header: string
  question: string
  options: Array<{ label: string; key: string; desc?: string }>
  multi: boolean
  allowInput: boolean
  travel?: boolean
  questions?: Array<{ header: string; question: string; options?: Array<{ label: string; description?: string }>; multiSelect?: boolean }>
}

/** Discrete event pushed to the renderer (pet:event). */
export interface PetEvent {
  kind:
    | 'greet'
    | 'user-turn'
    | 'operation'
    | 'error'
    | 'big-done'
    | 'turn-done'
    | 'say'
    | 'needsinput'
    | 'waiting'
    | 'loot'
    | 'territory'
  project: string
  ts: number
  agent?: string
  emotion?: string | null
  tool?: string
  icon?: string
  detail?: string
  file?: string
  text?: string
  errorType?: string | null
  reason?: string
  sessionId?: string
  choice?: PermChoice
  ops?: number
  phase?: string
  direction?: number
  rival?: string
  count?: number
}

/** pet:stats snapshot pushed to the renderer. */
export interface PetStats {
  today: { input: number; output: number; cacheCreate: number; cacheRead: number; tokens: number; cost: number; messages: number }
  window5h: { tokens: number; cost: number; startTs: number; resetTs: number }
  byModel: Record<string, { tokens: number; cost: number }>
  lastOps: Array<{ tool: string; icon: string; detail: string; file: string; project: string; agent: string; ts: number }>
  active: { sessionId: string; project: string; model: string | null; lastActivity: number } | null
  sessions: Array<{
    project: string
    agent: string
    state: PetState
    reason: string | null
    idleMs: number
    updatedAt: number
    op: string | null
    sessionId: string
    headless: boolean
    sessionRole: string | null
    travelAgent: string | null
    badge: string
    model: string | null
    contextPercent: number | null
    choice: PermChoice | null
    todos: unknown[]
  }>
  waitingCount: number
  needsinputCount: number
  workingCount: number
  jugglingCount: number
  sweepingCount: number
  thinkingCount: number
  loafingCount: number
  errorCount: number
  todos: unknown[]
  todosProject: string
  hourly: number[]
  hourlyTok: number[]
  daily: Record<string, unknown>
  diagnostics: unknown
  lastActivityTs: number
  idleMs: number | null
  bg: { running: number; zombie: number; total: number; items: unknown[] }
  context: { percent: number | null; used: number; limit: number | null } | null
  codexLimits: unknown
  codexUsage: unknown
  usageProvider: string
  ts: number
}

/** Frontend config (pet:config). */
export interface PetConfig {
  mode: string
  skin: string
  petPosition: { x: number; y: number } | null
  budget5h: number
  muted: boolean
  permHook: string
  territory: boolean
  territorySupported: boolean
  agent: string
  petMode: string
  lang: string
  pinnedSessions: string[]
  archivedSessions: string[]
  anticsEnabled: boolean
  online: boolean
}

/** Normalized gateway event fed into the core store (produced by squilla-watch). */
export interface WatchUpdate {
  sid: string
  state: CoreState
  event: string
  fields?: Record<string, unknown>
}

/** Persisted pet preferences (pet-config.json). */
export interface PetPrefs {
  petPosition: { x: number; y: number } | null
  skin: string
  muted: boolean
  mode: string
  lang: string
  pinnedSessions: string[]
  archivedSessions: string[]
  petEnabled: boolean
  anticsEnabled: boolean
  travelLedger: Array<Record<string, unknown>>
  [key: string]: unknown
}
