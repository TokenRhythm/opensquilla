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
      // A new turn can start rendering before its accepted task id arrives.
      // Only an already-live turn may retain its previous identity.
      ownerTask = chatTaskId(run.task) || (wasLive ? ownerTask : '')
      wasLive = true
      announcement.value = ''
      return
    }
    const terminalTask = chatTaskId(run.task)
    // A partial-output done can precede the runtime's failure receipt. Keep
    // that live turn's identity after its first announcement so the exact
    // same turn can correct it, without announcing unrelated/history rows.
    if (!wasLive && (!ownerTask || terminalTask !== ownerTask)) return
    if (ownerTask && terminalTask && ownerTask !== terminalTask) return

    // A stopped render alone does not prove success. Reconnects, navigation,
    // and pending ownership can all retire the live bubble without a receipt.
    const status = run.status === 'idle' ? run.task?.status : run.status
    const lifecycle = ['succeeded', 'completed'].includes(status || '') ? 'settled'
      : ['cancelled', 'interrupted', 'abandoned'].includes(status || '') ? 'interrupted'
        : ['failed', 'timeout', 'timed_out'].includes(status || '') ? 'failed' : ''
    // A late done must not turn an already announced failure back into success.
    if (lifecycle && (wasLive || lifecycle !== 'settled')) {
      wasLive = false
      ownerTask ||= terminalTask
      announcement.value = options.t(`chat.activity.lifecycle.${lifecycle}`)
    }
  }, { flush: 'post' })

  return readonly(announcement)
}
