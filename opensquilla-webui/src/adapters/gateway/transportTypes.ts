export type TransportTerminationAction = 'reject' | 'reconnect'

/** Request lifecycle policy shared only between the private transport and its Adapters. */
export interface TransportCallOptions {
  timeoutMs?: number
  signal?: AbortSignal
  timeoutAction?: TransportTerminationAction
  abortAction?: TransportTerminationAction
  expectedGeneration?: number
  onSent?: (socketGeneration: number) => void
}

export interface TransportConnectionWaitOptions {
  timeoutAction?: TransportTerminationAction
  abortAction?: TransportTerminationAction
}

export type TransportEventHandler = {
  bivarianceHack(...args: unknown[]): void
}['bivarianceHack']
