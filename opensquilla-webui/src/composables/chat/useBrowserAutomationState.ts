import { onScopeDispose, watch, type Ref } from 'vue'
import type { NativeBrowserAutomationState, NativeWorkbenchApi } from '@/platform/types'
import type { ChatRunStatus } from '@/types/chat'
import { chatTaskId } from './useChatTaskOwnership'

const BUSY_STATUSES = new Set(['queued', 'running', 'approval_pending'])
const owners = new WeakMap<NativeWorkbenchApi, Map<string, symbol>>()

interface BrowserAutomationStateOptions {
  native?: NativeWorkbenchApi
  sessionKey: Readonly<Ref<string>>
  connected: Readonly<Ref<boolean>>
  isStreaming: Readonly<Ref<boolean>>
  runStatus: Readonly<Ref<ChatRunStatus>>
  activeStreamTaskId: Readonly<Ref<string>>
  activeStreamSessionKey: Readonly<Ref<string>>
}

/** Projects existing chat ownership to a decorative native browser cursor. */
export function useBrowserAutomationState(options: BrowserAutomationStateOptions): void {
  const native = options.native
  if (!native?.setBrowserAutomationState) return
  const owner = Symbol('browser-automation-view')
  let sessionOwners = owners.get(native)
  if (!sessionOwners) {
    sessionOwners = new Map()
    owners.set(native, sessionOwners)
  }
  let current: NativeBrowserAutomationState | undefined
  let identity: { sessionKey: string; visualTaskId: string; backendTaskId: string } | undefined

  function send(state: NativeBrowserAutomationState) {
    // Visual feedback cannot fail a chat turn or block its cleanup.
    try { void native!.setBrowserAutomationState!(state).catch(() => {}) } catch {}
  }

  function release() {
    if (!current) return
    if (sessionOwners!.get(current.sessionKey) === owner) {
      sessionOwners!.delete(current.sessionKey)
      send({ ...current, active: false })
    }
    current = undefined
  }

  const stop = watch(() => {
    const sessionKey = options.sessionKey.value
    const ownsStream = !options.activeStreamSessionKey.value
      || options.activeStreamSessionKey.value === sessionKey
    const streaming = options.isStreaming.value && ownsStream
    const busy = BUSY_STATUSES.has(options.runStatus.value.status)
    return {
      sessionKey,
      connected: options.connected.value,
      running: Boolean(sessionKey) && (streaming || busy),
      taskId: (streaming ? options.activeStreamTaskId.value : '')
        || (busy ? chatTaskId(options.runStatus.value.task) : ''),
    }
  }, next => {
    if (!next.connected) { release(); return }
    if (!next.running) { release(); identity = undefined; return }
    if (identity && identity.sessionKey !== next.sessionKey) identity = undefined
    // Admission can start streaming before a backend identity arrives. Keep
    // that visual activation intact when its real task id becomes available.
    if (identity && next.taskId && !identity.backendTaskId) identity.backendTaskId = next.taskId
    if (!identity || (next.taskId && next.taskId !== identity.backendTaskId)) {
      identity = { sessionKey: next.sessionKey, backendTaskId: next.taskId,
        visualTaskId: next.taskId || `browser-visual-${crypto.randomUUID()}` }
    }
    if (current?.sessionKey === next.sessionKey && current.taskId === identity.visualTaskId) return
    release()
    current = { sessionKey: next.sessionKey, taskId: identity.visualTaskId, active: true }
    sessionOwners!.set(next.sessionKey, owner)
    send(current)
  }, { immediate: true, flush: 'post' })

  // Retire the old scope immediately; the post-flush projection waits for the
  // new session's streaming and hydrated state to settle before activating it.
  const stopSession = watch(options.sessionKey, () => {
    release()
    identity = undefined
  }, { flush: 'sync' })
  onScopeDispose(() => { stop(); stopSession(); release(); identity = undefined })
}
