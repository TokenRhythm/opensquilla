// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('scheduled input source', () => {
  it.each(['en', 'zh-Hans'] as const)('labels automatic prompts in %s', async (locale) => {
    await loadLocaleMessages(locale)
    i18n.global.locale.value = locale
    const host = document.createElement('div')
    document.body.appendChild(host)
    const message: ChatRenderedMessage = {
      id: 'scheduled-input',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: 'Count the synthetic inventory.',
      timeStr: '',
      showHeader: false,
      provenanceKind: 'cron',
    }
    const app = createApp(UserMessage, {
      message,
      shareMode: false,
      shareSelected: false,
      shareMessageId: message.id,
      stripTimePrefix: (text: string) => text,
      copyMessage: async () => true,
      downloadAttachment: async () => true,
    })
    app.use(i18n)
    app.mount(host)
    try {
      await nextTick()
      expect(host.querySelector('[data-testid="cron-input-source"]')?.textContent)
        .toContain(locale === 'en' ? 'Scheduled trigger' : '定时触发')
      expect(host.textContent).toContain(message.text)
    } finally {
      app.unmount()
    }
  })
})
