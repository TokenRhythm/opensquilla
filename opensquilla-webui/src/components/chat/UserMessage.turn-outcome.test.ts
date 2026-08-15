// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('UserMessage turn outcome', () => {
  it('shows restart guidance when an interrupted turn has no assistant message', async () => {
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'user-restart',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: 'Continue the task',
      timeStr: '',
      showHeader: false,
      turnOutcome: {
        turnId: 'turn-restart',
        status: 'abandoned',
        kind: 'interrupted',
        reason: 'process_restart',
      },
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (value: string) => value,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
      showTurnOutcome: true,
    })
    app.use(i18n)
    app.mount(host)
    await nextTick()

    expect(host.querySelector('.turn-outcome--process-restart')?.textContent)
      .toContain("This task won't continue automatically")
    app.unmount()
  })
})
