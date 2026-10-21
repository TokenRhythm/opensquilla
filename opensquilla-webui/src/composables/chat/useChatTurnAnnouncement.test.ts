import { afterEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, ref, type EffectScope } from 'vue'
import type { ChatRunStatus } from '@/types/chat'
import { useChatTurnAnnouncement } from './useChatTurnAnnouncement'
import chatViewSource from '@/views/ChatView.vue?raw'

const scopes: EffectScope[] = []
afterEach(() => { scopes.splice(0).forEach(scope => scope.stop()) })
function setup() {
  const scope = effectScope()
  scopes.push(scope)
  return scope.run(() => {
    const sessionKey = ref('session-a')
    const isStreaming = ref(false)
    const runStatus = ref<ChatRunStatus>({ status: 'idle', label: '', task: null })
    const announcement = useChatTurnAnnouncement({ sessionKey, isStreaming, runStatus, t: key => key })
    return { sessionKey, isStreaming, runStatus, announcement,
      async start(taskId = 'turn-a') {
        runStatus.value = { status: 'running', label: '', task: { task_id: taskId, status: 'running' } }
        isStreaming.value = true
        await nextTick()
      },
      async settle(status: ChatRunStatus['status'], taskStatus?: string, taskId?: string) {
        // Event handlers retire the live bubble before applying the receipt in
        // the same turn. The watcher must read the final authoritative state.
        isStreaming.value = false
        runStatus.value = { status, label: '', task: taskStatus ? { status: taskStatus, task_id: taskId } : null }
        await nextTick()
      },
    }
  })!
}

describe('confirmed live turn announcements', () => {
  it.each([
    ['idle', 'succeeded', 'settled'], ['idle', 'completed', 'settled'],
    ['failed', 'failed', 'failed'], ['timeout', 'timeout', 'failed'],
    ['cancelled', 'cancelled', 'interrupted'], ['interrupted', 'abandoned', 'interrupted'],
  ] as const)('announces %s/%s as %s', async (status, taskStatus, label) => {
    const api = setup()
    await api.start()
    await api.settle(status, taskStatus, 'turn-a')
    expect(api.announcement.value).toBe(`chat.activity.lifecycle.${label}`)
  })

  it.each(['idle', 'running', 'queued', 'approval_pending'] as const)('does not infer completion from %s', async status => {
    const api = setup()
    await api.start()
    await api.settle(status)
    expect(api.announcement.value).toBe('')
  })

  it('does not announce history loading, switching chat, or another task', async () => {
    const api = setup()
    await api.settle('idle', 'succeeded', 'old-turn')
    expect(api.announcement.value).toBe('')
    await api.start()
    api.sessionKey.value = 'session-b'
    await api.settle('idle', 'succeeded', 'turn-a')
    expect(api.announcement.value).toBe('')
    await api.start('turn-b')
    await api.settle('failed', 'failed', 'turn-a')
    expect(api.announcement.value).toBe('')
  })

  it('clears between turns so the same outcome is announced again', async () => {
    const api = setup()
    for (const id of ['a', 'b']) {
      await api.start(id)
      expect(api.announcement.value).toBe('')
      await api.settle('idle', 'succeeded')
      expect(api.announcement.value).toBe('chat.activity.lifecycle.settled')
    }
    api.sessionKey.value = 'session-c'
    await nextTick()
    expect(api.announcement.value).toBe('')
  })

  it.each([
    ['idle', 'succeeded', 'settled'], ['failed', 'failed', 'failed'],
    ['cancelled', 'cancelled', 'interrupted'],
  ] as const)('waits for a delayed %s receipt after the renderer stops', async (status, taskStatus, label) => {
    const api = setup()
    await api.start()
    api.isStreaming.value = false
    await nextTick()
    expect(api.announcement.value).toBe('')
    await api.settle(status, taskStatus, 'turn-a')
    expect(api.announcement.value).toBe(`chat.activity.lifecycle.${label}`)
    api.sessionKey.value = 'session-b'
    await nextTick()
    await api.settle(status, taskStatus, 'turn-a')
    expect(api.announcement.value).toBe('')
  })

  it('observes a running chat opened from another session without announcing navigation', async () => {
    const api = setup()
    api.sessionKey.value = 'session-b'
    await api.start('turn-b')
    expect(api.announcement.value).toBe('')
    await api.settle('cancelled', 'cancelled', 'turn-b')
    expect(api.announcement.value).toBe('chat.activity.lifecycle.interrupted')
  })

  it('is wired to the existing hidden polite region without another notification', () => {
    expect(chatViewSource).toContain('useChatTurnAnnouncement({ sessionKey, isStreaming, runStatus, t })')
    expect(chatViewSource).toContain('class="chat-turn-settled-announcer" role="status" aria-live="polite"')
    expect(chatViewSource).not.toContain("turnSettledAnnouncement.value = String(t('chat.activity.lifecycle.settled'))")
  })
})
