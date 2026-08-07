import { reactive, ref, watch, type Ref } from 'vue'
import i18n from '@/i18n'
import type { RpcEventHandler } from '@/lib/rpc'
import type {
  MetaPreflightPayload,
  MetaRunAnnouncedPayload,
  MetaRunCompletedPayload,
  MetaStepStatePayload,
  SessionEventPayload,
} from '@/types/rpc'
import { isCurrentSessionPayload, isStaleEpoch } from '@/utils/chat/streamEvents'
import {
  completeRun,
  createRibbon,
  updateStep,
  type MetaRibbonState,
} from '@/utils/chat/metaRibbon'
import {
  createPreflight,
  skillDisplayName,
  type MetaPreflightState,
} from '@/utils/chat/metaPreflight'
import type {
  MetaPreflightActionPayload,
  MetaPreflightPhase,
} from '@/components/chat/MetaPreflightCard.vue'

/**
 * Self-contained controller for the MetaSkill run UI, mirroring
 * useChatApprovals. Owns the four `session.event.meta_*` subscriptions, the
 * per-run_id state Maps, the action handlers, and the confirm/replay RPC.
 *
 * Seq gating: the `*` wildcard (handleRpcAny) advances the shared lastStreamSeq
 * for every session.event.* and runs AFTER these exact meta handlers, so this
 * controller must NOT call acceptStreamSeq (advancing twice would drop the next
 * frame). Instead gatePayload reads the pre-frame cursor read-only and drops
 * stale/duplicate frames (seq <= lastStreamSeq) — without this, a replayed
 * meta_run_announced (e.g. on reconnect) would recreate the ribbon and reset it
 * to all-pending. It also gates on isStaleEpoch + isCurrentSessionPayload and
 * is a no-op on an unknown run_id (preserved from the vanilla modules).
 */

type RpcClient = {
  call: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  on: (event: string, handler: RpcEventHandler) => () => void
}

export interface MetaPreflightEntry {
  state: MetaPreflightState
  phase: MetaPreflightPhase
  errorText: string
}

interface MetaRunRecoveryPayload {
  announced?: MetaRunAnnouncedPayload
  step_states?: MetaStepStatePayload[]
  completed?: MetaRunCompletedPayload
}

interface MetaReplayPayload {
  message?: string
  launch_text?: string
  display_text?: string
  live_replay?: {
    available?: boolean
    replay_token?: string
    committed?: boolean
  }
}

function canonicalMetaReplayMessage(value: unknown): string {
  if (typeof value !== 'string') return ''
  // The inspection RPC owns reconstruction of the originating command. Only
  // accept its canonical command shape so an invalid payload can never become
  // an arbitrary composer send. Return the original string byte-for-byte.
  return /^\/meta [^\s]+(?: -- [\s\S]*\S)?$/.test(value) ? value : ''
}

export interface UseMetaRunsOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  /**
   * Shared stream-seq cursor (the same ref advanced by handleRpcAny for every
   * session.event.*). Used read-only here to drop stale/duplicate meta frames.
   */
  lastStreamSeq: Ref<number>
  /**
   * Send the hidden preflight confirmation (provider text with markers +
   * visible bubble text). Wired from ChatView's send path.
   */
  sendHiddenConfirmation: (
    confirmed: { message?: string } | null,
    detail: {
      runId: string
      metaSkillName: string
      interpretedRequest: string
      language: string
    },
  ) => void
  /** Scroll the in-thread step card for a chip click into view. */
  scrollToStepCard: (stepId: string) => void
  /**
   * Refill the composer with `text` and fire the send path (mirrors vanilla's
   * retry/replay tail: `_textarea.value = text; _autoResizeTextarea(); _onSend()`).
   */
  sendComposerText: (text: string) => void
  /**
   * Dispatch a trusted replay sentinel while showing only human-readable text.
   * The short-lived replay token is consumed by RPC before this callback; it
   * must never be included in either argument.
   */
  sendHiddenReplay: (providerText: string, displayText: string) => void
  /** The most recent user message text (mirrors vanilla `_latestUserMessageText`). */
  lastUserMessageText: () => string
  /** Composer affordances (placeholder hint + focus) for switch-skill. */
  setComposerPlaceholder?: (text: string) => void
  focusComposer?: () => void
  pushToast: (message: string, options?: { tone?: 'info' | 'danger'; duration?: number }) => void
}

export function useMetaRuns(options: UseMetaRunsOptions) {
  const { rpc, sessionKey, currentEpoch, lastStreamSeq } = options

  // Reactive Maps keyed by run_id. ribbonOrder keeps render order stable.
  const ribbons = ref<Map<string, MetaRibbonState>>(new Map())
  const preflights = ref<Map<string, MetaPreflightEntry>>(new Map())
  const ribbonOrder = ref<string[]>([])
  let recoveryRequestVersion = 0
  let recoveryFlight: { sessionKey: string; promise: Promise<void> } | null = null

  function noteRunId(runId: string) {
    if (!runId) return
    if (!ribbonOrder.value.includes(runId)) ribbonOrder.value = [...ribbonOrder.value, runId]
  }

  function gatePayload(payload: SessionEventPayload | null | undefined): boolean {
    if (!payload || typeof payload !== 'object') return false
    if (isStaleEpoch(payload, currentEpoch.value)) return false
    if (!isCurrentSessionPayload(payload, sessionKey.value)) return false
    // Drop stale/duplicate stream frames (e.g. replayed on reconnect). Without
    // this, a re-delivered meta_run_announced would reset the ribbon to
    // all-pending and lose progress. The wildcard handleRpcAny advances
    // lastStreamSeq for every session.event.* and runs AFTER the exact meta
    // handlers, so we read the pre-frame cursor here (read-only, never advance).
    // Frames without a numeric stream_seq are accepted, matching acceptStreamSeq.
    const seq = payload.stream_seq
    if (typeof seq === 'number' && Number.isFinite(seq) && seq <= lastStreamSeq.value) return false
    return true
  }

  /* ── Event handlers ──────────────────────────────────────────────── */

  function onPreflight(payload: MetaPreflightPayload) {
    if (!gatePayload(payload)) return
    const state = reactive(createPreflight(payload)) as MetaPreflightState
    if (!state.runId) return
    noteRunId(state.runId)
    const next = new Map(preflights.value)
    next.set(state.runId, { state, phase: 'ready', errorText: '' })
    preflights.value = next
  }

  function onRunAnnounced(payload: MetaRunAnnouncedPayload) {
    if (!gatePayload(payload)) return
    const runId = payload.run_id || ''
    // A run's compiled DAG is immutable. Duplicate announce frames can be
    // replayed after reconnect; never let them reset live progress or a
    // durable terminal recovery snapshot back to all-pending.
    if (runId && ribbons.value.has(runId)) return
    const ribbon = reactive(createRibbon(payload)) as MetaRibbonState
    if (!ribbon.runId) return
    noteRunId(ribbon.runId)
    const next = new Map(ribbons.value)
    next.set(ribbon.runId, ribbon)
    ribbons.value = next
    // The run started: collapse the preflight checkpoint into a running line.
    const entry = preflights.value.get(ribbon.runId)
    if (entry && entry.phase !== 'cancelled') setPreflightPhase(ribbon.runId, 'running')
  }

  function onStepState(payload: MetaStepStatePayload) {
    if (!gatePayload(payload)) return
    const runId = payload.run_id || ''
    const ribbon = ribbons.value.get(runId)
    if (!ribbon) return // out-of-order / unknown run — tolerate
    updateStep(ribbon, payload)
  }

  function onRunCompleted(payload: MetaRunCompletedPayload) {
    if (!gatePayload(payload)) return
    const runId = payload.run_id || ''
    const ribbon = ribbons.value.get(runId)
    if (!ribbon) return
    completeRun(ribbon, payload)
  }

  /**
   * Restore the latest unresolved failed run from the persisted meta ledger.
   * Meta progress events intentionally do not enter chat.history, so reconnect
   * hydration has to use this dedicated read model instead of model-visible
   * transcript content.
   */
  function hydrateRecovery(): Promise<void> {
    const targetSessionKey = sessionKey.value.trim()
    if (!targetSessionKey) return Promise.resolve()
    if (recoveryFlight?.sessionKey === targetSessionKey) return recoveryFlight.promise

    const requestVersion = ++recoveryRequestVersion
    const run = async () => {
      try {
        const payload = await rpc.call<{ recovery?: MetaRunRecoveryPayload | null }>(
          'meta.runs.recovery',
          { sessionKey: targetSessionKey },
        )
        if (
          requestVersion !== recoveryRequestVersion
          || sessionKey.value !== targetSessionKey
        ) return
        const recovery = payload?.recovery
        const announced = recovery?.announced
        if (!announced?.run_id) return

        const ribbon = reactive(createRibbon(announced)) as MetaRibbonState
        for (const stepState of recovery?.step_states || []) updateStep(ribbon, stepState)
        if (recovery?.completed) completeRun(ribbon, recovery.completed)
        noteRunId(ribbon.runId)
        const next = new Map(ribbons.value)
        // A same-run live ribbon can be partial when the socket drops just
        // before its terminal events. The durable failed snapshot is newer
        // truth after reconnect, so replace that stale in-memory state.
        next.set(ribbon.runId, ribbon)
        ribbons.value = next
      } catch {
        // Recovery hydration is an optional reconnect affordance. Older
        // gateways do not expose this RPC; live event handling remains intact.
      }
    }
    const promise = run().finally(() => {
      if (recoveryFlight?.promise === promise) recoveryFlight = null
    })
    recoveryFlight = { sessionKey: targetSessionKey, promise }
    return promise
  }

  /* ── Phase helpers ───────────────────────────────────────────────── */

  function setPreflightPhase(runId: string, phase: MetaPreflightPhase, errorText = '') {
    const entry = preflights.value.get(runId)
    if (!entry) return
    const next = new Map(preflights.value)
    next.set(runId, { ...entry, phase, errorText })
    preflights.value = next
  }

  /* ── Action handlers ─────────────────────────────────────────────── */

  async function onPreflightAction(payload: MetaPreflightActionPayload) {
    const { action, runId } = payload
    const entry = preflights.value.get(runId)
    if (!entry) return

    if (action === 'dismiss') {
      setPreflightPhase(runId, 'cancelled')
      return
    }

    // continue / defaults both confirm the preflight then fire the hidden send.
    const originatingSessionKey = sessionKey.value
    setPreflightPhase(runId, 'submitting')
    let confirmed: { message?: string } | null = null
    try {
      confirmed = await rpc.call<{ message?: string }>('meta.runs.confirm_preflight', {
        sessionKey: originatingSessionKey,
        runId,
        run_id: runId,
        // The server feeds interpretedRequest into confirmation_message() so the
        // authored confirmation carries the interpreted-request context.
        interpretedRequest: payload.interpretedRequest,
        fields: payload.confirmedFields,
        useDefaults: action === 'defaults',
      })
      if (sessionKey.value !== originatingSessionKey) return
    } catch (err) {
      if (sessionKey.value !== originatingSessionKey) return
      const message = err instanceof Error ? err.message : String(err)
      setPreflightPhase(runId, 'error', message)
      return
    }
    try {
      options.sendHiddenConfirmation(confirmed, {
        runId,
        metaSkillName: payload.metaSkillName,
        interpretedRequest: payload.interpretedRequest,
        language: entry.state.language,
      })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setPreflightPhase(runId, 'error', message)
      return
    }
    // A meta_run_announced will flip this to 'running'; until then show submitting.
  }

  // Map a replay-bearing action to the server replay mode (mirrors vanilla
  // `_onMetaRibbonAction`: retry-step → failed-step, retry-with-partial-context
  // → partial-context).
  const REPLAY_MODES: Record<string, string> = {
    'retry-step': 'failed-step',
    'retry-with-partial-context': 'partial-context',
  }

  // Toast-only actions: vanilla surfaces guidance and does NOT call replay.
  // Maps the action to its i18n message key (resolved at toast time).
  const TOAST_ACTIONS: Record<string, string> = {
    'install-dependency': 'chat.metaRuns.toastInstallDependency',
    'continue-text-only': 'chat.metaRuns.toastContinueTextOnly',
    'review-paid-submit': 'chat.metaRuns.toastReviewPaidSubmit',
  }

  async function onRibbonAction(payload: { action: string; stepId: string | null; runId: string }) {
    const { action, runId, stepId } = payload

    if (action === 'retry-run') {
      // Resolve the selected run through the durable ledger. The latest visible
      // user bubble may belong to a later turn (or another recovered run), so it
      // is not an authoritative retry seed.
      const originatingSessionKey = sessionKey.value
      try {
        const payloadOut = await rpc.call<{ replay?: MetaReplayPayload } & MetaReplayPayload>(
          'meta.runs.replay',
          {
            sessionKey: originatingSessionKey,
            runId,
            run_id: runId,
            mode: 'run',
          },
        )
        if (sessionKey.value !== originatingSessionKey) return
        const replay = payloadOut?.replay || payloadOut
        const message = canonicalMetaReplayMessage(replay?.message)
        if (!message) {
          options.pushToast(i18n.global.t('chat.metaRuns.replayUnavailable'), { tone: 'danger' })
          return
        }
        options.sendComposerText(message)
      } catch (err) {
        if (sessionKey.value !== originatingSessionKey) return
        const message = err instanceof Error ? err.message : String(err)
        options.pushToast(i18n.global.t('chat.metaRuns.replayFailed', { message }), {
          tone: 'danger',
        })
      }
      return
    }

    if (action === 'switch-skill' || action === 'switch-meta-skill') {
      // Hand control back to the composer so the operator can pick a new skill,
      // surfacing the vanilla guidance hint (placeholder if the composer exposes
      // a setter, otherwise via the toast path so it is not silently dropped).
      const hint = '想换哪个 meta-skill？例如：Use meta-skill `meta-paper-write`'
      options.setComposerPlaceholder?.(hint)
      options.focusComposer?.()
      return
    }

    if (action === 'show-detail') {
      // Vanilla expands the target step card (data-expanded='true') before
      // scrolling. The Vue thread keys tool cards by renderKey, not by a
      // meta_step_<id> anchor, so there is no equivalent expand target to set
      // here; scroll the card into view (no-op if it is not yet rendered).
      if (stepId) options.scrollToStepCard(`meta_step_${stepId}`)
      return
    }

    if (action in TOAST_ACTIONS) {
      // Guidance actions are deliberately toast-only. In particular, an
      // ambiguous paid submission must never fall through to live replay:
      // upstream may already have accepted and billed the original request.
      options.pushToast(i18n.global.t(TOAST_ACTIONS[action]), {
        tone: 'info',
        duration: action === 'review-paid-submit' ? 8000 : 3000,
      })
      return
    }

    if (!(action in REPLAY_MODES)) {
      // Rescue actions are server-authored and may be newer than this client.
      // Fail closed instead of treating an unknown action as retry-step, which
      // could repeat an irreversible or paid side effect.
      options.pushToast(i18n.global.t('chat.metaRuns.toastUnsupportedRescueAction'), {
        tone: 'info',
        duration: 5000,
      })
      return
    }

    // retry-step, retry-with-partial-context → two-phase live replay.
    // Phase 1 prepares a short-lived session/run/mode-bound capability; phase
    // 2 consumes it and returns the token-free hidden sentinel.  Only that
    // sentinel enters chat.send, so neither the model nor transcript ever sees
    // the capability. Older gateways return a canonical /meta command in
    // replay.message, which remains a safe fresh-run fallback.
    const mode = REPLAY_MODES[action]
    const originatingSessionKey = sessionKey.value
    try {
      const payloadOut = await rpc.call<{ replay?: MetaReplayPayload } & MetaReplayPayload>(
        'meta.runs.replay',
        {
          sessionKey: originatingSessionKey,
          runId,
          run_id: runId,
          mode,
          action,
          stepId: stepId || undefined,
          prepareLive: true,
        },
      )
      if (sessionKey.value !== originatingSessionKey) return
      const replay = payloadOut && payloadOut.replay ? payloadOut.replay : payloadOut
      const replayToken = replay?.live_replay?.replay_token || ''
      if (!replayToken) {
        // Compatibility with older gateways: replay.message is now a
        // canonical `/meta <skill> -- <request>` command, never replay prose.
        const fallbackMessage = canonicalMetaReplayMessage(replay?.message)
        if (fallbackMessage) {
          options.sendComposerText(fallbackMessage)
        } else {
          options.pushToast(i18n.global.t('chat.metaRuns.replayUnavailable'), {
            tone: 'danger',
          })
        }
        return
      }

      const committedOut = await rpc.call<{ replay?: MetaReplayPayload } & MetaReplayPayload>(
        'meta.runs.replay',
        {
          sessionKey: originatingSessionKey,
          runId,
          run_id: runId,
          mode,
          replayToken,
        },
      )
      if (sessionKey.value !== originatingSessionKey) return
      const committed = committedOut?.replay || committedOut
      const launchText = committed?.launch_text || ''
      if (!launchText || committed?.live_replay?.committed !== true) {
        const fallbackMessage = canonicalMetaReplayMessage(replay?.message)
        if (fallbackMessage) {
          options.sendComposerText(fallbackMessage)
        } else {
          options.pushToast(i18n.global.t('chat.metaRuns.replayUnavailable'), {
            tone: 'danger',
          })
        }
        return
      }
      const displayText = committed.display_text || (
        action === 'retry-step' ? 'Retry failed step' : 'Retry with partial context'
      )
      options.sendHiddenReplay(launchText, displayText)
    } catch (err) {
      if (sessionKey.value !== originatingSessionKey) return
      const message = err instanceof Error ? err.message : String(err)
      options.pushToast(i18n.global.t('chat.metaRuns.replayFailed', { message }), { tone: 'danger' })
    }
  }

  function onChipSelect(payload: { stepId: string; runId: string }) {
    if (!payload.stepId) return
    options.scrollToStepCard(`meta_step_${payload.stepId}`)
  }

  /* ── Subscription lifecycle ──────────────────────────────────────── */

  function subscribe(): () => void {
    const unsubs = [
      rpc.on('session.event.meta_preflight', onPreflight as RpcEventHandler),
      rpc.on('session.event.meta_run_announced', onRunAnnounced as RpcEventHandler),
      rpc.on('session.event.meta_step_state', onStepState as RpcEventHandler),
      rpc.on('session.event.meta_run_completed', onRunCompleted as RpcEventHandler),
    ]
    return () => {
      unsubs.forEach((unsub) => unsub())
    }
  }

  function reset() {
    recoveryRequestVersion += 1
    ribbons.value = new Map()
    preflights.value = new Map()
    ribbonOrder.value = []
  }

  // Session switches clear all per-run state. ChatView starts optional durable
  // recovery only after the new session subscription is authoritative, so this
  // read can never overtake subscribe/snapshot/history during bootstrap.
  watch(sessionKey, reset)

  function cleanup() {
    reset()
  }

  // skillDisplayName is re-exported so ChatView can label the hidden send.
  return {
    ribbons,
    preflights,
    ribbonOrder,
    onPreflightAction,
    onRibbonAction,
    onChipSelect,
    hydrateRecovery,
    subscribe,
    cleanup,
    skillDisplayName,
  }
}
