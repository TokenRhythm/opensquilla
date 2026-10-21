import { computed, watch, type Ref } from 'vue'
import type { ChatMessage, ChatRenderedMessage } from '@/types/chat'
import {
  assistantActivityContinuityKey,
  migrateAssistantActivityExpansion,
} from '@/utils/chat/activityDisclosureState'
import {
  FINISHED_STREAM_TASK_ID,
  PENDING_STREAM_TASK_ID,
  STOPPED_STREAM_TASK_ID,
} from '@/utils/chat/streamEvents'

interface ToolDisclosureScopeOptions {
  sessionKey: Readonly<Ref<string>>
  activeTaskId: Readonly<Ref<string>>
  isStreaming: Readonly<Ref<boolean>>
  messages: Readonly<Ref<ChatMessage[]>>
  renderedMessages: Readonly<Ref<ChatRenderedMessage[]>>
  migrateScope: (previous: string, next: string) => void
}

/** Live and settled disclosures share the accepted task's causal turn identity. */
export function useChatToolDisclosureScope(options: ToolDisclosureScopeOptions) {
  const userAnchor = computed(() => {
    for (let index = options.messages.value.length - 1; index >= 0; index--) {
      const message = options.messages.value[index]
      if (message?.role === 'user') {
        return message.clientId || message.messageId || String(message.ts || '')
      }
    }
    return ''
  })
  const turnKey = computed(() => {
    const taskId = options.activeTaskId.value.trim()
    if (taskId && ![
      PENDING_STREAM_TASK_ID, FINISHED_STREAM_TASK_ID, STOPPED_STREAM_TASK_ID,
    ].includes(taskId)) return `turn:${taskId}`

    // A hidden, empty user message still starts a turn. Looking only at the
    // rendered rows would accidentally reuse the preceding visible turn.
    for (let index = options.messages.value.length - 1; index >= 0; index--) {
      const message = options.messages.value[index]!
      const turnId = String(message.turnId || '').trim()
      if (turnId) return `turn:${turnId}`
      if (message.role === 'user' && userAnchor.value) return `turn:${userAnchor.value}`
    }
    const rows = options.renderedMessages.value
    return rows[rows.length - 1]?.turnKey || 'stream'
  })
  const stateScope = computed(() => JSON.stringify([options.sessionKey.value || '', turnKey.value]))
  const activityContinuityKey = computed(() => assistantActivityContinuityKey(
    options.sessionKey.value || '', turnKey.value,
  ))

  let optimistic: { session: string; anchor: string; scope: string; activityKey: string } | null = null
  watch(
    () => [
      options.sessionKey.value || '', userAnchor.value, stateScope.value,
      options.activeTaskId.value, options.isStreaming.value,
      options.messages.value[options.messages.value.length - 1]?.role === 'user',
    ] as const,
    ([session, anchor, scope, taskId, streaming, userTail]) => {
      if (optimistic && (session !== optimistic.session || anchor !== optimistic.anchor)) optimistic = null
      if (optimistic && scope !== optimistic.scope) {
        options.migrateScope(optimistic.scope, scope)
        migrateAssistantActivityExpansion(optimistic.activityKey, activityContinuityKey.value)
        optimistic = null
      }
      // Remember only a live optimistic user row. Keep it through the short
      // pending -> done handoff, where the committed assistant gets its id
      // immediately after endStreaming. Completed legacy rows cannot alias.
      if (streaming && userTail && anchor && ['', PENDING_STREAM_TASK_ID].includes(taskId)
        && scope === JSON.stringify([session, `turn:${anchor}`])) {
        optimistic = { session, anchor, scope, activityKey: activityContinuityKey.value }
      }
      if ((!streaming && taskId !== PENDING_STREAM_TASK_ID)
        || [FINISHED_STREAM_TASK_ID, STOPPED_STREAM_TASK_ID].includes(taskId)) optimistic = null
    },
    { flush: 'sync', immediate: true },
  )
  return { stateScope, activityContinuityKey }
}
