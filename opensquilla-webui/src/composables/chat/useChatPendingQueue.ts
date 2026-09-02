import { computed, nextTick, ref, watch, type Ref } from 'vue'
import type {
  Attachment,
  ChatPendingItem,
  HiddenControlDispatchResult,
  PendingSteerPhase,
} from '@/types/chat'
import type { SessionSteerV2Params } from '@/types/chat'
import type { AttachmentPreparationOptions } from '@/composables/chat/useChatAttachments'
import { isControlInput } from '@/utils/chat/inputSemantics'
import { createClientMessageId, createClientRequestId } from '@/utils/chat/messageIdentity'
import {
  isSendableAttachment,
  serializeSendableAttachment,
} from '@/utils/chat/attachments'
import { canonicalSessionKey } from '@/utils/chat/sessionKeys'
import type {
  AcceptedHandoffCommit,
  PendingInputWal,
  PendingInputWalRecord,
  PendingInputWalState,
} from '@/utils/chat/pendingInputWal'
import type {
  PendingInputQueuePort,
  PendingInputServerItem,
} from '@/modules/pendingInputQueue'
import { snapshotSteerRequest } from './useChatSteerDelivery'

const MAX_PENDING = 5
const MAX_REMOVAL_TOMBSTONES = 256
const MAX_PROMPT_ANNOTATION_IDS = 16

function normalizePromptAnnotationIds(
  ids: unknown,
): string[] {
  return (Array.isArray(ids) ? ids : [])
    .map(value => String(value || '').trim())
    .filter((value, index, values) => Boolean(value) && values.indexOf(value) === index)
    .slice(0, MAX_PROMPT_ANNOTATION_IDS)
}

type PendingReorderMode = 'local' | 'server'

interface PendingReorderSnapshot {
  mode: PendingReorderMode
  originalOrder: string[]
  expectedWalRevisions: Record<string, number>
}

interface ComposerAttachmentSnapshotEntry {
  readonly identity: Attachment
  readonly content: Readonly<Record<string, unknown>>
}

type ComposerAttachmentSnapshot = ReadonlyArray<ComposerAttachmentSnapshotEntry>

function snapshotComposerAttachments(
  attachments: readonly Attachment[],
): ComposerAttachmentSnapshot {
  return Object.freeze(attachments.map(attachment => Object.freeze({
    identity: attachment,
    content: Object.freeze({ ...attachment }) as Readonly<Record<string, unknown>>,
  })))
}

function composerAttachmentsMatch(
  attachments: readonly Attachment[],
  snapshot: ComposerAttachmentSnapshot,
): boolean {
  if (attachments.length !== snapshot.length) return false
  return attachments.every((attachment, index) => {
    const expected = snapshot[index]
    if (!expected || attachment !== expected.identity) return false
    const current = attachment as unknown as Record<string, unknown>
    const currentKeys = Object.keys(current)
    const expectedKeys = Object.keys(expected.content)
    return currentKeys.length === expectedKeys.length
      && expectedKeys.every(key => (
        Object.prototype.hasOwnProperty.call(current, key)
        && Object.is(current[key], expected.content[key])
      ))
  })
}

interface PendingQueueBroadcastMessage {
  sessionKey?: unknown
  pendingInputId?: unknown
  action?: unknown
}

export type BusySendMode = 'queue' | 'steer'
export type PendingDeliveryOutcome =
  | 'accepted'
  | 'deferred'
  | 'not_sent'
  | 'retryable_failure'

export interface PendingQueueOwner {
  ownerRequestId?: string
}

export interface PendingCancelOptions {
  retainAfterCancel?: boolean
  /** Internal post-restore cleanup must not invalidate sibling restores. */
  invalidateRestore?: boolean
}

export interface PendingQueueOwnerContext {
  sessionKey: string
  ownerRequestId: string
}

export interface PendingQueuePayload {
  text: string
  promptAnnotationIds?: readonly string[]
  attachments?: Attachment[]
  intent?: string | null
  confirmedPlainText?: boolean
}

export interface PendingSteerPayload {
  request: SessionSteerV2Params
  phase?: PendingSteerPhase
}

export interface UseChatPendingQueueOptions {
  sessionKey: Ref<string>
  ownerContext?: Readonly<Ref<PendingQueueOwnerContext | null>>
  inputText: Ref<string>
  pendingAttachments: Ref<Attachment[]>
  pendingSessionIntent: Ref<string | null>
  isStreaming: Ref<boolean>
  isBlocked: () => boolean
  autoResizeTextarea: () => void
  sendCurrentInput: () => void
  resetInputHistory: () => void
  hasComposer: () => boolean
  pendingInputWal?: PendingInputWal | null
  pendingInputQueue?: PendingInputQueuePort | null
  connectionState?: Readonly<Ref<string>>
  prepareAttachmentsForSend?: (
    options: Extract<AttachmentPreparationOptions, { ownership: 'detached' }>,
  ) => Promise<boolean>
  onPendingPersistenceError?: (
    reason: 'wal_failed' | 'attachments_unsupported' | 'server_rejected' | 'order_conflict',
  ) => void
  // Drain a queued hidden-control send (e.g. meta-preflight confirmation)
  // directly through the dedicated hidden-send path instead of the composer.
  dispatchHiddenControl?: (
    item: ChatPendingItem,
    ownerSessionKey: string,
  ) => Promise<PendingDeliveryOutcome>
  // Returning false for an explicit discard keeps the chip queued. This lets
  // the caller fail closed when it cannot persist the cancellation tombstone.
  onHiddenControlDispatchResult?: (result: HiddenControlDispatchResult) => void | boolean
  // The WebUI drains visible queue items through the same composer-preserving
  // transport used by explicit Steer. The legacy callback remains as a
  // fallback for isolated composable consumers.
  dispatchPendingItem?: (
    item: ChatPendingItem,
    ownerSessionKey: string,
  ) => Promise<PendingDeliveryOutcome>
}

export function useChatPendingQueue(options: UseChatPendingQueueOptions) {
  const queueSessionKey = (key = options.sessionKey.value) => {
    const value = String(key || '').trim()
    return value ? canonicalSessionKey(value) : ''
  }
  const walLookupSessionKeys = (key: string) => {
    const canonicalKey = queueSessionKey(key)
    const keys = new Set([canonicalKey])
    const rawKey = String(key || '').trim()
    if (rawKey && queueSessionKey(rawKey) === canonicalKey) keys.add(rawKey)
    if (canonicalKey.startsWith('agent:main:')) {
      keys.add(`agent:default:${canonicalKey.slice('agent:main:'.length)}`)
    }
    const webchatPrefix = 'agent:main:webchat:'
    if (canonicalKey.startsWith(webchatPrefix)) {
      keys.add(`sess-${canonicalKey.slice(webchatPrefix.length)}`)
    }
    if (canonicalKey === 'agent:main:webchat:default') {
      keys.add('default')
      keys.add('webchat:default')
    }
    keys.delete('')
    return [...keys]
  }
  const pendingInputQueue = options.pendingInputQueue
  const pendingQueue = ref<ChatPendingItem[]>([])
  const parkedQueues = new Map<string, ChatPendingItem[]>()
  let activeQueueLease = 0
  let composerRevision = 0
  const stopComposerRevisionWatch = watch(
    [options.inputText, options.pendingAttachments, options.pendingSessionIntent],
    () => { composerRevision += 1 },
    { deep: true, flush: 'sync' },
  )
  let pendingDrainTimer: ReturnType<typeof setTimeout> | null = null
  let deferredDrainRequested = false
  const isReordering = ref(false)
  let reorderSnapshot: PendingReorderSnapshot | null = null
  let reorderCommitPromise: Promise<void> | null = null
  let deferredHydrateSession = ''
  let hydrateGeneration = 0
  let disposed = false
  const stagingOperations = new Map<string, Promise<void>>()
  const cancellationOperations = new Map<string, {
    promise: Promise<boolean>
    retainAfterCancel: boolean
  }>()
  const cancellationInvalidations = new Map<string, number>()
  const locallyCreatingIds = new Set<string>()
  const removedIdentityOrder: string[] = []
  const removedIdentities = new Set<string>()
  const broadcast = options.pendingInputWal && typeof BroadcastChannel !== 'undefined'
    ? new BroadcastChannel('opensquilla.pending-chat-inputs.v1')
    : null

  // A not-yet-durable Steer owns a separate transport slot. It must not
  // consume one of the five ordinary follow-up slots, regardless of whether
  // the Steer or the drafts were queued first.
  const ordinaryPendingCount = computed(() =>
    pendingQueue.value.filter(item => !item.steerAttempt).length,
  )
  const canQueueMore = computed(() => ordinaryPendingCount.value < MAX_PENDING)
  const canReorder = computed(() => pendingReorderMode() !== null)
  // A direct queued delivery owns its item until it succeeds, is removed, or
  // becomes eligible for another explicit retry.
  const hasDeliveryBarrier = computed(() =>
    pendingQueue.value.some(
      item => Boolean(
        item.deliveryState
        || item.steerAttempt
        || item.pendingPersistenceState === 'saving'
        || item.pendingPersistenceState === 'cancelling',
      ),
    ),
  )

  // Busy-composer delivery mode: 'queue' holds the message until the turn
  // ends (pending queue), 'steer' sends it immediately into the active run.
  // The choice only applies while a run is active, so it snaps back to the
  // safe default whenever streaming stops.
  const busySendMode = ref<BusySendMode>('queue')
  watch(options.isStreaming, (streaming) => {
    if (!streaming) {
      busySendMode.value = 'queue'
      flushDeferredPendingDrain()
    }
  })
  watch(hasDeliveryBarrier, (blocked, wasBlocked) => {
    if (blocked) {
      cancelPendingDrainTimer()
    } else if (wasBlocked) {
      flushDeferredPendingDrain()
    }
  })
  if (options.connectionState) {
    watch(options.connectionState, state => {
      if (state !== 'connected') return
      if (isReordering.value && reorderSnapshot?.mode === 'server') {
        void recoverServerReorder().then(recovered => {
          if (recovered) finishPendingReorder()
        })
        return
      }
      void hydratePendingQueue(options.sessionKey.value)
    })
  }
  watch(options.sessionKey, (sessionKey, previousSessionKey) => {
    if (sessionKey && sessionKey !== previousSessionKey) {
      void hydratePendingQueue(sessionKey)
    }
  })

  function supportsServerQueue(): boolean {
    return pendingInputQueue?.supportsQueue() === true
  }

  function supportsServerReorder(): boolean {
    return Boolean(
      supportsServerQueue()
      && pendingInputQueue?.supportsReorder()
      && (!options.connectionState || options.connectionState.value === 'connected'),
    )
  }

  function durableItem(item: ChatPendingItem): boolean {
    return Boolean(item.pendingInputId && options.pendingInputWal)
  }

  function walRecordForItem(
    item: ChatPendingItem,
    state: PendingInputWalState = item.pendingPersistenceState || 'saving',
  ): PendingInputWalRecord {
    const now = Date.now()
    return {
      schemaVersion: 1,
      pendingInputId: item.pendingInputId!,
      sessionKey: queueSessionKey(item.ownerSessionKey),
      clientRequestId: item.pendingClientRequestId!,
      clientMessageId: item.pendingClientMessageId!,
      text: item.text,
      ...(item.promptAnnotationIds?.length
        ? { promptAnnotationIds: normalizePromptAnnotationIds(item.promptAnnotationIds) }
        : {}),
      attachments: (item.attachments || []).map(attachment => ({ ...attachment })),
      intent: item.intent,
      ...(item.confirmedPlainText ? { confirmedPlainText: true } : {}),
      ...(item.ownerRequestId ? { ownerRequestId: item.ownerRequestId } : {}),
      state,
      mayHaveServerCopy: item.pendingMayHaveServerCopy === true,
      ...(item.pendingRetainAfterCancel ? { retainAfterCancel: true } : {}),
      ...(item.pendingRequestFingerprint
        ? { requestFingerprint: item.pendingRequestFingerprint }
        : {}),
      ...(item.pendingServerRevision
        ? { serverRevision: item.pendingServerRevision }
        : {}),
      ...(Number.isSafeInteger(item.pendingPosition)
        ? { position: item.pendingPosition }
        : {}),
      ...(Number.isSafeInteger(item.pendingWalRevision)
        ? { walRevision: item.pendingWalRevision }
        : {}),
      createdAt: Number(item.pendingCreatedAt) || now,
      updatedAt: now,
    }
  }

  function itemFromWalRecord(record: PendingInputWalRecord): ChatPendingItem {
    const mayHaveServerCopy = typeof record.mayHaveServerCopy === 'boolean'
      ? record.mayHaveServerCopy
      // Older clients downgraded an ambiguous `saving` row to `local_only`
      // when they met an older Gateway. No legacy state can therefore prove
      // that an enqueue never committed; only a newly persisted false bit can.
      : true
    return {
      pendingUiId: record.pendingInputId,
      text: record.text,
      ...(normalizePromptAnnotationIds(record.promptAnnotationIds).length
        ? { promptAnnotationIds: normalizePromptAnnotationIds(record.promptAnnotationIds) }
        : {}),
      attachments: record.attachments.map(attachment => ({ ...attachment })),
      intent: record.intent,
      ...(record.confirmedPlainText ? { confirmedPlainText: true } : {}),
      ownerSessionKey: queueSessionKey(record.sessionKey),
      ...(record.ownerRequestId ? { ownerRequestId: record.ownerRequestId } : {}),
      pendingInputId: record.pendingInputId,
      pendingClientRequestId: record.clientRequestId,
      pendingClientMessageId: record.clientMessageId,
      pendingPersistenceState: record.state,
      pendingMayHaveServerCopy: mayHaveServerCopy,
      ...(record.retainAfterCancel ? { pendingRetainAfterCancel: true } : {}),
      ...(record.requestFingerprint
        ? { pendingRequestFingerprint: record.requestFingerprint }
        : {}),
      ...(record.serverRevision
        ? { pendingServerRevision: record.serverRevision }
        : {}),
      ...(Number.isSafeInteger(record.position)
        ? { pendingPosition: record.position }
        : {}),
      pendingWalRevision: record.walRevision ?? 1,
      pendingCreatedAt: record.createdAt,
    } as ChatPendingItem
  }

  function replaceItemFromWalRecord(
    item: ChatPendingItem,
    record: PendingInputWalRecord,
  ) {
    delete item.promptAnnotationIds
    delete item.confirmedPlainText
    delete item.ownerRequestId
    delete item.pendingRetainAfterCancel
    delete item.pendingRequestFingerprint
    delete item.pendingServerRevision
    delete item.pendingPosition
    delete item.deliveryState
    Object.assign(item, itemFromWalRecord(record))
  }

  function walRecordOwnsItem(
    record: PendingInputWalRecord,
    item: ChatPendingItem,
    sessionKey: string,
  ): boolean {
    return queueSessionKey(record.sessionKey) === queueSessionKey(sessionKey)
      && record.clientRequestId === item.pendingClientRequestId
      && record.clientMessageId === item.pendingClientMessageId
  }

  function removedIdentity(sessionKey: string, pendingInputId: string): string {
    return `${queueSessionKey(sessionKey)}\u0000${pendingInputId}`
  }

  function rememberRemoval(sessionKey: string, pendingInputId: string) {
    const identity = removedIdentity(sessionKey, pendingInputId)
    if (removedIdentities.has(identity)) return
    removedIdentities.add(identity)
    removedIdentityOrder.push(identity)
    while (removedIdentityOrder.length > MAX_REMOVAL_TOMBSTONES) {
      const expired = removedIdentityOrder.shift()
      if (expired) removedIdentities.delete(expired)
    }
  }

  function wasRemoved(sessionKey: string, pendingInputId: string): boolean {
    return removedIdentities.has(removedIdentity(sessionKey, pendingInputId))
  }

  function forgetRemoval(sessionKey: string, pendingInputId: string) {
    removedIdentities.delete(removedIdentity(sessionKey, pendingInputId))
  }

  function invalidateCancellation(pendingInputId: string) {
    cancellationInvalidations.set(
      pendingInputId,
      (cancellationInvalidations.get(pendingInputId) ?? 0) + 1,
    )
  }

  function removePendingIdentity(sessionKey: string, pendingInputId: string) {
    const ownerSessionKey = queueSessionKey(sessionKey)
    if (queueSessionKey() === ownerSessionKey) {
      pendingQueue.value = pendingQueue.value.filter(item => (
        item.pendingInputId !== pendingInputId
      ))
    }
    const parked = parkedQueues.get(ownerSessionKey)
    if (parked) {
      const retained = parked.filter(item => item.pendingInputId !== pendingInputId)
      if (retained.length > 0) parkedQueues.set(ownerSessionKey, retained)
      else parkedQueues.delete(ownerSessionKey)
    }
  }

  async function cancelServerIdentity(sessionKey: string, pendingInputId: string) {
    if (!supportsServerQueue()) return
    await pendingInputQueue!.cancel({
      key: queueSessionKey(sessionKey),
      pendingInputId,
    })
  }

  function broadcastChange(
    sessionKey: string,
    pendingInputId?: string,
    action: 'changed' | 'removed' = 'changed',
  ) {
    try {
      broadcast?.postMessage({
        sessionKey: queueSessionKey(sessionKey),
        pendingInputId,
        action,
      })
    } catch {}
  }

  async function writeWalItem(
    item: ChatPendingItem,
    state: PendingInputWalState = item.pendingPersistenceState || 'saving',
  ): Promise<void> {
    if (!options.pendingInputWal || !item.pendingInputId) return
    // Mutate through the reactive array proxy when this item is mounted. New
    // durable items are first created as plain objects, and mutating only that
    // raw reference would not invalidate computed delivery barriers or UI.
    const trackedItem = pendingQueue.value.find(candidate => (
      candidate.pendingInputId === item.pendingInputId
    )) || item
    const previousWalRevision = trackedItem.pendingWalRevision
    trackedItem.pendingPersistenceState = state
    trackedItem.pendingWalRevision = (previousWalRevision ?? 0) + 1
    try {
      await options.pendingInputWal.put(walRecordForItem(trackedItem, state))
    } catch (error) {
      trackedItem.pendingWalRevision = previousWalRevision
      throw error
    }
  }

  async function writeStagingWalItem(
    item: ChatPendingItem,
    state: PendingInputWalState,
    sessionKey: string,
  ): Promise<boolean> {
    const pendingInputId = item.pendingInputId
    const wal = options.pendingInputWal
    if (
      !pendingInputId
      || !wal
      || wasRemoved(sessionKey, pendingInputId)
      || item.pendingPersistenceState === 'cancelling'
      || item.pendingRetainAfterCancel === true
    ) return false
    if (!wal.compareAndSwapPendingInput) {
      await writeWalItem(item, state)
      return !wasRemoved(sessionKey, pendingInputId)
    }
    const expectedWalRevision = item.pendingWalRevision ?? 1
    const mutation = await wal.compareAndSwapPendingInput(
      {
        ...walRecordForItem(item, state),
        walRevision: expectedWalRevision + 1,
      },
      expectedWalRevision,
      walLookupSessionKeys(sessionKey),
    )
    if (mutation.applied) {
      item.pendingPersistenceState = mutation.record!.state
      item.pendingWalRevision = mutation.record!.walRevision
      return true
    }
    if (!mutation.record) {
      rememberRemoval(sessionKey, pendingInputId)
      removePendingIdentity(sessionKey, pendingInputId)
      return false
    }
    if (walRecordOwnsItem(mutation.record, item, sessionKey)) {
      replaceItemFromWalRecord(item, mutation.record)
      if (
        mutation.record.state === 'cancelling'
        && mutation.record.retainAfterCancel !== true
      ) rememberRemoval(sessionKey, pendingInputId)
    }
    return false
  }

  function ordinaryDurableItem(item: ChatPendingItem): boolean {
    return Boolean(
      item.pendingInputId
      && !item.hiddenControl
      && !item.steerAttempt
      && !item.deliveryState,
    )
  }

  function sortOrdinaryPendingItems() {
    const ordinary = pendingQueue.value
      .filter(ordinaryDurableItem)
      .sort((left, right) => (
        (left.pendingPosition ?? Number.MAX_SAFE_INTEGER)
        - (right.pendingPosition ?? Number.MAX_SAFE_INTEGER)
        || (left.pendingCreatedAt ?? 0) - (right.pendingCreatedAt ?? 0)
        || String(left.pendingInputId).localeCompare(String(right.pendingInputId))
      ))
    let ordinaryIndex = 0
    pendingQueue.value = pendingQueue.value.map(item => (
      ordinaryDurableItem(item) ? ordinary[ordinaryIndex++]! : item
    ))
  }

  function rpcErrorCode(error: unknown): string {
    const code = (error as { code?: unknown } | null)?.code
    return typeof code === 'string' ? code : ''
  }

  function durableAttachmentMetadata(attachment: Attachment): Attachment {
    return {
      kind: 'staged',
      local_id: attachment.local_id,
      name: attachment.name,
      mime: attachment.mime,
      ...(typeof attachment.size === 'number' ? { size: attachment.size } : {}),
      durable_material: true,
    }
  }

  function attachmentsFromServerItem(serverItem: PendingInputServerItem): Attachment[] {
    return (serverItem.attachments || []).map((attachment, index) => ({
      kind: 'staged' as const,
      local_id: -(index + 1),
      name: attachment.name,
      mime: attachment.mime,
      durable_material: true as const,
      ...(typeof attachment.size === 'number' ? { size: attachment.size } : {}),
    }))
  }

  async function ensureServerStaged(item: ChatPendingItem): Promise<void> {
    const pendingInputId = item.pendingInputId
    if (!pendingInputId || !options.pendingInputWal) return
    if (item.ownerRequestId) return
    if (
      item.pendingPersistenceState === 'cancelling'
      || item.pendingRetainAfterCancel === true
    ) return
    const sessionKey = queueSessionKey(item.ownerSessionKey)
    if (wasRemoved(sessionKey, pendingInputId)) return
    const existing = stagingOperations.get(pendingInputId)
    if (existing) return existing
    const operation = (async () => {
      if (wasRemoved(sessionKey, pendingInputId)) return
      if (!supportsServerQueue()) {
        await writeStagingWalItem(item, 'local_only', sessionKey)
        return
      }
      let refreshedLostUpload = false
      try {
        while (true) {
          if (wasRemoved(sessionKey, pendingInputId)) return
          if (item.attachments.length > 0 && options.prepareAttachmentsForSend) {
            const ready = await options.prepareAttachmentsForSend({
              attachments: item.attachments,
              isCurrent: () => pendingQueue.value.some(candidate => (
                candidate.pendingInputId === pendingInputId
              )),
              ownership: 'detached',
            })
            if (
              wasRemoved(sessionKey, pendingInputId)
              || item.pendingPersistenceState === 'cancelling'
              || item.pendingRetainAfterCancel === true
            ) return
            if (!ready) {
              if (!await writeStagingWalItem(item, 'retryable', sessionKey)) return
              options.onPendingPersistenceError?.('server_rejected')
              return
            }
            // Persist refreshed upload UUIDs before the request can become
            // ambiguous. A reload then retries the same material snapshot.
            if (!await writeStagingWalItem(item, 'saving', sessionKey)) return
          }
          if (wasRemoved(sessionKey, pendingInputId)) return
          const sendable = item.attachments.filter(isSendableAttachment)
          if (sendable.length !== item.attachments.length) {
            if (!await writeStagingWalItem(item, 'retryable', sessionKey)) return
            options.onPendingPersistenceError?.('server_rejected')
            return
          }
          try {
            const queuedText = item.text.trim()
            const literalSlashEscape = !item.hiddenControl && queuedText.startsWith('//')
            const providerMessage = literalSlashEscape
              ? queuedText.slice(1)
              : queuedText
            // Write the cross-boundary provenance before sending.  A crash or
            // lost ACK can then distinguish this identity from a genuinely
            // IndexedDB-only draft when the next Gateway is older/offline.
            item.pendingMayHaveServerCopy = true
            if (!await writeStagingWalItem(item, 'saving', sessionKey)) return
            const response = await pendingInputQueue!.enqueue({
                key: queueSessionKey(item.ownerSessionKey),
                pendingInputId,
                clientRequestId: item.pendingClientRequestId,
                clientMessageId: item.pendingClientMessageId,
                message: providerMessage || 'Describe these attachments',
                attachments: sendable.map(serializeSendableAttachment),
                ...(item.promptAnnotationIds?.length
                  ? { promptAnnotationIds: normalizePromptAnnotationIds(item.promptAnnotationIds) }
                  : {}),
                ...(item.confirmedPlainText ? { confirmedPlainText: true } : {}),
                ...(sendable.length > 0 || literalSlashEscape
                  ? { displayText: queuedText }
                  : {}),
                ...(item.intent ? { intent: item.intent } : {}),
                ...(Number.isSafeInteger(item.pendingPosition)
                  ? { position: item.pendingPosition }
                  : {}),
              })
            if (wasRemoved(sessionKey, pendingInputId)) {
              // A peer may cancel while this enqueue response is in flight.
              // Cancel again after the ACK so an enqueue that committed after
              // the first tombstone cannot resurrect the draft server-side.
              await cancelServerIdentity(sessionKey, pendingInputId).catch(() => {})
              await options.pendingInputWal!.delete(pendingInputId).catch(() => {})
              return
            }
            const fingerprint = response.requestFingerprint
            const revision = response.revision
            if (typeof fingerprint !== 'string' || !fingerprint) {
              throw new Error('Gateway returned an invalid pending-input acknowledgement')
            }
            item.pendingRequestFingerprint = fingerprint
            item.pendingServerRevision = typeof revision === 'number' ? revision : 1
            item.pendingPosition = typeof response.position === 'number'
              ? response.position
              : item.pendingPosition
            // The server ACK proves the session material store owns the bytes.
            // Drop base64, File objects and expiring upload capabilities from
            // IndexedDB; dispatch now needs only the stable pending identity.
            item.attachments = item.attachments.map(durableAttachmentMetadata)
            if (!await writeStagingWalItem(item, 'staged', sessionKey)) return
            flushDeferredPendingDrain()
            return
          } catch (error) {
            const code = rpcErrorCode(error)
            if (
              !refreshedLostUpload
              && options.prepareAttachmentsForSend
              && (code === 'ATTACHMENT_EXPIRED' || code === 'ATTACHMENT_LOST_IN_RESTART')
              && item.attachments.some(attachment => attachment.kind === 'staged' && attachment.file)
            ) {
              refreshedLostUpload = true
              for (const attachment of item.attachments) {
                if (attachment.kind === 'staged' && attachment.file) {
                  attachment.expires_at = 0
                }
              }
              continue
            }
            throw error
          }
        }
      } catch (error) {
        const code = rpcErrorCode(error)
        if (
          code === 'PENDING_INPUT_CANCELLED'
          || code === 'PENDING_INPUT_ALREADY_DISPATCHED'
        ) {
          // A peer or an earlier crashed tab already committed the durable
          // terminal outcome. Treat that server tombstone as authoritative
          // even when this tab missed the BroadcastChannel notification while
          // its enqueue result was ambiguous.
          rememberRemoval(sessionKey, pendingInputId)
          await options.pendingInputWal!.delete(pendingInputId).catch(() => {})
          removePendingIdentity(sessionKey, pendingInputId)
          broadcastChange(sessionKey, pendingInputId, 'removed')
          return
        }
        if (
          wasRemoved(sessionKey, pendingInputId)
          || item.pendingPersistenceState === 'cancelling'
          || item.pendingRetainAfterCancel === true
        ) return
        if (code === 'METHOD_NOT_FOUND') {
          if (await writeStagingWalItem(item, 'local_only', sessionKey)) {
            flushDeferredPendingDrain()
          }
          return
        }
        if ((error as { accepted?: unknown } | null)?.accepted === false) {
          await writeStagingWalItem(item, 'retryable', sessionKey).catch(() => false)
          options.onPendingPersistenceError?.('server_rejected')
          return
        }
        // Unknown transport results deliberately remain "saving". Reconnect,
        // hydrate, or another tab retries this exact identity byte-for-byte.
        await writeStagingWalItem(item, 'saving', sessionKey).catch(() => false)
      }
    })().finally(() => {
      stagingOperations.delete(pendingInputId)
    })
    stagingOperations.set(pendingInputId, operation)
    return operation
  }

  function mergeWalRecords(records: PendingInputWalRecord[], sessionKey: string) {
    const existingById = new Map(
      pendingQueue.value
        .filter(item => item.pendingInputId)
        .map(item => [item.pendingInputId!, item]),
    )
    for (const record of records) {
      const existing = existingById.get(record.pendingInputId)
      if (
        existing
        && (record.retainAfterCancel === true || record.state === 'cancelling')
      ) {
        replaceItemFromWalRecord(existing, record)
        continue
      }
      if (
        record.sessionKey !== sessionKey
        || existing
        || wasRemoved(sessionKey, record.pendingInputId)
      ) continue
      const added = itemFromWalRecord(record)
      pendingQueue.value.push(added)
      existingById.set(record.pendingInputId, added)
    }
    sortOrdinaryPendingItems()
  }

  async function hydratePendingQueue(sessionKey = options.sessionKey.value): Promise<void> {
    const wal = options.pendingInputWal
    const ownerSessionKey = queueSessionKey(sessionKey)
    if (!wal || !ownerSessionKey || disposed) return
    if (isReordering.value) {
      deferredHydrateSession = ownerSessionKey
      return
    }
    const generation = ++hydrateGeneration
    let records: PendingInputWalRecord[]
    try {
      const recordsById = new Map<string, PendingInputWalRecord>()
      const lookupResults = await Promise.all(
        walLookupSessionKeys(sessionKey).map(lookupKey => wal.list(lookupKey)),
      )
      for (const record of lookupResults.flat()) {
        if (queueSessionKey(record.sessionKey) !== ownerSessionKey) continue
        recordsById.set(record.pendingInputId, {
          ...record,
          sessionKey: ownerSessionKey,
        })
      }
      records = [...recordsById.values()]
    } catch {
      options.onPendingPersistenceError?.('wal_failed')
      return
    }
    if (
      disposed
      || generation !== hydrateGeneration
      || queueSessionKey() !== ownerSessionKey
    ) {
      return
    }
    mergeWalRecords(records, ownerSessionKey)
    const walIds = new Set(records.map(record => record.pendingInputId))

    if (!supportsServerQueue()) {
      for (const item of [...pendingQueue.value]) {
        if (!durableItem(item) || queueSessionKey(item.ownerSessionKey) !== ownerSessionKey) continue
        const pendingInputId = item.pendingInputId!
        // A snapshot can race the WAL write and the enqueue itself. Keep an
        // in-flight/saving row visible until its owner settles; otherwise an
        // empty list response would tombstone a perfectly valid local item.
        const saving = item.pendingPersistenceState === 'saving'
        const stagingInFlight = stagingOperations.has(pendingInputId)
        if (
          !walIds.has(pendingInputId)
          && !locallyCreatingIds.has(pendingInputId)
          && !saving
          && !stagingInFlight
        ) {
          rememberRemoval(ownerSessionKey, pendingInputId)
          removePendingIdentity(ownerSessionKey, pendingInputId)
          continue
        }
        // A cancellation WAL is a durable delete intent, never a draft to
        // downgrade or re-enqueue. Keep it intact until a queue-capable
        // Gateway can accept the idempotent tombstone.
        if (
          item.pendingPersistenceState === 'cancelling'
          || item.pendingRetainAfterCancel === true
        ) continue
        if (item.pendingPersistenceState !== 'local_only') {
          void writeWalItem(item, 'local_only')
        }
      }
      return
    }

    // Cancellation does not depend on list reconciliation. Retry it as soon
    // as the capability is available so a failing/stale list response can
    // never turn a durable delete intent back into an enqueue attempt.
    for (const item of [...pendingQueue.value]) {
      if (
        durableItem(item)
        && queueSessionKey(item.ownerSessionKey) === ownerSessionKey
        && item.pendingPersistenceState === 'cancelling'
      ) void retryCancellingItem(item)
    }

    try {
      const response = { items: await pendingInputQueue!.list(ownerSessionKey) }
      if (
        disposed
        || generation !== hydrateGeneration
        || queueSessionKey() !== ownerSessionKey
      ) {
        return
      }
      const serverItems = Array.isArray(response.items) ? response.items : []
      const serverIds = new Set<string>()
      for (const serverItem of serverItems) {
        const pendingInputId = serverItem.pendingInputId
        const clientRequestId = serverItem.clientRequestId
        const clientMessageId = serverItem.clientMessageId
        if (wasRemoved(ownerSessionKey, pendingInputId)) {
          const cancellingItem = pendingQueue.value.find(candidate => (
            candidate.pendingInputId === pendingInputId
            && candidate.pendingPersistenceState === 'cancelling'
          ))
          if (cancellingItem) void retryCancellingItem(cancellingItem)
          else void cancelServerIdentity(ownerSessionKey, pendingInputId).catch(() => {})
          continue
        }
        serverIds.add(pendingInputId)
        const serverAttachments = attachmentsFromServerItem(serverItem)
        let item = pendingQueue.value.find(candidate => (
          candidate.pendingInputId === pendingInputId
        ))
        if (!item) {
          item = {
            pendingUiId: pendingInputId,
            text: typeof serverItem.displayText === 'string'
              ? serverItem.displayText
              : serverItem.message || '',
            attachments: serverAttachments,
            intent: typeof serverItem.intent === 'string' ? serverItem.intent : null,
            ...(normalizePromptAnnotationIds(serverItem.promptAnnotationIds).length
              ? {
                  promptAnnotationIds: normalizePromptAnnotationIds(serverItem.promptAnnotationIds),
                }
              : {}),
            ...(serverItem.confirmedPlainText === true ? { confirmedPlainText: true } : {}),
            ownerSessionKey,
            pendingInputId,
            pendingClientRequestId: clientRequestId,
            pendingClientMessageId: clientMessageId,
          }
          pendingQueue.value.push(item)
        }
        if (item.pendingPersistenceState === 'cancelling') {
          // The local delete intent wins over a stale server list row. Do not
          // replace its payload/attachments or transition it back to staged;
          // retry only the same idempotent cancellation.
          void retryCancellingItem(item)
          continue
        }
        if (Array.isArray(serverItem.attachments)) {
          // A list response is also the authoritative ACK for an enqueue whose
          // transport response was lost. Replace the WAL's File/base64/upload
          // snapshot with safe server-owned metadata before marking it staged.
          item.attachments = serverAttachments
        }
        const serverPromptAnnotationIds = normalizePromptAnnotationIds(serverItem.promptAnnotationIds)
        if (serverPromptAnnotationIds.length > 0) {
          item.promptAnnotationIds = serverPromptAnnotationIds
        }
        item.pendingRequestFingerprint = serverItem.requestFingerprint
        item.pendingServerRevision = typeof serverItem.revision === 'number'
          ? serverItem.revision
          : 1
        item.pendingPosition = typeof serverItem.position === 'number'
          ? serverItem.position
          : item.pendingPosition
        item.pendingMayHaveServerCopy = true
        await writeStagingWalItem(item, 'staged', ownerSessionKey)
      }

      sortOrdinaryPendingItems()

      for (const item of [...pendingQueue.value]) {
        if (!durableItem(item) || queueSessionKey(item.ownerSessionKey) !== ownerSessionKey) continue
        if (serverIds.has(item.pendingInputId!)) continue
        const pendingInputId = item.pendingInputId!
        // The list snapshot may have started before the WAL write or enqueue
        // completed. Preserve an in-flight row until its owner settles.
        const saving = item.pendingPersistenceState === 'saving'
        const stagingInFlight = stagingOperations.has(pendingInputId)
        if (
          !walIds.has(pendingInputId)
          && !locallyCreatingIds.has(pendingInputId)
          && !saving
          && !stagingInFlight
        ) {
          rememberRemoval(ownerSessionKey, pendingInputId)
          removePendingIdentity(ownerSessionKey, pendingInputId)
          continue
        }
        if (item.pendingPersistenceState === 'cancelling') {
          void retryCancellingItem(item)
          continue
        }
        if (item.pendingRetainAfterCancel === true) continue
        if (item.pendingPersistenceState === 'staged') {
          // Another tab either cancelled or dispatched the server row. Both
          // outcomes are terminal for this WAL entry.
          await wal.delete(item.pendingInputId!)
          const index = pendingQueue.value.indexOf(item)
          if (index >= 0) pendingQueue.value.splice(index, 1)
          continue
        }
        // This also upgrades IndexedDB-only rows created against an older
        // Gateway once a compatible Gateway is available.
        void ensureServerStaged(item)
      }
    } catch {
      // Server reconciliation is best effort; IndexedDB remains authoritative
      // until the next connected hydrate.
    }
  }

  if (broadcast) {
    broadcast.onmessage = event => {
      const message = event.data as PendingQueueBroadcastMessage | null
      const sessionKey = typeof message?.sessionKey === 'string'
        ? queueSessionKey(message.sessionKey)
        : ''
      const pendingInputId = typeof message?.pendingInputId === 'string'
        ? message.pendingInputId
        : ''
      if (!sessionKey) return
      if (message?.action === 'removed' && pendingInputId) {
        invalidateCancellation(pendingInputId)
        rememberRemoval(sessionKey, pendingInputId)
        removePendingIdentity(sessionKey, pendingInputId)
        void options.pendingInputWal?.delete(pendingInputId).catch(() => {})
        // Idempotently race any peer enqueue whose request was already in
        // flight when the cancellation tombstone arrived.
        void cancelServerIdentity(sessionKey, pendingInputId).catch(() => {})
        return
      }
      if (sessionKey === queueSessionKey()) {
        void hydratePendingQueue(sessionKey)
      }
    }
  }
  if (options.pendingInputWal) void hydratePendingQueue()

  function resolveOwnerRequestId(owner?: PendingQueueOwner): string | undefined {
    if (owner?.ownerRequestId) return owner.ownerRequestId
    const context = options.ownerContext?.value
    return context && queueSessionKey(context.sessionKey) === queueSessionKey()
      ? context.ownerRequestId
      : undefined
  }

  function enqueuePendingPayload(
    payload: PendingQueuePayload,
    owner?: PendingQueueOwner,
  ): boolean | Promise<boolean> {
    if (ordinaryPendingCount.value >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    // Clearing or otherwise transferring a composer payload is only safe once
    // IndexedDB owns the exact text, attachments and intent. An unavailable
    // WAL is a hard local persistence failure, not permission to create a
    // volatile queue item that disappears on refresh.
    if (!options.pendingInputWal) {
      options.onPendingPersistenceError?.('wal_failed')
      return false
    }
    const ownerRequestId = resolveOwnerRequestId(owner)
    const promptAnnotationIds = normalizePromptAnnotationIds(payload.promptAnnotationIds)
    const item: ChatPendingItem = {
      pendingUiId: createClientRequestId(),
      text: payload.text,
      ...(promptAnnotationIds.length ? { promptAnnotationIds } : {}),
      attachments: (payload.attachments || []).map(a => ({ ...a })),
      intent: payload.intent ?? null,
      ...(payload.confirmedPlainText ? { confirmedPlainText: true } : {}),
      ownerSessionKey: queueSessionKey(),
      ...(ownerRequestId ? { ownerRequestId } : {}),
    }
    const now = Date.now()
    item.pendingInputId = createClientRequestId()
    item.pendingClientRequestId = createClientRequestId()
    item.pendingClientMessageId = createClientMessageId()
    item.pendingPersistenceState = 'saving'
    item.pendingMayHaveServerCopy = false
    item.pendingCreatedAt = now
    item.pendingPosition = pendingQueue.value.filter(ordinaryDurableItem).length
    item.pendingWalRevision = 0
    pendingQueue.value.push(item)
    flushDeferredPendingDrain()
    locallyCreatingIds.add(item.pendingInputId)
    return (async () => {
      try {
        await writeWalItem(item, 'saving')
      } catch {
        const index = pendingQueue.value.findIndex(candidate => (
          candidate.pendingInputId === item.pendingInputId
        ))
        if (index >= 0) pendingQueue.value.splice(index, 1)
        options.onPendingPersistenceError?.('wal_failed')
        return false
      } finally {
        locallyCreatingIds.delete(item.pendingInputId!)
      }
      broadcastChange(queueSessionKey(item.ownerSessionKey))
      void ensureServerStaged(item)
      return true
    })()
  }

  function enqueuePendingInput(
    text: string,
    owner?: PendingQueueOwner,
    enqueueOptions?: {
      confirmedPlainText?: boolean
      promptAnnotationIds?: readonly string[]
    },
  ): boolean | Promise<boolean> {
    if (isControlInput(text) && !enqueueOptions?.confirmedPlainText) return false
    const composerText = options.inputText.value
    const composerAttachments = snapshotComposerAttachments(options.pendingAttachments.value)
    const composerIntent = options.pendingSessionIntent.value
    const queued = enqueuePendingPayload({
      text,
      ...(enqueueOptions?.promptAnnotationIds?.length
        ? { promptAnnotationIds: enqueueOptions.promptAnnotationIds }
        : {}),
      attachments: options.pendingAttachments.value,
      intent: composerIntent,
      ...(enqueueOptions?.confirmedPlainText ? { confirmedPlainText: true } : {}),
    }, owner)
    const clearMatchingComposer = () => {
      if (
        options.inputText.value !== composerText
        || !composerAttachmentsMatch(options.pendingAttachments.value, composerAttachments)
        || options.pendingSessionIntent.value !== composerIntent
      ) return
      options.inputText.value = ''
      options.pendingAttachments.value = []
      options.pendingSessionIntent.value = null
      options.autoResizeTextarea()
    }
    if (typeof queued === 'boolean') {
      if (queued) clearMatchingComposer()
      return queued
    }
    return queued.then(saved => {
      if (saved) clearMatchingComposer()
      return saved
    })
  }

  function enqueueRecoveredInput(text: string, owner?: PendingQueueOwner) {
    const recovered = String(text || '').trim()
    if (!recovered) return true
    if (pendingQueue.value.some(item => !item.hiddenControl && item.text === recovered)) {
      return true
    }
    return enqueuePendingPayload({ text: recovered }, owner)
  }

  function enqueueHiddenControl(
    item: {
      text: string
      displayText: string
      clientRequestId?: string
      sessionKey?: string
      clientMessageId?: string
      visibleCommitted?: boolean
    },
    owner?: PendingQueueOwner,
  ) {
    const stableRequestId = String(item.clientRequestId || '').trim()
    const hiddenControlSessionKey = queueSessionKey(item.sessionKey)
    if (
      stableRequestId
      && pendingQueue.value.some(candidate => (
        candidate.hiddenControl
        && candidate.clientRequestId === stableRequestId
        && candidate.hiddenControlSessionKey === hiddenControlSessionKey
      ))
    ) return true
    if (ordinaryPendingCount.value >= MAX_PENDING) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return false
    }
    // A hidden-control send does NOT consume the composer draft/attachments.
    const ownerRequestId = resolveOwnerRequestId(owner)
    pendingQueue.value.push({
      pendingUiId: stableRequestId || createClientRequestId(),
      text: item.text,
      attachments: [],
      intent: null,
      ownerSessionKey: queueSessionKey(),
      ...(ownerRequestId ? { ownerRequestId } : {}),
      hiddenControl: true,
      displayTextOverride: item.displayText,
      clientRequestId: item.clientRequestId,
      hiddenControlSessionKey,
      ...(item.clientRequestId
        ? { hiddenClientRequestId: item.clientRequestId }
        : {}),
      ...(item.clientMessageId
        ? { hiddenClientMessageId: item.clientMessageId }
        : {}),
      ...(item.visibleCommitted ? { hiddenVisibleCommitted: true } : {}),
    })
    flushDeferredPendingDrain()
    return true
  }

  function enqueuePendingSteerAttempt(
    payload: PendingSteerPayload,
    owner?: PendingQueueOwner,
  ) {
    const request = snapshotSteerRequest(payload.request)
    const existing = pendingQueue.value.find(item => (
      item.steerAttempt?.request.client_request_id === request.client_request_id
    ))
    if (existing) return existing
    // A direct Steer needs a transport-owned pending row before its RPC can be
    // sent. Exactly one delivery barrier may own the extra transport slot;
    // ordinary queue capacity is accounted independently above.
    if (hasDeliveryBarrier.value) {
      console.warn(`Pending queue full (${MAX_PENDING})`)
      return null
    }
    const ownerRequestId = resolveOwnerRequestId(owner)
    const item: ChatPendingItem = {
      pendingUiId: request.client_request_id || createClientRequestId(),
      text: request.message,
      attachments: [],
      intent: null,
      ownerSessionKey: queueSessionKey(),
      ...(ownerRequestId ? { ownerRequestId } : {}),
      steerAttempt: {
        phase: payload.phase || 'submitting',
        request,
      },
    }
    pendingQueue.value.push(item)
    // Return the array-owned reactive proxy. Mutating the raw object after it
    // was inserted would bypass Vue's phase-change notifications in the UI.
    return pendingQueue.value[pendingQueue.value.length - 1] || item
  }

  async function forgetDurableItem(
    item: ChatPendingItem,
    action: 'changed' | 'removed' = 'changed',
  ): Promise<void> {
    if (!options.pendingInputWal || !item.pendingInputId) return
    const sessionKey = queueSessionKey(item.ownerSessionKey)
    if (action === 'removed') invalidateCancellation(item.pendingInputId)
    await options.pendingInputWal.delete(item.pendingInputId)
    if (action === 'removed') rememberRemoval(sessionKey, item.pendingInputId)
    broadcastChange(sessionKey, item.pendingInputId, action)
  }

  async function retainCancelledDraft(
    item: ChatPendingItem,
    sessionKey: string,
    expectedInvalidation: number,
  ): Promise<boolean> {
    if ((cancellationInvalidations.get(item.pendingInputId!) ?? 0) !== expectedInvalidation) {
      return false
    }
    const previousMayHaveServerCopy = item.pendingMayHaveServerCopy
    const previousFingerprint = item.pendingRequestFingerprint
    const previousServerRevision = item.pendingServerRevision
    const previousPosition = item.pendingPosition
    item.pendingMayHaveServerCopy = false
    delete item.deliveryState
    delete item.pendingRequestFingerprint
    delete item.pendingServerRevision
    delete item.pendingPosition
    try {
      if (options.pendingInputWal?.retainCancelled) {
        const expectedWalRevision = item.pendingWalRevision ?? 1
        const mutation = await options.pendingInputWal.retainCancelled(
          {
            ...walRecordForItem(item, 'local_only'),
            walRevision: expectedWalRevision + 1,
          },
          expectedWalRevision,
          walLookupSessionKeys(sessionKey),
        )
        if (!mutation.applied) {
          if (mutation.record) {
            // Another tab still owns a live row. Keep this queue slot as an
            // ordering barrier and allow a later hydrate to reconcile it.
            if (
              mutation.record.state !== 'cancelling'
              || mutation.record.retainAfterCancel === true
            ) {
              forgetRemoval(sessionKey, item.pendingInputId!)
            }
            if (walRecordOwnsItem(mutation.record, item, sessionKey)) {
              replaceItemFromWalRecord(item, mutation.record)
            }
            return false
          }
          removePendingIdentity(sessionKey, item.pendingInputId!)
          return false
        }
        const retained = mutation.record!
        item.pendingPersistenceState = retained.state
        item.pendingWalRevision = retained.walRevision
      } else {
        await writeWalItem(item, 'local_only')
      }
      if ((cancellationInvalidations.get(item.pendingInputId!) ?? 0) !== expectedInvalidation) {
        await options.pendingInputWal?.delete(item.pendingInputId!).catch(() => {})
        return false
      }
      broadcastChange(sessionKey, item.pendingInputId, 'changed')
      return true
    } catch {
      item.pendingPersistenceState = 'cancelling'
      item.pendingMayHaveServerCopy = previousMayHaveServerCopy
      if (previousFingerprint) item.pendingRequestFingerprint = previousFingerprint
      if (previousServerRevision) item.pendingServerRevision = previousServerRevision
      if (Number.isSafeInteger(previousPosition)) item.pendingPosition = previousPosition
      broadcastChange(sessionKey, item.pendingInputId, 'changed')
      return false
    }
  }

  async function performDurableCancellation(
    item: ChatPendingItem,
    cancelOptions: PendingCancelOptions = {},
  ): Promise<boolean> {
    if (!durableItem(item)) return true
    const previousState = item.pendingPersistenceState || 'saving'
    const sessionKey = queueSessionKey(item.ownerSessionKey)
    const retainAfterCancel = cancelOptions.retainAfterCancel === true
    const expectedInvalidation = cancellationInvalidations.get(item.pendingInputId!) ?? 0
    if (
      item.pendingRetainAfterCancel === true
      && !retainAfterCancel
      && item.pendingPersistenceState === 'local_only'
      && item.pendingMayHaveServerCopy === false
    ) {
      try {
        await forgetDurableItem(item, 'removed')
        return true
      } catch {
        broadcastChange(sessionKey, item.pendingInputId, 'changed')
        return false
      }
    }
    const previousRetainAfterCancel = item.pendingRetainAfterCancel
    if (retainAfterCancel) item.pendingRetainAfterCancel = true
    else delete item.pendingRetainAfterCancel
    rememberRemoval(sessionKey, item.pendingInputId!)
    const usesAtomicCancellation = Boolean(options.pendingInputWal?.beginCancellation)
    try {
      if (options.pendingInputWal?.beginCancellation) {
        let transition: 'applied' | 'missing' | 'conflict' = 'conflict'
        for (let attempt = 0; attempt < 3; attempt++) {
          if (retainAfterCancel) item.pendingRetainAfterCancel = true
          else delete item.pendingRetainAfterCancel
          const expectedWalRevision = item.pendingWalRevision ?? 1
          const mutation = await options.pendingInputWal.beginCancellation(
            {
              ...walRecordForItem(item, 'cancelling'),
              walRevision: expectedWalRevision + 1,
            },
            expectedWalRevision,
            walLookupSessionKeys(sessionKey),
          )
          if (mutation.applied) {
            item.pendingPersistenceState = mutation.record!.state
            item.pendingWalRevision = mutation.record!.walRevision
            transition = 'applied'
            break
          }
          if (!mutation.record) {
            removePendingIdentity(sessionKey, item.pendingInputId!)
            transition = 'missing'
            break
          }
          const sameIdentity = walRecordOwnsItem(mutation.record, item, sessionKey)
          if (
            mutation.record.state !== 'cancelling'
            || mutation.record.retainAfterCancel === true
          ) {
            forgetRemoval(sessionKey, item.pendingInputId!)
          }
          if (sameIdentity) replaceItemFromWalRecord(item, mutation.record)
          if (
            !retainAfterCancel
            && sameIdentity
            && mutation.record.state === 'cancelling'
            && mutation.record.retainAfterCancel !== true
          ) {
            transition = 'applied'
            break
          }
          if (!retainAfterCancel && sameIdentity) {
            rememberRemoval(sessionKey, item.pendingInputId!)
            continue
          }
          transition = 'conflict'
          break
        }
        if (transition !== 'applied') return transition === 'missing' && !retainAfterCancel
      } else {
        await writeWalItem(item, 'cancelling')
      }
    } catch {
      // The delete intent never became durable, so the composer/queue must keep
      // owning the previous state. No cancellation RPC was sent.
      forgetRemoval(sessionKey, item.pendingInputId!)
      if (previousRetainAfterCancel) item.pendingRetainAfterCancel = true
      else delete item.pendingRetainAfterCancel
      if (usesAtomicCancellation) item.pendingPersistenceState = previousState
      else await writeWalItem(item, previousState).catch(() => {})
      broadcastChange(sessionKey, item.pendingInputId, 'changed')
      return false
    }

    // A proven IndexedDB-only row can be forgotten immediately on an older
    // Gateway. Anything that may have crossed the network must retain its
    // cancelling WAL until a queue-capable Gateway can write the tombstone.
    if (!supportsServerQueue()) {
      if (item.pendingMayHaveServerCopy) {
        broadcastChange(sessionKey, item.pendingInputId, 'changed')
        return false
      }
      try {
        if (retainAfterCancel) {
          return retainCancelledDraft(item, sessionKey, expectedInvalidation)
        }
        await forgetDurableItem(item, 'removed')
        return true
      } catch {
        forgetRemoval(sessionKey, item.pendingInputId!)
        await writeWalItem(item, previousState).catch(() => {})
        broadcastChange(sessionKey, item.pendingInputId, 'changed')
        return false
      }
    }

    try {
      // An enqueue ACK may be lost after the Gateway committed the row, leaving
      // the WAL in `saving`. Cancellation is idempotent, so every durable item
      // connected to a queue-capable Gateway must attempt the server tombstone
      // before its local WAL is removed. Otherwise a deleted chip can reappear
      // on the next hydrate.
      await pendingInputQueue!.cancel({
        key: queueSessionKey(item.ownerSessionKey),
        pendingInputId: item.pendingInputId!,
        ...(previousState === 'staged' && item.pendingServerRevision
          ? { expectedRevision: item.pendingServerRevision }
          : {}),
      })
      if (retainAfterCancel) {
        return retainCancelledDraft(item, sessionKey, expectedInvalidation)
      }
      await forgetDurableItem(item, 'removed')
      return true
    } catch {
      // The RPC or local delete may have committed despite a lost response.
      // Preserve the durable delete intent and retry the same identity after
      // reconnect instead of making the item dispatchable again.
      broadcastChange(sessionKey, item.pendingInputId, 'changed')
      return false
    }
  }

  function cancelDurableItem(
    item: ChatPendingItem,
    cancelOptions: PendingCancelOptions = {},
  ): Promise<boolean> {
    const pendingInputId = item.pendingInputId
    if (!pendingInputId || !options.pendingInputWal) return Promise.resolve(true)
    const retainAfterCancel = cancelOptions.retainAfterCancel === true
    if (
      !retainAfterCancel
      && cancelOptions.invalidateRestore !== false
      && item.pendingRetainAfterCancel === true
    ) {
      invalidateCancellation(pendingInputId)
      activeQueueLease += 1
    }
    const existing = cancellationOperations.get(pendingInputId)
    if (existing) {
      if (!existing.retainAfterCancel || retainAfterCancel) return existing.promise
      const chained = existing.promise
        .then(() => performDurableCancellation(item))
        .finally(() => {
          if (cancellationOperations.get(pendingInputId)?.promise === chained) {
            cancellationOperations.delete(pendingInputId)
          }
        })
      cancellationOperations.set(pendingInputId, {
        promise: chained,
        retainAfterCancel: false,
      })
      return chained
    }
    const operation = performDurableCancellation(item, cancelOptions).finally(() => {
      if (cancellationOperations.get(pendingInputId)?.promise === operation) {
        cancellationOperations.delete(pendingInputId)
      }
    })
    cancellationOperations.set(pendingInputId, { promise: operation, retainAfterCancel })
    return operation
  }

  function retryCancellingItem(item: ChatPendingItem): Promise<boolean> {
    const pendingInputId = item.pendingInputId
    if (!pendingInputId || item.pendingPersistenceState !== 'cancelling') {
      return Promise.resolve(false)
    }
    return cancelDurableItem(item, {
      retainAfterCancel: item.pendingRetainAfterCancel === true,
    }).then(cancelled => {
      if (!cancelled) return false
      if (
        item.pendingPersistenceState === 'local_only'
        && item.pendingRetainAfterCancel === true
      ) return true
      removePendingIdentity(
        queueSessionKey(item.ownerSessionKey),
        pendingInputId,
      )
      return true
    })
  }

  function pendingIndex(pendingUiId: string): number {
    return pendingQueue.value.findIndex(item => item.pendingUiId === pendingUiId)
  }

  function removePendingChip(pendingUiId: string) {
    const index = pendingIndex(pendingUiId)
    const item = pendingQueue.value[index]
    if (
      isReordering.value
      || !item
      || item.deliveryState === 'steering'
      || item.steerAttempt?.phase === 'submitting'
    ) return false
    if (!notifyDiscardedHiddenControl(item)) return false
    if (durableItem(item)) {
      void cancelDurableItem(item).then(cancelled => {
        if (!cancelled) return
        const currentIndex = pendingQueue.value.indexOf(item)
        if (currentIndex >= 0) pendingQueue.value.splice(currentIndex, 1)
      })
      return true
    }
    pendingQueue.value.splice(index, 1)
    return true
  }

  function beginPendingDelivery(
    pendingUiId: string,
    allowHiddenControl = false,
  ): ChatPendingItem | null {
    if (isReordering.value) return null
    const index = pendingIndex(pendingUiId)
    const item = pendingQueue.value[index]
    if (
      !item
      || (item.hiddenControl && !allowHiddenControl)
      || item.deliveryState === 'steering'
      || item.steerAttempt?.phase === 'submitting'
      || item.pendingPersistenceState === 'saving'
      || item.pendingPersistenceState === 'cancelling'
      || item.pendingPersistenceState === 'retryable'
    ) return null
    const otherDelivery = pendingQueue.value.find(
      candidate => candidate !== item && (candidate.deliveryState || candidate.steerAttempt),
    )
    if (otherDelivery) return null
    // This generic lease covers the small validation window before a queued
    // draft becomes a Steer. `steerDelivery.begin` clears it atomically when
    // the canonical attempt is installed.
    item.deliveryState = 'steering'
    return item
  }

  function settlePendingDelivery(item: ChatPendingItem, outcome: PendingDeliveryOutcome) {
    let container = pendingQueue.value
    let index = container.indexOf(item)
    if (index < 0) {
      for (const parked of parkedQueues.values()) {
        const parkedIndex = parked.indexOf(item)
        if (parkedIndex < 0) continue
        container = parked
        index = parkedIndex
        break
      }
    }
    // Explicit navigation intentionally discards the old queue. If that
    // happened while an RPC settled, there is no queue ownership left to
    // update.
    if (index < 0) return
    if (outcome === 'accepted') {
      container.splice(index, 1)
      void forgetDurableItem(item).catch(() => {
        // The accepted server receipt is authoritative. A failed local delete
        // is reconciled against the now-missing server row on next hydrate.
      })
      flushDeferredPendingDrain()
      return
    }
    if (outcome === 'deferred' && !item.steerAttempt) {
      item.deliveryState = undefined
      deferredDrainRequested = true
      flushDeferredPendingDrain()
      return
    }
    if (!item.steerAttempt) {
      item.deliveryState = outcome === 'retryable_failure' ? 'retryable' : undefined
    }
    flushDeferredPendingDrain()
  }

  function clearPendingQueue() {
    cancelPendingReorder()
    clearPendingDrainAfterTerminalTimer()
    activeQueueLease += 1
    for (const item of [...pendingQueue.value]) {
      if (
        item.deliveryState === 'steering'
        || item.steerAttempt?.phase === 'submitting'
        || !notifyDiscardedHiddenControl(item)
      ) continue
      const index = pendingQueue.value.indexOf(item)
      if (index < 0) continue
      if (durableItem(item)) {
        void cancelDurableItem(item).then(cancelled => {
          if (!cancelled) return
          const currentIndex = pendingQueue.value.indexOf(item)
          if (currentIndex >= 0) pendingQueue.value.splice(currentIndex, 1)
        })
      } else {
        pendingQueue.value.splice(index, 1)
      }
    }
  }

  function notifyDiscardedHiddenControl(item?: ChatPendingItem): boolean {
    if (!item?.hiddenControl || !item.clientRequestId) return true
    const result = options.onHiddenControlDispatchResult?.({
      status: 'rejected',
      reason: 'discarded',
      clientRequestId: item.clientRequestId,
      sessionKey: item.hiddenControlSessionKey || '',
    })
    return result !== false
  }

  function switchPendingQueue(
    targetSessionKey: string,
    shouldCommit: () => boolean = () => true,
    handoffSignal?: AbortSignal,
  ): void | Promise<void> {
    if (reorderCommitPromise) {
      return reorderCommitPromise.then(() => {
        if (!shouldCommit()) return
        return switchPendingQueue(targetSessionKey, shouldCommit, handoffSignal)
      })
    }
    if (!shouldCommit()) return
    cancelPendingReorder()
    clearPendingDrainAfterTerminalTimer()
    activeQueueLease += 1
    const sourceSessionKey = queueSessionKey()
    const canonicalTargetSessionKey = queueSessionKey(targetSessionKey)
    if (sourceSessionKey && pendingQueue.value.length > 0) {
      for (const item of pendingQueue.value) {
        if (queueSessionKey(item.ownerSessionKey) === sourceSessionKey) {
          item.ownerSessionKey = sourceSessionKey
        }
      }
      const existing = parkedQueues.get(sourceSessionKey) || []
      const existingIds = new Set(existing.map(item => item.pendingInputId).filter(Boolean))
      parkedQueues.set(sourceSessionKey, [
        ...existing,
        ...pendingQueue.value.filter(item => (
          !item.pendingInputId || !existingIds.has(item.pendingInputId)
        )),
      ])
    }
    const restored = parkedQueues.get(canonicalTargetSessionKey) || []
    parkedQueues.delete(canonicalTargetSessionKey)
    pendingQueue.value = restored
    nextTick(() => void hydratePendingQueue(canonicalTargetSessionKey))
  }

  function applyAcceptedHandoffCommit(
    commit: AcceptedHandoffCommit,
    targetSessionKey: string,
    ownerRequestId: string,
  ) {
    targetSessionKey = queueSessionKey(targetSessionKey)
    const committedById = new Map(
      commit.records.map(record => [record.pendingInputId, itemFromWalRecord({
        ...record,
        sessionKey: queueSessionKey(record.sessionKey),
      })]),
    )
    const migrated: ChatPendingItem[] = []
    const updateOwned = (items: ChatPendingItem[]) => items.flatMap(item => {
      if (item.ownerRequestId !== ownerRequestId) return [item]
      const committed = item.pendingInputId
        ? committedById.get(item.pendingInputId)
        : undefined
      if (!committed) return [item]
      Object.assign(item, committed)
      item.ownerRequestId = undefined
      committedById.delete(committed.pendingInputId!)
      migrated.push(item)
      return []
    })
    pendingQueue.value = updateOwned(pendingQueue.value)
    for (const [sessionKey, items] of parkedQueues) {
      const retained = updateOwned(items)
      if (retained.length > 0) parkedQueues.set(sessionKey, retained)
      else parkedQueues.delete(sessionKey)
    }
    const targetItems = parkedQueues.get(targetSessionKey) || []
    const targetIds = new Set(targetItems.map(item => item.pendingInputId).filter(Boolean))
    for (const committed of [...migrated, ...committedById.values()]) {
      if (!targetIds.has(committed.pendingInputId)) targetItems.push(committed)
    }
    if (targetItems.length > 0) parkedQueues.set(targetSessionKey, targetItems)
  }

  async function acceptDurableHandoff(
    targetSessionKey: string,
    ownerRequestId: string,
    shouldApply: () => boolean = () => true,
    handoffSignal?: AbortSignal,
  ): Promise<boolean> {
    targetSessionKey = queueSessionKey(targetSessionKey)
    if (!options.pendingInputWal?.acceptHandoff) return false
    if (options.pendingInputWal.listHandoffs) {
      const records = await options.pendingInputWal.listHandoffs()
      if (!shouldApply()) return false
      if (!records.some(record => record.ownerRequestId === ownerRequestId)) return false
    }
    if (!shouldApply()) return false
    const commit = await options.pendingInputWal.acceptHandoff(
      ownerRequestId,
      targetSessionKey,
      shouldApply,
      handoffSignal,
    )
    if (!commit) return false
    if (shouldApply()) {
      applyAcceptedHandoffCommit(commit, targetSessionKey, ownerRequestId)
    }
    return true
  }

  async function recoverPendingQueueHandoff(
    sourceSessionKey: string,
    targetSessionKey: string,
    ownerRequestId: string,
  ): Promise<void> {
    if (!sourceSessionKey || !targetSessionKey || !ownerRequestId) return
    sourceSessionKey = queueSessionKey(sourceSessionKey)
    targetSessionKey = queueSessionKey(targetSessionKey)
    const committed = await acceptDurableHandoff(targetSessionKey, ownerRequestId)
    if (!committed) return
    if (queueSessionKey() === targetSessionKey) {
      activeQueueLease += 1
      const restored = parkedQueues.get(targetSessionKey) || []
      parkedQueues.delete(targetSessionKey)
      pendingQueue.value = [...pendingQueue.value, ...restored]
      sortOrdinaryPendingItems()
      for (const item of pendingQueue.value) {
        if (queueSessionKey(item.ownerSessionKey) === targetSessionKey) {
          void ensureServerStaged(item)
        }
      }
    }
    broadcastChange(sourceSessionKey)
    broadcastChange(targetSessionKey)
  }

  async function failPendingQueueHandoff(ownerRequestId: string): Promise<void> {
    const owned = [
      ...pendingQueue.value,
      ...[...parkedQueues.values()].flat(),
    ].filter(item => item.ownerRequestId === ownerRequestId && durableItem(item))
    await Promise.all(owned.map(item => writeWalItem(item, 'retryable').catch(() => {})))
    for (const item of owned) {
      broadcastChange(queueSessionKey(item.ownerSessionKey), item.pendingInputId)
    }
  }

  async function adoptPendingQueue(
    targetSessionKey: string,
    ownerRequestId: string,
    shouldCommit: () => boolean = () => true,
    handoffSignal?: AbortSignal,
  ) {
    if (reorderCommitPromise) await reorderCommitPromise
    else cancelPendingReorder()
    if (!shouldCommit()) return
    const sourceSessionKey = queueSessionKey()
    targetSessionKey = queueSessionKey(targetSessionKey)
    const durableCommitApplied = await acceptDurableHandoff(
      targetSessionKey,
      ownerRequestId,
      shouldCommit,
      handoffSignal,
    )
    if (!shouldCommit()) return
    // The source queue still owns its terminal drain signal until the durable
    // handoff has committed and this epoch is current. Clearing it before the
    // await would strand A if IndexedDB failed or A→B was superseded by A.
    clearPendingDrainAfterTerminalTimer()
    activeQueueLease += 1
    const carried: ChatPendingItem[] = []
    const stayingVisible: ChatPendingItem[] = []
    const stayingHidden: ChatPendingItem[] = []
    for (const item of pendingQueue.value) {
      if (item.hiddenControl) {
        stayingHidden.push(item)
        continue
      }
      if (
        !durableCommitApplied
        && ownerRequestId
        && queueSessionKey(item.ownerSessionKey) === sourceSessionKey
        && item.ownerRequestId === ownerRequestId
      ) {
        // Keep object identity: an in-flight explicit steer stores its
        // idempotent retry attempt against this exact queue item.
        item.ownerSessionKey = targetSessionKey
        item.ownerRequestId = undefined
        carried.push(item)
        if (durableItem(item)) {
          nextTick(() => {
            void writeWalItem(item, 'saving').then(() => ensureServerStaged(item))
          })
        }
      } else {
        stayingVisible.push(item)
      }
    }
    if (stayingVisible.length > 0 || stayingHidden.length > 0) {
      parkedQueues.set(sourceSessionKey, [
        ...(parkedQueues.get(sourceSessionKey) || []),
        ...stayingVisible,
        ...stayingHidden,
      ])
    }
    const targetItems = parkedQueues.get(targetSessionKey) || []
    parkedQueues.delete(targetSessionKey)
    pendingQueue.value = [...targetItems, ...carried]
    sortOrdinaryPendingItems()
    for (const item of pendingQueue.value) {
      if (queueSessionKey(item.ownerSessionKey) === targetSessionKey) {
        void ensureServerStaged(item)
      }
    }
    broadcastChange(sourceSessionKey)
    broadcastChange(targetSessionKey)
    nextTick(() => void hydratePendingQueue(targetSessionKey))
  }

  function hasUneditablePendingAttachments(item: ChatPendingItem): boolean {
    return item.attachments.some(attachment => (
      attachment.durable_material
      || (item.pendingPersistenceState === 'staged'
        && attachment.kind === 'staged'
        && !attachment.file)
    ))
  }

  function collisionFreeComposerAttachments(
    attachments: Attachment[],
    existing: Attachment[] = options.pendingAttachments.value,
  ): Attachment[] {
    const usedLocalIds = new Set(existing.map(attachment => attachment.local_id))
    let nextRecoveredLocalId = -1
    return attachments.map(attachment => {
      if (!usedLocalIds.has(attachment.local_id)) {
        usedLocalIds.add(attachment.local_id)
        return attachment
      }
      while (usedLocalIds.has(nextRecoveredLocalId)) nextRecoveredLocalId -= 1
      const rekeyed = { ...attachment, local_id: nextRecoveredLocalId }
      usedLocalIds.add(nextRecoveredLocalId)
      nextRecoveredLocalId -= 1
      return rekeyed
    })
  }

  function restoreDurableItemIntoComposer(
    item: ChatPendingItem,
    restore: () => void,
    lease = {
      ownerSessionKey: queueSessionKey(item.ownerSessionKey),
      queueLease: activeQueueLease,
      composerRevision,
      cancellationInvalidation: item.pendingInputId
        ? cancellationInvalidations.get(item.pendingInputId) ?? 0
        : 0,
    },
  ) {
    void cancelDurableItem(item, { retainAfterCancel: true }).then(retained => {
      if (
        !retained
        || disposed
        || queueSessionKey(item.ownerSessionKey) !== lease.ownerSessionKey
        || queueSessionKey() !== lease.ownerSessionKey
        || activeQueueLease !== lease.queueLease
        || composerRevision !== lease.composerRevision
        || (item.pendingInputId
          && (cancellationInvalidations.get(item.pendingInputId) ?? 0)
            !== lease.cancellationInvalidation)
      ) return
      const index = pendingQueue.value.indexOf(item)
      if (index < 0) return
      pendingQueue.value.splice(index, 1)
      restore()
      lease.composerRevision = composerRevision
      // The composer now owns the retained local-only payload. Remove its WAL
      // record without reopening a window where a navigation can restore the
      // source item into a different session's composer.
      void cancelDurableItem(item, { invalidateRestore: false })
    })
  }

  function restoreDurableItemsIntoComposerInOrder(
    items: ChatPendingItem[],
    restore: (item: ChatPendingItem) => void,
    lease: {
      ownerSessionKey: string
      queueLease: number
      composerRevision: number
      cancellationInvalidations: Map<string, number>
    },
  ) {
    void Promise.all(items.map(item => (
      cancelDurableItem(item, { retainAfterCancel: true })
    ))).then(retainedItems => {
      if (
        disposed
        || queueSessionKey() !== lease.ownerSessionKey
        || activeQueueLease !== lease.queueLease
        || composerRevision !== lease.composerRevision
      ) return
      for (const [index, item] of items.entries()) {
        if (
          item.pendingInputId
          && (cancellationInvalidations.get(item.pendingInputId) ?? 0)
            !== (lease.cancellationInvalidations.get(item.pendingInputId) ?? 0)
        ) break
        const queueIndex = pendingQueue.value.indexOf(item)
        if (!retainedItems[index]) {
          // A failed predecessor that still owns a queue slot is an ordering
          // barrier. A peer-terminal row is already absent and can be skipped.
          if (queueIndex >= 0) break
          continue
        }
        if (queueSessionKey(item.ownerSessionKey) !== lease.ownerSessionKey) break
        if (queueIndex < 0) continue
        pendingQueue.value.splice(queueIndex, 1)
        restore(item)
        lease.composerRevision = composerRevision
        // Each retained row is removed only after its ordered composer commit.
        void cancelDurableItem(item, { invalidateRestore: false })
      }
    })
  }

  function editPendingItem(pendingUiId: string): boolean {
    const index = pendingIndex(pendingUiId)
    const item = pendingQueue.value[index]
    if (
      !item
      || item.hiddenControl
      || item.deliveryState
      || item.steerAttempt
      // Annotation IDs refer to durable drafts owned by the annotation store.
      // This queue only has the IDs, not enough snapshot data to reconstruct
      // those drafts in the composer. Never turn such a queued batch into a
      // plain-text edit and silently drop its annotation context.
      || item.promptAnnotationIds?.length
      || item.pendingPersistenceState === 'saving'
      || item.pendingPersistenceState === 'cancelling'
      || hasUneditablePendingAttachments(item)
    ) return false
    const restore = () => {
      options.inputText.value = [item.text, options.inputText.value]
        .filter(text => text.trim())
        .join('\n')
      const restoredAttachments = (item.attachments || []).map(attachment => (
        item.pendingPersistenceState === 'staged'
        && attachment.kind === 'staged'
        && attachment.file
          ? { ...attachment, expires_at: 0 }
          : attachment
      ))
      options.pendingAttachments.value = [
        ...collisionFreeComposerAttachments(restoredAttachments),
        ...options.pendingAttachments.value,
      ]
      options.pendingSessionIntent.value = (
        item.intent || options.pendingSessionIntent.value
      )
      options.autoResizeTextarea()
    }
    if (durableItem(item)) {
      restoreDurableItemIntoComposer(item, restore)
      return true
    }
    pendingQueue.value.splice(index, 1)
    restore()
    return true
  }

  function popPendingTail() {
    // Hidden controls and explicit/ambiguous steer deliveries must retain
    // their own transport identity instead of being converted into a fresh
    // composer send.
    let tailIndex = pendingQueue.value.length - 1
    while (
      tailIndex >= 0
      && (
        pendingQueue.value[tailIndex]?.hiddenControl
        || pendingQueue.value[tailIndex]?.deliveryState
        || pendingQueue.value[tailIndex]?.steerAttempt
      )
    ) tailIndex--
    if (tailIndex < 0) return false
    const tail = pendingQueue.value[tailIndex]
    if (!tail) return false
    if (hasUneditablePendingAttachments(tail)) return false
    if (durableItem(tail)) {
      restoreDurableItemIntoComposer(tail, () => {
        options.inputText.value = tail.text || ''
        options.pendingAttachments.value = collisionFreeComposerAttachments(
          tail.attachments || [],
          [],
        )
        options.pendingSessionIntent.value = tail.intent || null
        options.autoResizeTextarea()
      })
      return true
    }
    pendingQueue.value.splice(tailIndex, 1)
    options.inputText.value = tail?.text || ''
    options.pendingAttachments.value = collisionFreeComposerAttachments(
      tail?.attachments || [],
      [],
    )
    options.pendingSessionIntent.value = tail?.intent || null
    options.autoResizeTextarea()
    return true
  }

  function popAllPendingIntoComposer(): boolean {
    cancelPendingReorder()
    clearPendingDrainAfterTerminalTimer()
    if (!options.hasComposer() || pendingQueue.value.length === 0) return false
    // Hidden controls and explicit/ambiguous steer deliveries stay queued;
    // only transport-free visible drafts can safely return to the composer.
    const visible = pendingQueue.value.filter(
      p => !p.hiddenControl
        && !p.deliveryState
        && !p.steerAttempt
        && !hasUneditablePendingAttachments(p),
    )
    if (visible.length === 0) return false
    const immediate = visible.filter(item => !durableItem(item))
    const durable = visible.filter(durableItem)
    const queuedTexts = immediate.map(p => p.text).filter(Boolean)
    const queuedAttachments = immediate.flatMap(p => p.attachments || [])
    const headIntent = immediate[0]?.intent
    const current = options.inputText.value || ''
    const joined = [current, ...queuedTexts].filter(Boolean).join('\n')
    const immediateItems = new Set(immediate)
    for (let index = pendingQueue.value.length - 1; index >= 0; index--) {
      if (immediateItems.has(pendingQueue.value[index]!)) {
        pendingQueue.value.splice(index, 1)
      }
    }
    options.inputText.value = joined
    options.pendingAttachments.value = [
      ...options.pendingAttachments.value,
      ...collisionFreeComposerAttachments(queuedAttachments),
    ]
    options.pendingSessionIntent.value = options.pendingSessionIntent.value || headIntent || null
    options.autoResizeTextarea()
    options.resetInputHistory()
    const restoreLease = {
      ownerSessionKey: queueSessionKey(),
      queueLease: activeQueueLease,
      composerRevision,
      cancellationInvalidations: new Map(durable.flatMap(item => (
        item.pendingInputId
          ? [[item.pendingInputId, cancellationInvalidations.get(item.pendingInputId) ?? 0]]
          : []
      ))),
    }
    restoreDurableItemsIntoComposerInOrder(durable, item => {
      options.inputText.value = [options.inputText.value, item.text]
        .filter(Boolean)
        .join('\n')
      options.pendingAttachments.value = [
        ...options.pendingAttachments.value,
        ...collisionFreeComposerAttachments(item.attachments || []),
      ]
      options.pendingSessionIntent.value = (
        options.pendingSessionIntent.value || item.intent || null
      )
      options.autoResizeTextarea()
      options.resetInputHistory()
    }, restoreLease)
    return true
  }

  function drainQueueHead() {
    clearPendingDrainAfterTerminalTimer()
    if (pendingQueue.value.length === 0) return
    const head = pendingQueue.value[0]
    const ownerSessionKey = queueSessionKey(head?.ownerSessionKey)
    if (ownerSessionKey !== queueSessionKey()) {
      if (head) head.deliveryState = 'retryable'
      return
    }
    if (head?.hiddenControl) {
      head.deliveryState = 'steering'
      // Hidden-control sends bypass the composer entirely, but retain their
      // queue lease until the transport confirms acceptance.
      nextTick(() => {
        void (async () => {
          let outcome: PendingDeliveryOutcome = 'retryable_failure'
          try {
            if (queueSessionKey() === ownerSessionKey) {
              outcome = await options.dispatchHiddenControl?.(
                head,
                ownerSessionKey,
              ) ?? 'retryable_failure'
            }
          } catch {
            outcome = 'retryable_failure'
          } finally {
            if (head.clientRequestId) {
              options.onHiddenControlDispatchResult?.({
                status: outcome === 'accepted'
                  ? 'accepted'
                  : outcome === 'not_sent'
                    ? 'rejected'
                    : 'unknown',
                reason: outcome === 'accepted'
                  ? 'accepted'
                  : outcome === 'not_sent'
                    ? 'send_rejected'
                    : 'response_unknown',
                clientRequestId: head.clientRequestId,
                sessionKey: head.hiddenControlSessionKey || ownerSessionKey,
              })
            }
            settlePendingDelivery(head, outcome)
          }
        })()
      })
      return
    }
    if (options.dispatchPendingItem) {
      const item = beginPendingDelivery(head.pendingUiId)
      if (!item) return
      nextTick(() => {
        void (async () => {
          let outcome: PendingDeliveryOutcome = 'retryable_failure'
          try {
            if (queueSessionKey() === ownerSessionKey) {
              outcome = await options.dispatchPendingItem!(item, ownerSessionKey)
            }
          } catch {
            // Keep the queue item as an explicit idempotent retry. The send
            // layer normally converts transport errors to this outcome, but
            // the queue must also fail closed if an unexpected error escapes.
            outcome = 'retryable_failure'
          } finally {
            settlePendingDelivery(item, outcome)
          }
        })()
      })
      return
    }
    if (!head) return
    head.deliveryState = 'steering'
    nextTick(() => {
      if (
        queueSessionKey() !== ownerSessionKey
        || pendingQueue.value[0] !== head
      ) return
      pendingQueue.value.shift()
      options.inputText.value = head.text || ''
      options.pendingAttachments.value = collisionFreeComposerAttachments(
        head.attachments || [],
        [],
      )
      options.pendingSessionIntent.value = head.intent || null
      options.sendCurrentInput()
    })
  }

  function schedulePendingDrainAfterTerminal() {
    if (pendingQueue.value.length === 0) {
      // A terminal subscription replay can arrive while response handoff is
      // still hydrating, before the matching follow-up reaches the queue.
      // Preserve that terminal signal until the blocker releases.
      deferredDrainRequested = options.isBlocked()
      return
    }
    deferredDrainRequested = true
    if (hasDeliveryBarrier.value || isReordering.value) return
    armPendingDrainTimer()
  }

  function armPendingDrainTimer() {
    cancelPendingDrainTimer()
    if (hasDeliveryBarrier.value || isReordering.value) return
    pendingDrainTimer = setTimeout(() => {
      pendingDrainTimer = null
      if (pendingQueue.value.length === 0) {
        deferredDrainRequested = false
        return
      }
      if (
        options.isStreaming.value
        || options.isBlocked()
        || hasDeliveryBarrier.value
        || isReordering.value
      ) return
      deferredDrainRequested = false
      drainQueueHead()
    }, 50)
  }

  function flushDeferredPendingDrain() {
    if (
      !deferredDrainRequested
      || pendingQueue.value.length === 0
      || hasDeliveryBarrier.value
      || isReordering.value
    ) return
    armPendingDrainTimer()
  }

  function pendingReorderMode(): PendingReorderMode | null {
    if (
      pendingQueue.value.length < 2
      || pendingQueue.value.some(item => (
        !ordinaryDurableItem(item)
        || Boolean(item.ownerRequestId)
        || !Number.isSafeInteger(item.pendingWalRevision)
        || item.pendingPersistenceState === 'saving'
        || item.pendingPersistenceState === 'retryable'
        || item.pendingPersistenceState === 'cancelling'
      ))
    ) return null
    if (
      pendingQueue.value.every(item => (
        item.pendingPersistenceState === 'local_only'
        && item.pendingMayHaveServerCopy === false
      ))
      && options.pendingInputWal?.commitOrder
    ) return 'local'
    if (
      pendingQueue.value.every(item => (
        item.pendingPersistenceState === 'staged'
        && Number.isSafeInteger(item.pendingServerRevision)
      ))
      && supportsServerReorder()
    ) return 'server'
    return null
  }

  function canReorderPendingQueue(): boolean {
    return pendingReorderMode() !== null
  }

  function restorePendingOrder(orderedIds: string[]) {
    const byId = new Map(
      pendingQueue.value.map(item => [item.pendingInputId || '', item]),
    )
    if (orderedIds.some(id => !byId.has(id))) return
    pendingQueue.value = orderedIds.map(id => byId.get(id)!)
  }

  function finishPendingReorder() {
    reorderSnapshot = null
    isReordering.value = false
    const hydrateSession = deferredHydrateSession
    deferredHydrateSession = ''
    if (hydrateSession) void hydratePendingQueue(hydrateSession)
    flushDeferredPendingDrain()
  }

  async function applyServerReorderItems(
    items: PendingInputServerItem[],
  ): Promise<void> {
    const byId = new Map(
      items.map(item => [item.pendingInputId, item]),
    )
    const orderedIds = items
      .slice()
      .sort((left, right) => Number(left.position) - Number(right.position))
      .map(item => item.pendingInputId)
    if (
      orderedIds.length !== pendingQueue.value.length
      || orderedIds.some(id => !id || !byId.has(id))
    ) throw new Error('Gateway returned an incomplete pending order')
    restorePendingOrder(orderedIds)
    for (const item of pendingQueue.value) {
      const serverItem = byId.get(item.pendingInputId || '')!
      item.pendingPosition = Number(serverItem.position)
      item.pendingServerRevision = Number(serverItem.revision)
      item.pendingWalRevision = (item.pendingWalRevision ?? 0) + 1
    }
    if (!options.pendingInputWal?.putMany) {
      for (const item of pendingQueue.value) {
        await options.pendingInputWal?.put(walRecordForItem(item, 'staged'))
      }
    } else {
      await options.pendingInputWal.putMany(
        pendingQueue.value.map(item => walRecordForItem(item, 'staged')),
      )
    }
  }

  async function recoverServerReorder(): Promise<boolean> {
    if (!pendingInputQueue || !supportsServerQueue()) return false
    const expectedOrder = pendingQueue.value.map(item => item.pendingInputId)
    try {
      const response = { items: await pendingInputQueue.list(queueSessionKey()) }
      const items = Array.isArray(response.items) ? response.items : []
      const serverOrder = items
        .slice()
        .sort((left, right) => Number(left.position) - Number(right.position))
        .map(item => item.pendingInputId)
      await applyServerReorderItems(items)
      if (serverOrder.some((id, index) => id !== expectedOrder[index])) {
        options.onPendingPersistenceError?.('order_conflict')
      }
      broadcastChange(queueSessionKey())
      return true
    } catch {
      return false
    }
  }

  function beginPendingReorder(index: number): boolean {
    if (
      isReordering.value
      || !canReorderPendingQueue()
      || !pendingQueue.value[index]
    ) return false
    cancelPendingDrainTimer()
    const mode = pendingReorderMode()!
    reorderSnapshot = {
      mode,
      originalOrder: pendingQueue.value.map(item => item.pendingInputId!),
      expectedWalRevisions: Object.fromEntries(
        pendingQueue.value.map(item => [item.pendingInputId!, item.pendingWalRevision!]),
      ),
    }
    isReordering.value = true
    return true
  }

  function reorderPendingItem(fromIndex: number, toIndex: number): boolean {
    if (
      !isReordering.value
      || !reorderSnapshot
      || fromIndex === toIndex
      || fromIndex < 0
      || toIndex < 0
      || fromIndex >= pendingQueue.value.length
      || toIndex >= pendingQueue.value.length
    ) return false
    const [item] = pendingQueue.value.splice(fromIndex, 1)
    if (!item) return false
    pendingQueue.value.splice(toIndex, 0, item)
    return true
  }

  async function commitPendingReorder() {
    const snapshot = reorderSnapshot
    if (!isReordering.value || !snapshot) return
    const orderedIds = pendingQueue.value.map(item => item.pendingInputId!)
    if (orderedIds.every((id, index) => id === snapshot.originalOrder[index])) {
      finishPendingReorder()
      return
    }
    if (snapshot.mode === 'local') {
      try {
        const result = await options.pendingInputWal!.commitOrder!(
          queueSessionKey(),
          orderedIds,
          snapshot.expectedWalRevisions,
          walLookupSessionKeys(options.sessionKey.value),
        )
        const byId = new Map(result.records.map(record => [record.pendingInputId, record]))
        for (const item of pendingQueue.value) {
          const record = byId.get(item.pendingInputId!)!
          item.pendingPosition = record.position
          item.pendingWalRevision = record.walRevision
        }
        broadcastChange(queueSessionKey())
      } catch {
        finishPendingReorder()
        await hydratePendingQueue(queueSessionKey())
        options.onPendingPersistenceError?.('wal_failed')
        return
      }
      finishPendingReorder()
      return
    }
    try {
      const response = await pendingInputQueue!.reorder({
          key: queueSessionKey(),
          items: pendingQueue.value.map(item => ({
            pendingInputId: item.pendingInputId,
            expectedRevision: item.pendingServerRevision,
          })),
        })
      await applyServerReorderItems(Array.isArray(response.items) ? response.items : [])
      broadcastChange(queueSessionKey())
      finishPendingReorder()
    } catch {
      // An unknown RPC result may have committed. Keep the delivery barrier
      // until an authoritative list proves which order won.
      if (await recoverServerReorder()) finishPendingReorder()
      else options.onPendingPersistenceError?.('server_rejected')
    }
  }

  function endPendingReorder(): Promise<void> {
    if (reorderCommitPromise) return reorderCommitPromise
    reorderCommitPromise = commitPendingReorder().finally(() => {
      reorderCommitPromise = null
    })
    return reorderCommitPromise
  }

  function cancelPendingReorder() {
    if (reorderCommitPromise) return
    if (reorderSnapshot) restorePendingOrder(reorderSnapshot.originalOrder)
    finishPendingReorder()
  }

  function cancelPendingDrainTimer() {
    if (pendingDrainTimer) {
      clearTimeout(pendingDrainTimer)
      pendingDrainTimer = null
    }
  }

  function clearPendingDrainAfterTerminalTimer() {
    cancelPendingDrainTimer()
    deferredDrainRequested = false
  }

  function cleanup() {
    cancelPendingReorder()
    disposed = true
    activeQueueLease += 1
    hydrateGeneration += 1
    stopComposerRevisionWatch()
    clearPendingDrainAfterTerminalTimer()
    parkedQueues.clear()
    broadcast?.close()
    options.pendingInputWal?.close()
  }

  return {
    pendingQueue,
    canQueueMore,
    canReorder,
    busySendMode,
    isReordering,
    maxPending: MAX_PENDING,
    enqueuePendingPayload,
    enqueuePendingInput,
    enqueueRecoveredInput,
    enqueueHiddenControl,
    enqueuePendingSteerAttempt,
    removePendingChip,
    beginPendingDelivery,
    settlePendingDelivery,
    cancelDurableItem,
    clearPendingQueue,
    switchPendingQueue,
    adoptPendingQueue,
    recoverPendingQueueHandoff,
    failPendingQueueHandoff,
    editPendingItem,
    popPendingTail,
    popAllPendingIntoComposer,
    beginPendingReorder,
    reorderPendingItem,
    endPendingReorder,
    schedulePendingDrainAfterTerminal,
    flushDeferredPendingDrain,
    hydratePendingQueue,
    clearPendingDrainAfterTerminalTimer,
    cleanup,
  }
}
