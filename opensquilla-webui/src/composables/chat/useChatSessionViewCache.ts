import { computed, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import type { ChatMessage } from '@/types/chat'
import { createChatViewCaptureBudget } from './chatViewCaptureBudget'

interface ViewSnapshot<Presentation, Viewport> {
  messages: ChatMessage[]
  presentation?: Presentation
  viewport?: Viewport
  bytes: number
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

  function capture(key: string) {
    if (!installed || !options.deliveryIdentity.value || !key) return
    remove(key)
    if (maxSessions <= 0) return
    // Clone once on departure: mutable live/history objects cannot change a
    // detached view, and serialization accounts for nested tool/reasoning data.
    const retainedMessages = options.messages.value.slice(-maxMessages)
    const budget = createChatViewCaptureBudget(maxBytes)
    if (!budget.include('messages', retainedMessages)) return
    const presentation = options.capturePresentation?.(key)
    if (!budget.include('presentation', presentation)) return
    const viewport = options.captureViewport?.(retainedMessages)
    if (!budget.include('viewport', viewport)) return
    const serialized = JSON.stringify({ messages: retainedMessages, presentation, viewport })
    const bytes = budget.bytes
    snapshots.set(key, { ...JSON.parse(serialized), bytes, epoch })
    totalBytes += bytes
    while (snapshots.size > maxSessions || totalBytes > maxBytes) {
      remove(snapshots.keys().next().value!)
    }
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
    const keepHistory = adopt && historyReady.value && historyConfirmed && historyAuthority === authority
    const keepLive = adopt && liveReady.value && liveAuthority === authority
    const keepCompleteLive = keepLive && liveConfirmed
    const keepEpoch = adopt && epochAuthority === authority ? epoch : null
    invalidate()
    if (keepEpoch !== null) observeEpoch(options.sessionKey.value, keepEpoch)
    if (keepHistory) historyInstalled(options.sessionKey.value)
    if (keepCompleteLive) liveInstalled(options.sessionKey.value)
    else if (keepLive) liveInstalledWithoutMetadata(options.sessionKey.value)
  }, { flush: 'sync' })
  watch(options.sessionKey, (key, previous) => {
    capture(previous)
    installed = false
    historyReady.value = false
    historyConfirmed = false
    historyAuthority = null
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

  function historyInstalled(key: string, authoritative = true) {
    if (key !== options.sessionKey.value) return
    historyReady.value = true
    historyConfirmed = authoritative
    historyAuthority = authoritative ? currentAuthority() : null
    releasePreview()
  }

  function beginLiveRead(key: string) {
    if (key !== options.sessionKey.value) return
    // Freeze the last complete view before the subscription clears live state.
    // Repeated recovery attempts must not recapture their partial replacement.
    capture(key)
    if (!preview.value && options.deliveryIdentity.value) {
      preview.value = snapshots.get(key) ?? null
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

  /** The live read succeeded, but full presentation metadata is unavailable. */
  function liveInstalledWithoutMetadata(key: string) {
    if (key !== options.sessionKey.value) return
    // Publish current history/live instead of freezing a healthy subscription
    // behind its previous preview. This does not certify a complete cache entry
    // or grant task authority; a rejected live read still uses liveInstalled(false).
    liveReady.value = true
    liveConfirmed = false
    // Keep the exact live receipt for initial identity proof, independently of
    // whether its full presentation metadata can be cached.
    liveAuthority = currentAuthority()
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
    liveInstalledWithoutMetadata,
    observeEpoch,
    invalidate,
  }
}
