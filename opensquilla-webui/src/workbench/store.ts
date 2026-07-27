import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  WORKBENCH_WIDTH_STORAGE_KEY,
  defaultWorkbenchWidthPreference,
  normalizeWorkbenchWidthPreference,
  parseWorkbenchWidthPreference,
  type WorkbenchWidthPreference,
} from './layout'
import type {
  WorkbenchDisposeReason,
  WorkbenchItem,
  WorkbenchLifecycleEvent,
  WorkbenchLifecycleListener,
  WorkbenchScope,
} from './types'

export const WORKBENCH_PREVIEW_ITEM_LIMIT = 8

function hydrateWidthPreference(): WorkbenchWidthPreference {
  if (typeof localStorage === 'undefined') return defaultWorkbenchWidthPreference()
  try {
    return parseWorkbenchWidthPreference(
      localStorage.getItem(WORKBENCH_WIDTH_STORAGE_KEY),
    )
  } catch {
    return defaultWorkbenchWidthPreference()
  }
}

function sameScope(left: WorkbenchScope, right: WorkbenchScope): boolean {
  return left.type === right.type
    && (left.type === 'app' || (right.type !== 'app' && left.id === right.id))
}

export const useWorkbenchStore = defineStore('workbench', () => {
  const items = ref<WorkbenchItem[]>([])
  const activeItemId = ref<string | null>(null)
  const expanded = ref(false)
  const hostAvailable = ref(true)
  const widthPreference = ref<WorkbenchWidthPreference>(hydrateWidthPreference())
  const activeSessionId = ref<string | null>(null)

  // Both collections are runtime-only. They are intentionally not reactive or
  // persisted, and therefore cannot leak controller state into Pinia snapshots.
  const activationOrder: string[] = []
  const lifecycleListeners = new Set<WorkbenchLifecycleListener>()

  const activeItem = computed<WorkbenchItem | null>(() =>
    items.value.find(item => item.id === activeItemId.value) ?? null)
  const isVisible = computed(() =>
    expanded.value && hostAvailable.value && activeItem.value !== null)
  const hasMultipleItems = computed(() => items.value.length > 1)

  function notify(event: WorkbenchLifecycleEvent) {
    for (const listener of lifecycleListeners) {
      try {
        listener(event)
      } catch (error) {
        console.error('[workbench] lifecycle listener failed', error)
      }
    }
  }

  function onLifecycle(listener: WorkbenchLifecycleListener): () => void {
    lifecycleListeners.add(listener)
    return () => lifecycleListeners.delete(listener)
  }

  function rememberActivation(id: string) {
    const previous = activationOrder.indexOf(id)
    if (previous >= 0) activationOrder.splice(previous, 1)
    activationOrder.push(id)
  }

  function forgetActivation(id: string) {
    const index = activationOrder.indexOf(id)
    if (index >= 0) activationOrder.splice(index, 1)
  }

  function nextRecentItem(): WorkbenchItem | null {
    for (let index = activationOrder.length - 1; index >= 0; index -= 1) {
      const id = activationOrder[index]
      const item = items.value.find(candidate => candidate.id === id)
      if (item) return item
      activationOrder.splice(index, 1)
    }
    return items.value.length > 0 ? items.value[items.value.length - 1] : null
  }

  function findMostRecentItem(
    predicate: (item: WorkbenchItem) => boolean,
  ): WorkbenchItem | null {
    for (let index = activationOrder.length - 1; index >= 0; index -= 1) {
      const id = activationOrder[index]
      const candidate = items.value.find(item => item.id === id)
      if (candidate && predicate(candidate)) return candidate
    }
    return null
  }

  function hasAvailableItemForSession(sessionId: string | null): boolean {
    return items.value.some(item =>
      item.scope.type !== 'session' || item.scope.id === sessionId)
  }

  function suspendItem(item: WorkbenchItem | null) {
    if (item) notify({ type: 'suspend', item })
  }

  function resumeItem(item: WorkbenchItem | null) {
    if (item) notify({ type: 'resume', item })
  }

  function activateItem(id: string): boolean {
    const item = items.value.find(candidate => candidate.id === id)
    if (!item) return false
    const previous = activeItem.value
    if (previous?.id === item.id) {
      rememberActivation(item.id)
      if (expanded.value && hostAvailable.value) resumeItem(item)
      return true
    }
    if (expanded.value && hostAvailable.value) suspendItem(previous)
    activeItemId.value = item.id
    rememberActivation(item.id)
    notify({ type: 'activate', item })
    if (expanded.value && hostAvailable.value) resumeItem(item)
    return true
  }

  function openItem(item: WorkbenchItem) {
    if (!updateItem(item)) {
      items.value.push(item)
      notify({ type: 'open', item })
    }
    expanded.value = true
    activateItem(item.id)
    evictLeastRecentArtifactPreviews(item.id)
  }

  /**
   * Preview tabs are intentionally bounded. Eviction follows the same
   * activation order used when closing tabs, so a newly opened document and
   * recently inspected documents survive while stale Blob-backed previews are
   * disposed deterministically.
   */
  function evictLeastRecentArtifactPreviews(protectedId: string) {
    let previewCount = items.value.filter(
      candidate => candidate.kind === 'artifact-preview',
    ).length
    while (previewCount > WORKBENCH_PREVIEW_ITEM_LIMIT) {
      const staleId = activationOrder.find(id => {
        if (id === protectedId) return false
        return items.value.some(
          candidate => candidate.id === id && candidate.kind === 'artifact-preview',
        )
      })
      if (!staleId || !closeItem(staleId, 'evicted')) break
      previewCount -= 1
    }
  }

  /** Refresh a descriptor without stealing focus from the active panel. */
  function updateItem(item: WorkbenchItem): boolean {
    const existingIndex = items.value.findIndex(candidate => candidate.id === item.id)
    if (existingIndex < 0) return false
    items.value[existingIndex] = item
    notify({ type: 'update', item })
    return true
  }

  function closeItem(
    id: string,
    reason: WorkbenchDisposeReason = 'closed',
  ): boolean {
    const index = items.value.findIndex(item => item.id === id)
    if (index < 0) return false
    const [removed] = items.value.splice(index, 1)
    const wasActive = activeItemId.value === id
    forgetActivation(id)
    notify({ type: 'dispose', item: removed, reason })

    if (wasActive) {
      activeItemId.value = null
      const next = nextRecentItem()
      if (next) {
        activeItemId.value = next.id
        rememberActivation(next.id)
        notify({ type: 'activate', item: next })
        if (expanded.value && hostAvailable.value) resumeItem(next)
      } else {
        expanded.value = false
      }
    }
    return true
  }

  function closeScope(
    scope: WorkbenchScope,
    reason: WorkbenchDisposeReason = 'scope-changed',
  ) {
    closeMatchingItems(item => sameScope(item.scope, scope), reason)
  }

  function closeAllItems(
    reason: WorkbenchDisposeReason = 'closed',
  ) {
    closeMatchingItems(() => true, reason)
  }

  function closeMatchingItems(
    predicate: (item: WorkbenchItem) => boolean,
    reason: WorkbenchDisposeReason,
  ) {
    const removed = items.value.filter(predicate)
    if (removed.length === 0) return
    const removedIds = new Set(removed.map(item => item.id))
    const activeWasRemoved = activeItemId.value !== null
      && removedIds.has(activeItemId.value)
    items.value = items.value.filter(item => !removedIds.has(item.id))
    for (const id of removedIds) forgetActivation(id)
    for (const item of removed) notify({ type: 'dispose', item, reason })

    if (!activeWasRemoved) return
    activeItemId.value = null
    const next = nextRecentItem()
    if (!next) {
      expanded.value = false
      return
    }
    activeItemId.value = next.id
    rememberActivation(next.id)
    notify({ type: 'activate', item: next })
    if (expanded.value && hostAvailable.value) resumeItem(next)
  }

  function setSessionScope(sessionId: string | null) {
    if (activeSessionId.value === sessionId) return
    closeMatchingItems(
      item => item.scope.type === 'session' && item.scope.id !== sessionId,
      'scope-changed',
    )
    activeSessionId.value = sessionId
  }

  function setExpanded(next: boolean) {
    if (expanded.value === next) return
    if (!next && hostAvailable.value) suspendItem(activeItem.value)
    expanded.value = next && activeItem.value !== null
    if (expanded.value && hostAvailable.value) resumeItem(activeItem.value)
  }

  function toggleExpanded() {
    setExpanded(!expanded.value)
  }

  function setHostAvailable(next: boolean) {
    if (hostAvailable.value === next) return
    if (!next && expanded.value) suspendItem(activeItem.value)
    hostAvailable.value = next
    if (next && expanded.value) resumeItem(activeItem.value)
  }

  function setWidth(width: number) {
    const next = normalizeWorkbenchWidthPreference({
      version: 1,
      width,
      source: 'user',
    })
    widthPreference.value = next
    try {
      localStorage.setItem(WORKBENCH_WIDTH_STORAGE_KEY, JSON.stringify(next))
    } catch {
      // A private or storage-constrained browser still gets the in-memory layout.
    }
  }

  function resetWidth() {
    widthPreference.value = defaultWorkbenchWidthPreference()
    try {
      localStorage.removeItem(WORKBENCH_WIDTH_STORAGE_KEY)
    } catch {
      // The in-memory default still restores an even split.
    }
  }

  function reset() {
    const openItems = [...items.value]
    items.value = []
    activeItemId.value = null
    expanded.value = false
    activeSessionId.value = null
    activationOrder.splice(0)
    for (const item of openItems) {
      notify({ type: 'dispose', item, reason: 'store-reset' })
    }
  }

  return {
    items,
    activeItemId,
    activeItem,
    activeSessionId,
    expanded,
    hostAvailable,
    widthPreference,
    isVisible,
    hasMultipleItems,
    onLifecycle,
    findMostRecentItem,
    hasAvailableItemForSession,
    openItem,
    updateItem,
    activateItem,
    closeItem,
    closeAllItems,
    closeScope,
    setSessionScope,
    setExpanded,
    toggleExpanded,
    setHostAvailable,
    setWidth,
    resetWidth,
    reset,
  }
})
