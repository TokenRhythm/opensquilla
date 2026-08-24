import { nextTick, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import type { ChatRenderedMessage } from '@/types/chat'
import { useDeliverableUpdateIndicator } from './useDeliverableUpdateIndicator'

function message(
  id: string,
  status: 'applied' | 'not_applied',
  restoredFromHistory = false,
): ChatRenderedMessage {
  return {
    id,
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: '',
    timeStr: '',
    showHeader: true,
    restoredFromHistory,
    turnOutcome: {
      turnId: id,
      status: 'completed',
      documentMutationOutcome: { version: 1, status, resultRevisionId: id },
    },
  }
}

describe('useDeliverableUpdateIndicator', () => {
  it('marks only newly applied live mutations and clears on acknowledgement', async () => {
    const sessionKey = ref('session-a')
    const messages = ref<ChatRenderedMessage[]>([message('old', 'applied', true)])
    const isStreaming = ref(false)
    const indicator = useDeliverableUpdateIndicator({ sessionKey, messages, isStreaming })

    expect(indicator.hasNewDeliverable.value).toBe(false)

    messages.value = [...messages.value, message('failed', 'not_applied')]
    await nextTick()
    expect(indicator.hasNewDeliverable.value).toBe(false)

    messages.value = [...messages.value, message('new', 'applied')]
    await nextTick()
    expect(indicator.hasNewDeliverable.value).toBe(true)

    indicator.acknowledge()
    expect(indicator.hasNewDeliverable.value).toBe(false)
  })

  it('resets the marker and baselines existing updates when the session changes', async () => {
    const sessionKey = ref('session-a')
    const messages = ref<ChatRenderedMessage[]>([])
    const isStreaming = ref(false)
    const indicator = useDeliverableUpdateIndicator({ sessionKey, messages, isStreaming })

    messages.value = [message('new-a', 'applied')]
    await nextTick()
    expect(indicator.hasNewDeliverable.value).toBe(true)

    sessionKey.value = 'session-b'
    messages.value = [message('existing-b', 'applied')]
    await nextTick()
    expect(indicator.hasNewDeliverable.value).toBe(false)
  })

  it('recognizes a live turn outcome even when completion is restored through history sync', async () => {
    const sessionKey = ref('session-a')
    const messages = ref<ChatRenderedMessage[]>([message('old', 'applied', true)])
    const isStreaming = ref(false)
    const indicator = useDeliverableUpdateIndicator({ sessionKey, messages, isStreaming })

    isStreaming.value = true
    messages.value = [
      ...messages.value,
      {
        id: 'live-user',
        role: 'user',
        displayRole: 'user',
        roleLabel: 'You',
        text: 'edit',
        timeStr: '',
        showHeader: true,
        turnKey: 'turn:live',
      },
    ]
    await nextTick()

    isStreaming.value = false
    messages.value = [
      ...messages.value,
      { ...message('live-result', 'applied', true), turnKey: 'turn:live' },
      { ...message('unrelated-history', 'applied', true), turnKey: 'turn:old' },
    ]
    await nextTick()

    expect(indicator.hasNewDeliverable.value).toBe(true)
  })
})
