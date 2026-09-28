import { setImmediate } from 'node:timers/promises'
import { afterEach } from 'vitest'
import { effectScope, ref } from 'vue'
import { createDurableDelivery } from '@/runtime/durableDelivery'
import type { PendingInputWal, ResponseHandoffWalRecord, DeliveryWalRecord } from '@/utils/chat/pendingInputWal'
import { useChatSend, type UseChatSendOptions } from './useChatSend'

export function memoryDeliveryWal(): PendingInputWal {
  const deliveries = new Map<string, DeliveryWalRecord>()
  const handoffs = new Map<string, ResponseHandoffWalRecord>()
  const handoff = (id: string) => deliveries.get(id)?.handoff ?? handoffs.get(id) ?? null
  const saveHandoff = (id: string, record: ResponseHandoffWalRecord | null) => {
    const delivery = deliveries.get(id)
    if (delivery) {
      const next = { ...delivery, revision: delivery.revision + 1 }
      if (record) next.handoff = structuredClone(record)
      else delete next.handoff
      deliveries.set(id, next)
    }
    if (record && !delivery) handoffs.set(id, structuredClone(record))
    else handoffs.delete(id)
  }
  return {
    put: async () => {}, list: async () => [], delete: async () => {}, close: () => {},
    putHandoff: async record => { saveHandoff(record.ownerRequestId, record) },
    prepareHandoff: async record => {
      const current = handoff(record.ownerRequestId)
      if (current || deliveries.has(record.ownerRequestId)) return { applied: false, record: structuredClone(current) }
      saveHandoff(record.ownerRequestId, record)
      return { applied: true, record: structuredClone(record) }
    },
    compareAndSwapHandoff: async (id, owner, revision, record) => {
      const current = handoff(id)
      if (!current || current.walOwnerId !== owner || current.walRevision !== revision) {
        return { applied: false, record: structuredClone(current) }
      }
      saveHandoff(id, record)
      return { applied: true, record: structuredClone(record) }
    },
    listHandoffs: async session => [...handoffs.values(), ...[...deliveries.values()].flatMap(row => row.handoff ? [row.handoff] : [])]
      .filter(row => !session || row.requestSessionKey === session).map(row => structuredClone(row)),
    acceptHandoff: async (id, acceptedSessionKey) => {
      const current = handoff(id)
      if (!current) throw new Error('missing handoff')
      const accepted = { ...current, state: 'accepted' as const, acceptedSessionKey, updatedAt: Date.now() }
      saveHandoff(id, accepted)
      return { handoff: accepted, records: [] }
    },
    deleteHandoff: async id => { saveHandoff(id, null) },
    getDelivery: async id => structuredClone(deliveries.get(id) ?? null),
    listDeliveries: async () => [...deliveries.values()].map(row => structuredClone(row)),
    prepareDelivery: async (record, owner) => {
      const existing = deliveries.get(record.ownerRequestId)
      if (existing) return { applied: false, record: structuredClone(existing) }
      const prepared = handoffs.get(record.ownerRequestId)
      if (prepared && (!owner || prepared.walOwnerId !== owner.owner || prepared.walRevision !== owner.revision
        || prepared.state !== 'submitting')) return { applied: false, record: null }
      const next = structuredClone({ ...record, ...(prepared ? { handoff: prepared } : {}) })
      deliveries.set(record.ownerRequestId, next)
      handoffs.delete(record.ownerRequestId)
      return { applied: true, record: structuredClone(next) }
    },
    compareAndSwapDelivery: async (id, revision, record) => {
      const current = deliveries.get(id)
      if (!current || current.revision !== revision) return { applied: false, record: structuredClone(current ?? null) }
      if (record) deliveries.set(id, structuredClone(record))
      else deliveries.delete(id)
      return { applied: true, record: structuredClone(record) }
    },
  }
}

type SendOptions = Omit<UseChatSendOptions, 'durableDelivery'> & Partial<Pick<UseChatSendOptions, 'durableDelivery'>>
const cleanups: (() => void)[] = []
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup() })

export function createChatSendHarness<T extends SendOptions>(input: T) {
  const deliveryIdentity = input.deliveryIdentity ?? ref('synthetic-identity')
  const pendingInputWal = input.pendingInputWal === undefined ? memoryDeliveryWal() : input.pendingInputWal
  const owner = input.durableDelivery ?? createDurableDelivery({
    commands: input.turnCommands, wal: pendingInputWal,
    access: { identity: () => deliveryIdentity.value, available: () => true, generation: () => 1 },
  })
  const options = { ...input, pendingInputWal, deliveryIdentity, durableDelivery: owner,
    turnCommands: input.durableDelivery ? input.turnCommands : owner.commands }
  const scope = effectScope()
  const api = scope.run(() => useChatSend(options))!
  cleanups.push(() => {
    api.dispose()
    scope.stop()
    if (!input.durableDelivery) owner.dispose()
  })
  return { api, options }
}

// Flush queued WAL/owner promises without advancing recovery timers.
export async function flushDelivery() { await setImmediate() }
