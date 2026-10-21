import { readonly, ref, watch, type Ref } from 'vue'
import type { ChatRunStatus } from '@/types/chat'
import { chatTaskId } from './useChatTaskOwnership'

/** Announce only a live turn's confirmed outcome, never a history/navigation reset. */
export function useChatTurnAnnouncement(options: {
  sessionKey: Readonly<Ref<string>>
  isStreaming: Readonly<Ref<boolean>>
  runStatus: Readonly<Ref<ChatRunStatus>>
  t: (key: string) => string
}) {
  const announcement = ref('')
  let ownerSession = options.sessionKey.value
  let ownerTask = ''
  let wasLive = false

  watch([options.sessionKey, options.isStreaming, options.runStatus], ([key, streaming, run]) => {
    if (key !== ownerSession) {
      ownerSession = key
      ownerTask = streaming ? chatTaskId(run.task) : ''
      wasLive = streaming
      announcement.value = ''
      return
    }
    if (streaming) {
      wasLive = true
      ownerTask = chatTaskId(run.task) || ownerTask
      announcement.value = ''
      return
    }
    if (!wasLive) return
    const terminalTask = chatTaskId(run.task)
    if (ownerTask && terminalTask && ownerTask !== terminalTask) return

    // A stopped render alone does not prove success. Reconnects, navigation,
    // and pending ownership can all retire the live bubble without a receipt.
    const status = run.status === 'idle' ? run.task?.status : run.status
    const lifecycle = ['succeeded', 'completed'].includes(status || '') ? 'settled'
      : ['cancelled', 'interrupted', 'abandoned'].includes(status || '') ? 'interrupted'
        : ['failed', 'timeout', 'timed_out'].includes(status || '') ? 'failed' : ''
    if (lifecycle) {
      wasLive = false
      ownerTask = ''
      announcement.value = options.t(`chat.activity.lifecycle.${lifecycle}`)
    }
  }, { flush: 'post' })

  return readonly(announcement)
}
