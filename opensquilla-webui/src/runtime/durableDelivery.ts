import { TurnCommandError } from '@/modules/turnCommands'
import type {
  TurnCommands, TurnCommandRequestOptions, TurnReceiptRequest, TurnReceiptResult,
  TurnSendResponse, TurnSteerResponse, TurnCancelRequest, TurnCancelResponse,
} from '@/modules/turnCommands'
import type { DeliverySnapshot, DeliveryWaitReason, DurableDelivery, DeliveryUpdate } from '@/modules/delivery'
import type { PendingInputWal, DeliveryWalRecord, ResponseHandoffWalRecord } from '@/utils/chat/pendingInputWal'

interface DeliveryAccess {
  identity(): string | null
  available(): boolean
  generation(): number
}

interface DeliveryOptions {
  commands: TurnCommands
  wal: PendingInputWal | null
  access: DeliveryAccess
  now?: () => number
  ownerId?: string
}

const LEASE_MS = 60_000
const RENEW_MS = 10_000
const ROUND_MS = 30_000
const MAX_CALLS = 4
const DELAYS = [250, 1_000, 4_000] as const

/** Snapshot domain values without retaining framework proxies in the WAL. */
function snapshotDomain<T>(value: T, seen = new WeakMap<object, unknown>()): T {
  if (value === null || typeof value !== 'object') return value
  if (value instanceof Blob || value instanceof Date || value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    return structuredClone(value)
  }
  const existing = seen.get(value)
  if (existing) return existing as T
  const result: unknown[] | Record<string, unknown> = Array.isArray(value) ? [] : {}
  seen.set(value, result)
  for (const [key, child] of Object.entries(value)) (result as Record<string, unknown>)[key] = snapshotDomain(child, seen)
  return result as T
}

function requestIdentity(request: TurnReceiptRequest): { id: string; session: string } {
  if (request.kind === 'steer') return { id: request.request.clientRequestId, session: request.request.key }
  const params = request.request.params
  return { id: params.clientRequestId || '', session: request.request.kind === 'new-turn'
    ? request.request.params.sessionKey : request.request.params.key }
}

function unfinished(record: DeliveryWalRecord): boolean {
  return record.phase === 'prepared' || record.phase === 'submitting' || record.phase === 'unknown' || !!(record.stop && !record.stop.completed)
}

function taskIdentity(record: DeliveryWalRecord): string | undefined {
  const response = record.response
  if (response && 'disposition' in response && response.disposition === 'promoted' && response.promotedTurnId) return response.promotedTurnId
  return response?.taskId || (response && 'promotedTurnId' in response ? response.promotedTurnId : undefined)
    || (response && 'turnId' in response ? response.turnId : undefined)
}

/** Delivery owns only admission and exact Stop; socket, reads and event recovery keep their existing owners. */
export function createDurableDelivery(options: DeliveryOptions): DurableDelivery {
  const now = options.now || Date.now
  const owner = options.ownerId || crypto.randomUUID()
  const wal = options.wal
  const listeners = new Set<(record: DeliveryUpdate) => void>()
  const observed = new Map<string, string>()
  const changes = new Set<() => void>()
  const summaries = new Map<string, DeliverySnapshot>()
  const flights = new Map<string, Promise<TurnSendResponse | TurnSteerResponse>>()
  const handoffOwners = new Map<string, { owner: string; revision: number }>()
  const stopIntents = new Set<string>()
  const controllers = new Set<AbortController>()
  const renewals = new Set<ReturnType<typeof setInterval>>()
  const controllerIdentities = new Map<AbortController, string>()
  const roundTriggers = new Map<string, string>()
  const manualRetries = new Set<string>()
  const receiptEventTokens = new Map<string, string>()
  const activeLeases = new Map<string, number>()
  let disposed = false
  let invalidated = false
  let quarantineChecked = false
  let recovery: Promise<void> | null = null
  let recoveryAgain = false
  let ordinaryActive = 0
  let stopActive = 0
  let leaseWakeTimer: ReturnType<typeof setTimeout> | undefined
  let leaseWakeAt = Infinity
  const ordinaryWaiters: Array<() => void> = []
  const stopWaiters: Array<() => void> = []
  const stopInvalidation = wal?.onInvalidated?.(() => {
    invalidated = true
    for (const controller of controllers) controller.abort()
    for (const timer of renewals) clearInterval(timer)
    summaries.set('delivery-storage-reload', { id: 'delivery-storage-reload', sessionKey: '', phase: 'unknown', stopPending: false, waitReason: 'reload' })
    for (const listener of changes) listener()
  })

  function storageReady(): void {
    if (invalidated || !wal?.getDelivery || !wal.prepareDelivery || !wal.compareAndSwapDelivery || (!wal.listRecoveryDeliveries && !wal.listDeliveries)) {
      throw new TurnCommandError('unavailable', 'Durable delivery storage is unavailable', 'DELIVERY_STORAGE_UNAVAILABLE', false, true)
    }
  }

  function publish(record: DeliveryWalRecord, waitReason?: DeliveryWaitReason | null) {
    const previous = summaries.get(record.ownerRequestId)
    const reason = waitReason === undefined && previous?.phase === record.phase
      ? previous.waitReason : waitReason
    const summary: DeliverySnapshot = {
      id: record.ownerRequestId, sessionKey: record.requestSessionKey, phase: record.phase,
      stopPending: !!(record.stop && !record.stop.completed), ...(reason ? { waitReason: reason } : {}),
    }
    if (JSON.stringify(summaries.get(record.ownerRequestId)) !== JSON.stringify(summary)) {
      summaries.set(record.ownerRequestId, summary)
      for (const listener of changes) listener()
    }
    const update: DeliveryUpdate = {
      ownerRequestId: record.ownerRequestId, deliveryIdentity: record.deliveryIdentity,
      requestSessionKey: record.requestSessionKey, phase: record.phase,
      response: record.response, stop: record.stop, kind: record.request?.kind,
    }
    const signature = JSON.stringify(update)
    if (observed.get(record.ownerRequestId) !== signature) {
      observed.set(record.ownerRequestId, signature)
      for (const listener of listeners) listener(structuredClone(update))
    }
    // Completed receipts stay on disk for deduplication, while the app retains
    // only a small notification window. Unresolved delivery is never evicted.
    const completed = [...summaries.values()].filter(item => item.phase === 'accepted' && !item.stopPending)
    for (const item of completed.slice(0, Math.max(0, completed.length - 128))) {
      summaries.delete(item.id); observed.delete(item.id)
    }
  }

  function allowed(record: DeliveryWalRecord): DeliveryWaitReason | undefined {
    if (!options.access.identity() || options.access.identity() !== record.deliveryIdentity) return 'identity'
    if (!options.access.available()) return 'offline'
    return undefined
  }

  async function update(id: string, mutate: (current: DeliveryWalRecord) => DeliveryWalRecord | null): Promise<DeliveryWalRecord | null> {
    storageReady()
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const current = await wal!.getDelivery!(id)
      if (!current) return null
      const next = mutate(current)
      if (!next) return current
      const result = await wal!.compareAndSwapDelivery!(id, current.revision, {
        ...next, revision: current.revision + 1, updatedAt: now(),
      })
      if (result.applied) { if (result.record) publish(result.record); return result.record }
    }
    throw new Error('Delivery record changed concurrently')
  }

  async function slot<T>(stop: boolean, operation: () => Promise<T>): Promise<T> {
    const waiters = stop ? stopWaiters : ordinaryWaiters
    if ((stop ? stopActive : ordinaryActive) >= (stop ? 1 : 2)) {
      await new Promise<void>(resolve => waiters.push(resolve))
    } else if (stop) stopActive += 1
    else ordinaryActive += 1
    if (disposed) throw new TurnCommandError('aborted', 'Delivery owner disposed', undefined, null)
    try { return await operation() } finally {
      const next = waiters.shift()
      // Transfer the occupied slot before waking its waiter; a newly arriving
      // caller must not steal the same capacity in the intervening microtask.
      if (next) next()
      else if (stop) stopActive -= 1
      else ordinaryActive -= 1
    }
  }

  async function fenced<T>(record: DeliveryWalRecord, signal: AbortSignal | undefined,
    operation: (requestOptions: TurnCommandRequestOptions) => Promise<T>): Promise<T> {
    const reason = allowed(record)
    if (reason || disposed) throw new TurnCommandError('unavailable', 'Delivery is waiting for its original connection', 'DELIVERY_WAITING', false, true)
    if (signal?.aborted) throw new TurnCommandError('aborted', 'Delivery was cancelled before dispatch', 'DELIVERY_NOT_SENT', false)
    const generation = options.access.generation()
    return operation({ signal, expectedGeneration: generation })
  }

  async function claim(record: DeliveryWalRecord): Promise<DeliveryWalRecord | null> {
    let epoch: number | undefined
    const claimed = await update(record.ownerRequestId, current => {
      epoch = undefined
      if (current.lease && current.lease.expiresAt > now()) return null
      epoch = (current.lease?.epoch || 0) + 1
      return { ...current, lease: { owner, epoch, expiresAt: now() + LEASE_MS } }
    })
    return epoch !== undefined && claimed?.lease?.owner === owner && claimed.lease.epoch === epoch ? claimed : null
  }

  async function leased<T>(record: DeliveryWalRecord, run: (controller: AbortController, claimed: DeliveryWalRecord) => Promise<T>): Promise<T | undefined> {
    const claimed = await claim(record)
    if (!claimed) {
      const current = await wal!.getDelivery!(record.ownerRequestId)
      publish(current || record, 'lease')
      const wakeAt = (current?.lease?.expiresAt || now() + LEASE_MS) + 25
      if (wakeAt < leaseWakeAt && !disposed) {
        if (leaseWakeTimer) clearTimeout(leaseWakeTimer)
        leaseWakeAt = wakeAt
        leaseWakeTimer = setTimeout(() => { leaseWakeTimer = undefined; leaseWakeAt = Infinity; void wake() }, Math.max(1, wakeAt - now()))
      }
      return undefined
    }
    const epoch = claimed.lease!.epoch
    activeLeases.set(record.ownerRequestId, epoch)
    const controller = new AbortController()
    controllers.add(controller)
    controllerIdentities.set(controller, record.deliveryIdentity)
    let renewing = false
    const timer = globalThis.setInterval(() => {
      if (renewing) return
      renewing = true
      void update(record.ownerRequestId, current => {
        if (current.lease?.owner !== owner || current.lease.epoch !== epoch) { controller.abort(); return null }
        return { ...current, lease: { ...current.lease, expiresAt: now() + LEASE_MS } }
      }).catch(() => controller.abort()).finally(() => { renewing = false })
    }, RENEW_MS)
    renewals.add(timer)
    try { return await run(controller, claimed) } finally {
      clearInterval(timer)
      renewals.delete(timer)
      controllers.delete(controller)
      controllerIdentities.delete(controller)
      await update(record.ownerRequestId, current => current.lease?.owner === owner && current.lease.epoch === epoch
        ? { ...current, lease: { ...current.lease, expiresAt: 0 } } : null).catch(() => {})
      if (activeLeases.get(record.ownerRequestId) === epoch) activeLeases.delete(record.ownerRequestId)
      if (manualRetries.has(record.ownerRequestId)) void wake()
    }
  }

  async function receive(id: string, response: TurnSendResponse | TurnSteerResponse, epoch: number) {
    let accepted = false
    const result = await update(id, current => {
      accepted = current.lease?.owner === owner && current.lease.epoch === epoch && current.lease.expiresAt > now()
      if (!accepted) return null
      const steer = current.request?.kind === 'steer'
      const terminal = ('taskStatus' in response && ['succeeded', 'completed', 'finished', 'cancelled', 'canceled', 'failed', 'timeout', 'abandoned', 'interrupted', 'aborted', 'stopped'].includes(response.taskStatus || ''))
        || (steer && 'disposition' in response && ['cancelled', 'rejected'].includes(response.disposition || ''))
      return { ...current, phase: steer && 'accepted' in response && response.accepted === false ? 'not-sent'
        : steer && (!('accepted' in response) || response.accepted !== true) ? 'unknown' : 'accepted',
      response: structuredClone(response), ...(terminal && current.stop ? { stop: { ...current.stop, completed: true } } : {}) }
    })
    if (!accepted) throw new TurnCommandError('session-changed', 'Delivery lease changed', 'DELIVERY_LEASE_LOST', null, true)
    return result
  }

  async function lookupRecord(record: DeliveryWalRecord, signal: AbortSignal | undefined, epoch: number): Promise<TurnReceiptResult> {
    if (!record.request || !options.commands.lookupReceipt || options.commands.supportsReceiptLookup?.() === false) {
      publish(record, 'receipt-unsupported')
      return { status: 'unsupported' }
    }
    const result = await fenced(record, signal, opts => options.commands.lookupReceipt!(record.request!, opts))
    if (result.status === 'found') await receive(record.ownerRequestId, result.response, epoch)
    else publish(record, result.status === 'unsupported' ? 'receipt-unsupported' : 'receipt-missing')
    return result
  }

  async function dispatch(request: TurnReceiptRequest, commandOptions?: TurnCommandRequestOptions) {
    request = snapshotDomain(request)
    const { id, session } = requestIdentity(request)
    if (!id || !session) throw new TurnCommandError('rejected', 'Delivery requires a stable request identity', 'DELIVERY_ID_REQUIRED', false)
    const existingFlight = flights.get(id)
    if (existingFlight) return existingFlight
    const identity = options.access.identity()
    if (!identity) throw new TurnCommandError('unavailable', 'Delivery identity is unavailable', 'DELIVERY_IDENTITY_REQUIRED', false, true)
    const operation = (async () => {
      storageReady()
      const created = await wal!.prepareDelivery!({
        schemaVersion: 2, ownerRequestId: id, deliveryIdentity: identity, requestSessionKey: session,
        request: structuredClone(request), phase: 'prepared', revision: 1, createdAt: now(), updatedAt: now(),
        ...(stopIntents.has(id) ? { stop: { requested: true as const } } : {}),
        ...(request.kind === 'send' && request.request.kind === 'new-turn' ? {
          handoff: {
            schemaVersion: 1 as const, ownerRequestId: id, requestSessionKey: session,
            clientRequestId: id, clientMessageId: request.request.params.clientMessageId || id,
            params: structuredClone(request.request.params), composerText: request.request.params.message,
            recoveryAttachments: [], state: 'submitting' as const, createdAt: now(), updatedAt: now(),
          },
        } : {}),
      }, handoffOwners.get(id)).catch(error => {
        throw new TurnCommandError('unavailable', error instanceof Error ? error.message : 'Durable delivery could not be saved',
          'DELIVERY_STORAGE_UNAVAILABLE', false, true)
      })
      handoffOwners.delete(id)
      const record = created.record
      if (!record || record.deliveryIdentity !== identity) {
        throw new TurnCommandError('conflict', 'Delivery belongs to another identity or an older client', 'DELIVERY_QUARANTINED', null, false)
      }
      if (stopIntents.has(id)) await requestStop(id)
      publish(record)
      if (!created.applied && record.phase === 'accepted' && record.response) return record.response
      // A previously submitted frame is never sent again, even if lookup is
      // missing or a new frame would be locally rejected before transmission.
      const result = await slot(false, () => leased(record, async (controller, claimed) => {
        if (claimed.phase === 'accepted' && claimed.response) return claimed.response
        if (claimed.phase !== 'prepared' && claimed.phase !== 'not-sent') {
          const receipt = await lookupRecord(claimed, controller.signal, claimed.lease!.epoch)
          if (receipt.status === 'found') return receipt.response
          throw new TurnCommandError('unavailable', 'Delivery receipt is still unknown', 'DELIVERY_RECEIPT_UNKNOWN', null, true)
        }
        const epoch = claimed.lease!.epoch
        try {
          const armed = await update(id, current => {
            if (current.lease?.owner !== owner || current.lease.epoch !== epoch
              || (current.phase !== 'prepared' && current.phase !== 'not-sent')) {
              throw new TurnCommandError('session-changed', 'Delivery lease changed', 'DELIVERY_LEASE_LOST', null)
            }
            return { ...current, phase: 'submitting', response: undefined }
          })
          const frozen = armed?.request
          if (!armed || !frozen) throw new TurnCommandError('conflict', 'Delivery request missing', 'DELIVERY_REQUEST_MISSING', null)
          const abort = () => controller.abort()
          commandOptions?.signal?.addEventListener('abort', abort, { once: true })
          if (commandOptions?.signal?.aborted) controller.abort()
          let response: TurnSendResponse | TurnSteerResponse
          try { response = await fenced(armed, controller.signal, opts => frozen.kind === 'send'
            ? options.commands.send(frozen.request, opts) : options.commands.steer(frozen.request, opts))
          } finally { commandOptions?.signal?.removeEventListener('abort', abort) }
          await receive(id, response, epoch)
          return response
        } catch (error) {
          await update(id, current => current.lease?.owner === owner && current.lease.epoch === epoch ? ({ ...current,
            phase: error instanceof TurnCommandError && error.accepted === false ? 'not-sent' : 'unknown',
          }) : null).catch(() => {})
          throw error
        }
      }))
      if (!result) throw new TurnCommandError('unavailable', 'Another client is recovering this delivery', 'DELIVERY_LEASED', null, true)
      return result
    })().finally(() => {
      flights.delete(id)
      void wake()
    })
    flights.set(id, operation)
    return operation
  }

  async function requestStop(id: string): Promise<void> {
    stopIntents.add(id)
    manualRetries.add(id)
    const record = await update(id, current => current.deliveryIdentity === options.access.identity()
      ? { ...current, stop: { ...current.stop, requested: true, completed: false } } : null)
    if (record) { stopIntents.delete(id); void wake() }
  }

  async function requestSteerStop(sessionKey: string, expectedTurnId: string): Promise<void> {
    if (!expectedTurnId) return
    storageReady()
    const identity = options.access.identity()
    if (!identity) return
    const records = wal!.findSteerDeliveries
      ? await wal!.findSteerDeliveries(identity, sessionKey, expectedTurnId)
      : (await wal!.listDeliveries!()).filter(record => record.deliveryIdentity === identity
        && record.request?.kind === 'steer' && record.request.request.key === sessionKey
        && record.request.request.expectedTurnId === expectedTurnId)
    for (const record of records) {
      if (record.phase === 'not-sent' || record.stop?.completed) continue
      const response = record.response
      if (response && 'disposition' in response && ['rejected', 'cancelled'].includes(response.disposition || '')) continue
      await requestStop(record.ownerRequestId)
    }
  }

  async function noteReceiptChanged(id: string, token: string): Promise<void> {
    if (disposed || invalidated || !token || receiptEventTokens.get(id) === token) return
    if (!summaries.has(id) && !flights.has(id)) return
    const record = await wal?.getDelivery?.(id)
    if (!record || !unfinished(record) || record.deliveryIdentity !== options.access.identity() || record.paused) return
    receiptEventTokens.set(id, token)
    if (receiptEventTokens.size > 256) receiptEventTokens.delete(receiptEventTokens.keys().next().value!)
    const fresh = record.stop && record.phase === 'accepted'
      ? await update(id, current => ({ ...current, phase: 'unknown' })) : record
    manualRetries.add(id)
    if (activeLeases.has(id)) return
    if (fresh) await round(fresh)
  }

  async function stopRecord(record: DeliveryWalRecord, signal: AbortSignal | undefined, epoch: number, recovering = false): Promise<TurnCancelResponse | undefined> {
    const promoted = record.request?.kind === 'steer' && record.response && 'disposition' in record.response
      && record.response.disposition === 'promoted'
    const taskId = promoted ? taskIdentity(record) : record.stop?.request?.taskId || taskIdentity(record)
    if (!record.stop || record.stop.completed || !taskId) return undefined
    const request: TurnCancelRequest = (!promoted && record.stop.request) || {
      sessionKey: record.response?.sessionKey || record.response?.key || record.requestSessionKey,
      taskId, source: 'webui_stop', scope: 'task',
    }
    const response = await slot(true, () => fenced(record, signal, opts => options.commands.cancel(request, opts)))
    const inactive = ['task_not_active', 'task_mismatch'].includes(response.reason || '')
    const pendingSteer = record.request?.kind === 'steer' && (!record.response
      || !('disposition' in record.response) || !['applied', 'promoted', 'cancelled', 'rejected'].includes(record.response.disposition || ''))
    if (!response.aborted && inactive && pendingSteer) {
      // The old turn ending does not prove that a queued Steer was cancelled:
      // its receipt can move to a promoted task after this exact Stop answer.
      await update(record.ownerRequestId, current => current.lease?.owner === owner && current.lease.epoch === epoch
        ? { ...current, phase: 'unknown' } : null)
      if (!recovering) roundTriggers.delete(record.ownerRequestId)
    } else if (response.aborted || inactive) {
      await update(record.ownerRequestId, current => current.lease?.owner === owner && current.lease.epoch === epoch
        ? { ...current, stop: { requested: true, request, completed: true } } : null)
    }
    return response
  }

  async function cancel(request: TurnCancelRequest, commandOptions?: TurnCommandRequestOptions): Promise<TurnCancelResponse> {
    // Preserve the existing explicit session/group Stop surface. Unknown
    // acceptance Stop is separately attached to its request by useChatSend.
    if (!request.taskId || request.scope !== 'task') return options.commands.cancel(request, commandOptions)
    const identity = options.access.identity()
    const generation = options.access.generation()
    let record: DeliveryWalRecord | null = null
    try {
      storageReady()
      if (!identity) throw new Error('Stop identity unavailable')
      record = wal!.findDeliveryByTask
        ? await wal!.findDeliveryByTask(identity, request.sessionKey, request.taskId)
        : (await wal!.listDeliveries!()).find(item => item.deliveryIdentity === identity && item.requestSessionKey === request.sessionKey
          && (item.stop?.request?.taskId === request.taskId || taskIdentity(item) === request.taskId)) || null
      if (record) record = await update(record.ownerRequestId, current => ({ ...current, stop: { requested: true, request: structuredClone(request) } }))
      else {
        const created = await wal!.prepareDelivery!({ schemaVersion: 2, ownerRequestId: `stop:${crypto.randomUUID()}`,
          deliveryIdentity: identity, requestSessionKey: request.sessionKey, phase: 'accepted',
          stop: { requested: true, request: structuredClone(request) }, revision: 1, createdAt: now(), updatedAt: now() })
        record = created.record
      }
    } catch {
      // A known exact task Stop remains best effort if durable storage fails.
      if (identity !== options.access.identity()) throw new TurnCommandError('session-changed', 'Stop identity changed', 'DELIVERY_IDENTITY_CHANGED', null)
      const id = `volatile-stop:${request.sessionKey}:${request.taskId}`
      summaries.set(id, { id, sessionKey: request.sessionKey, phase: 'accepted', stopPending: true, waitReason: 'storage' })
      for (const listener of changes) listener()
      return slot(true, () => options.commands.cancel(request, { ...commandOptions, expectedGeneration: generation }))
    }
    if (!record) return { aborted: false, reason: 'task_cancel_unknown' }
    try {
      const result = await leased(record!, (controller, claimed) => stopRecord(claimed, controller.signal, claimed.lease!.epoch))
      return result || { aborted: false, reason: 'task_cancel_unknown' }
    } finally { void wake() }
  }

  async function round(record: DeliveryWalRecord): Promise<void> {
    if (record.phase === 'prepared') { publish(record, 'not-sent'); return }
    const waitReason = allowed(record)
    if (waitReason) { publish(record, waitReason); return }
    if (flights.has(record.ownerRequestId)) return
    if (record.paused) { publish(record, record.paused === 'authority' ? 'permission' : 'conflict'); return }
    const trigger = `${record.deliveryIdentity}:${options.access.generation()}`
    if (!manualRetries.has(record.ownerRequestId) && roundTriggers.get(record.ownerRequestId) === trigger) return
    const run = () => leased(record, async (controller, claimed) => {
      const epoch = claimed.lease!.epoch
      roundTriggers.set(record.ownerRequestId, trigger)
      manualRetries.delete(record.ownerRequestId)
      const deadline = globalThis.setTimeout(() => controller.abort(), ROUND_MS)
      try {
        for (let calls = 0; calls < MAX_CALLS && !controller.signal.aborted; calls += 1) {
          const current = await wal!.getDelivery!(record.ownerRequestId)
          if (!current || !unfinished(current)) return
          if (current.lease?.owner !== owner || current.lease.epoch !== epoch) return
          const waiting = allowed(current)
          if (waiting) { publish(current, waiting); return }
          try {
            if (current.phase === 'prepared') { publish(current, 'not-sent'); return }
            if (current.phase === 'accepted' && current.stop) await stopRecord(current, controller.signal, epoch, true)
            else if (current.request) {
              const result = await lookupRecord(current, controller.signal, epoch)
              if (result.status === 'unsupported') return
            }
          } catch (error) {
            if (error instanceof TurnCommandError && (error.kind === 'conflict'
              || /FORBIDDEN|UNAUTHORIZED|PERMISSION|SCOPE|IDENTITY|FINGERPRINT/i.test(error.failureCode || ''))) {
              const paused = error.kind === 'conflict' ? 'conflict' : 'authority'
              const latest = await update(current.ownerRequestId, value => ({ ...value, paused }))
              if (latest) publish(latest, paused === 'conflict' ? 'conflict' : 'permission')
              return
            }
            // Local failures only describe this read and never settle ingress.
          }
          const latest = await wal!.getDelivery!(record.ownerRequestId)
          if (!latest || !unfinished(latest)) return
          if (calls < MAX_CALLS - 1) await new Promise<void>(resolve => {
            const timer = globalThis.setTimeout(done, DELAYS[calls]!)
            function done() { clearTimeout(timer); controller.signal.removeEventListener('abort', done); resolve() }
            controller.signal.addEventListener('abort', done, { once: true })
            if (controller.signal.aborted) done()
          })
        }
        const latest = await wal!.getDelivery!(record.ownerRequestId)
        if (latest && unfinished(latest)) publish(latest, 'budget')
      } finally { clearTimeout(deadline) }
    })
    await (record.phase === 'accepted' && record.stop ? run() : slot(false, run))
      .catch(() => publish(record, 'storage'))
  }

  function wake(): Promise<void> {
    if (disposed || invalidated) return Promise.resolve()
    for (const [controller, identity] of controllerIdentities) {
      if (identity !== options.access.identity()) controller.abort()
    }
    if (recovery) { recoveryAgain = true; return recovery }
    const operation = (async () => {
      try {
        storageReady()
        if (!quarantineChecked && wal!.countQuarantinedDeliveries) {
          quarantineChecked = true
          if (await wal!.countQuarantinedDeliveries() > 0) {
            summaries.set('legacy-deliveries', { id: 'legacy-deliveries', sessionKey: '', phase: 'unknown', stopPending: false, waitReason: 'legacy' })
            for (const listener of changes) listener()
          }
        }
        let after: string | undefined
        do {
          const page = wal!.listRecoveryDeliveries ? await wal!.listRecoveryDeliveries(after, 16)
            : { records: (await wal!.listDeliveries!()).filter(unfinished), next: undefined }
          const pending = page.records
          const stops = pending.filter(record => record.phase === 'accepted' && record.stop)
          const admissions = pending.filter(record => !(record.phase === 'accepted' && record.stop))
          async function consume(queue: DeliveryWalRecord[]) {
            while (!disposed && !invalidated && queue.length) await round(queue.shift()!)
          }
          await Promise.all([consume(stops), consume(admissions), consume(admissions)])
          after = page.next
        } while (after && !disposed && !invalidated)
      } catch { /* Admission reports storage failures; wake does not break app startup. */ }
    })().finally(() => {
      recovery = null
      // Only an external state change or a new foreground result wakes another
      // bounded round. No timer is left polling a parked unknown delivery.
      if (recoveryAgain) { recoveryAgain = false; void wake() }
    })
    recovery = operation
    return operation
  }

  return {
    commands: {
      send: (request, opts) => dispatch({ kind: 'send', request }, opts) as Promise<TurnSendResponse>,
      steer: (request, opts) => dispatch({ kind: 'steer', request }, opts) as Promise<TurnSteerResponse>,
      cancel,
      supports: capability => options.commands.supports(capability),
      supportsReceiptLookup: () => options.commands.supportsReceiptLookup?.() ?? false,
      lookupReceipt: request => lookup(request),
    },
    registerPreparedHandoff(record: ResponseHandoffWalRecord) {
      if (record.walOwnerId && record.walRevision) handoffOwners.set(record.ownerRequestId,
        { owner: record.walOwnerId, revision: record.walRevision })
    },
    requestStop,
    requestSteerStop,
    noteReceiptChanged,
    observe(listener) { listeners.add(listener); return () => { listeners.delete(listener) } },
    snapshots: () => [...summaries.values()],
    subscribe(listener) { changes.add(listener); return () => { changes.delete(listener) } },
    get: id => wal?.getDelivery?.(id) || Promise.resolve(null),
    async retry(id) {
      if (id) {
        manualRetries.add(id)
        const record = await update(id, current => current.deliveryIdentity === options.access.identity() ? { ...current, paused: undefined } : null)
        if (record) await round(record)
        return
      }
      await wake()
    },
    wake,
    lookup,
    dispose() {
      disposed = true
      if (leaseWakeTimer) clearTimeout(leaseWakeTimer)
      for (const controller of controllers) controller.abort()
      for (const timer of renewals) clearInterval(timer)
      renewals.clear()
      stopInvalidation?.()
      for (const resolve of [...ordinaryWaiters.splice(0), ...stopWaiters.splice(0)]) resolve()
      listeners.clear(); changes.clear(); summaries.clear(); observed.clear(); handoffOwners.clear(); stopIntents.clear(); receiptEventTokens.clear()
      wal?.close()
    },
  }

  async function lookup(request: TurnReceiptRequest): Promise<TurnReceiptResult> {
    const record = await wal?.getDelivery?.(requestIdentity(request).id)
    if (!record || allowed(record)) return { status: 'not-found' }
    if (record.phase === 'accepted' && record.response) return { status: 'found', response: record.response }
    // Recovery stays owned by the app scheduler and its bounded leases.
    void wake()
    return { status: options.commands.supportsReceiptLookup?.() === false ? 'unsupported' : 'not-found' }
  }
}
