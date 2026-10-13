import { computed, onScopeDispose, ref, type ComputedRef } from 'vue'

export interface ChatSpeedSnapshot {
  /** Tokens per second over the trailing sliding window. */
  live: number
  /** Average tokens per second for the current round. */
  avg: number
  /** Highest live rate observed since the round began. */
  peak: number
  /** Cumulative tokens recorded since the round began. */
  total: number
  /** Milliseconds from round start to the first token (null until the first delta lands). */
  ttftMs: number | null
  /** Milliseconds from round start to stream end (0 while running). */
  roundMs: number
  /** True while the stream is actively producing output. */
  running: boolean
}

interface UseChatSpeedTrackerOptions {
  /**
   * Refresh cadence in ms for the snapshot ref. The per-delta path stays
   * allocation-free (plain field writes only); this interval is what moves
   * the reactive snapshot so the card's elapsed time and avg rate keep
   * ticking even while tool waits or reasoning silence pause the deltas.
   */
  refreshMs?: number
}

/**
 * Non-reactive per-turn speed tracker with a reactive snapshot surface.
 *
 * `recordToken()` runs on every text delta, so it only mutates plain fields —
 * it must never touch reactive state (provider deltas can arrive many thousands
 * of times per turn). Callers observe `snapshot`, a computed that reads the
 * internal state and refreshes on the provided cadence signal.
 */
export function useChatSpeedTracker(options: UseChatSpeedTrackerOptions = {}) {
  const refreshMs = options.refreshMs ?? 250
  const WINDOW_MS = 5000
  const MAX_LIVE_TPS = 200

  const samples: Array<{ ts: number; total: number }> = []
  let roundStart = 0
  let roundTokens = 0
  let totalTokens = 0
  let peak = 0
  let ttftMs: number | null = null
  let roundEndMs = 0
  let running = false

  const snapshotRef = ref<ChatSpeedSnapshot>(emptySnapshot())
  let lastSnapshotJson = ''

  function startRound(ts: number = Date.now()) {
    roundStart = ts
    roundTokens = 0
    totalTokens = 0
    peak = 0
    ttftMs = null
    roundEndMs = 0
    running = true
    samples.length = 0
    publish({})
  }

  function endRound(ts: number = Date.now()) {
    if (!running) return
    running = false
    roundEndMs = Math.max(0, ts - roundStart)
    publish({})
  }

  /** Record one text-delta event. Cheap enough to run on every provider delta. */
  function recordToken(ts: number = Date.now()) {
    if (!running) startRound(ts)
    if (ttftMs === null) ttftMs = ts - roundStart
    roundTokens += 1
    totalTokens += 1
    samples.push({ ts, total: totalTokens })
    const cutoff = ts - WINDOW_MS
    while (samples.length > 0 && samples[0].ts < cutoff) samples.shift()
  }

  function computeSnapshot(now: number = Date.now()): ChatSpeedSnapshot {
    const ts = now
    const span = samples.length >= 2
      ? Math.max((ts - samples[0].ts) / 1000, 0.25)
      : 0
    const windowTokens = samples.length >= 2
      ? samples[samples.length - 1].total - samples[0].total
      : 0
    const live = span > 0 ? Math.min(windowTokens / span, MAX_LIVE_TPS) : 0
    if (live > peak) peak = live
    const elapsedMs = running
      ? Math.max(0, ts - roundStart)
      : Math.max(1, roundEndMs || ts - roundStart)
    return {
      live,
      avg: roundTokens / Math.max(0.2, elapsedMs / 1000),
      peak,
      total: totalTokens,
      ttftMs,
      roundMs: running ? 0 : roundEndMs,
      running,
    }
  }

  function publish(_opts: { now?: number }) {
    const snap = computeSnapshot()
    const json = JSON.stringify(snap)
    if (json !== lastSnapshotJson) {
      lastSnapshotJson = json
      snapshotRef.value = snap
    }
  }

  /**
   * Periodic refresh so the card's elapsed time and avg rate keep ticking even
   * when no new delta arrives (e.g. tool waits, reasoning silence).
   */
  function refresh(now: number = Date.now()) {
    if (!running && roundEndMs === 0) {
      publish({ now })
      return
    }
    publish({ now })
  }

  const tickId = setInterval(() => refresh(), refreshMs)
  onScopeDispose(() => clearInterval(tickId))
  void tickId

  return {
    startRound,
    endRound,
    recordToken,
    refresh,
    snapshot: computed<ChatSpeedSnapshot>(() => {
      snapshotRef.value
      return snapshotRef.value
    }),
    isRunning: computed(() => snapshotRef.value.running),
  }
}

function emptySnapshot(): ChatSpeedSnapshot {
  return {
    live: 0,
    avg: 0,
    peak: 0,
    total: 0,
    ttftMs: null,
    roundMs: 0,
    running: false,
  }
}

export type ChatSpeedTracker = ReturnType<typeof useChatSpeedTracker>
export type { ComputedRef }
