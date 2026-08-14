import type { ChatRenderedMessage } from '@/types/chat'

export interface ForkRpcResponse {
  key?: string
  forkMode?: string
  throughTurnId?: string
}

export interface ForkTransitionLifetime {
  begin: () => number
  invalidate: (generation?: number) => void
  dispose: () => void
  isCurrent: (generation: number) => boolean
}

/**
 * Own asynchronous fork continuations independently of Vue component state.
 * A disposed view permanently rejects every captured generation, even after a
 * deferred RPC or navigation promise settles.
 */
export function createForkTransitionLifetime(): ForkTransitionLifetime {
  let currentGeneration = 0
  let disposed = false
  return {
    begin() {
      if (disposed) return 0
      currentGeneration += 1
      return currentGeneration
    },
    invalidate(generation) {
      if (generation !== undefined && generation !== currentGeneration) return
      currentGeneration += 1
    },
    dispose() {
      disposed = true
      currentGeneration += 1
    },
    isCurrent(generation) {
      return !disposed && generation > 0 && generation === currentGeneration
    },
  }
}

export function forkRouteHandoffAction(
  newSession: unknown,
  transition: {
    parentKey: string
    targetKey: string
    phase: 'creating' | 'opening' | 'returning' | 'error'
  },
): 'keep' | 'returning' | 'clear' {
  if (
    transition.phase !== 'creating'
    && typeof newSession === 'string'
    && newSession === transition.parentKey
  ) return 'returning'
  if (
    typeof newSession !== 'string'
    || transition.phase === 'creating'
    || newSession !== transition.targetKey
  ) return 'clear'
  return 'keep'
}

export function forkNavigationPhase(
  targetKey: string,
  parentKey: string,
): 'opening' | 'returning' {
  return targetKey === parentKey ? 'returning' : 'opening'
}

export function forkRpcRequest(parentKey: string, throughTurnId?: string): {
  method: 'sessions.fork' | 'sessions.forkThroughTurn'
  params: { key: string; throughTurnId?: string }
} {
  return throughTurnId
    ? {
        method: 'sessions.forkThroughTurn',
        params: { key: parentKey, throughTurnId },
      }
    : {
        method: 'sessions.fork',
        params: { key: parentKey },
      }
}

export function validatedForkChildKey(
  response: ForkRpcResponse | null | undefined,
  throughTurnId?: string,
): string {
  if (
    throughTurnId
    && (
      response?.forkMode !== 'through_turn'
      || response.throughTurnId !== throughTurnId
    )
  ) {
    throw new Error('Fork response did not confirm the requested turn boundary')
  }
  const childKey = typeof response?.key === 'string' ? response.key : ''
  if (!childKey) throw new Error('Fork response returned no key')
  return childKey
}

/**
 * Freeze the visible prefix for a fork hand-off without retaining canonical
 * parent Message objects. Turn outcomes are attached to every durable row in a
 * turn, so the inclusive boundary must be the final rendered row for that turn.
 */
export function snapshotForkPreviewMessages(
  source: ChatRenderedMessage[],
  throughTurnId?: string,
): ChatRenderedMessage[] {
  let boundary = source.length - 1
  if (throughTurnId) {
    let matchedBoundary = -1
    for (let index = 0; index < source.length; index++) {
      if (source[index]?.turnOutcome?.turnId === throughTurnId) {
        matchedBoundary = index
      }
    }
    if (matchedBoundary >= 0) boundary = matchedBoundary
  }
  return source.slice(0, boundary + 1).map(message => ({ ...message }))
}
