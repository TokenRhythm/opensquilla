import { computed, onScopeDispose, ref, shallowRef, watch, type Ref } from 'vue'
import type { ChatMessage } from '@/types/chat'

interface ViewSnapshot {
  messages: ChatMessage[]
  bytes: number
  epoch: number | null
}

interface Options {
  sessionKey: Readonly<Ref<string>>
  deliveryIdentity: Readonly<Ref<string | null>>
  messages: Readonly<Ref<ChatMessage[]>>
  maxSessions?: number
  maxBytes?: number
  maxMessages?: number
}

/**
 * Last-known presentation only. Never installs history cursors, task owners,
 * approvals or live state. The normal bootstrap remains authoritative.
 * Owned by one ChatView, bounded across sessions, and never persisted to disk.
 */
export function useChatSessionViewCache(options: Options) {
  const snapshots = new Map<string, ViewSnapshot>()
  const preview = shallowRef<ViewSnapshot | null>(null)
  const generation = ref(0)
  const maxSessions = options.maxSessions ?? 6
  const maxBytes = options.maxBytes ?? 8 * 1024 * 1024
  const maxMessages = options.maxMessages ?? 200
  let totalBytes = 0
  let installed = false
  let epoch: number | null = null

  function remove(key: string) {
    const previous = snapshots.get(key)
    if (previous) totalBytes -= previous.bytes
    snapshots.delete(key)
  }

  function capture(key: string) {
    if (!installed || !options.deliveryIdentity.value || !key) return
    // Clone once on departure: mutable live/history objects cannot change a
    // detached view, and serialization accounts for nested tool/reasoning data.
    const serialized = JSON.stringify(options.messages.value.slice(-maxMessages))
    const bytes = new TextEncoder().encode(serialized).byteLength
    remove(key)
    if (bytes > maxBytes || maxSessions <= 0) return
    snapshots.set(key, { messages: JSON.parse(serialized), bytes, epoch })
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
    }
    // Also retires completed-body and disclosure caches at the composition root.
    generation.value += 1
  }

  watch(options.deliveryIdentity, () => invalidate(), { flush: 'sync' })
  watch(options.sessionKey, (key, previous) => {
    capture(previous)
    installed = false
    epoch = null
    const cached = options.deliveryIdentity.value ? snapshots.get(key) : undefined
    if (cached) {
      snapshots.delete(key)
      snapshots.set(key, cached)
    }
    preview.value = cached ?? null
    epoch = cached?.epoch ?? null
  }, { flush: 'sync' })

  function historyInstalled(key: string) {
    if (key !== options.sessionKey.value) return
    preview.value = null
    installed = true
  }

  function observeEpoch(key: string, next: number | null) {
    if (key !== options.sessionKey.value || next === null) return
    const previous = epoch ?? preview.value?.epoch
    if (previous != null && previous !== next) invalidate(key)
    epoch = next
  }

  onScopeDispose(() => {
    snapshots.clear()
    preview.value = null
  })

  return {
    messages: computed(() => preview.value?.messages ?? options.messages.value),
    showingPreview: computed(() => preview.value !== null),
    generation,
    historyInstalled,
    observeEpoch,
    invalidate,
  }
}
