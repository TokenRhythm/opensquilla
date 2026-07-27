export type InitialHistoryLoadStatus = 'pending' | 'loading' | 'ready' | 'error'
export type ChatSessionLoadSurface = 'loading' | 'error' | null

export interface ResolveChatSessionLoadStateOptions {
  isDraftLanding: boolean
  isStreaming: boolean
  messageCount: number
  initialHistoryStatus: InitialHistoryLoadStatus
  sessionHydrating: boolean
}

export function resolveChatSessionLoadState(
  options: ResolveChatSessionLoadStateOptions,
): ChatSessionLoadSurface {
  if (options.isDraftLanding || options.isStreaming || options.messageCount > 0) return null
  if (options.initialHistoryStatus === 'error') return 'error'
  if (options.initialHistoryStatus !== 'ready' || options.sessionHydrating) return 'loading'
  return null
}

export function shouldShowHistorySentinelError(options: {
  loadEarlierError: boolean
  initialHistoryStatus: InitialHistoryLoadStatus
  initialLoadSurface: ChatSessionLoadSurface
}): boolean {
  return options.loadEarlierError
    || (
      options.initialHistoryStatus === 'error'
      && options.initialLoadSurface === null
    )
}

export function shouldShowHistorySentinelLoading(options: {
  loadingEarlier: boolean
  historyLoading: boolean
  historyRetrying: boolean
  initialHistoryStatus: InitialHistoryLoadStatus
  initialLoadSurface: ChatSessionLoadSurface
}): boolean {
  return options.loadingEarlier
    || (options.historyRetrying && options.initialLoadSurface === null)
    || (
      options.historyLoading
      && options.initialHistoryStatus === 'loading'
      && options.initialLoadSurface === null
    )
}
