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

const CLOSED_BROWSER_HISTORY_LIMIT = 8
// Matches the existing Gateway PTY admission limit. Keeping terminals across
// navigation must not turn a bounded connection into unbounded mounted views.
const TERMINAL_ITEM_LIMIT = 4
// Native interactive artifact pages cannot be safely reconstructed yet. Bound
// new protected preview owners instead of evicting existing user work. This is
// an admission count, not a heap limit or a cap on adopted browser targets.
const NATIVE_ARTIFACT_ITEM_LIMIT = 4

interface WorkbenchAdmissionRejection {
  reason: 'native-preview-capacity'
  item: WorkbenchItem
  limit: number
}

interface SessionPresentation {
  activeItemId: string | null
  expanded: boolean
  widthPreference: WorkbenchWidthPreference
}

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
  // Transient presentation intent from an explicit user action. It never
  // changes panel ownership, retention, width preference, or execution state.
  const revealRequestId = ref(0)
  const hostAvailable = ref(true)
  const widthPreference = ref<WorkbenchWidthPreference>(hydrateWidthPreference())
  const activeSessionId = ref<string | null>(null)
  // Closed browser tabs remain available as a scoped, in-memory reopen shelf.
  // Native surface handles are deliberately stripped when reopened by the
  // caller; the page gets a fresh safe surface instead of a stale targetRef.
  const closedBrowserItems = ref<WorkbenchItem[]>([])

  // These collections are runtime-only. They are intentionally not reactive or
  // persisted, and therefore cannot leak controller state into Pinia snapshots.
  const activationOrder: string[] = []
  // Runtime-only presentation state: no child bindings, credentials, document
  // contents or native handles are serialized when the user navigates.
  const sessionPresentations = new Map<string | null, SessionPresentation>()
  const lifecycleListeners = new Set<WorkbenchLifecycleListener>()
  const admissionListeners = new Set<(rejection: WorkbenchAdmissionRejection) => void>()

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

  function onAdmissionRejected(listener: (rejection: WorkbenchAdmissionRejection) => void): () => void {
    admissionListeners.add(listener)
    return () => admissionListeners.delete(listener)
  }

  function rejectAdmission(rejection: WorkbenchAdmissionRejection): false {
    for (const listener of admissionListeners) {
      try { listener(rejection) } catch (error) {
        console.error('[workbench] admission listener failed', error)
      }
    }
    return false
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
    if (!item || item.scope.type === 'session' && item.scope.id !== activeSessionId.value) return false
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
    if (!existing && item.kind === 'artifact-preview' && item.hostKind === 'native-webcontents'
      && items.value.filter(candidate => candidate.kind === 'artifact-preview'
        && candidate.hostKind === 'native-webcontents').length >= NATIVE_ARTIFACT_ITEM_LIMIT) {
      return rejectAdmission({ reason: 'native-preview-capacity', item, limit: NATIVE_ARTIFACT_ITEM_LIMIT })
    }
    if (item.kind === 'terminal' && !existing
      && items.value.filter(candidate => candidate.kind === 'terminal').length >= TERMINAL_ITEM_LIMIT) return false
    if (options.activate !== false && activeSessionId.value === null && item.scope.type === 'session') {
      setSessionScope(item.scope.id)
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
    // Background conversations may finish a resource after navigation. Keep
    // that resource on its owner's shelf without replacing the visible panel.
    const matchesActiveSession = item.scope.type !== 'session'
      || item.scope.id === activeSessionId.value
    if (options.activate !== false && matchesActiveSession) {
      expanded.value = true
      activateItem(item.id)
    } else {
      rememberActivation(item.id)
    }
    return true
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
      ].slice(0, CLOSED_BROWSER_HISTORY_LIMIT)
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
    if (scope.type === 'session') sessionPresentations.delete(scope.id)
  }

  function closeAllItems(
    reason: WorkbenchDisposeReason = 'closed',
  ) {
    closeMatchingItems(() => true, reason)
    sessionPresentations.clear()
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
    const emptyLauncherExpanded = expanded.value && activeItemId.value === null
    sessionPresentations.set(activeSessionId.value, {
      activeItemId: activeItemId.value,
      expanded: expanded.value,
      widthPreference: { ...widthPreference.value },
    })
    // Navigation relinquishes the visible rectangle, never the business owner.
    // In particular it must not run close hooks or remove retained descriptors.
    if (expanded.value && hostAvailable.value) suspendItem(activeItem.value)
    activeSessionId.value = sessionId
    const saved = sessionPresentations.get(sessionId)
    const matchesScope = (item: WorkbenchItem) => item.scope.type !== 'session'
      || item.scope.id === sessionId
    const savedItem = saved?.activeItemId
      ? items.value.find(item => item.id === saved.activeItemId && matchesScope(item))
      : null
    // A deliberately selected empty start page is also part of the layout.
    const replacement = saved && saved.activeItemId === null
      ? null : savedItem ?? findMostRecentItem(matchesScope)
    activeItemId.value = replacement?.id ?? null
    // An explicitly open launcher remains a stable shell while a new owner
    // resolves. A retained conversation's own preference always takes priority.
    expanded.value = saved?.expanded ?? emptyLauncherExpanded
    maximized.value = false
    widthPreference.value = saved ? { ...saved.widthPreference } : hydrateWidthPreference()
    if (replacement) {
      rememberActivation(replacement.id)
      notify({ type: 'activate', item: replacement })
      if (expanded.value && hostAvailable.value) resumeItem(replacement)
    }
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

  function requestReveal() {
    revealRequestId.value += 1
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
    sessionPresentations.clear()
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
    revealRequestId,
    hostAvailable,
    widthPreference,
    isVisible,
    hasMultipleItems,
    onLifecycle,
    onAdmissionRejected,
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
    requestReveal,
    setHostAvailable,
    setWidth,
    resetWidth,
    reset,
  }
})
