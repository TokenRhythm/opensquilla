import { ref } from 'vue'

export type SessionTaskAttention = 'none' | 'running' | 'completed' | 'failed'
export type UnreadSessionTaskAttention = Exclude<SessionTaskAttention, 'none' | 'running'>

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

interface StoredAttention {
  taskId: string
  state: UnreadSessionTaskAttention
}

interface StoredAttentionState {
  version: 1
  unread: Record<string, StoredAttention>
  read: Record<string, string>
}

export interface SessionTaskAttentionContext {
  currentSessionKey: string
  currentSessionVisible: boolean
}

export const SESSION_TASK_ATTENTION_STORAGE_KEY = 'opensquilla-session-task-attention-v1'

const MAX_HANDLED_TASKS = 256

function browserStorage(): StorageLike | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null
}

function emptyStoredAttentionState(): StoredAttentionState {
  return {
    version: 1,
    unread: {},
    read: {},
  }
}

function readStoredAttentionState(storage: StorageLike | null): StoredAttentionState {
  if (!storage) return emptyStoredAttentionState()
  try {
    const raw = storage.getItem(SESSION_TASK_ATTENTION_STORAGE_KEY)
    if (!raw) return emptyStoredAttentionState()
    const parsed = objectValue(JSON.parse(raw))
    if (parsed?.version !== 1) return emptyStoredAttentionState()

    const unread: Record<string, StoredAttention> = {}
    for (const [sessionKey, rawEntry] of Object.entries(objectValue(parsed.unread) || {})) {
      const entry = objectValue(rawEntry)
      const taskId = textValue(entry?.taskId)
      const state = entry?.state
      if (
        sessionKey
        && taskId
        && (state === 'completed' || state === 'failed')
      ) {
        unread[sessionKey] = { taskId, state }
      }
    }

    const read: Record<string, string> = {}
    for (const [sessionKey, rawTaskId] of Object.entries(objectValue(parsed.read) || {})) {
      const taskId = textValue(rawTaskId)
      if (sessionKey && taskId) read[sessionKey] = taskId
    }
    return {
      version: 1,
      unread,
      read,
    }
  } catch {
    return emptyStoredAttentionState()
  }
}

function sessionKeyFrom(payload: Record<string, unknown>): string {
  return (
    textValue(payload.key)
    || textValue(payload.session_key)
    || textValue(payload.sessionKey)
  )
}

function taskFrom(payload: Record<string, unknown>): Record<string, unknown> | null {
  return objectValue(payload.last_task) || objectValue(payload.lastTask)
}

function taskIdFrom(
  payload: Record<string, unknown>,
  task: Record<string, unknown> | null,
): string {
  return (
    textValue(task?.task_id)
    || textValue(task?.taskId)
    || textValue(payload.task_id)
    || textValue(payload.taskId)
  )
}

function terminalAttention(status: string): UnreadSessionTaskAttention | null {
  const normalized = status.toLowerCase()
  if (['succeeded', 'success', 'complete', 'completed'].includes(normalized)) {
    return 'completed'
  }
  if (['failed', 'timeout', 'abandoned', 'interrupted'].includes(normalized)) {
    return 'failed'
  }
  return null
}

export function createSessionTaskAttentionStore(
  storage: StorageLike | null = browserStorage(),
) {
  const storedState = readStoredAttentionState(storage)
  const unread = ref<Record<string, StoredAttention>>(storedState.unread)
  const read = ref<Record<string, string>>(storedState.read)
  const handledTasks = new Set<string>()
  const handledTaskOrder: string[] = []

  function handledTaskKey(sessionKey: string, taskId: string): string {
    return `${sessionKey}\u0000${taskId}`
  }

  function rememberHandledTask(sessionKey: string, taskId: string) {
    const key = handledTaskKey(sessionKey, taskId)
    if (handledTasks.has(key)) return
    handledTasks.add(key)
    handledTaskOrder.push(key)
    while (handledTaskOrder.length > MAX_HANDLED_TASKS) {
      const oldest = handledTaskOrder.shift()
      if (oldest) handledTasks.delete(oldest)
    }
  }

  for (const [sessionKey, entry] of Object.entries(unread.value)) {
    rememberHandledTask(sessionKey, entry.taskId)
  }
  for (const [sessionKey, taskId] of Object.entries(read.value)) {
    rememberHandledTask(sessionKey, taskId)
  }

  function persist() {
    if (!storage) return
    const state: StoredAttentionState = {
      version: 1,
      unread: unread.value,
      read: read.value,
    }
    try {
      storage.setItem(SESSION_TASK_ATTENTION_STORAGE_KEY, JSON.stringify(state))
    } catch {
      // Storage can be unavailable or full in restricted browser contexts.
    }
  }

  function markTaskRead(sessionKey: string, taskId: string) {
    if (!sessionKey || !taskId) return
    const { [sessionKey]: _removed, ...rest } = unread.value
    unread.value = rest
    read.value = {
      ...read.value,
      [sessionKey]: taskId,
    }
    rememberHandledTask(sessionKey, taskId)
    persist()
  }

  function markRead(sessionKey: string) {
    const taskId = unread.value[sessionKey]?.taskId
    if (taskId) markTaskRead(sessionKey, taskId)
  }

  function removeMany(sessionKeys: Iterable<string>) {
    const nextUnread = { ...unread.value }
    const nextRead = { ...read.value }
    let changed = false
    for (const sessionKey of sessionKeys) {
      if (!sessionKey) continue
      if (nextUnread[sessionKey]) {
        delete nextUnread[sessionKey]
        changed = true
      }
      if (nextRead[sessionKey]) {
        delete nextRead[sessionKey]
        changed = true
      }
    }
    if (!changed) return
    unread.value = nextUnread
    read.value = nextRead
    persist()
  }

  function attentionFor(sessionKey: string, runStatus: string): SessionTaskAttention {
    const normalized = runStatus.toLowerCase()
    if (normalized === 'queued' || normalized === 'running') return 'running'
    return unread.value[sessionKey]?.state || 'none'
  }

  function handleSessionsChanged(
    rawPayload: unknown,
    context: SessionTaskAttentionContext,
  ) {
    const payload = objectValue(rawPayload)
    if (!payload) return
    const sessionKey = sessionKeyFrom(payload)
    const reason = textValue(payload.reason)
    if (!sessionKey) return

    if (reason === 'task_queued' || reason === 'task_running') {
      markRead(sessionKey)
      return
    }
    if (reason !== 'task_terminal') return

    const task = taskFrom(payload)
    const taskId = taskIdFrom(payload, task)
    if (!taskId) return
    const handledKey = handledTaskKey(sessionKey, taskId)
    if (read.value[sessionKey] === taskId || handledTasks.has(handledKey)) return
    rememberHandledTask(sessionKey, taskId)

    if (
      sessionKey === context.currentSessionKey
      && context.currentSessionVisible
    ) {
      markTaskRead(sessionKey, taskId)
      return
    }

    const state = terminalAttention(
      textValue(task?.status) || textValue(payload.status),
    )
    if (!state) {
      markRead(sessionKey)
      return
    }
    unread.value = {
      ...unread.value,
      [sessionKey]: { taskId, state },
    }
    persist()
  }

  return {
    unread,
    attentionFor,
    handleSessionsChanged,
    markRead,
    removeMany,
  }
}

let singleton: ReturnType<typeof createSessionTaskAttentionStore> | null = null

export function useSessionTaskAttention() {
  if (!singleton) singleton = createSessionTaskAttentionStore()
  return singleton
}
