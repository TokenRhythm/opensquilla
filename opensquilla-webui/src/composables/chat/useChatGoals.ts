import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { payloadBelongsToSession } from '@/utils/chat/plans'

export interface GoalSnapshot {
  goalId: string
  sessionKey: string
  goalText: string
  status: string
  turns: number
  idleTurns?: number
  blockedReason?: string | null
  pauseReason?: string | null
  terminalReason?: string | null
  startedAt?: number
  lastTurnAt?: number | null
  finishedAt?: number | null
}

interface GoalStatusResult {
  goal?: GoalSnapshot | null
}

type RpcClient = {
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
  ) => Promise<T>
  on: (event: string, handler: (...args: unknown[]) => void) => () => void
}

export interface UseChatGoalsOptions {
  rpc: RpcClient
  sessionKey: Ref<string>
  // Resolve the durable session key for a new goal. On the new-chat landing the
  // session is a client-side draft until first send; the host materializes it
  // (sessions.create) and switches the view before the goal is registered.
  ensureSessionKey?: () => Promise<string>
  notify?: (message: string) => void
}

const GOAL_ACTIVE_STATUSES = new Set(['running', 'paused'])
const GOAL_TERMINAL_STATUSES = new Set(['complete', 'blocked', 'cancelled'])
const GOAL_TERMINAL_HOLD_MS = 6000
const GOAL_POLL_INTERVAL_MS = 5000

export function goalStatusIsTerminal(status: string | undefined): boolean {
  return !!status && GOAL_TERMINAL_STATUSES.has(status)
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringField(source: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const raw = source[key]
    if (typeof raw === 'string' && raw) return raw
  }
  return undefined
}

function numberField(source: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const raw = source[key]
    if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  }
  return undefined
}

function normalizeGoal(value: unknown): GoalSnapshot | null {
  const source = record(value)
  if (!source) return null
  const goalId = stringField(source, 'goalId', 'goal_id')
  const sessionKey = stringField(source, 'sessionKey', 'session_key')
  const goalText = stringField(source, 'goalText', 'goal_text')
  const status = stringField(source, 'status')
  if (!goalId || !goalText || !status) return null
  return {
    goalId,
    sessionKey: sessionKey || '',
    goalText,
    status,
    turns: numberField(source, 'turns') ?? 0,
    idleTurns: numberField(source, 'idleTurns', 'idle_turns'),
    blockedReason: stringField(source, 'blockedReason', 'blocked_reason') ?? null,
    pauseReason: stringField(source, 'pauseReason', 'pause_reason') ?? null,
    terminalReason: stringField(source, 'terminalReason', 'terminal_reason') ?? null,
    startedAt: numberField(source, 'startedAt', 'started_at'),
    lastTurnAt: numberField(source, 'lastTurnAt', 'last_turn_at') ?? null,
    finishedAt: numberField(source, 'finishedAt', 'finished_at') ?? null,
  }
}

export function formatGoalElapsed(startedAt: number | undefined, finishedAt: number | null | undefined, now: number): string {
  if (!startedAt) return ''
  const end = finishedAt ?? now
  const totalSeconds = Math.max(0, Math.floor((end - startedAt) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

export function useChatGoals(options: UseChatGoalsOptions) {
  // The composer chip is visible between selecting /goal and sending the goal.
  const draftArmed = ref(false)
  const activeGoal = ref<GoalSnapshot | null>(null)
  // The ribbon auto-hides shortly after a terminal state, but the transcript
  // tail keeps a durable "Goal complete · 6m 52s" line; this snapshot survives
  // the ribbon fade and session switches reset it.
  const lastGoal = ref<GoalSnapshot | null>(null)
  const busy = ref(false)
  const nowTick = ref(Date.now())

  let terminalHideTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let tickTimer: ReturnType<typeof setInterval> | null = null
  let refreshing = false

  function clearTerminalHideTimer() {
    if (terminalHideTimer === null) return
    clearTimeout(terminalHideTimer)
    terminalHideTimer = null
  }

  function arm() {
    draftArmed.value = true
  }

  function disarm() {
    draftArmed.value = false
  }

  function applyGoal(goal: GoalSnapshot | null) {
    clearTerminalHideTimer()
    if (!goal) {
      activeGoal.value = null
      return
    }
    lastGoal.value = goal
    activeGoal.value = goal
    if (GOAL_TERMINAL_STATUSES.has(goal.status)) {
      terminalHideTimer = setTimeout(() => {
        if (activeGoal.value?.goalId === goal.goalId) activeGoal.value = null
        terminalHideTimer = null
      }, GOAL_TERMINAL_HOLD_MS)
    }
  }

 async function refresh(): Promise<void> {
   const key = options.sessionKey.value
   if (!key || refreshing) return
   refreshing = true
   try {
     const result = await options.rpc.call<GoalStatusResult>('goals.status', { sessionKey: key })
     const goal = normalizeGoal(result?.goal)
     // A goal from another session must not leak into this view.
     if (goal && goal.sessionKey && goal.sessionKey !== key) return
     applyGoal(goal)
   } catch {
     // Status is best-effort; the ribbon simply stays on its last known state.
   } finally {
     refreshing = false
   }
 }

  async function startGoal(text: string): Promise<boolean> {
    const goalText = String(text || '').trim()
    if (!goalText) return false
    const resolveKey = options.ensureSessionKey
      ?? (async () => options.sessionKey.value)
    let key = ''
    try {
      key = await resolveKey()
    } catch {
      return false
    }
    if (!key) return false
    busy.value = true
    let watcherRegistered = false
    let goalAccepted = false
    try {
      // Register before accepting the first turn. A fast first response can
      // otherwise finish before the watcher exists, leaving the goal paused at
      // the continuation anchor with no event left to restart it.
      await options.rpc
        .call('goals.observe', { sessionKey: key, watch: true })
      watcherRegistered = true
      await options.rpc.call('goals.set', { sessionKey: key, message: goalText })
      goalAccepted = true
      await refresh()
      return true
    } catch (err) {
      if (watcherRegistered && !goalAccepted) {
        void options.rpc
          .call('goals.unobserve', { sessionKey: key })
          .catch(() => undefined)
      }
      options.notify?.(err instanceof Error ? err.message : String(err))
      return false
    } finally {
      busy.value = false
    }
  }

  async function mutate(method: 'goals.pause' | 'goals.resume' | 'goals.clear') {
    const key = options.sessionKey.value
    if (!key || busy.value) return
    busy.value = true
    try {
      await options.rpc.call(method, { sessionKey: key })
      await refresh()
    } catch (err) {
      options.notify?.(err instanceof Error ? err.message : String(err))
    } finally {
      busy.value = false
    }
  }

  const pause = () => mutate('goals.pause')
  const resume = () => mutate('goals.resume')
  const clear = () => mutate('goals.clear')

  function dismissRibbon() {
    clearTerminalHideTimer()
    activeGoal.value = null
  }

  // Realtime: Goal runs have their own event namespace. Keep the legacy
  // plan_run subscription as a compatibility bridge for older gateways; new
  // Goal events never enter the generic Plan composable.
  function onGoalRunEvent(payload: unknown) {
    if (!payloadBelongsToSession(payload, options.sessionKey.value)) return
    const source = record(payload)
    const run = record(source?.plan_run ?? source?.planRun)
    if (!run) return
    const driverKind = stringField(run, 'driverKind', 'driver_kind')
    if (driverKind !== 'goal') return
    void refresh()
  }

  const unsubscribeGoal = options.rpc.on('session.event.goal_run', onGoalRunEvent)
  const unsubscribeLegacy = options.rpc.on('session.event.plan_run', onGoalRunEvent)

  watch(options.sessionKey, () => {
    disarm()
    lastGoal.value = null
    void refresh()
  }, { immediate: true })

  // Polling backstop: goal status transitions that bypass the plan-run event
  // path (failure retries, unwatched pause) still surface within a few seconds.
  function syncPolling() {
    const active = activeGoal.value !== null
      && GOAL_ACTIVE_STATUSES.has(activeGoal.value.status)
    if (active && pollTimer === null) {
      pollTimer = setInterval(() => { void refresh() }, GOAL_POLL_INTERVAL_MS)
    } else if (!active && pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
    if (active && tickTimer === null) {
      tickTimer = setInterval(() => { nowTick.value = Date.now() }, 1000)
    } else if (!active && tickTimer !== null) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  }
  watch(activeGoal, syncPolling, { immediate: true })

  const elapsed = computed(() => formatGoalElapsed(
    activeGoal.value?.startedAt,
    activeGoal.value?.finishedAt,
    nowTick.value,
  ))

  const lastGoalElapsed = computed(() => formatGoalElapsed(
    lastGoal.value?.startedAt,
    lastGoal.value?.finishedAt,
    nowTick.value,
  ))

  onBeforeUnmount(() => {
    unsubscribeGoal()
    unsubscribeLegacy()
    clearTerminalHideTimer()
    if (pollTimer !== null) clearInterval(pollTimer)
    if (tickTimer !== null) clearInterval(tickTimer)
  })

  return {
    draftArmed,
    activeGoal,
    lastGoal,
    busy,
    elapsed,
    lastGoalElapsed,
    arm,
    disarm,
    startGoal,
    pause,
    resume,
    clear,
    dismissRibbon,
    refresh,
  }
}
