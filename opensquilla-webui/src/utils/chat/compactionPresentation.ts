import type { ChatMaintenanceEvent } from '@/types/chat'
import type { InitialHistoryLoadStatus } from './sessionLoadState'

interface InitialCompactionPresentation {
  sessionKey: string
  historySessionKey: string
  initialHistoryStatus: InitialHistoryLoadStatus
  retrying: boolean
  showingPreview: boolean
  compactionId: string
}

/** Display only: accepted maintenance remains in the authoritative message list. */
export function projectInitialCompactionRows<T extends { role: string; maintenance?: ChatMaintenanceEvent }>(
  messages: T[],
  state: InitialCompactionPresentation,
): T[] {
  const sameHistorySession = state.historySessionKey === state.sessionKey
  const initialHistoryPending = !sameHistorySession
    || state.initialHistoryStatus === 'pending' || state.initialHistoryStatus === 'loading'
  // A live snapshot may restore maintenance before the first canonical page.
  // Keep its actual owner/state, but do not paint it at the empty-list head.
  // A readable preview already has an anchor; ready/error both release this
  // gate, including a confirmed empty session or a failed cold history read.
  if (state.showingPreview || (sameHistorySession && state.retrying)
    || !state.compactionId || !initialHistoryPending) return messages
  return messages.filter(message => !(message.role === 'maintenance'
    && message.maintenance?.kind === 'context_compaction'
    && message.maintenance.compactionId === state.compactionId))
}
