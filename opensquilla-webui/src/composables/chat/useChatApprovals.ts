import { computed, ref, watch, type Ref } from 'vue'
import type { ChatRunStatus } from '@/types/chat'
import type { ApprovalStatusPayload, ToolResultPayload } from '@/types/chat'
import type {
  InterruptApprovalData,
  InterruptClarifyData,
  InterruptViewState,
} from '@/types/parts'
import { clarifyRequestFromValue, userInputOutcomeFromValue } from '@/utils/chat/clarify'
import {
  conversationCursorSignal,
  isCurrentSessionPayload,
  isStaleEpoch,
  type TaskTerminalStatus,
} from '@/utils/chat/streamEvents'
import type {
  ApprovalCenter,
  ApprovalAvailability,
  ApprovalEvent,
  ApprovalItem,
  ApprovalDecision,
} from '@/modules/approvalCenter'
import type { SessionConversation } from '@/modules/sessionConversation'

const MAX_RESOLVED_OUTCOMES = 4

// The chat approval poll is gone: approvals stream in as interrupt frames, and
// the snapshot fetch is a one-shot hydration on subscribe / session-switch /
// reconnect (recovers approvals that predate the socket and backfills
// args/warning). Setting `opensquilla.chat.approvalPoll` to '1' restores the old
// 2s interval as a recovery fallback (resolve-from-another-client self-healing).
const APPROVAL_POLL_INTERVAL_MS = 2000

// Legacy compatibility for explicitly timed approvals from older Gateways.
// Current human approval cards have no deadline and expose no Extend control.
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

export type ChatApprovalItem = ApprovalItem

export type ChatApprovalResolution = 'approved' | 'denied' | 'expired' | 'unavailable'

export interface ChatApprovalEntry {
  approval: ChatApprovalItem
  resolution: ChatApprovalResolution | null
  error: string
}

export type ChatApprovalDecision = ApprovalDecision

export interface ChatClarifyField {
  name: string
  prompt: string
  type: string
  required: boolean
  defaultValue: string
  choices: string[]
  header?: string
  options?: Array<{ label: string; description: string }>
  allowOther?: boolean
}

export interface ChatClarifyRequest {
  intro: string
  fields: ChatClarifyField[]
  presentation?: string
  requestId?: string
  runId: string
  step: string
}

interface ActiveClarifyRequest {
  request: ChatClarifyRequest
  observedStreamSeq?: number
  observedStreamGeneration?: string
}

interface ApprovalResolveResponse {
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
    activityOrder?: number
  }) => void
  ensureInterruptBubble: () => void
}

export interface UseChatApprovalsOptions {
  sessionConversation: SessionConversation
  approvalCenter: ApprovalCenter
  sessionKey: Ref<string>
  /** Current session epoch, used to reject replay from a retired reset. */
  currentEpoch?: Readonly<Ref<number>>
  /** Current transport cursor namespace, used to order reconnect hydration. */
  streamGeneration?: Readonly<Ref<string | null>>
  /** Atomically adopt a new transport namespace before appending its frame. */
  observeStreamGeneration?: (source: unknown) => boolean
  runStatus: Ref<ChatRunStatus>
  /** The live-turn stream surface that hosts interrupt frames. */
  stream: ApprovalsStreamSurface
  /** The resolution side-map the fold reads to stamp each interrupt part. Shared
   *  with the stream (which threads it into the turn log); this composable is its
   *  sole writer. */
  interruptState: Ref<ReadonlyMap<string, InterruptViewState>>
  /** Mirror the gateway-wide pending count (topbar pill / nav badge). */
  onSnapshotCount?: (count: number) => void
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
    displayKind: item.displayKind,
    displayTarget: item.displayTarget,
    destructive: item.destructive,
    irreversible: item.irreversible,
    backupState: item.backupState,
    agent: item.agent,
    sessionKey: item.sessionKey,
    deadline: item.deadline,
  }
}

/** Map a resolved `*.approval.resolved` push to an inline resolution state.
 *  `resolution: 'expired'` distinguishes a lapsed-deadline request from an
 *  explicit human deny so the card reads "Expired — not run" apart from
 *  "Denied"; older payloads without the field fall back to approved/denied. */
export function resolutionFromPayload(payload: { approved?: boolean; resolution?: string }): ChatApprovalResolution {
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
  return clarifyRequestFromValue(payload.result)
    ?? clarifyRequestFromValue((payload as Record<string, unknown>).arguments)
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
 * result JSON (or a legacy arguments payload) carries
 * `kind: "user_input", paused: true, clarify_schema`; the card state is
 * derived from that stream event and submitted through `chat.clarify_submit`.
 */
export function useChatApprovals(options: UseChatApprovalsOptions) {
  const { approvalCenter, sessionKey, stream, interruptState } = options
  const conversation = options.sessionConversation

  const approvalEntries = ref<ChatApprovalEntry[]>([])
  const approvalBusyIds = ref<Set<string>>(new Set())
  const pendingClarify = ref<ChatClarifyRequest | null>(null)
  const clarifySubmitted = ref(false)
  const clarifyBusy = ref(false)
  const clarifyError = ref('')
  // The dock presents one questionnaire at a time, but a task may have more
  // than one paused tool call represented by inline frames. Track every known
  // unresolved request so task settlement and reconnect reconciliation cannot
  // leave an older frame actionable.
  const activeClarifyRequests = new Map<string, ActiveClarifyRequest>()
  // A legacy Meta clarify resumes through a new chat.send turn. Its run_id is
  // a Meta-run identifier, not a TaskRuntime task id, so retain the exact task
  // returned by the successful RPC instead of comparing those ID domains.
  const retainedClarifyTaskOwners = new Map<string, string>()
  // A very short continuation can terminate before its chat.send acceptance
  // response reaches this client. Retain a bounded terminal ledger so the late
  // ACK cannot install a stale retained receipt.
  const terminalClarifyTaskIds = new Set<string>()
  let acceptedClarifyStreamGeneration = String(
    options.streamGeneration?.value || '',
  ).trim()
  const retiredClarifyStreamGenerations = new Set<string>()

  function rememberTerminalClarifyTask(taskId: string) {
    terminalClarifyTaskIds.delete(taskId)
    terminalClarifyTaskIds.add(taskId)
    if (terminalClarifyTaskIds.size <= 128) return
    const oldest = terminalClarifyTaskIds.values().next().value
    if (oldest) terminalClarifyTaskIds.delete(oldest)
  }

  function retireClarifyStreamGeneration(generation: string) {
    if (!generation) return
    retiredClarifyStreamGenerations.delete(generation)
    retiredClarifyStreamGenerations.add(generation)
    if (retiredClarifyStreamGenerations.size <= 16) return
    const oldest = retiredClarifyStreamGenerations.values().next().value
    if (oldest) retiredClarifyStreamGenerations.delete(oldest)
  }

  function syncClarifyStreamGenerationFromShared() {
    const shared = String(options.streamGeneration?.value || '').trim()
    if (
      !shared
      || shared === acceptedClarifyStreamGeneration
      || retiredClarifyStreamGenerations.has(shared)
    ) return
    retireClarifyStreamGeneration(acceptedClarifyStreamGeneration)
    acceptedClarifyStreamGeneration = shared
  }

  function acceptsClarifyStreamGeneration(source: unknown): boolean {
    const incoming = String(
      conversationCursorSignal(source).streamGeneration || '',
    ).trim()
    if (!incoming) return true

    // Exact RPC listeners run before the wildcard lane advances the shared
    // cursor. Treat the first unseen generation as the new namespace here so
    // its first questionnaire is not dropped, while remembering the prior
    // namespace so a late replay can never roll this ingress backwards.
    syncClarifyStreamGenerationFromShared()
    if (incoming === acceptedClarifyStreamGeneration) return true
    if (retiredClarifyStreamGenerations.has(incoming)) return false
    retireClarifyStreamGeneration(acceptedClarifyStreamGeneration)
    acceptedClarifyStreamGeneration = incoming
    // RpcClient dispatches exact listeners before the wildcard conversation
    // lane. Advance/reset the shared cursor now so that later wildcard handling
    // cannot clear the interrupt frame we are about to append.
    const observedSource = source && typeof source === 'object'
      ? {
          ...(source as Record<string, unknown>),
          stream_generation: incoming,
          streamGeneration: incoming,
        }
      : { streamGeneration: incoming }
    options.observeStreamGeneration?.(observedSource)
    return true
  }

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

  // Last-seen approval data per id. Deadline mutation remains compatible with
  // explicitly timed approvals from older Gateways, but current human cards use 0.
  const interruptApprovals = new Map<string, InterruptApprovalData>()

  // The clarify frame is keyed by a runId|step composite (a clarify has no
  // approval id); arg-less clarifies fall back to a stable per-session key.
  function clarifyFrameKey(request: ChatClarifyRequest): string {
    if (request.requestId) return request.requestId
    const composite = `${request.runId}|${request.step}`
    return composite === '|' ? `clarify:${sessionKey.value}` : composite
  }

  function resetClarifyPresentation() {
    clarifySubmitted.value = false
    clarifyBusy.value = false
    clarifyError.value = ''
  }

  function streamSeqFrom(value: Record<string, unknown>): number | undefined {
    const raw = value.stream_seq ?? value.streamSeq
    if (raw === null || raw === undefined || raw === '' || typeof raw === 'boolean') {
      return undefined
    }
    const sequence = Number(raw)
    return Number.isSafeInteger(sequence) && sequence >= 0 ? sequence : undefined
  }

  function streamGenerationFrom(value: Record<string, unknown>): string | undefined {
    const explicit = String(value.stream_generation ?? value.streamGeneration ?? '').trim()
    if (explicit) return explicit
    return acceptedClarifyStreamGeneration
      || String(options.streamGeneration?.value || '').trim()
      || undefined
  }

  function rememberActiveClarify(
    key: string,
    request: ChatClarifyRequest,
    observedStreamSeq?: number,
    observedStreamGeneration?: string,
  ) {
    const prior = activeClarifyRequests.get(key)
    const priorSequence = prior?.observedStreamSeq
    const priorGeneration = prior?.observedStreamGeneration
    const currentGeneration = acceptedClarifyStreamGeneration
      || String(options.streamGeneration?.value || '').trim()
    const priorIsCurrent = Boolean(
      currentGeneration && priorGeneration === currentGeneration,
    )
    const incomingIsCurrent = Boolean(
      currentGeneration && observedStreamGeneration === currentGeneration,
    )
    const sameGeneration = !priorGeneration
      || !observedStreamGeneration
      || priorGeneration === observedStreamGeneration
    const keepPrior = Boolean(prior) && (
      (priorIsCurrent && observedStreamGeneration && !incomingIsCurrent)
      || (
        sameGeneration
        && priorSequence !== undefined
        && observedStreamSeq !== undefined
        && priorSequence > observedStreamSeq
      )
    )
    const nextGeneration = keepPrior
      ? priorGeneration
      : observedStreamGeneration ?? priorGeneration
    const nextSequence = keepPrior
      ? priorSequence
      : sameGeneration
        ? priorSequence === undefined
          ? observedStreamSeq
          : observedStreamSeq === undefined
            ? priorSequence
            : Math.max(priorSequence, observedStreamSeq)
        : observedStreamSeq
    const active: ActiveClarifyRequest = {
      request: keepPrior && prior ? prior.request : request,
      ...(nextSequence !== undefined ? { observedStreamSeq: nextSequence } : {}),
      ...(nextGeneration ? { observedStreamGeneration: nextGeneration } : {}),
    }
    activeClarifyRequests.set(key, active)
    return active
  }

  function presentPendingClarify(request: ChatClarifyRequest, key: string) {
    pendingClarify.value = request
    const state = interruptState.value.get(key)
    clarifySubmitted.value = state?.resolution === 'replied'
    clarifyBusy.value = state?.busy === true
    clarifyError.value = state?.error || ''
  }

  function pendingClarifyMatches(key: string): boolean {
    return pendingClarify.value != null && clarifyFrameKey(pendingClarify.value) === key
  }

  /**
   * Remove only the request whose terminal state was just confirmed. This
   * identity guard prevents a delayed submit response from dismissing a newer
   * questionnaire that arrived while the earlier RPC was in flight.
   */
  function clearPendingClarify(key: string): boolean {
    if (!pendingClarifyMatches(key)) return false
    pendingClarify.value = null
    resetClarifyPresentation()
    return true
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

  function syncSnapshot(pending: readonly ApprovalItem[]) {
    const sessionItems = pending
      .filter(item => !!sessionKey.value && item.sessionKey === sessionKey.value)
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
      const data = await approvalCenter.snapshot()
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

  function applyApprovalDeadline(id: string, value: unknown) {
    const deadline = Number(value)
    if (!id || !Number.isFinite(deadline) || deadline <= 0) return
    if (interruptState.value.get(id)?.resolution) return
    const known = interruptApprovals.get(id)
    if (known) appendApprovalInterrupt({ ...known, deadline })
    approvalEntries.value = approvalEntries.value.map(entry => {
      if (entry.approval.id !== id || entry.resolution !== null) return entry
      return {
        ...entry,
        approval: {
          ...entry.approval,
          deadline,
        },
      }
    })
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
    applyApprovalDeadline(id, payload.deadline)
  }

  async function fetchApprovalStatus(
    id: string,
    namespace: string,
    generation = statusGeneration,
  ): Promise<ApprovalStatusPayload | null> {
    if (statusRpcUnavailable || !id) return null
    try {
      const status = await approvalCenter.status(
        namespace === 'plugin' ? 'plugin' : 'exec',
        id,
      )
      const payload = {
        ...status,
        deadline: status.deadline === null ? undefined : status.deadline,
      } as ApprovalStatusPayload
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

  async function resolveApproval(entry: ChatApprovalEntry, decision: ChatApprovalDecision) {
    const id = entry.approval.id
    if (approvalBusyIds.value.has(id) || entry.resolution) return
    setApprovalBusy(id, true)
    entry.error = ''
    try {
      const result = await approvalCenter.resolve({
        id,
        namespace: entry.approval.namespace,
        decision,
      })
      const resolution = resolutionFromResolveResponse(result)
      if (resolution !== null) entry.resolution = resolution
      else await fetchApprovalStatus(id, entry.approval.namespace)
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
   * of a card entry. A denial is terminal for this turn and never schedules a
   * follow-up user message.
   */
  async function resolveInterrupt(id: string, decision: ChatApprovalDecision) {
    const current = interruptState.value.get(id)
    if (approvalBusyIds.value.has(id) || current?.resolution) return
    setApprovalBusy(id, true)
    setInterruptState(id, { busy: true, error: '' })
    try {
      const result = await approvalCenter.resolve({
        id,
        namespace: namespaceForInterrupt(id) === 'plugin' ? 'plugin' : 'exec',
        decision,
      })
      const resolution = resolutionFromResolveResponse(result)
      if (resolution) setInterruptState(id, { resolution, busy: false })
      else await fetchApprovalStatus(id, namespaceForInterrupt(id))
    } catch (err) {
      setInterruptState(id, {
        busy: false,
        error: 'Could not resolve — ' + (err instanceof Error ? err.message : String(err)),
      })
    } finally {
      if (!interruptState.value.get(id)?.busy) setApprovalBusy(id, false)
    }
  }

  /** Compatibility path for explicitly timed approvals from older Gateways. */
  async function extendInterrupt(id: string, seconds = APPROVAL_EXTEND_SECONDS) {
    const current = interruptState.value.get(id)
    if (approvalBusyIds.value.has(id) || current?.resolution) return
    setApprovalBusy(id, true)
    setInterruptState(id, { busy: true, error: '' })
    try {
      const result = await approvalCenter.extend(
        namespaceForInterrupt(id) === 'plugin' ? 'plugin' : 'exec',
        id,
        seconds,
      )
      const deadline = Number(result?.deadline) || 0
      applyApprovalDeadline(id, deadline)
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
  function appendApprovalInterrupt(data: InterruptApprovalData, event?: ApprovalEvent) {
    interruptNamespaces.set(data.approvalId, data.namespace)
    // A lean push (or backfill) may omit the legacy deadline (0); keep any
    // explicit deadline already received for compatibility.
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
      at: event?.emittedAt || Date.now(),
      activityOrder: event?.activityOrder,
    })
  }

  function handleToolResult(payload: ToolResultPayload) {
    if (!payload || typeof payload !== 'object') return
    if (!isCurrentSessionPayload(payload, sessionKey.value)) return
    if (
      options.currentEpoch
      && isStaleEpoch(payload, options.currentEpoch.value)
    ) return
    if (!acceptsClarifyStreamGeneration(payload)) return
    const outcome = userInputOutcomeFromValue(payload.result)
    if (outcome) {
      activeClarifyRequests.delete(outcome.requestId)
      const priorResolution = interruptState.value.get(outcome.requestId)?.resolution
      setInterruptState(outcome.requestId, {
        // A positive acknowledgement dominates later expiry/cancellation
        // replays, while a late authoritative answer may upgrade unavailable.
        resolution: outcome.status === 'answered' || priorResolution === 'replied'
          ? 'replied'
          : 'unavailable',
        busy: false,
        error: '',
      })
      clearPendingClarify(outcome.requestId)
      return
    }
    const request = parseClarifyRequest(payload)
    if (!request) return
    const key = clarifyFrameKey(request)
    // Tool-result replay and reconnect delivery can surface the paused half
    // after its terminal outcome. Never resurrect an already-settled request.
    if (interruptState.value.get(key)?.resolution) return
    const payloadRecord = payload as Record<string, unknown>
    const active = rememberActiveClarify(
      key,
      request,
      streamSeqFrom(payloadRecord),
      streamGenerationFrom(payloadRecord),
    )
    // A duplicate paused result can race an in-flight submit. Re-select the
    // request from its own state so switching between multiple forms cannot
    // erase a request-scoped busy/error fence.
    presentPendingClarify(active.request, key)
    // Mirror the clarify into the turn log so it folds into an inline interrupt
    // part. The clarify keeps no approval id, so the runId|step composite keys it.
    const clarifyData: InterruptClarifyData = {
      intro: request.intro,
      fields: request.fields,
      ...(request.presentation ? { presentation: request.presentation } : {}),
      ...(request.requestId ? { requestId: request.requestId } : {}),
      runId: request.runId,
      step: request.step,
    }
    if (!interruptState.value.has(key)) setInterruptState(key, {})
    if (!stream.isStreaming.value) stream.ensureInterruptBubble()
    stream.appendInterruptFrame({
      interruptKind: 'clarify',
      approvalId: key,
      data: clarifyData,
      at: Number(
        (payload as Record<string, unknown>).emitted_at
        || (payload as Record<string, unknown>).started_at,
      ) || Date.now(),
      activityOrder: (
        Number.isSafeInteger((payload as Record<string, unknown>).stream_seq)
        && Number((payload as Record<string, unknown>).stream_seq) > 0
          ? Number((payload as Record<string, unknown>).stream_seq)
          : undefined
      ),
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
  function handleApprovalRequested(event: ApprovalEvent) {
    const data = event.approval ? approvalItemToInterruptData(event.approval) : null
    if (data && (!sessionKey.value || data.sessionKey === sessionKey.value)) {
      appendApprovalInterrupt(data, event)
      // New Gateways always include both additive fields, including the explicit
      // null/empty values. Only old lean pushes require a snapshot backfill.
      if (event.needsHydration && !legacyPushBackfills.has(data.approvalId)) {
        legacyPushBackfills.add(data.approvalId)
        void fetchSnapshot()
      }
    }
  }

  function handleApprovalUpdated(event: ApprovalEvent) {
    const id = event.approvalId
    if (!id || interruptState.value.get(id)?.resolution) return
    const data = event.approval ? approvalItemToInterruptData(event.approval) : null
    if (data) {
      if (sessionKey.value && data.sessionKey !== sessionKey.value) return
      appendApprovalInterrupt(data, event)
    }
    applyApprovalDeadline(id, event.approval?.deadline)
  }

  /**
   * `*.approval.resolved` push: stamp `interruptState` from `payload.approved` so
   * a decision landing elsewhere (another client) collapses
   * the inline part here too. No snapshot fetch — the push carries the outcome.
   */
  function handleApprovalResolved(event: ApprovalEvent) {
    const id = event.approvalId
    const resolution = event.resolution === 'expired'
      ? 'expired' : event.approved === false ? 'denied' : 'approved'
    if (id && !interruptState.value.get(id)?.resolution) {
      setInterruptState(id, {
        resolution,
        busy: false,
      })
      setApprovalBusy(id, false)
      const entry = approvalEntries.value.find(candidate => candidate.approval.id === id)
      if (entry && entry.resolution === null) entry.resolution = resolution
    }
  }

  // Reconnect recovers approvals that arrived while the socket was down: a fresh
  // hydration re-surfaces still-pending items as frames (deduped by the fold).
  function handleAvailability(state: ApprovalAvailability) {
    if (state !== 'available') return
    const generation = ++statusGeneration
    void (async () => {
      await hydrateApprovals()
      await reconcileLocalApprovalStatuses(generation)
    })()
  }

  /** Register stream listeners; returns the unsubscribe function. */
  function subscribe(): () => void {
    const toolResultSubscription = conversation.subscribeToolResults((payload) => {
      handleToolResult(payload as ToolResultPayload)
    })
    const approvalEvents = approvalCenter.subscribe(event => {
      if (event.kind === 'requested') handleApprovalRequested(event)
      else if (event.kind === 'updated') handleApprovalUpdated(event)
      else handleApprovalResolved(event)
    })
    const connection = approvalCenter.subscribeAvailability(handleAvailability)
    // One-shot hydration on subscribe recovers any approval already pending
    // before the listeners attached.
    hydrateApprovals()
    return () => {
      toolResultSubscription.close()
      approvalEvents.close()
      connection.close()
      stopFallbackPoll()
    }
  }

  async function submitClarify(
    fields: Record<string, string | boolean>,
    requestOverride?: ChatClarifyRequest,
  ) {
    const request = requestOverride || pendingClarify.value
    if (!request) return
    const key = clarifyFrameKey(request)
    const currentState = interruptState.value.get(key)
    if (currentState?.resolution || currentState?.busy) return
    if (!requestOverride && clarifySubmitted.value) return
    const submittedSessionKey = sessionKey.value
    const controlsPendingPresentation = pendingClarifyMatches(key)
    if (controlsPendingPresentation) {
      clarifyBusy.value = true
      clarifySubmitted.value = false
      clarifyError.value = ''
    }
    rememberActiveClarify(key, request, undefined, streamGenerationFrom({}))
    setInterruptState(key, { resolution: null, busy: true, error: '' })
    const params: Record<string, unknown> = { sessionKey: sessionKey.value, fields }
    if (request.requestId) params.request_id = request.requestId
    if (request.runId) params.run_id = request.runId
    try {
      const response = await conversation.submitClarify(params)
      if (submittedSessionKey !== sessionKey.value) return
      activeClarifyRequests.delete(key)
      let legacyOwnerAlreadyTerminal = false
      if (!request.requestId) {
        const acceptedTaskId = String(
          response.task_id
          ?? response.taskId
          ?? response.turn_id
          ?? response.turnId
          ?? '',
        ).trim()
        legacyOwnerAlreadyTerminal = Boolean(
          acceptedTaskId && terminalClarifyTaskIds.has(acceptedTaskId),
        )
        if (acceptedTaskId && !legacyOwnerAlreadyTerminal) {
          retainedClarifyTaskOwners.set(key, acceptedTaskId)
        }
      }
      setInterruptState(key, { resolution: 'replied', busy: false })
      if (pendingClarifyMatches(key)) clarifySubmitted.value = true
      // request_id submissions resolve the exact paused tool call in the same
      // turn. A successful RPC is therefore authoritative and can release the
      // dock/composer immediately. Legacy clarifications create a new chat turn
      // and intentionally retain their existing submitted receipt.
      if (request.requestId || legacyOwnerAlreadyTerminal) clearPendingClarify(key)
    } catch (err) {
      if (submittedSessionKey !== sessionKey.value) return
      const message = 'Send failed — ' + (err instanceof Error ? err.message : String(err))
      const stillPending = pendingClarifyMatches(key)
      // A terminal tool result or authoritative empty snapshot can win the
      // race with a rejected/lost RPC acknowledgement. Never reopen that
      // already-settled request from the late rejection.
      const terminalConfirmed = !stillPending
        && interruptState.value.get(key)?.resolution != null
      if (stillPending) {
        clarifySubmitted.value = false
        clarifyError.value = message
      }
      if (terminalConfirmed) {
        activeClarifyRequests.delete(key)
      } else {
        setInterruptState(key, { resolution: null, busy: false, error: message })
      }
    } finally {
      if (submittedSessionKey === sessionKey.value && pendingClarifyMatches(key)) {
        clarifyBusy.value = false
      }
    }
  }

  function dismissClarify() {
    pendingClarify.value = null
    resetClarifyPresentation()
  }

  /** Settle only the structured input owned by the authoritative terminal task. */
  function settlePendingClarifyForTerminalTask(
    taskId: string,
    _taskStatus: TaskTerminalStatus,
  ) {
    if (!taskId) return false
    rememberTerminalClarifyTask(taskId)
    let settled = false
    for (const [key, active] of activeClarifyRequests) {
      // Broker-owned structured requests stamp run_id with their TaskRuntime
      // owner. Legacy Meta run ids use a different identity domain and are
      // correlated only after their continuation RPC returns a task id below.
      if (!active.request.requestId) continue
      if (active.request.runId !== taskId) continue
      activeClarifyRequests.delete(key)
      if (interruptState.value.get(key)?.resolution !== 'replied') {
        setInterruptState(key, { resolution: 'unavailable', busy: false, error: '' })
      }
      clearPendingClarify(key)
      settled = true
    }
    for (const [key, ownerTaskId] of retainedClarifyTaskOwners) {
      if (ownerTaskId !== taskId) continue
      retainedClarifyTaskOwners.delete(key)
      if (interruptState.value.get(key)?.resolution !== 'replied') {
        setInterruptState(key, { resolution: 'unavailable', busy: false, error: '' })
      }
      activeClarifyRequests.delete(key)
      clearPendingClarify(key)
      settled = true
    }
    return settled
  }

  function applyUserInputBootstrap(snapshot: {
    pendingUserInputs?: readonly unknown[]
    pending_user_inputs?: readonly unknown[]
    goalSnapshotStreamSeq?: number | null
    goal_snapshot_stream_seq?: number | null
    streamGeneration?: string
    stream_generation?: string
    deferredFields?: readonly string[]
    deferred_fields?: readonly string[]
  }) {
    const hasAuthoritativePendingList = Object.prototype.hasOwnProperty.call(
      snapshot,
      'pendingUserInputs',
    ) || Object.prototype.hasOwnProperty.call(snapshot, 'pending_user_inputs')
    if (!hasAuthoritativePendingList) return
    const deferred = snapshot.deferredFields ?? snapshot.deferred_fields
    if (Array.isArray(deferred) && deferred.some(field => (
      field === 'pendingUserInputs' || field === 'pending_user_inputs'
    ))) return
    // Reject a retired namespace before either negative reconciliation or
    // positive additions can mutate the current dock/inline state.
    if (!acceptsClarifyStreamGeneration(snapshot)) return

    const pending = snapshot.pendingUserInputs || snapshot.pending_user_inputs || []
    const requests = pending
      .map(value => clarifyRequestFromValue(value))
      .filter((request): request is ChatClarifyRequest => request != null)
    const pendingKeys = new Set(requests.map(request => clarifyFrameKey(request)))
    const snapshotStreamSeq = streamSeqFrom({
      streamSeq: snapshot.goalSnapshotStreamSeq ?? snapshot.goal_snapshot_stream_seq,
    })
    const snapshotStreamGeneration = streamGenerationFrom(snapshot)
    for (const [key, active] of activeClarifyRequests) {
      if (pendingKeys.has(key)) continue
      // pendingUserInputs is authoritative only for broker-owned structured
      // requests. Legacy Meta clarifies are resumed by a follow-up chat turn
      // and never appear in this snapshot.
      if (!active.request.requestId) continue
      // Hydration captures this cursor before reading pending inputs. A live
      // request observed after that cursor is newer than an absent entry in
      // this snapshot and must survive until an equal/newer snapshot arrives.
      const activeGeneration = active.observedStreamGeneration
      const currentGeneration = acceptedClarifyStreamGeneration
        || String(options.streamGeneration?.value || '').trim()
      if (activeGeneration && snapshotStreamGeneration && activeGeneration !== snapshotStreamGeneration) {
        // Only the subscription's current generation may supersede state from
        // another cursor namespace. An old hydrate that arrives after a new
        // live event cannot compare its numeric sequence to that event.
        if (
          !currentGeneration
          || activeGeneration === currentGeneration
          || snapshotStreamGeneration !== currentGeneration
        ) continue
      } else if (
        snapshotStreamSeq !== undefined
        && active.observedStreamSeq !== undefined
        && active.observedStreamSeq > snapshotStreamSeq
      ) continue
      activeClarifyRequests.delete(key)
      const priorResolution = interruptState.value.get(key)?.resolution
      setInterruptState(key, {
        resolution: priorResolution === 'replied' ? 'replied' : 'unavailable',
        busy: false,
        error: '',
      })
      clearPendingClarify(key)
    }

    for (const request of requests) {
      const key = clarifyFrameKey(request)
      if (interruptState.value.get(key)?.resolution) {
        activeClarifyRequests.delete(key)
        continue
      }
      const active = rememberActiveClarify(
        key,
        request,
        snapshotStreamSeq,
        snapshotStreamGeneration,
      )
      if (!interruptState.value.has(key)) setInterruptState(key, {})
      // Do not make an in-flight submission actionable again just because a
      // racing snapshot still contains its pre-submit pending record.
      presentPendingClarify(active.request, key)
      if (!stream.isStreaming.value) stream.ensureInterruptBubble()
      stream.appendInterruptFrame({
        interruptKind: 'clarify',
        approvalId: key,
        data: request,
        at: Date.now(),
      })
    }
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
    activeClarifyRequests.clear()
    retainedClarifyTaskOwners.clear()
    terminalClarifyTaskIds.clear()
    // Stream generations belong to the Gateway transport, not one session;
    // keep retired namespaces fenced across navigation.
    syncClarifyStreamGenerationFromShared()
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
    settlePendingClarifyForTerminalTask,
    applyUserInputBootstrap,
    subscribe,
    cleanup,
  }
}
