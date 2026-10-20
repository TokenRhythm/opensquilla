import type { InjectionKey } from 'vue'
import type { TurnCommands, TurnReceiptRequest } from './turnCommands'
import type { DeliveryWalRecord, ResponseHandoffWalRecord } from '@/utils/chat/pendingInputWal'

export type DeliveryWaitReason = 'offline' | 'identity' | 'receipt-unsupported' | 'receipt-missing' | 'storage' | 'lease' | 'budget'
  | 'permission' | 'conflict' | 'reload' | 'legacy' | 'not-sent' | 'storage-check'
export interface DeliverySnapshot {
  id: string
  sessionKey: string
  /** Derived from the current connection, never persisted with the request. */
  isCurrentIdentity: boolean
  phase: DeliveryWalRecord['phase']
  /** Current connection forbids new admission; this is not proof of a historical outcome. */
  admissionClosed?: boolean
  stopPending: boolean
  /** The owner permits recording an exact Stop, even while offline. */
  stopAvailable?: boolean
  /** Bounded user-facing request text, never attachment material. */
  preview?: string
  /** A fenced, unaccepted input still has a local draft to recover. */
  draftAvailable?: boolean
  waitReason?: DeliveryWaitReason
}
export type DeliveryUpdate = Pick<DeliveryWalRecord,
  'ownerRequestId' | 'deliveryIdentity' | 'requestSessionKey' | 'phase' | 'response' | 'stop' | 'retired' | 'acceptedUnavailable'> & {
    kind?: 'send' | 'steer'
    backgroundOnly?: true
  }

/** Application lifetime owner; subscribing never grants dispatch ownership. */
export interface DurableDelivery {
  readonly commands: TurnCommands
  registerPreparedHandoff(record: ResponseHandoffWalRecord): void
  /** Persistently release composer/navigation ownership, without resolving historical acceptance. */
  detachComposer(requestId: string): Promise<boolean>
  requestStop(requestId: string): Promise<void>
  requestSteerStop(sessionKey: string, expectedTurnId: string): Promise<void>
  noteReceiptChanged(requestId: string, eventToken: string): Promise<void>
  observe(listener: (record: DeliveryUpdate) => void): () => void
  snapshots(): readonly DeliverySnapshot[]
  subscribe(listener: () => void): () => void
  get(requestId: string): Promise<DeliveryWalRecord | null>
  retry(requestId?: string): Promise<void>
  wake(): Promise<void>
  lookup(request: TurnReceiptRequest): ReturnType<NonNullable<TurnCommands['lookupReceipt']>>
  dispose(): void
}

export const DURABLE_DELIVERY_KEY: InjectionKey<DurableDelivery> = Symbol('DurableDelivery')
