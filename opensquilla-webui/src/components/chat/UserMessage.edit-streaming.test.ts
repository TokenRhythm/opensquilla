// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage } from '@/types/chat'
import UserMessage from './UserMessage.vue'

const EDIT_LABEL = 'Edit'
const EDIT_STREAMING_LABEL = 'Wait for the current reply to finish before editing'

async function renderUserMessage(isStreaming: boolean) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const message: ChatRenderedMessage = {
    id: 'edit-1',
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: 'hello',
    timeStr: '',
    showHeader: false,
  }
  const app = createApp(UserMessage, {
    message,
    shareMode: false,
    shareSelected: false,
    shareMessageId: message.id,
    stripTimePrefix: (value: string) => value,
    copyMessage: async () => true,
    downloadAttachment: async () => true,
    isStreaming,
  })
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { app, host }
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('UserMessage edit action', () => {
  it('disables the edit button while the assistant is streaming', async () => {
    const { app, host } = await renderUserMessage(true)

    const editBtn = host.querySelector<HTMLButtonElement>(
      `button[aria-label="${EDIT_STREAMING_LABEL}"]`,
    )
    expect(editBtn).not.toBeNull()
    expect(editBtn?.disabled).toBe(true)
    expect(editBtn?.classList.contains('msg-action--disabled')).toBe(true)
    expect(editBtn?.title).toBe(EDIT_STREAMING_LABEL)
    app.unmount()
  })

  it('keeps the edit button enabled when idle', async () => {
    const { app, host } = await renderUserMessage(false)

    const editBtn = host.querySelector<HTMLButtonElement>(
      `button[aria-label="${EDIT_LABEL}"]`,
    )
    expect(editBtn).not.toBeNull()
    expect(editBtn?.disabled).toBe(false)
    expect(editBtn?.title).toBe(EDIT_LABEL)
    app.unmount()
  })
})
