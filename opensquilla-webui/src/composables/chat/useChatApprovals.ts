import { computed, ref, watch, type Ref } from 'vue'
import type { ChatRunStatus } from '@/types/chat'
import type { ApprovalStatusPayload, ToolResultPayload } from '@/types/rpc'
import type { RpcEventHandler } from '@/lib/rpc'
import type {
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
} from '@/types/parts'
import { isCurrentSessionPayload } from '@/utils/chat/streamEvents'

const MAX_RESOLVED_OUTCOMES = 4

// The chat approval poll is gone: approvals stream in as interrupt frames, and
// the snapshot fetch is a one-shot hydration on subscribe / session-switch /
// reconnect (recovers approvals that predate the socket and backfills
// args/warning). Setting `opensquilla.chat.approvalPoll` to '1' restores the old
// 2s interval as a recovery fallback (resolve-from-another-client self-healing).
const APPROVAL_POLL_INTERVAL_MS = 2000

// Seconds an Extend click pushes the approval deadline out by (mirrors the
// backend default re-arm window).
const APPROVAL_EXTEND_SECONDS = 300

/** Format a whole-second remaining count as a compact `m:ss` / `s` countdown.
 *  Negative inputs clamp to 0. Pure so the card's countdown can be unit-tested. */
export function formatCountdown(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  if (total < 60) return `${total}s`
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}:${String(secs).padStart(2, '0')}`
}

function approvalPollEnabled(): boolean {
  try {
    return localStorage.getItem('opensquilla.chat.approvalPoll') === '1'
  } catch {
    return false
  }
}

export interface ChatApprovalItem {
  id: string
  namespace: string
  toolName: string
  command: string
  approvalKind: string
  args: Record<string, unknown> | null
  warning: string
  agent: string
  sessionKey: string
  deadline: number          // epoch seconds the request expires; 0 when unknown
}

export type ChatApprovalResolution = 'approved' | 'denied' | 'expired' | 'unavailable'

export interface ChatApprovalEntry {
  approval: ChatApprovalItem
  resolution: ChatApprovalResolution | null
  error: string
}

export type ChatApprovalDecision = 'allow-once' | 'allow-always' | 'deny'

export function approvalChoiceForDecision(decision: ChatApprovalDecision): string {
  if (decision === 'allow-once') return 'allow_once'
  if (decision === 'allow-always') return 'allow_same_type'
  return 'deny'
}

export interface ApprovalResolveBody {
  id: string
  namespace: string
  approved: boolean
  choice: string
}

/**
 * Build the `*.approval.resolve` POST body. A plain approve carries only
 * {id, namespace, approved, choice} — the removed persistent "allow always"
 * path no longer contributes allowAlways/rememberIntent (the gateway now
 * rejects a truthy value), so a sandbox "allow same type" is expressed through
 * `choice` alone.
 */
export function buildApprovalResolveBody(
  id: string,
  namespace: string,
  decision: ChatApprovalDecision,
): ApprovalResolveBody {
  return {
    id,
    namespace: namespace || 'exec',
    approved: decision !== 'deny',
    choice: approvalChoiceForDecision(decision),
  }
}

export interface ChatClarifyField {
  name: string
  prompt: string
  type: string
  required: boolean
  defaultValue: string
  choices: string[]
}

export interface ChatClarifyRequest {
  intro: string
  fields: ChatClarifyField[]
  runId: string
  step: string
}

interface ApprovalsSnapshotItem {
  id?: string
  namespace?: string
  toolName?: string
  pluginId?: string
  actionKind?: string
  approvalKind?: string
  command?: string
  argv?: unknown
  args?: Record<string, unknown> | null
  params?: Record<string, unknown>
  warning?: string
  agent?: string
  sessionKey?: string
  deadline?: number
}

interface ApprovalsSnapshotResponse {
  pending?: ApprovalsSnapshotItem[]
  mode?: string
}

export interface ApprovalResolveResponse {
  approved?: boolean
  resolved?: boolean
  pending?: boolean
  resolution?: string
  resolutionInProgress?: boolean
}

/**
 * The `*.approval.requested|resolved` push payload (build_approval_event_payload).
 * A subset of the snapshot: it carries identity + command but omits `args`,
 * `warning`, `argv`, and `actionKind`, which the hydration fetch backfills.
 */
interface ApprovalPushPayload {
  approval_id?: string
  approvalId?: string
  namespace?: string
  session_key?: string
  sessionKey?: string
  tool_name?: string
  toolName?: string
  command?: string
  approval_kind?: string
  approvalKind?: string
  args?: Record<string, unknown> | null
  warning?: string
  agent?: string
  approved?: boolean
  resolution?: string
  deadline?: number
}

type ApprovalsRpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  on: (event: string, handler: RpcEventHandler) => () => void
}

/**
 * The slice of the live-turn stream the approvals composable drives: it appends
 * interrupt frames into the turn log and opens a render bubble for approvals that
 * arrive with no live turn streaming.
 */
export interface ApprovalsStreamSurface {
  isStreaming: Ref<boolean>
  appendInterruptFrame: (input: {
    interruptKind: 'approval' | 'clarify'
    approvalId: string
    data: InterruptApprovalData | InterruptClarifyData
    at: number
  }) => void
  ensureInterruptBubble: () => void
}

export interface UseChatApprovalsOptions {
  rpc: ApprovalsRpcClient
  sessionKey: Ref<string>
  runStatus: Ref<ChatRunStatus>
  /** The live-turn stream surface that hosts interrupt frames. */
  stream: ApprovalsStreamSurface
  /** The resolution side-map the fold reads to stamp each interrupt part. Shared
   *  with the stream (which threads it into the turn log); this composable is its
   *  sole writer. */
  interruptState: Ref<ReadonlyMap<string, InterruptViewState>>
  /** Deliver a deny note back to the agent through the normal send/queue path. */
  onDenyFeedback?: (note: string) => void
  /** Mirror the gateway-wide pending count (topbar pill / nav badge). */
  onSnapshotCount?: (count: number) => void
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra }
  let token = ''
  try { token = sessionStorage.getItem('opensquilla.wsToken') || '' } catch { /* ignore */ }
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

const SENSITIVE_DISPLAY_KEY = /(authorization|cookie|fingerprint|password|review.?action|secret|session.?(?:key|id)|token)/i
const INTERNAL_DISPLAY_KEY = /^(action|actions|choice|choices|params|policy|reviewer)$/i

function sanitizeDisplayValue(value: unknown, depth = 0): unknown {
  if (value == null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value
  if (depth >= 2) return undefined
  if (Array.isArray(value)) {
    return value.slice(0, 20).map(item => sanitizeDisplayValue(item, depth + 1)).filter(item => item !== undefined)
  }
  if (typeof value !== 'object') return undefined
  const safe: Record<string, unknown> = {}
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (SENSITIVE_DISPLAY_KEY.test(key) || INTERNAL_DISPLAY_KEY.test(key)) continue
    const normalized = sanitizeDisplayValue(item, depth + 1)
    if (normalized !== undefined) safe[key] = normalized
  }
  return safe
}

export function safeApprovalDisplayArgs(kind: string, source: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!source) return null
  if (kind === 'sandbox_path') {
    return pickDisplayArgs(source, ['path', 'access', 'workspace'])
  }
  if (kind === 'sandbox_network') {
    return pickDisplayArgs(source, ['host', 'bundle_id', 'workspace'])
  }
  // Other sandbox approval kinds may carry canonical policy actions and review
  // fingerprints. They have no approved browser display projection.
  if (kind.startsWith('sandbox_')) return null
  const safe = sanitizeDisplayValue(source)
  return safe && typeof safe === 'object' && !Array.isArray(safe)
    ? safe as Record<string, unknown>
    : null
}

function legacySnapshotDisplayArgs(
  approvalKind: string,
  rawArgs: Record<string, unknown> | null,
  params: Record<string, unknown> | null,
): Record<string, unknown> | null {
  if (rawArgs) return rawArgs
  if (!params) return null
  if (approvalKind === 'sandbox_path' || approvalKind === 'sandbox_network') return params
  if (params.args && typeof params.args === 'object' && !Array.isArray(params.args)) {
    return params.args as Record<string, unknown>
  }
  if (Object.prototype.hasOwnProperty.call(params, 'permissions')) {
    return { permissions: params.permissions }
  }
  return null
}

function pickDisplayArgs(source: Record<string, unknown>, keys: string[]): Record<string, unknown> | null {
  const selected: Record<string, unknown> = {}
  for (const key of keys) {
    const value = source[key]
    if (value != null && ['string', 'number', 'boolean'].includes(typeof value)) selected[key] = value
  }
  return Object.keys(selected).length ? selected : null
}

function approvalDisplayName(toolName: unknown, approvalKind: string): string {
  const explicit = String(toolName || '').trim()
  if (explicit && !/^unknown(?: tool)?$/i.test(explicit)) return explicit
  if (approvalKind) return approvalKind.replace(/_/g, ' ')
  return 'Approval'
}

function snapshotItemToApproval(item: ApprovalsSnapshotItem): ChatApprovalItem | null {
  const id = String(item.id || '').trim()
  if (!id) return null
  let command = String(item.command || '')
  if (!command && Array.isArray(item.argv) && item.argv.length > 0) {
    command = item.argv.map(String).join(' ')
  }
  const rawArgs = item.args && typeof item.args === 'object' ? item.args : null
  const params = item.params && typeof item.params === 'object' ? item.params : null
  const approvalKind = String(
    item.approvalKind
    || params?.approvalKind
    || params?.approval_kind
    || rawArgs?.approvalKind
    || rawArgs?.approval_kind
    || '',
  ).trim()
  const args = safeApprovalDisplayArgs(
    approvalKind,
    legacySnapshotDisplayArgs(approvalKind, rawArgs, params),
  )
  if (!command && rawArgs && typeof rawArgs.command === 'string') command = rawArgs.command
  return {
    id,
    namespace: String(item.namespace || 'exec'),
    toolName: approvalDisplayName(item.toolName || item.pluginId || item.actionKind, approvalKind),
    command,
    approvalKind,
    args,
    warning: String(item.warning || ''),
    agent: String(item.agent || ''),
    sessionKey: String(item.sessionKey || ''),
    deadline: Number(item.deadline) || 0,
  }
}

/** ChatApprovalItem → InterruptApprovalData (rename id→approvalId; identical
 *  otherwise). Lets the hydration fetch reuse snapshotItemToApproval and still
 *  emit the frame payload shape with args/warning populated. */
function approvalItemToInterruptData(item: ChatApprovalItem): InterruptApprovalData {
  return {
    approvalId: item.id,
    namespace: item.namespace,
    toolName: item.toolName,
    command: item.command,
    approvalKind: item.approvalKind,
    args: item.args,
    warning: item.warning,
    agent: item.agent,
    sessionKey: item.sessionKey,
    deadline: item.deadline,
  }
}

/** Build InterruptApprovalData from the lean `*.approval.requested` push payload.
 *  `args`/`warning` are absent on the wire (see build_approval_event_payload) and
 *  are backfilled by the hydration fetch; command + tool name still render. */
function pushPayloadToInterruptData(payload: ApprovalPushPayload): InterruptApprovalData | null {
  const approvalId = String(payload.approval_id || payload.approvalId || '').trim()
  if (!approvalId) return null
  const approvalKind = String(payload.approval_kind || payload.approvalKind || '')
  return {
    approvalId,
    namespace: String(payload.namespace || 'exec'),
    toolName: approvalDisplayName(payload.tool_name || payload.toolName, approvalKind),
    command: String(payload.command || ''),
    approvalKind,
    args: safeApprovalDisplayArgs(approvalKind, payload.args && typeof payload.args === 'object' ? payload.args : null),
    warning: String(payload.warning || ''),
    agent: String(payload.agent || ''),
    sessionKey: String(payload.session_key || payload.sessionKey || ''),
    deadline: Number(payload.deadline) || 0,
  }
}

/** Map a resolved `*.approval.resolved` push to an inline resolution state.
 *  `resolution: 'expired'` distinguishes a lapsed-deadline request from an
 *  explicit human deny so the card reads "Expired — not run" apart from
 *  "Denied"; older payloads without the field fall back to approved/denied. */
export function resolutionFromPayload(payload: ApprovalPushPayload): ChatApprovalResolution {
  if (payload.resolution === 'expired') return 'expired'
  return payload.approved === false ? 'denied' : 'approved'
}

/**
 * Read the canonical result returned by `*.approval.resolve`.
 *
 * A cross-surface loser can receive a still-pending response while the winning
 * surface finishes applying sandbox side effects. In that state the caller must
 * keep the approval open and must not present its own click as the outcome. Once
 * resolved, the Gateway's `approved` field is authoritative even when it is the
 * opposite of the local decision.
 */
export function resolutionFromResolveResponse(
  payload: ApprovalResolveResponse,
): ChatApprovalResolution | null {
  if (payload.pending === true || payload.resolutionInProgress === true) return null
  if (payload.resolved !== true || typeof payload.approved !== 'boolean') return null
  if (payload.resolution === 'expired') return 'expired'
  return payload.approved ? 'approved' : 'denied'
}

function parseClarifyRequest(payload: ToolResultPayload): ChatClarifyRequest | null {
  const rawArgs = (payload as Record<string, unknown>).arguments
  if (!rawArgs || typeof rawArgs !== 'object') return null
  const args = rawArgs as Record<string, unknown>
  if (args.kind !== 'user_input' || args.paused !== true) return null
  const schema = args.clarify_schema
  if (!schema || typeof schema !== 'object') return null
  const schemaObj = schema as Record<string, unknown>
  const rawFields = Array.isArray(schemaObj.fields) ? schemaObj.fields : []
  const fields: ChatClarifyField[] = []
  for (const raw of rawFields) {
    if (!raw || typeof raw !== 'object') continue
    const field = raw as Record<string, unknown>
    const name = String(field.name || '').trim()
    if (!name) continue
    fields.push({
      name,
      prompt: String(field.prompt || ''),
      type: String(field.type || 'string').toLowerCase(),
      required: field.required === true,
      defaultValue: field.default == null ? '' : String(field.default),
      choices: Array.isArray(field.choices) ? field.choices.map(String) : [],
    })
  }
  if (fields.length === 0) return null
  return {
    intro: String(schemaObj.intro || ''),
    fields,
    runId: typeof args.run_id === 'string' ? args.run_id : '',
    step: typeof args.step === 'string' ? args.step : '',
  }
}

/**
 * In-thread approvals and clarify requests for the current chat session.
 *
 * Approvals: the gateway pushes `exec.approval.requested` / `.resolved`
 * (and the plugin namespace equivalents) the moment a run blocks or a
 * decision lands; each push triggers an immediate snapshot refresh so the
 * in-thread card appears without waiting on the poll. While the run is
 * blocked on approval (or unresolved cards are on screen) the snapshot is
 * still polled every ~2s as a fallback and filtered to this session.
 * Resolution goes through the existing HTTP resolve endpoint; resolved
 * cards collapse into one-line outcome rows.
 *
 * Clarify: the engine surfaces a pending clarify form as a tool_result whose
 * arguments carry `kind: "user_input", paused: true, clarify_schema`; the
 * card state is derived from that stream event and submitted back through
 * the `chat.clarify_submit` RPC.
 */
export function useChatApprovals(options: UseChatApprovalsOptions) {
  const { rpc, sessionKey, stream, interruptState } = options

  const approvalEntries = ref<ChatApprovalEntry[]>([])
  const approvalBusyIds = ref<Set<string>>(new Set())
  const pendingClarify = ref<ChatClarifyRequest | null>(null)
  const clarifySubmitted = ref(false)
  const clarifyBusy = ref(false)
  const clarifyError = ref('')

  // Resolution view-state for inline interrupt parts is the shared `interruptState`
  // ref (keyed by approval id, or the clarify composite key). The fold reads it to
  // stamp each part's resolution/busy/error, mirroring how toolTimes is a side-map.
  // Frames stay append-only; optimistic resolution and resolve-from-elsewhere both
  // flow through here.
  function setInterruptState(id: string, patch: Partial<InterruptViewState>) {
    const next = new Map(interruptState.value)
    const prev = next.get(id) ?? { resolution: null, busy: false, error: '' }
    next.set(id, { ...prev, ...patch })
    interruptState.value = next
  }

  // The resolve endpoint wants the approval's namespace, which the part itself no
  // longer carries by the time the user clicks. Remember it per approval id when
  // the frame is appended so resolveInterrupt can recover it without the entries
  // list (which the hydration-only path no longer populates).
  const interruptNamespaces = new Map<string, string>()

  // Last-seen approval data per id, so extendInterrupt can re-append a frame
  // carrying the bumped deadline (the fold merges it onto the existing part) and
  // the countdown re-arms live without a snapshot round-trip.
  const interruptApprovals = new Map<string, InterruptApprovalData>()

  // The clarify frame is keyed by a runId|step composite (a clarify has no
  // approval id); arg-less clarifies fall back to a stable per-session key.
  function clarifyFrameKey(request: ChatClarifyRequest): string {
    const composite = `${request.runId}|${request.step}`
    return composite === '|' ? `clarify:${sessionKey.value}` : composite
  }

  let pollTimer: ReturnType<typeof setInterval> | null = null
  let fetchInFlight = false
  let refetchQueued = false
  let statusGeneration = 0
  let statusRpcUnavailable = false
  let statusRpcWarningShown = false
  const legacyPushBackfills = new Set<string>()

  const hasUnresolvedApproval = computed(() =>
    approvalEntries.value.some(entry => !entry.resolution))

  function syncSnapshot(pending: ApprovalsSnapshotItem[]) {
    const sessionItems = pending
      .map(snapshotItemToApproval)
      .filter((item): item is ChatApprovalItem =>
        item !== null && !!sessionKey.value && item.sessionKey === sessionKey.value)
    let next = approvalEntries.value.slice()
    const knownIds = new Map(next.map((entry, index) => [entry.approval.id, index]))
    for (const item of sessionItems) {
      const existingIndex = knownIds.get(item.id)
      if (existingIndex == null) {
        next = [...next, { approval: item, resolution: null, error: '' }]
        knownIds.set(item.id, next.length - 1)
      } else if (next[existingIndex].resolution === null) {
        next[existingIndex] = { ...next[existingIndex], approval: item }
      }
    }
    // Cap how many collapsed outcome rows linger in the thread.
    const resolved = next.filter(entry => entry.resolution !== null)
    if (resolved.length > MAX_RESOLVED_OUTCOMES) {
      const dropCount = resolved.length - MAX_RESOLVED_OUTCOMES
      const dropIds = new Set(resolved.slice(0, dropCount).map(entry => entry.approval.id))
      next = next.filter(entry => !dropIds.has(entry.approval.id))
    }
    approvalEntries.value = next

    // Surface every pending item as an interrupt frame too: this is the hydration
    // path that recovers approvals which predate the socket (reload / queued turn)
    // and backfills args/warning the lean push payload omits. The fold dedups by
    // approvalId, so re-appending a known id merges the richer snapshot fields
    // (args/warning) onto the existing part rather than duplicating it.
    for (const item of sessionItems) {
      const state = interruptState.value.get(item.id)
      if (state?.resolution) continue
      appendApprovalInterrupt(approvalItemToInterruptData(item))
    }
  }

  async function fetchSnapshot() {
    if (fetchInFlight) {
      // A push event landed mid-fetch; the in-flight response may predate
      // it, so run one more fetch when the current one settles.
      refetchQueued = true
      return
    }
    fetchInFlight = true
    try {
      const res = await fetch('/api/approvals', { headers: authHeaders() })
      if (!res.ok) throw new Error('HTTP ' + res.status)
      const data = await res.json() as ApprovalsSnapshotResponse
      const pending = data.pending || []
      options.onSnapshotCount?.(pending.length)
      syncSnapshot(pending)
    } catch (err) {
      console.warn('Approvals snapshot failed: ' + (err instanceof Error ? err.message : String(err)))
    } finally {
      fetchInFlight = false
      if (refetchQueued) {
        refetchQueued = false
        void fetchSnapshot()
      }
    }
  }

  function stopFallbackPoll() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // Hydrate once now; arm the 2s recovery interval only when the opt-in
  // `opensquilla.chat.approvalPoll` flag is set. Default behaviour is hydrate-
  // only — the stream carries new approvals, so no interval runs.
  function hydrateApprovals() {
    const hydration = fetchSnapshot()
    if (approvalPollEnabled() && !pollTimer) {
      pollTimer = setInterval(() => { void fetchSnapshot() }, APPROVAL_POLL_INTERVAL_MS)
    }
    return hydration
  }

  function setApprovalBusy(id: string, busy: boolean) {
    const next = new Set(approvalBusyIds.value)
    if (busy) next.add(id)
    else next.delete(id)
    approvalBusyIds.value = next
  }

  function statusResolution(payload: ApprovalStatusPayload): ChatApprovalResolution | null {
    if (payload.found === false) return 'unavailable'
    if (payload.resolved !== true) return null
    if (payload.resolution === 'expired') return 'expired'
    if (payload.resolution === 'denied') return 'denied'
    if (payload.resolution === 'approved') return 'approved'
    return typeof payload.approved === 'boolean'
      ? payload.approved ? 'approved' : 'denied'
      : null
  }

  function isMethodNotFound(error: unknown): boolean {
    const candidate = error as { code?: unknown; message?: unknown } | null
    return candidate?.code === 'METHOD_NOT_FOUND'
      || /method not found/i.test(error instanceof Error ? error.message : String(candidate?.message || error))
  }

  function applyApprovalStatus(id: string, payload: ApprovalStatusPayload, generation: number) {
    if (generation !== statusGeneration) return
    const resolution = statusResolution(payload)
    const current = interruptState.value.get(id)
    // A terminal push or earlier status is monotonic: a stale pending/status
    // response must never reopen or replace a card that already settled.
    if (!current?.resolution) {
      if (resolution) setInterruptState(id, { resolution, busy: false, error: '' })
      else if (payload.resolutionInProgress === true) setInterruptState(id, { busy: true, error: '' })
      else if (payload.pending === true) setInterruptState(id, { busy: false, error: '' })
    }

    const entry = approvalEntries.value.find(candidate => candidate.approval.id === id)
    if (entry && entry.resolution === null && resolution) entry.resolution = resolution
    const keepBusy = !resolution && payload.resolutionInProgress === true
    setApprovalBusy(id, keepBusy)
    if (payload.deadline && interruptApprovals.has(id)) {
      const known = interruptApprovals.get(id)!
      appendApprovalInterrupt({ ...known, deadline: Number(payload.deadline) || known.deadline })
    }
  }

  async function fetchApprovalStatus(
    id: string,
    namespace: string,
    generation = statusGeneration,
  ): Promise<ApprovalStatusPayload | null> {
    if (statusRpcUnavailable || !id) return null
    try {
      const payload = await rpc.call<ApprovalStatusPayload>(
        `${namespace || 'exec'}.approval.status`,
        { id },
      )
      applyApprovalStatus(id, payload || { found: false }, generation)
      return payload || { found: false }
    } catch (error) {
      if (isMethodNotFound(error)) {
        statusRpcUnavailable = true
        if (!statusRpcWarningShown) {
          statusRpcWarningShown = true
          console.warn('Approval status recovery is unavailable on this Gateway; retaining snapshot state.')
        }
      }
      return null
    }
  }

  async function reconcileLocalApprovalStatuses(generation: number) {
    if (generation !== statusGeneration || statusRpcUnavailable) return
    const pending = [...interruptApprovals.values()].filter(item =>
      !interruptState.value.get(item.approvalId)?.resolution)
    await Promise.all(pending.map(item =>
      fetchApprovalStatus(item.approvalId, item.namespace, generation)))
  }

  async function resolveApproval(entry: ChatApprovalEntry, decision: ChatApprovalDecision, note = '') {
    const id = entry.approval.id
    if (approvalBusyIds.value.has(id) || entry.resolution) return
    setApprovalBusy(id, true)
    entry.error = ''
    const body = buildApprovalResolveBody(id, entry.approval.namespace, decision)
    try {
      const res = await fetch('/api/approvals/resolve', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('HTTP ' + res.status)
      const result = await res.json() as ApprovalResolveResponse
      const resolution = resolutionFromResolveResponse(result)
      if (resolution !== null) entry.resolution = resolution
      else await fetchApprovalStatus(id, entry.approval.namespace)
      if (resolution === 'denied' && decision === 'deny' && note.trim()) {
        options.onDenyFeedback?.(note.trim())
      }
    } catch (err) {
      entry.error = 'Could not resolve — ' + (err instanceof Error ? err.message : String(err))
    } finally {
      if (!interruptState.value.get(id)?.busy) setApprovalBusy(id, false)
    }
  }

  /**
   * Resolve an inline interrupt part. Reuses the same resolve POST body and the
   * same idempotency guard as resolveApproval (busy or already-resolved is a
   * no-op), driving the optimistic, append-only `interruptState` side-map instead
   * of a card entry. A deny note rides the normal send/queue path via
   * onDenyFeedback, exactly as the legacy card does.
   */
  async function resolveInterrupt(id: string, decision: ChatApprovalDecision, note = '') {
    const current = interruptState.value.get(id)
    if (approvalBusyIds.value.has(id) || current?.resolution) return
    setApprovalBusy(id, true)
    setInterruptState(id, { busy: true, error: '' })
    const body = buildApprovalResolveBody(id, namespaceForInterrupt(id), decision)
    try {
      const res = await fetch('/api/approvals/resolve', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error('HTTP ' + res.status)
      const result = await res.json() as ApprovalResolveResponse
      const resolution = resolutionFromResolveResponse(result)
      if (resolution) setInterruptState(id, { resolution, busy: false })
      else await fetchApprovalStatus(id, namespaceForInterrupt(id))
      if (resolution === 'denied' && decision === 'deny' && note.trim()) {
        options.onDenyFeedback?.(note.trim())
      }
    } catch (err) {
      setInterruptState(id, {
        busy: false,
        error: 'Could not resolve — ' + (err instanceof Error ? err.message : String(err)),
      })
    } finally {
      if (!interruptState.value.get(id)?.busy) setApprovalBusy(id, false)
    }
  }

  /**
   * Push an inline approval's deadline out (WCAG 2.2.1 extend mechanism). Calls
   * the `<namespace>.approval.extend` RPC and re-appends the frame with the
   * bumped deadline so the countdown re-arms live (the fold merges by id). A
   * busy or already-resolved request is a no-op.
   */
  async function extendInterrupt(id: string, seconds = APPROVAL_EXTEND_SECONDS) {
    const current = interruptState.value.get(id)
    if (approvalBusyIds.value.has(id) || current?.resolution) return
    setApprovalBusy(id, true)
    setInterruptState(id, { busy: true, error: '' })
    try {
      const result = await rpc.call<{ deadline?: number }>(
        `${namespaceForInterrupt(id)}.approval.extend`,
        { id, seconds },
      )
      const deadline = Number(result?.deadline) || 0
      const known = interruptApprovals.get(id)
      if (deadline > 0 && known) {
        appendApprovalInterrupt({ ...known, deadline })
      }
      setInterruptState(id, { busy: false })
    } catch (err) {
      setInterruptState(id, {
        busy: false,
        error: 'Could not extend — ' + (err instanceof Error ? err.message : String(err)),
      })
    } finally {
      setApprovalBusy(id, false)
    }
  }

  // The resolve endpoint wants the approval's namespace; recover it from the
  // frame payload remembered at append time, else default to 'exec'.
  function namespaceForInterrupt(id: string): string {
    return interruptNamespaces.get(id) || 'exec'
  }

  // Append an approval interrupt frame onto the live turn, opening a render
  // bubble first when no turn is streaming (queued/background/reload). Remembers
  // the namespace for resolve, seeds an empty interruptState entry, and dedups in
  // the fold by approvalId — so a re-broadcast or hydration backfill merges richer
  // args/warning rather than duplicating the part.
  function appendApprovalInterrupt(data: InterruptApprovalData) {
    interruptNamespaces.set(data.approvalId, data.namespace)
    // A lean push (or backfill) may omit the deadline (0); keep the latest
    // known non-zero deadline so a countdown never regresses to "unknown".
    const prior = interruptApprovals.get(data.approvalId)
    const merged: InterruptApprovalData = {
      ...data,
      deadline: data.deadline || prior?.deadline || 0,
    }
    interruptApprovals.set(merged.approvalId, merged)
    if (!interruptState.value.has(merged.approvalId)) {
      setInterruptState(merged.approvalId, {})
    }
    if (!stream.isStreaming.value) stream.ensureInterruptBubble()
    stream.appendInterruptFrame({
      interruptKind: 'approval',
      approvalId: merged.approvalId,
      data: merged,
      at: Date.now(),
    })
  }

  function handleToolResult(payload: ToolResultPayload) {
    if (!payload || typeof payload !== 'object') return
    if (!isCurrentSessionPayload(payload, sessionKey.value)) return
    const request = parseClarifyRequest(payload)
    if (!request) return
    pendingClarify.value = request
    clarifySubmitted.value = false
    clarifyError.value = ''
    // Mirror the clarify into the turn log so it folds into an inline interrupt
    // part. The clarify keeps no approval id, so the runId|step composite keys it.
    const clarifyData: InterruptClarifyData = {
      intro: request.intro,
      fields: request.fields,
      runId: request.runId,
      step: request.step,
    }
    const key = clarifyFrameKey(request)
    if (!interruptState.value.has(key)) setInterruptState(key, {})
    if (!stream.isStreaming.value) stream.ensureInterruptBubble()
    stream.appendInterruptFrame({
      interruptKind: 'clarify',
      approvalId: key,
      data: clarifyData,
      at: Date.now(),
    })
  }

  /**
   * `*.approval.requested` push: build an interrupt frame straight from the push
   * payload (no snapshot round-trip) so the inline part appears immediately. The
   * lean push omits args/warning; those are backfilled by the one-shot hydration
   * on subscribe (and by the opt-in recovery interval), and rendered from the
   * source once the backend enriches the payload — command + tool name render
   * from the push alone meanwhile.
   */
  function handleApprovalRequested(payload: ApprovalPushPayload) {
    const data = pushPayloadToInterruptData(payload)
    if (data && (!sessionKey.value || data.sessionKey === sessionKey.value)) {
      appendApprovalInterrupt(data)
      const hasDisplayArgs = Object.prototype.hasOwnProperty.call(payload, 'args')
      const hasWarning = Object.prototype.hasOwnProperty.call(payload, 'warning')
      // New Gateways always include both additive fields, including the explicit
      // null/empty values. Only old lean pushes require a snapshot backfill.
      if ((!hasDisplayArgs || !hasWarning) && !legacyPushBackfills.has(data.approvalId)) {
        legacyPushBackfills.add(data.approvalId)
        void fetchSnapshot()
      }
    }
  }

  /**
   * `*.approval.resolved` push: stamp `interruptState` from `payload.approved` so
   * a decision landing elsewhere (another client) collapses
   * the inline part here too. No snapshot fetch — the push carries the outcome.
   */
  function handleApprovalResolved(payload: ApprovalPushPayload) {
    const id = String(payload.approval_id || payload.approvalId || '').trim()
    if (id && !interruptState.value.get(id)?.resolution) {
      setInterruptState(id, {
        resolution: resolutionFromPayload(payload),
        busy: false,
      })
      setApprovalBusy(id, false)
      const entry = approvalEntries.value.find(candidate => candidate.approval.id === id)
      if (entry && entry.resolution === null) entry.resolution = resolutionFromPayload(payload)
    }
  }

  // Reconnect recovers approvals that arrived while the socket was down: a fresh
  // hydration re-surfaces still-pending items as frames (deduped by the fold).
  function handleConnectionState(state: unknown) {
    if (state !== 'connected') return
    const generation = ++statusGeneration
    void (async () => {
      await hydrateApprovals()
      await reconcileLocalApprovalStatuses(generation)
    })()
  }

  /** Register stream listeners; returns the unsubscribe function. */
  function subscribe(): () => void {
    const unsubs = [
      rpc.on('session.event.tool_result', handleToolResult as RpcEventHandler),
      rpc.on('exec.approval.requested', handleApprovalRequested as RpcEventHandler),
      rpc.on('exec.approval.resolved', handleApprovalResolved as RpcEventHandler),
      rpc.on('plugin.approval.requested', handleApprovalRequested as RpcEventHandler),
      rpc.on('plugin.approval.resolved', handleApprovalResolved as RpcEventHandler),
      rpc.on('_state', handleConnectionState as RpcEventHandler),
    ]
    // One-shot hydration on subscribe recovers any approval already pending
    // before the listeners attached.
    hydrateApprovals()
    return () => {
      unsubs.forEach(unsub => unsub())
      stopFallbackPoll()
    }
  }

  async function submitClarify(
    fields: Record<string, string | boolean>,
    requestOverride?: ChatClarifyRequest,
  ) {
    const request = requestOverride || pendingClarify.value
    if (clarifyBusy.value || !request) return
    const key = clarifyFrameKey(request)
    if (interruptState.value.get(key)?.resolution === 'replied') return
    if (!requestOverride && clarifySubmitted.value) return
    clarifyBusy.value = true
    clarifySubmitted.value = true
    clarifyError.value = ''
    setInterruptState(key, { resolution: 'replied', busy: true, error: '' })
    const params: Record<string, unknown> = { sessionKey: sessionKey.value, fields }
    if (request.runId) params.run_id = request.runId
    try {
      await rpc.call('chat.clarify_submit', params)
      setInterruptState(key, { resolution: 'replied', busy: false })
    } catch (err) {
      const message = 'Send failed — ' + (err instanceof Error ? err.message : String(err))
      clarifySubmitted.value = false
      clarifyError.value = message
      setInterruptState(key, { resolution: null, busy: false, error: message })
    } finally {
      clarifyBusy.value = false
    }
  }

  function dismissClarify() {
    pendingClarify.value = null
    clarifySubmitted.value = false
    clarifyError.value = ''
  }

  // Session switches reset all in-thread card state; a one-shot hydration
  // recovers approvals that were already pending (e.g. reload mid-approval) and
  // re-arms the opt-in recovery interval for the new session.
  watch(sessionKey, key => {
    statusGeneration++
    stopFallbackPoll()
    approvalEntries.value = []
    approvalBusyIds.value = new Set()
    interruptState.value = new Map()
    interruptNamespaces.clear()
    interruptApprovals.clear()
    legacyPushBackfills.clear()
    dismissClarify()
    if (key) hydrateApprovals()
  }, { immediate: true })

  function cleanup() {
    statusGeneration++
    stopFallbackPoll()
  }

  return {
    approvalEntries,
    approvalBusyIds,
    hasUnresolvedApproval,
    pendingClarify,
    clarifySubmitted,
    clarifyBusy,
    clarifyError,
    resolveApproval,
    resolveInterrupt,
    extendInterrupt,
    submitClarify,
    dismissClarify,
    subscribe,
    cleanup,
  }
}
