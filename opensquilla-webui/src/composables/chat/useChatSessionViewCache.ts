import { computed, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import type { ChatMaintenanceEvent, ChatMessage } from '@/types/chat'
import { createChatViewCaptureBudget } from './chatViewCaptureBudget'
import { compactChatViewCaptureMessages } from './chatViewCaptureProjection'

interface ViewSnapshot<Presentation, Viewport> {
  messages: ChatMessage[]
  presentation?: Presentation
  viewport?: Viewport
  bytes: number
  epoch: number | null
  identity: string
}

export interface CompactionPresentationProof {
  identity: string | null
  authority: unknown
  epoch: number | null
}

interface Options<Presentation, Viewport> {
  sessionKey: Readonly<Ref<string>>
  deliveryIdentity: Readonly<Ref<string | null>>
  messages: Readonly<Ref<ChatMessage[]>>
  /** Exact current read lease; presentation receipts must never outlive it. */
  currentReadAuthority?: () => unknown
  capturePresentation?: (key: string) => Presentation
  captureViewport?: (retainedMessages: readonly ChatMessage[]) => Viewport
  waitForLive?: boolean
  maxSessions?: number
  maxBytes?: number
  maxMessages?: number
}

/**
 * Last-known presentation only. Never installs history cursors, task owners,
 * approvals or live state. The normal bootstrap remains authoritative.
 * Owned by one ChatView, bounded across sessions, and never persisted to disk.
 */
export function useChatSessionViewCache<Presentation = never, Viewport = never>(options: Options<Presentation, Viewport>) {
  const snapshots = new Map<string, ViewSnapshot<Presentation, Viewport>>()
  const preview = shallowRef<ViewSnapshot<Presentation, Viewport> | null>(null)
  const restoredViewport = shallowRef<Viewport | undefined>()
  const historyReady = ref(false)
  let historyConfirmed = false
  let historyAuthority: unknown = null
  let historyCompactions: readonly ChatMaintenanceEvent[] = []
  const liveReady = ref(!options.waitForLive)
  let liveConfirmed = !options.waitForLive
  let liveAuthority: unknown = null
  const generation = ref(0)
  const maxSessions = options.maxSessions ?? 6
  const maxBytes = options.maxBytes ?? 8 * 1024 * 1024
  const maxMessages = options.maxMessages ?? 200
  let totalBytes = 0
  let installed = false
  let epoch: number | null = null
  let epochAuthority: unknown = null
  let hasProvenIdentity = options.deliveryIdentity.value !== null
  const currentAuthority = () => options.currentReadAuthority?.() ?? null

  function remove(key: string) {
    const previous = snapshots.get(key)
    if (previous) totalBytes -= previous.bytes
    snapshots.delete(key)
  }

  function capture(key: string, retainReadable = false) {
    if (!options.deliveryIdentity.value || !key) return
    // A degraded read can already have released a readable view, and accepted
    // events can keep its live output growing before hydration is confirmed.
    // Freeze that displayed view for same-session recovery only. Confirmation
    // still owns navigation snapshots; a partial reread has liveReady=false
    // and cannot overwrite the last readable presentation.
    if (!installed && !(retainReadable && historyReady.value && liveReady.value)) return
    if (maxSessions <= 0) return
    // Clone once on departure: mutable live/history objects cannot change a
    // detached view, and serialization accounts for nested tool/reasoning data.
    let retainedMessages = options.messages.value.slice(-maxMessages)
    let budget = createChatViewCaptureBudget(maxBytes)
    if (!budget.include('messages', retainedMessages)) {
      // Byte/depth/unsafe failures retain the last valid snapshot. Only repeated
      // history activity graphs get a narrow, bounded presentation fallback.
      if (budget.rejectionReason !== 'nodes' && budget.rejectionReason !== 'properties') return
      const projected = compactChatViewCaptureMessages(retainedMessages)
      if (!projected) return
      budget = createChatViewCaptureBudget(maxBytes)
      if (!budget.include('messages', projected)) return
      retainedMessages = projected
    }
    const presentation = options.capturePresentation?.(key)
    if (!budget.include('presentation', presentation)) return
    const viewport = options.captureViewport?.(retainedMessages)
    if (!budget.include('viewport', viewport)) return
    const serialized = JSON.stringify({ messages: retainedMessages, presentation, viewport })
    const bytes = budget.bytes
    const snapshot: ViewSnapshot<Presentation, Viewport> = {
      ...JSON.parse(serialized), bytes, epoch, identity: options.deliveryIdentity.value,
    }
    if (!installed) return snapshot
    // Replacement is transactional: a failed capture must not erase a valid
    // last-known view. Identity/epoch/reset/terminal retirement still remove it.
    remove(key)
    snapshots.set(key, snapshot)
    totalBytes += bytes
    while (snapshots.size > maxSessions || totalBytes > maxBytes) {
      remove(snapshots.keys().next().value!)
    }
    return snapshot
  }

  function invalidate(key?: string) {
    if (key) remove(key)
    else {
      snapshots.clear()
      totalBytes = 0
    }
    if (!key || key === options.sessionKey.value) {
      preview.value = null
      installed = false
      epoch = null
      epochAuthority = null
      restoredViewport.value = undefined
      historyReady.value = false
      historyConfirmed = false
      historyAuthority = null
      historyCompactions = []
      liveReady.value = !options.waitForLive
      liveConfirmed = !options.waitForLive
      liveAuthority = null
    }
    // Also retires completed-body and disclosure caches at the composition root.
    generation.value += 1
  }

  watch(options.deliveryIdentity, (identity, previous) => {
    const authority = currentAuthority()
    const firstProof = !hasProvenIdentity && previous === null && identity !== null
    hasProvenIdentity ||= identity !== null
    // Target hashing runs alongside the first transport read. Its first proof
    // can arrive after either installation receipt, without a second read to
    // reopen the barrier. Adopt only receipts from the exact still-current
    // lease. A replacement/revoked identity always needs fresh installations.
    const adopt = firstProof && authority !== null
    // A same-lease fallback is readable, but remains unconfirmed and therefore
    // cannot become an authoritative navigation snapshot on identity arrival.
    const keepHistory = adopt && historyReady.value && historyAuthority === authority
    const keepHistoryConfirmed = historyConfirmed
    const keepLive = adopt && liveReady.value && liveConfirmed && liveAuthority === authority
    const keepEpoch = adopt && epochAuthority === authority ? epoch : null
    invalidate()
    if (keepEpoch !== null) observeEpoch(options.sessionKey.value, keepEpoch)
    if (keepHistory) historyInstalled(options.sessionKey.value, keepHistoryConfirmed)
    if (keepLive) liveInstalled(options.sessionKey.value)
  }, { flush: 'sync' })
  watch(options.sessionKey, (key, previous) => {
    capture(previous)
    installed = false
    historyReady.value = false
    historyConfirmed = false
    historyAuthority = null
    historyCompactions = []
    liveReady.value = !options.waitForLive
    liveConfirmed = !options.waitForLive
    liveAuthority = null
    epoch = null
    epochAuthority = null
    const cached = options.deliveryIdentity.value ? snapshots.get(key) : undefined
    if (cached) {
      snapshots.delete(key)
      snapshots.set(key, cached)
    }
    preview.value = cached ?? null
    restoredViewport.value = cached?.viewport
    epoch = cached?.epoch ?? null
  }, { flush: 'sync' })

  function historyInstalled(key: string, authoritative = true, compactions: readonly ChatMaintenanceEvent[] = []) {
    if (key !== options.sessionKey.value) return
    // A failed replacement read cannot supersede a readable preview. The
    // same lease's earlier canonical installation also remains valid when a
    // refresh failed without replacing its rows. Cold fallback still releases
    // its display barrier, but cannot become a confirmed navigation snapshot.
    if (!authoritative && (
      preview.value
      || (historyConfirmed && historyAuthority === currentAuthority())
    )) return
    historyReady.value = true
    historyConfirmed = authoritative
    historyAuthority = currentAuthority()
    // Only receipts in this canonical read are proof. Merged current messages
    // may also retain old/local maintenance, which must not mint new authority.
    const cachedIds = new Set(preview.value?.messages.flatMap(message => message.maintenance
      ? [message.maintenance.compactionId] : []) ?? [])
    historyCompactions = authoritative ? compactions.filter(receipt => cachedIds.has(receipt.compactionId)) : []
    if (authoritative) reconcileHistoryCompactions()
    releasePreview()
  }

  /** Update presentation only; the cached lease never becomes read authority. */
  function reconcileCompactionTerminal(key: string, receipt: ChatMaintenanceEvent, proof: CompactionPresentationProof): boolean {
    const cached = preview.value
    // Legacy semantic frames can omit epoch. A fast ACK from this exact
    // current lease can supply it; the preview's old lease cannot.
    const receiptEpoch = proof.epoch ?? (epochAuthority === proof.authority ? epoch : null)
    if (key !== options.sessionKey.value || !cached || receipt.kind !== 'context_compaction'
      || !receipt.compactionId || receipt.state === 'running'
      || !proof.identity || cached.identity !== proof.identity || options.deliveryIdentity.value !== proof.identity
      || proof.authority == null || proof.authority !== currentAuthority()
      || receiptEpoch === null || cached.epoch !== receiptEpoch) return false
    const index = cached.messages.findIndex(message => message.role === 'maintenance'
      && message.maintenance?.kind === 'context_compaction'
      && message.maintenance.compactionId === receipt.compactionId)
    const previous = cached.messages[index]
    if (!previous?.maintenance || (previous.maintenance.state !== 'running'
      && previous.maintenance.state !== receipt.state)) return false
    const messages = cached.messages.slice()
    messages[index] = { ...previous, maintenance: {
      ...previous.maintenance, ...receipt,
      source: previous.maintenance.source,
      durability: previous.maintenance.historyArchived ? previous.maintenance.durability
        : receipt.durability || previous.maintenance.durability,
      providerRecovery: undefined,
    } }
    const budget = createChatViewCaptureBudget(maxBytes)
    if (!budget.include('messages', messages) || !budget.include('presentation', cached.presentation)
      || !budget.include('viewport', cached.viewport)) return false
    const updated = { ...cached, messages, bytes: budget.bytes }
    // Keep the retained entry coherent across another A/B/A while a reread is
    // pending. Neither this receipt nor this copy installs current messages.
    if (snapshots.get(key) === cached) {
      totalBytes += updated.bytes - cached.bytes
      snapshots.set(key, updated)
      while (totalBytes > maxBytes) remove(snapshots.keys().next().value!)
    }
    preview.value = updated
    return true
  }

  function reconcileHistoryCompactions() {
    const authority = currentAuthority()
    if (!historyConfirmed || historyAuthority !== authority || epochAuthority !== authority) return
    for (const receipt of historyCompactions) {
      if (receipt.historyArchived && receipt.durability === 'durable') {
        reconcileCompactionTerminal(options.sessionKey.value, receipt, {
          identity: options.deliveryIdentity.value, authority, epoch,
        })
      }
    }
  }

  function beginLiveRead(key: string) {
    if (key !== options.sessionKey.value) return
    // Freeze the last complete view before the subscription clears live state.
    // Repeated recovery attempts must not recapture their partial replacement.
    const readable = capture(key, true)
    if (!preview.value && options.deliveryIdentity.value) {
      preview.value = readable ?? snapshots.get(key) ?? null
    }
    installed = false
    liveReady.value = false
    liveConfirmed = false
    liveAuthority = null
  }

  function retirePreview(key: string) {
    if (key !== options.sessionKey.value) return
    // An authoritative terminal result supersedes this task's cached live tail.
    // Keep already installed history and the reader's viewport, but wait for a
    // fresh live confirmation before allowing another complete snapshot.
    remove(key)
    preview.value = null
    installed = false
    liveReady.value = false
    liveConfirmed = false
    liveAuthority = null
  }

  function liveInstalled(key: string, authoritative = true) {
    if (key !== options.sessionKey.value) return
    // A failed/rejected live read is not a replacement for an already readable
    // snapshot. Keep that complete, inert view while the normal recovery retries.
    // Cold visits still release their readable history without granting control.
    if (!authoritative && preview.value) return
    liveReady.value = true
    liveConfirmed = authoritative
    liveAuthority = authoritative ? currentAuthority() : null
    releasePreview()
  }

  function releasePreview() {
    if (!historyReady.value || !liveReady.value) return
    // Publish history and the live tail together. A fast history response must
    // not erase a cached tail while subscribe/snapshot is still replaying it.
    preview.value = null
    installed = historyConfirmed && liveConfirmed
  }

  function observeEpoch(key: string, next: number | null) {
    if (key !== options.sessionKey.value || next === null) return
    const previous = epoch ?? preview.value?.epoch
    if (previous != null && previous !== next) invalidate(key)
    epoch = next
    epochAuthority = currentAuthority()
    reconcileHistoryCompactions()
  }

  onScopeDispose(() => {
    snapshots.clear()
    preview.value = null
  })

  return {
    messages: computed(() => preview.value?.messages ?? options.messages.value),
    presentation: computed(() => preview.value?.presentation),
    restoredViewport,
    ready: computed(() => historyReady.value && liveReady.value),
    showingPreview: computed(() => preview.value !== null),
    generation,
    historyInstalled,
    beginLiveRead,
    retirePreview,
    liveInstalled,
    observeEpoch,
    reconcileCompactionTerminal,
    invalidate,
  }
}
