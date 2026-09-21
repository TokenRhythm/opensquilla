import type { InjectionKey } from 'vue'
import type { TurnCommands, TurnReceiptRequest } from './turnCommands'
import type { DeliveryWalRecord, ResponseHandoffWalRecord } from '@/utils/chat/pendingInputWal'

export type DeliveryWaitReason = 'offline' | 'identity' | 'receipt-unsupported' | 'receipt-missing' | 'storage' | 'lease' | 'budget'
  | 'permission' | 'conflict' | 'reload' | 'legacy' | 'not-sent'
export interface DeliverySnapshot {
  id: string
  sessionKey: string
  phase: DeliveryWalRecord['phase']
  stopPending: boolean
  waitReason?: DeliveryWaitReason
}
export type DeliveryUpdate = Pick<DeliveryWalRecord,
  'ownerRequestId' | 'deliveryIdentity' | 'requestSessionKey' | 'phase' | 'response' | 'stop'> & { kind?: 'send' | 'steer' }

/** Application lifetime owner; subscribing never grants dispatch ownership. */
export interface DurableDelivery {
  readonly commands: TurnCommands
  registerPreparedHandoff(record: ResponseHandoffWalRecord): void
  requestStop(requestId: string): Promise<void>
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
