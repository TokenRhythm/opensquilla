export type RpcConnectionState = 'disconnected' | 'connecting' | 'connected'
export type ChatLiveConnectionPhase = 'idle' | 'connecting' | 'ready' | 'degraded'

/**
 * A healthy WebSocket is necessary but not sufficient for live chat. The
 * topbar must stay non-green while the current session subscription is still
 * recovering or has degraded to history-only mode.
 */
export function effectiveChatConnectionState(
  rpcState: RpcConnectionState,
  livePhase: ChatLiveConnectionPhase,
  chatRoute: boolean,
): RpcConnectionState {
  if (!chatRoute || rpcState !== 'connected') return rpcState
  if (livePhase === 'connecting') return 'connecting'
  if (livePhase === 'degraded') return 'disconnected'
  return 'connected'
}
