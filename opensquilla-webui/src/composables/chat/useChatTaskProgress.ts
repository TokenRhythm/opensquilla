import { computed, ref, watch, type Ref } from 'vue'
import type { ConversationEventData } from '@/modules/conversationEventContent'
import type { SessionReadMetadata } from '@/modules/sessionReadLifecycle'
import type { TaskProgressSnapshot } from '@/types/taskProgress'
import { normalizeTaskProgress } from '@/utils/chat/taskProgress'

const TERMINAL_STATUSES = new Set([
  'succeeded', 'failed', 'cancelled', 'timeout', 'abandoned', 'interrupted',
])

/** Progress belongs to a task and is descriptive; it never drives scheduling. */
export function useChatTaskProgress(options: {
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  activeTaskId: Readonly<Ref<string>>
}) {
  const progressByTask = ref<Record<string, TaskProgressSnapshot>>({})
  const settled = ref(new Set<string>())
  const taskId = computed(() => (
    settled.value.has(options.activeTaskId.value) ? '' : options.activeTaskId.value
  ))
  const progress = computed(() => progressByTask.value[taskId.value] ?? null)

  function reset() {
    progressByTask.value = {}
    settled.value.clear()
  }
  watch([options.sessionKey, options.currentEpoch], reset, { flush: 'sync' })

  function apply(task: string, value: unknown) {
    const incoming = normalizeTaskProgress(value)
    if (!task || !incoming || settled.value.has(task)) return
    const previous = progressByTask.value[task]
    if (previous && previous.revision >= incoming.revision) return
    progressByTask.value = { ...progressByTask.value, [task]: incoming }
  }

  function applySnapshot(snapshot: SessionReadMetadata) {
    if (snapshot.sessionKey !== options.sessionKey.value
      || snapshot.epoch !== options.currentEpoch.value) return
    // Terminal evidence wins even when an older active copy appears in the
    // same hydration response. Historical progress must not revive the dock.
    for (const task of [...snapshot.tasks, snapshot.activeTask, snapshot.lastTask]) {
      if (!task) continue
      const id = String(task.task_id || task.taskId || '')
      if (TERMINAL_STATUSES.has(String(task.status || '').trim().toLowerCase())) {
        noteTaskSettled(id)
      }
    }
    for (const task of [...snapshot.tasks, snapshot.activeTask]) {
      if (!task) continue
      const id = String(task.task_id || task.taskId || '')
      apply(id, task.progress)
    }
  }

  function applyEvent(payload: ConversationEventData) {
    if (payload.key !== options.sessionKey.value || payload.epoch !== options.currentEpoch.value
      || !payload.task_id) return
    apply(payload.task_id, payload.progress)
  }

  function noteTaskSettled(id: string, epoch?: number) {
    if (!id || (epoch !== undefined && epoch !== options.currentEpoch.value)) return
    settled.value.add(id)
    if (settled.value.size > 256) settled.value.delete(settled.value.values().next().value!)
    delete progressByTask.value[id]
  }

  return { progress, taskId, applySnapshot, applyEvent, noteTaskSettled }
}
