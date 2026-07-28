export type InitialHistoryLoadStatus = 'pending' | 'loading' | 'ready' | 'error'
export type ChatHistoryRecoveryState =
  | 'history-loading'
  | 'history-retrying'
  | 'history-error'
  | null
export type ChatSessionRecoveryState =
  | Exclude<ChatHistoryRecoveryState, null>
  | 'live-connecting'
  | 'live-degraded'

export interface ResolveChatHistoryRecoveryStateOptions {
  isDraftLanding: boolean
  initialHistoryStatus: InitialHistoryLoadStatus
  retrying: boolean
  recoveryError?: boolean
}

export function resolveChatHistoryRecoveryState(
  options: ResolveChatHistoryRecoveryStateOptions,
): ChatHistoryRecoveryState {
  if (options.isDraftLanding) return null
  if (options.retrying) return 'history-retrying'
  if (options.recoveryError || options.initialHistoryStatus === 'error') {
    return 'history-error'
  }
  if (
    options.initialHistoryStatus === 'pending'
    || options.initialHistoryStatus === 'loading'
  ) return 'history-loading'
  return null
}

export function shouldShowConfirmedEmptySession(options: {
  isDraftLanding: boolean
  isStreaming: boolean
  messageCount: number
  initialHistoryStatus: InitialHistoryLoadStatus
}): boolean {
  return !options.isDraftLanding
    && !options.isStreaming
    && options.messageCount === 0
    && options.initialHistoryStatus === 'ready'
}
