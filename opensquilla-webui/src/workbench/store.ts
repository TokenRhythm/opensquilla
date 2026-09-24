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
  const maximized = ref(false)
  const hostAvailable = ref(true)
  const widthPreference = ref<WorkbenchWidthPreference>(hydrateWidthPreference())
  const activeSessionId = ref<string | null>(null)
  // Closed browser tabs remain available as a scoped, in-memory reopen shelf.
  // Native surface handles are deliberately stripped when reopened by the
  // caller; the page gets a fresh safe surface instead of a stale targetRef.
  const closedBrowserItems = ref<WorkbenchItem[]>([])

  // Both collections are runtime-only. They are intentionally not reactive or
  // persisted, and therefore cannot leak controller state into Pinia snapshots.
  const activationOrder: string[] = []
  const lifecycleListeners = new Set<WorkbenchLifecycleListener>()

  const activeItem = computed<WorkbenchItem | null>(() =>
    items.value.find(item => item.id === activeItemId.value) ?? null)
  const visibleItems = computed(() => items.value.filter(item =>
    item.scope.type !== 'session'
    || item.scope.id === activeSessionId.value,
  ))
  const isVisible = computed(() =>
    expanded.value && hostAvailable.value)
  const hasMultipleItems = computed(() => visibleItems.value.length > 1)

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
      if (item && (item.scope.type !== 'session'
        || item.scope.id === activeSessionId.value)) return item
      if (item) continue
      activationOrder.splice(index, 1)
    }
    return visibleItems.value[visibleItems.value.length - 1] ?? null
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

  function openItem(item: WorkbenchItem, options: { activate?: boolean } = {}): boolean {
    const existing = items.value.some(candidate => candidate.id === item.id)
    if (
      !existing
      && item.kind === 'browser'
      && items.value.filter(candidate => candidate.kind === 'browser').length
        >= WORKBENCH_PREVIEW_ITEM_LIMIT
    ) {
      return false
    }
    if (
      !existing
      && item.hostKind === 'native-webcontents'
      && items.value.filter(candidate => candidate.hostKind === 'native-webcontents').length
        >= WORKBENCH_PREVIEW_ITEM_LIMIT
    ) {
      return false
    }
    if (item.kind === 'browser') {
      closedBrowserItems.value = closedBrowserItems.value.filter(
        candidate => candidate.id !== item.id,
      )
    }
    if (!updateItem(item)) {
      items.value.push(item)
      notify({ type: 'open', item })
    }
    if (options.activate !== false) {
      if (activeSessionId.value === null && item.scope.type === 'session') {
        activeSessionId.value = item.scope.id
      }
      expanded.value = true
      activateItem(item.id)
    } else {
      rememberActivation(item.id)
    }
    evictLeastRecentArtifactPreviews(item.id)
    return true
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
          candidate =>
            candidate.id === id
            && candidate.kind === 'artifact-preview'
            && candidate.hostKind !== 'native-webcontents',
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
    if (removed.kind === 'browser' && reason === 'closed') {
      closedBrowserItems.value = [
        {
          ...removed,
          payload: {
            initialUrl: removed.payload.initialUrl,
            scopeId: removed.scope.type === 'session' ? removed.scope.id : '',
          },
        },
        ...closedBrowserItems.value.filter(candidate => candidate.id !== removed.id),
      ].slice(0, WORKBENCH_PREVIEW_ITEM_LIMIT)
    }
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
        if (removed.kind !== 'browser') {
          expanded.value = false
          maximized.value = false
        }
      }
    }
    return true
  }

  function reopenBrowserForSession(sessionId: string): boolean {
    const matches = (item: WorkbenchItem) => item.kind === 'browser'
      && item.scope.type === 'session'
      && item.scope.id === sessionId
    const live = findMostRecentItem(matches)
    if (live) {
      activateItem(live.id)
      setExpanded(true)
      return true
    }
    const closed = closedBrowserItems.value.find(matches)
    return closed ? openItem(closed) : false
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
      maximized.value = false
      return
    }
    activeItemId.value = next.id
    rememberActivation(next.id)
    notify({ type: 'activate', item: next })
    if (expanded.value && hostAvailable.value) resumeItem(next)
  }

  function setSessionScope(sessionId: string | null) {
    if (activeSessionId.value === sessionId) return
    // Browser tabs represent live native pages. Keep them in the in-memory
    // workbench shelf while the user moves between chat sessions so returning
    // to the originating task can reveal the same page again. Other
    // session-scoped panels still follow the existing disposal contract.
    closeMatchingItems(
      item => item.scope.type === 'session'
        && item.scope.id !== sessionId
        && item.kind !== 'browser',
      'scope-changed',
    )
    const active = activeItem.value
    if (!active || active.scope.type === 'session' && active.scope.id !== sessionId) {
      // Scope changes keep the native page alive, but it must stop consuming
      // the current sidebar rectangle before another session can take focus.
      suspendItem(active)
      const replacement = findMostRecentItem(item =>
        item.scope.type !== 'session'
        || item.scope.id === sessionId,
      )
      activeItemId.value = replacement?.id ?? null
      if (replacement) {
        rememberActivation(replacement.id)
        notify({ type: 'activate', item: replacement })
        if (expanded.value && hostAvailable.value) resumeItem(replacement)
      }
    }
    activeSessionId.value = sessionId
  }

  function setExpanded(next: boolean) {
    if (!next) maximized.value = false
    if (expanded.value === next) return
    if (!next && hostAvailable.value) suspendItem(activeItem.value)
    expanded.value = next
    if (expanded.value && hostAvailable.value) resumeItem(activeItem.value)
  }

  function openEmpty() {
    if (expanded.value && hostAvailable.value) suspendItem(activeItem.value)
    activeItemId.value = null
    expanded.value = true
  }

  function toggleExpanded() {
    setExpanded(!expanded.value)
  }

  function setMaximized(next: boolean) {
    if (next) setExpanded(true)
    maximized.value = next
  }

  function toggleMaximized() {
    setMaximized(!maximized.value)
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
    maximized.value = false
    activeSessionId.value = null
    activationOrder.splice(0)
    closedBrowserItems.value = []
    for (const item of openItems) {
      notify({ type: 'dispose', item, reason: 'store-reset' })
    }
  }

  return {
    items,
    visibleItems,
    closedBrowserItems,
    activeItemId,
    activeItem,
    activeSessionId,
    expanded,
    maximized,
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
    reopenBrowserForSession,
    closeAllItems,
    closeScope,
    setSessionScope,
    setExpanded,
    openEmpty,
    toggleExpanded,
    setMaximized,
    toggleMaximized,
    setHostAvailable,
    setWidth,
    resetWidth,
    reset,
  }
})
