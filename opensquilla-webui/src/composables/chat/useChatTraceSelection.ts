import { computed, ref, watch, type Ref } from 'vue'
import type { ChatMessage, ChatRunStatus } from '@/types/chat'

export function useChatTraceSelection(options: {
  sessionKey: Ref<string>
  messages: Ref<ChatMessage[]>
  runStatus: Ref<ChatRunStatus>
  isStreaming: Ref<boolean>
}) {
  const view = ref<'conversation' | 'trace'>('conversation')
  const selectedTurn = ref('')
  const running = computed(() => options.isStreaming.value
    || ['queued', 'running', 'approval_pending'].includes(options.runStatus.value.status))
  const activeTurn = computed(() => {
    if (!running.value) return ''
    const task = options.runStatus.value.task
    return String(task?.turn_id || task?.turnId
      || task?.steer_capability?.expected_turn_id
      || task?.steerCapability?.expected_turn_id
      || task?.task_id || task?.taskId || '').trim()
  })
  const latestUser = computed(() => {
    const messages = options.messages.value
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'user') return messages[index]
    }
    return undefined
  })
  const turns = computed(() => {
    const entries = new Map<string, { id: string; preview: string }>()
    for (const message of options.messages.value) {
      const id = message.turnId?.trim()
      if (!id) continue
      if (!entries.has(id)) entries.set(id, { id, preview: '' })
      const entry = entries.get(id)!
      if (!entry.preview && message.role === 'user') {
        entry.preview = message.text.replace(/\s+/g, ' ').trim().slice(0, 70)
      }
    }
    if (activeTurn.value && !entries.has(activeTurn.value)) {
      entries.set(activeTurn.value, { id: activeTurn.value, preview: '' })
    }
    return [...entries.values()]
  })
  const latestTurn = computed(() => turns.value[turns.value.length - 1]?.id || '')
  const turnId = computed(() => {
    if (selectedTurn.value) return selectedTurn.value
    if (activeTurn.value) return activeTurn.value
    if (running.value) {
      // Streaming appends assistant rows before the next task-status update.
      // Keep the accepted user turn as the anchor throughout those updates.
      // A new optimistic user row without a turn id intentionally yields none.
      return latestUser.value?.turnId?.trim() || ''
    }
    return latestTurn.value
  })
  const selectedRunning = computed(() => !!turnId.value && running.value
    && turnId.value === (activeTurn.value || latestUser.value?.turnId?.trim()))

  watch(options.sessionKey, () => {
    view.value = 'conversation'
    selectedTurn.value = ''
  }, { flush: 'sync' })

  return { view, selectedTurn, turns, turnId, selectedRunning }
}
