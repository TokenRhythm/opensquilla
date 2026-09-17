export type RpcConnectionState = 'disconnected' | 'connecting' | 'connected'
export type ChatLiveConnectionPhase = 'idle' | 'connecting' | 'ready' | 'degraded'

/**
 * The topbar reports physical Gateway transport only. Session subscription
 * recovery is rendered inside ChatView so a healthy shared socket is never
 * described as reconnecting during workspace navigation.
 */
export function effectiveChatConnectionState(
  rpcState: RpcConnectionState,
  _livePhase: ChatLiveConnectionPhase,
  _chatRoute: boolean,
): RpcConnectionState {
  return rpcState
}
