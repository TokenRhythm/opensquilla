import { computed, ref, watch, type Ref } from 'vue'
import type { ConversationEventData } from '@/modules/conversationEventContent'
import type { SessionReadMetadata } from '@/modules/sessionReadLifecycle'
import type { TaskProgressSnapshot } from '@/types/taskProgress'
import { normalizeTaskProgress } from '@/utils/chat/taskProgress'

/** Progress belongs to a task and is descriptive; it never drives scheduling. */
export function useChatTaskProgress(options: {
  sessionKey: Ref<string>
  currentEpoch: Ref<number>
  activeTaskId: Readonly<Ref<string>>
}) {
  const progressByTask = ref<Record<string, TaskProgressSnapshot>>({})
  const lastTaskId = ref('')
  const settled = new Set<string>()
  const taskId = computed(() => options.activeTaskId.value || lastTaskId.value)
  const progress = computed(() => progressByTask.value[taskId.value] ?? null)

  function reset() {
    progressByTask.value = {}
    lastTaskId.value = ''
    settled.clear()
  }
  watch([options.sessionKey, options.currentEpoch], reset, { flush: 'sync' })
  watch(options.activeTaskId, value => {
    if (value) lastTaskId.value = value
  }, { flush: 'sync', immediate: true })

  function apply(task: string, value: unknown) {
    const incoming = normalizeTaskProgress(value)
    if (!task || !incoming) return
    const previous = progressByTask.value[task]
    if (previous && previous.revision >= incoming.revision) return
    progressByTask.value = { ...progressByTask.value, [task]: incoming }
  }

  function applySnapshot(snapshot: SessionReadMetadata) {
    if (snapshot.sessionKey !== options.sessionKey.value
      || snapshot.epoch !== options.currentEpoch.value) return
    for (const task of [...snapshot.tasks, snapshot.activeTask, snapshot.lastTask]) {
      if (!task) continue
      const id = String(task.task_id || task.taskId || '')
      apply(id, task.progress)
    }
    if (!lastTaskId.value) {
      const task = snapshot.activeTask ?? snapshot.lastTask
      lastTaskId.value = String(task?.task_id || task?.taskId || '')
    }
  }

  function applyEvent(payload: ConversationEventData) {
    if (payload.key !== options.sessionKey.value || payload.epoch !== options.currentEpoch.value
      || !payload.task_id || settled.has(payload.task_id)) return
    apply(payload.task_id, payload.progress)
    if (!lastTaskId.value && payload.task_id === options.activeTaskId.value) {
      lastTaskId.value = payload.task_id
    }
  }

  function noteTaskSettled(id: string, epoch?: number) {
    if (!id || (epoch !== undefined && epoch !== options.currentEpoch.value)) return
    settled.add(id)
    if (settled.size > 256) settled.delete(settled.values().next().value!)
  }

  return { progress, taskId, applySnapshot, applyEvent, noteTaskSettled }
}
