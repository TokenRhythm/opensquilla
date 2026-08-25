import { computed, ref, type ComputedRef, type Ref } from 'vue'
import type { ChatRunStatusSource, ChatRunTask } from '@/types/chat'

const TERMINAL_STATUSES = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'timeout',
  'abandoned',
  'interrupted',
])

function normalizedStatus(value: unknown): string {
  return String(value || '').trim().toLowerCase()
}

export function chatTaskId(task: ChatRunTask | null | undefined): string {
  return String(
    task?.task_id
    || task?.taskId
    || task?.turn_id
    || task?.turnId
    || '',
  ).trim()
}

function taskStatus(task: ChatRunTask | null | undefined): string {
  return normalizedStatus(task?.status)
}

function taskCancelRequested(task: ChatRunTask | null | undefined): boolean {
  return task?.cancel_requested === true || task?.cancelRequested === true
}

function taskList(source: ChatRunStatusSource | null | undefined): ChatRunTask[] {
  if (!source || typeof source !== 'object') return []
  const value = (source as ChatRunStatusSource & { tasks?: unknown }).tasks
  return Array.isArray(value)
    ? value.filter((task): task is ChatRunTask => Boolean(task && typeof task === 'object'))
    : []
}

function queuedIdsFromSnapshot(
  source: ChatRunStatusSource | null | undefined,
  activeTask: ChatRunTask | null,
): string[] {
  const envelope = (source || {}) as ChatRunStatusSource & {
    queued_task_ids?: unknown
    queuedTaskIds?: unknown
    queued_tasks?: unknown
    queuedTasks?: unknown
  }
  const explicitIds = envelope.queued_task_ids ?? envelope.queuedTaskIds
  const explicitTasks = envelope.queued_tasks ?? envelope.queuedTasks
  const candidates: unknown[] = Array.isArray(explicitIds)
    ? explicitIds
    : Array.isArray(explicitTasks)
      ? explicitTasks
      : taskList(source).filter(task => taskStatus(task) === 'queued')
  const ids = candidates
    .map(candidate => (
      typeof candidate === 'string'
        ? candidate.trim()
        : chatTaskId(candidate as ChatRunTask)
    ))
    .filter(Boolean)
  const activeId = taskStatus(activeTask) === 'queued' ? chatTaskId(activeTask) : ''
  return Array.from(new Set(activeId ? [activeId, ...ids] : ids))
}

export interface ChatTaskOwnershipApi {
  runningTaskId: Ref<string>
  queuedTaskIds: Ref<ReadonlySet<string>>
  stopRequestedTaskId: Ref<string>
  hydrationResolved: Ref<boolean>
  hasAuthoritativeWork: ComputedRef<boolean>
  stopTargetTaskId: ComputedRef<string>
  beginHydration: () => void
  applySnapshot: (
    source: ChatRunStatusSource | null | undefined,
    hydrationComplete: boolean,
  ) => void
  noteAccepted: (taskId: string, status?: string) => {
    claimRender: boolean
    renderTaskId: string
  }
  noteQueued: (task: ChatRunTask | string) => {
    foreground: boolean
    renderTaskId: string
  }
  noteRunning: (task: ChatRunTask | string) => boolean
  noteTerminal: (taskId: string, authoritative?: boolean) => {
    wasRunning: boolean
    wasQueued: boolean
    wasStopTarget: boolean
  }
  isQueued: (taskId: string) => boolean
  isBackgroundQueued: (taskId: string) => boolean
  beginStop: () => string
  requestStop: (taskId: string) => string
  clearStop: (taskId?: string) => void
  reset: (resolved?: boolean) => void
}

/**
 * Session-local control ownership. Rendering deliberately remains outside this
 * controller: one accepted queued task must never steal a running task's live
 * bubble merely because its acknowledgement arrived later.
 */
export function useChatTaskOwnership(initiallyResolved = true): ChatTaskOwnershipApi {
  const runningTaskId = ref('')
  const queuedTaskIds = ref<ReadonlySet<string>>(new Set())
  const stopRequestedTaskId = ref('')
  const hydrationResolved = ref(initiallyResolved)
  const settledTaskIds = new Set<string>()

  function rememberSettled(taskId: string) {
    settledTaskIds.add(taskId)
    if (settledTaskIds.size <= 256) return
    const oldest = settledTaskIds.values().next().value
    if (oldest) settledTaskIds.delete(oldest)
  }

  const firstQueuedTaskId = computed(() => queuedTaskIds.value.values().next().value || '')
  const stopTargetTaskId = computed(() => runningTaskId.value || firstQueuedTaskId.value)
  const hasAuthoritativeWork = computed(() => (
    !hydrationResolved.value
    || Boolean(runningTaskId.value)
    || queuedTaskIds.value.size > 0
  ))

  function replaceQueued(mutator: (next: Set<string>) => void) {
    const next = new Set(queuedTaskIds.value)
    mutator(next)
    queuedTaskIds.value = next
  }

  function beginHydration() {
    hydrationResolved.value = false
  }

  function reset(resolved = false) {
    runningTaskId.value = ''
    queuedTaskIds.value = new Set()
    stopRequestedTaskId.value = ''
    hydrationResolved.value = resolved
    settledTaskIds.clear()
  }

  function noteQueued(task: ChatRunTask | string) {
    const taskId = typeof task === 'string' ? task.trim() : chatTaskId(task)
    if (!taskId || settledTaskIds.has(taskId) || runningTaskId.value === taskId) {
      return {
        foreground: runningTaskId.value === '',
        renderTaskId: runningTaskId.value || firstQueuedTaskId.value,
      }
    }
    replaceQueued(next => next.add(taskId))
    return {
      foreground: runningTaskId.value === '' && firstQueuedTaskId.value === taskId,
      renderTaskId: runningTaskId.value || firstQueuedTaskId.value,
    }
  }

  function noteRunning(task: ChatRunTask | string): boolean {
    const taskId = typeof task === 'string' ? task.trim() : chatTaskId(task)
    if (!taskId || settledTaskIds.has(taskId)) return false
    runningTaskId.value = taskId
    replaceQueued(next => next.delete(taskId))
    hydrationResolved.value = true
    // B can acquire the same-session execution lock before A's cancelled
    // terminal is broadcast. Keep the exact Stop target until A settles; the
    // render layer uses it to finish A before replaying B's buffered frames.
    return true
  }

  function noteTerminal(taskId: string, authoritative = true) {
    const normalizedTaskId = String(taskId || '').trim()
    const wasRunning = Boolean(normalizedTaskId && runningTaskId.value === normalizedTaskId)
    const wasQueued = Boolean(normalizedTaskId && queuedTaskIds.value.has(normalizedTaskId))
    const wasStopTarget = Boolean(
      normalizedTaskId && stopRequestedTaskId.value === normalizedTaskId,
    )
    if (normalizedTaskId) {
      rememberSettled(normalizedTaskId)
      if (wasRunning) runningTaskId.value = ''
      replaceQueued(next => next.delete(normalizedTaskId))
      if (wasStopTarget) stopRequestedTaskId.value = ''
    }
    if (authoritative) hydrationResolved.value = true
    return { wasRunning, wasQueued, wasStopTarget }
  }

  function noteAccepted(taskId: string, status = '') {
    const normalizedTaskId = String(taskId || '').trim()
    const normalizedTaskStatus = normalizedStatus(status)
    if (!normalizedTaskId) {
      return { claimRender: false, renderTaskId: runningTaskId.value }
    }
    if (TERMINAL_STATUSES.has(normalizedTaskStatus)) {
      noteTerminal(normalizedTaskId)
    } else if (normalizedTaskStatus === 'running' || normalizedTaskStatus === 'approval_pending') {
      noteRunning(normalizedTaskId)
    } else if (!settledTaskIds.has(normalizedTaskId)) {
      // Missing task_status is an older-Gateway acceptance snapshot. Treat it
      // as accepted-queued until task.running proves execution ownership.
      noteQueued(normalizedTaskId)
    }
    const renderTaskId = runningTaskId.value || firstQueuedTaskId.value
    return {
      claimRender: renderTaskId === normalizedTaskId,
      renderTaskId,
    }
  }

  function applySnapshot(
    source: ChatRunStatusSource | null | undefined,
    hydrationComplete: boolean,
  ) {
    if (!hydrationComplete) {
      hydrationResolved.value = false
      return
    }
    const envelope = source || {}
    const activeTask = envelope.active_task || envelope.activeTask || null
    const tasks = taskList(source)
    for (const task of tasks) {
      const taskId = chatTaskId(task)
      if (taskId && TERMINAL_STATUSES.has(taskStatus(task))) rememberSettled(taskId)
    }
    const snapshotLastTask = envelope.last_task || envelope.lastTask || null
    const snapshotLastTaskId = chatTaskId(snapshotLastTask)
    if (snapshotLastTaskId && TERMINAL_STATUSES.has(taskStatus(snapshotLastTask))) {
      rememberSettled(snapshotLastTaskId)
    }
    const activeStatus = taskStatus(activeTask)
    const runningTask = (
      activeStatus === 'running' || activeStatus === 'approval_pending'
        ? activeTask
        : tasks.find(task => ['running', 'approval_pending'].includes(taskStatus(task))) || null
    )
    const nextRunningTaskId = chatTaskId(runningTask)
    const nextQueuedTaskIds = queuedIdsFromSnapshot(source, activeTask)
      .filter(taskId => taskId !== nextRunningTaskId && !settledTaskIds.has(taskId))

    runningTaskId.value = nextRunningTaskId
    queuedTaskIds.value = new Set(nextQueuedTaskIds)
    hydrationResolved.value = true
    const snapshotStopTaskId = taskCancelRequested(activeTask)
      ? chatTaskId(activeTask)
      : ''
    if (snapshotStopTaskId) stopRequestedTaskId.value = snapshotStopTaskId
    const stoppedTask = stopRequestedTaskId.value
      ? tasks.find(task => chatTaskId(task) === stopRequestedTaskId.value)
      : null
    const lastTask = snapshotLastTask
    const stoppedTaskStatus = taskStatus(stoppedTask)
    const stoppedLastStatus = chatTaskId(lastTask) === stopRequestedTaskId.value
      ? taskStatus(lastTask)
      : ''
    if (
      stopRequestedTaskId.value
      && (
        TERMINAL_STATUSES.has(stoppedTaskStatus)
        || TERMINAL_STATUSES.has(stoppedLastStatus)
      )
    ) {
      noteTerminal(stopRequestedTaskId.value)
    }
  }

  function isQueued(taskId: string): boolean {
    return Boolean(taskId && queuedTaskIds.value.has(taskId))
  }

  function isBackgroundQueued(taskId: string): boolean {
    return Boolean(taskId && runningTaskId.value && runningTaskId.value !== taskId && isQueued(taskId))
  }

  function beginStop(): string {
    const target = stopTargetTaskId.value
    if (target) stopRequestedTaskId.value = target
    return target
  }

  function requestStop(taskId: string): string {
    const target = String(taskId || '').trim()
    if (target) stopRequestedTaskId.value = target
    return target
  }

  function clearStop(taskId = '') {
    if (!taskId || stopRequestedTaskId.value === taskId) {
      stopRequestedTaskId.value = ''
    }
  }

  return {
    runningTaskId,
    queuedTaskIds,
    stopRequestedTaskId,
    hydrationResolved,
    hasAuthoritativeWork,
    stopTargetTaskId,
    beginHydration,
    applySnapshot,
    noteAccepted,
    noteQueued,
    noteRunning,
    noteTerminal,
    isQueued,
    isBackgroundQueued,
    beginStop,
    requestStop,
    clearStop,
    reset,
  }
}
