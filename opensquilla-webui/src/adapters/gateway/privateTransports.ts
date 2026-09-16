import type {
  TransportCallOptions,
  TransportConnectionWaitOptions,
  TransportEventHandler,
  TransportConsumptionHandler,
  TransportDeliveryReceipt,
  TransportInstalledReceipt,
  TransportGapHandler,
} from './transportTypes'
import { TransportFlowV4 } from './transportFlowV4'
import type { SessionsMessagesSnapshotReadResult } from '@/contracts/generated/v4/sessionsMessagesSnapshotRead'
import { validateSessionsMessagesSnapshotReadResult } from '@/contracts/generated/v4/sessionsMessagesSnapshotReadValidators.mjs'

/**
 * Raw v4 transport capabilities.
 *
 * These interfaces are intentionally private to Gateway Adapters. Domain
 * Modules must expose typed operations instead of forwarding method names,
 * event names, URLs, or wire payloads to Vue code.
 */
export interface RpcTransport {
  request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  ready(options?: TransportReadyOptions): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
  acknowledgeDelivery?(receipt: TransportDeliveryReceipt): Promise<void> | void
  resumeFlow?(receipt: TransportInstalledReceipt): Promise<void> | void
  readonly generation: number
}

export type RpcRequester = Pick<RpcTransport, 'request'>

export interface EventTransport {
  subscribeConsumed?(event: string, handler: TransportConsumptionHandler): TransportSubscription
  subscribeGap?(handler: TransportGapHandler): TransportSubscription
  subscribe(event: string, handler: TransportEventHandler): TransportSubscription
  supports(event: string): boolean
}

export interface TransportReadyOptions extends TransportConnectionWaitOptions {
  timeoutMs?: number
  signal?: AbortSignal
}

export interface TransportSubscription {
  close(): void
}

export interface GatewayTransports {
  readonly rpc: RpcTransport
  readonly events: EventTransport
}

interface RpcStoreTransportSource {
  readonly connectionGeneration: number
  call<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: TransportCallOptions,
  ): Promise<T>
  on(event: string, handler: TransportEventHandler): () => void
  onConsumedEvent?(event: string, handler: TransportConsumptionHandler): () => void
  onGap?(handler: TransportGapHandler): () => void
  acknowledgeDelivery?(receipt: TransportDeliveryReceipt): Promise<void> | void
  resumeFlow?(receipt: TransportInstalledReceipt): Promise<void> | void
  enableConsumptionFlow?(): void
  consumeEvent?(event: string, payload: unknown, meta: Record<string, unknown>): Promise<'applied' | 'dirty'>
  recoverGap?(detail: unknown): Promise<boolean>
  hasRpcMethod(method: string): boolean
  hasRpcEvent(event: string): boolean
  rememberUnsupportedMethod(method: string): void
  ready(
    timeoutMs?: number,
    signal?: AbortSignal,
    actions?: TransportConnectionWaitOptions,
  ): Promise<void>
}

const flowOwners = new WeakMap<RpcStoreTransportSource, TransportFlowV4>()

function consumptionFlow(source: RpcStoreTransportSource): TransportFlowV4 | undefined {
  if (!source.enableConsumptionFlow || !source.consumeEvent || !source.recoverGap) return undefined
  let owner = flowOwners.get(source)
  if (!owner) {
    owner = new TransportFlowV4({
      request: (method, params, options) => source.call(method, params, options),
      on: (event, handler) => source.on(event, handler),
      enableConsumptionFlow: () => source.enableConsumptionFlow!(),
      consumeEvent: (event, payload, meta) => source.consumeEvent!(event, payload, meta),
      recoverGap: detail => source.recoverGap!(detail),
      get connectionGeneration() { return source.connectionGeneration },
    })
    flowOwners.set(source, owner)
    source.on('_orphan_response', (value: unknown) => {
      if (!value || typeof value !== 'object') return
      const detail = value as { generation?: unknown; payload?: unknown }
      if (detail.generation !== source.connectionGeneration) return
      // A timed-out/aborted read no longer has a byte owner. Validate the
      // complete snapshot response before returning its one recovery credit;
      // a coincidental `delivery` field in another result is not sufficient.
      // Discarding never resumes a domain or installs an obsolete snapshot.
      if (!validateSessionsMessagesSnapshotReadResult(detail.payload)) return
      const snapshot = detail.payload as SessionsMessagesSnapshotReadResult
      if (!snapshot.delivery) return
      void owner!.acknowledgeDelivery(snapshot.delivery).catch(() => {
        // Connection replacement invalidates both the receipt and its waiter.
      })
    })
  }
  return owner
}

/** Create the only generic wire-level capabilities exposed to v4 Adapters. */
export function createPrivateGatewayTransports(
  source: RpcStoreTransportSource,
): GatewayTransports {
  const flow = consumptionFlow(source)
  return {
    rpc: {
      ...(flow || source.acknowledgeDelivery ? {
        acknowledgeDelivery: (receipt: TransportDeliveryReceipt) => flow
          ? flow.acknowledgeDelivery(receipt) : source.acknowledgeDelivery!(receipt),
      } : {}),
      ...(flow || source.resumeFlow ? {
        resumeFlow: (receipt: TransportInstalledReceipt) => flow
          ? flow.resumeFlow(receipt) : source.resumeFlow!(receipt),
      } : {}),
      request(method, params, options) {
        return source.call(method, params, options)
      },
      ready(options) {
        return source.ready(
          options?.timeoutMs,
          options?.signal,
          options ? {
            timeoutAction: options.timeoutAction,
            abortAction: options.abortAction,
          } : undefined,
        )
      },
      supports(method) {
        return source.hasRpcMethod(method)
      },
      markUnsupported(method) {
        source.rememberUnsupportedMethod(method)
      },
      get generation() {
        return source.connectionGeneration
      },
    },
    events: {
      ...(source.onConsumedEvent ? {
        subscribeConsumed: (event: string, handler: TransportConsumptionHandler) => ({
          close: source.onConsumedEvent!(event, handler),
        }),
      } : {}),
      ...(source.onGap ? {
        subscribeGap: (handler: TransportGapHandler) => ({ close: source.onGap!(handler) }),
      } : {}),
      subscribe(event, handler) {
        const unsubscribe = source.on(event, handler)
        let closed = false
        return {
          close() {
            if (closed) return
            closed = true
            unsubscribe()
          },
        }
      },
      supports(event) {
        return source.hasRpcEvent(event)
      },
    },
  }
}
