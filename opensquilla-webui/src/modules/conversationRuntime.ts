/**
 * Transport-independent conversation consistency rules.
 *
 * This module is deliberately free of Vue, RpcClient and generated Contract
 * imports.  It owns the small but subtle state machine shared by bootstrap,
 * snapshot replay and live event consumers:
 *
 *   session epoch  ->  stream generation  ->  monotonic stream sequence
 *
 * Adapters translate wire aliases into ConversationCursorSignal before
 * entering this seam.  Consumers receive a decision and a new cursor; they do
 * not need to know why a stale frame was rejected or why a Gateway restart
 * resets the numeric sequence.  Keeping the policy here prevents the same
 * race rules from being reimplemented in every composable.
 */

export interface ConversationCursor {
  readonly sessionKey: string
  readonly sessionEpoch: number
  readonly streamGeneration: string | null
  readonly streamSeq: number
}

/** Canonical, wire-independent cursor facts supplied by an adapter. */
export interface ConversationCursorSignal {
  readonly sessionKey?: string | null
  readonly sessionEpoch?: number | null
  readonly streamGeneration?: string | null
  readonly streamSeq?: number | null
  readonly currentStreamSeq?: number | null
  readonly replayComplete?: boolean | null
  readonly replayGapReason?: string | null
}

export type ConversationCursorRejection =
  | 'session-mismatch'
  | 'stale-epoch'
  | 'duplicate-sequence'
  | 'snapshot-behind'
  | 'generation-mismatch'
  | 'invalid-snapshot'

export interface ConversationGenerationTransition {
  readonly cursor: ConversationCursor
  readonly changed: boolean
  /** True when the old numeric cursor can no longer be compared safely. */
  readonly reset: boolean
}

export interface ConversationEventDecision extends ConversationGenerationTransition {
  readonly accepted: boolean
  readonly reason?: ConversationCursorRejection
}

export interface ConversationSnapshotDecision {
  readonly cursor: ConversationCursor
  readonly accepted: boolean
  readonly reason?: ConversationCursorRejection
}

export interface ConversationReplayTransition {
  readonly cursor: ConversationCursor
  /** The caller should reload durable history when this is true. */
  readonly requiresHistory: boolean
}

export interface ConversationEpochTransition {
  readonly cursor: ConversationCursor
  readonly changed: boolean
}

export interface ConversationRuntime {
  createCursor(sessionKey: string, seed?: Partial<ConversationCursor>): ConversationCursor
  reset(cursor: ConversationCursor, sessionKey?: string): ConversationCursor
  observeGeneration(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationGenerationTransition
  acceptEvent(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
    options?: { observeGeneration?: boolean },
  ): ConversationEventDecision
  acceptSnapshot(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationSnapshotDecision
  /** Adopt a known authoritative snapshot after the caller reset its view. */
  restoreSnapshot(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationCursor
  applyReplayCursor(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
    generationReset?: boolean,
  ): ConversationReplayTransition
  advanceEpoch(
    cursor: ConversationCursor,
    sessionEpoch: number | null | undefined,
  ): ConversationEpochTransition
  isStaleEpoch(cursor: ConversationCursor, sessionEpoch: number | null | undefined): boolean
}

const EMPTY_CURSOR: Omit<ConversationCursor, 'sessionKey'> = {
  sessionEpoch: 0,
  streamGeneration: null,
  streamSeq: 0,
}

function copyCursor(cursor: ConversationCursor, patch: Partial<ConversationCursor> = {}): ConversationCursor {
  return Object.freeze({ ...cursor, ...patch })
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function presentText(value: unknown): string | null {
  // Keep legacy v4 comparison semantics: a non-empty string is significant
  // exactly as received. Contract validation may reject whitespace-only
  // values later, but this seam must not rewrite an existing wire value.
  return typeof value === 'string' && value ? value : null
}

function signalSequence(signal: ConversationCursorSignal): number | null {
  return finiteNumber(signal.streamSeq)
}

function snapshotSequence(signal: ConversationCursorSignal): number | null {
  return finiteNumber(signal.currentStreamSeq ?? signal.streamSeq)
}

function sessionMatches(cursor: ConversationCursor, signal: ConversationCursorSignal): boolean {
  const incoming = presentText(signal.sessionKey)
  return !incoming || !cursor.sessionKey || incoming === cursor.sessionKey
}

function generationChangedRequiresReset(
  cursor: ConversationCursor,
  signal: ConversationCursorSignal,
  previousGeneration: string | null,
): boolean {
  if (previousGeneration !== null) return true
  const sequence = signalSequence(signal) ?? snapshotSequence(signal)
  const visibleGap = signal.replayGapReason === 'stream_generation_changed'
  return visibleGap || (sequence !== null && sequence < cursor.streamSeq)
}

/**
 * Build the runtime as a pure policy object.  State remains owned by the
 * composition root for now, which makes this seam easy to introduce without
 * changing Vue reactivity or the public client API.  A later slice can move
 * the cursor store behind this same interface without changing callers.
 */
export function createConversationRuntime(): ConversationRuntime {
  function createCursor(sessionKey: string, seed: Partial<ConversationCursor> = {}): ConversationCursor {
    return Object.freeze({
      sessionKey,
      // Keep finite legacy values comparable while the v4 adapter is being
      // migrated. New generated Contract frames are stricter integers.
      sessionEpoch: finiteNumber(seed.sessionEpoch) ?? EMPTY_CURSOR.sessionEpoch,
      streamGeneration: presentText(seed.streamGeneration) ?? EMPTY_CURSOR.streamGeneration,
      streamSeq: finiteNumber(seed.streamSeq) ?? EMPTY_CURSOR.streamSeq,
    })
  }

  function reset(cursor: ConversationCursor, sessionKey = cursor.sessionKey): ConversationCursor {
    return createCursor(sessionKey)
  }

  function observeGeneration(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationGenerationTransition {
    const generation = presentText(signal.streamGeneration)
    if (!generation || generation === cursor.streamGeneration) {
      return { cursor, changed: false, reset: false }
    }
    const resetSequence = generationChangedRequiresReset(
      cursor,
      signal,
      cursor.streamGeneration,
    )
    return {
      cursor: copyCursor(cursor, {
        streamGeneration: generation,
        ...(resetSequence ? { streamSeq: 0 } : {}),
      }),
      changed: true,
      reset: resetSequence,
    }
  }

  function acceptEvent(
    originalCursor: ConversationCursor,
    signal: ConversationCursorSignal,
    options: { observeGeneration?: boolean } = {},
  ): ConversationEventDecision {
    // Identity and epoch fences run before generation adoption. A late frame
    // from another session (or an older epoch) must never be able to advance
    // the active stream's generation or reset its sequence ledger.
    if (!sessionMatches(originalCursor, signal)) {
      return {
        cursor: originalCursor,
        changed: false,
        reset: false,
        accepted: false,
        reason: 'session-mismatch',
      }
    }
    if (isStaleEpoch(originalCursor, signal.sessionEpoch)) {
      return {
        cursor: originalCursor,
        changed: false,
        reset: false,
        accepted: false,
        reason: 'stale-epoch',
      }
    }
    let cursor = originalCursor
    let generationReset = false
    let generationChanged = false
    if (options.observeGeneration !== false) {
      const transition = observeGeneration(cursor, signal)
      cursor = transition.cursor
      generationReset = transition.reset
      generationChanged = transition.changed
    }
    const sequence = signalSequence(signal)
    if (sequence !== null && sequence <= cursor.streamSeq) {
      return {
        cursor,
        changed: generationChanged,
        reset: generationReset,
        accepted: false,
        reason: 'duplicate-sequence',
      }
    }
    return {
      cursor: sequence === null ? cursor : copyCursor(cursor, { streamSeq: sequence }),
      changed: generationChanged,
      reset: generationReset,
      accepted: true,
    }
  }

  function acceptSnapshot(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationSnapshotDecision {
    if (!sessionMatches(cursor, signal)) {
      return { cursor, accepted: false, reason: 'session-mismatch' }
    }
    const incomingGeneration = presentText(signal.streamGeneration)
    if (
      incomingGeneration
      && cursor.streamGeneration
      && incomingGeneration !== cursor.streamGeneration
    ) {
      return { cursor, accepted: false, reason: 'generation-mismatch' }
    }
    const sequence = snapshotSequence(signal)
    if (sequence === null) {
      return { cursor, accepted: false, reason: 'invalid-snapshot' }
    }
    if (sequence < cursor.streamSeq) {
      return { cursor, accepted: false, reason: 'snapshot-behind' }
    }
    return {
      cursor: copyCursor(cursor, {
        streamSeq: Math.max(0, sequence),
      }),
      accepted: true,
    }
  }

  function applyReplayCursor(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
    generationReset = false,
  ): ConversationReplayTransition {
    const rawSequence = snapshotSequence(signal)
    const sequence = rawSequence === null ? null : Math.max(0, rawSequence)
    if (sequence === null) {
      return {
        cursor,
        requiresHistory: signal.replayComplete === false || generationReset,
      }
    }
    const requiresHistory = signal.replayComplete === false || generationReset
    if (!requiresHistory) {
      return {
        cursor: copyCursor(cursor, { streamSeq: Math.max(cursor.streamSeq, sequence) }),
        requiresHistory: false,
      }
    }
    return {
      cursor: copyCursor(cursor, {
        streamSeq: generationReset && cursor.streamSeq === 0
          ? sequence
          : Math.max(cursor.streamSeq, sequence),
      }),
      requiresHistory: true,
    }
  }

  function restoreSnapshot(
    cursor: ConversationCursor,
    signal: ConversationCursorSignal,
  ): ConversationCursor {
    if (!sessionMatches(cursor, signal)) return cursor
    const sequence = snapshotSequence(signal)
    if (sequence === null) return cursor
    return copyCursor(cursor, {
      streamSeq: Math.max(0, sequence),
    })
  }

  function advanceEpoch(
    cursor: ConversationCursor,
    sessionEpoch: number | null | undefined,
  ): ConversationEpochTransition {
    const next = finiteNumber(sessionEpoch)
    if (next === null || next <= cursor.sessionEpoch) {
      return { cursor, changed: false }
    }
    return {
      cursor: copyCursor(cursor, { sessionEpoch: next }),
      changed: true,
    }
  }

  function isStaleEpoch(
    cursor: ConversationCursor,
    sessionEpoch: number | null | undefined,
  ): boolean {
    // Legacy task/event frames use -1 as an explicit pre-session sentinel;
    // it is still older than the initial epoch 0 and must be rejected.
    const incoming = finiteNumber(sessionEpoch)
    return incoming !== null && incoming < cursor.sessionEpoch
  }

  return {
    createCursor,
    reset,
    observeGeneration,
    acceptEvent,
    acceptSnapshot,
    restoreSnapshot,
    applyReplayCursor,
    advanceEpoch,
    isStaleEpoch,
  }
}
