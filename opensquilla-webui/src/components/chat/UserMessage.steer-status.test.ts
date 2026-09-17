// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage, ChatSteerDisposition } from '@/types/chat'
import UserMessage from './UserMessage.vue'

async function renderDisposition(inputDisposition: ChatSteerDisposition) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const message: ChatRenderedMessage = {
    id: `steer-${inputDisposition}`,
    role: 'user',
    displayRole: 'user',
    roleLabel: 'You',
    text: 'adjust the active task',
    timeStr: '',
    showHeader: false,
    inputDisposition,
  }
  const app = createApp(UserMessage, {
    message,
    shareMode: false,
    shareSelected: false,
    shareMessageId: message.id,
    stripTimePrefix: (value: string) => value,
    copyMessage: async () => true,
    downloadAttachment: async () => true,
  })
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { app, host, message }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('UserMessage steer status', () => {
  it.each([
    ['applied', 'Steer'],
    ['promoted', 'Queued for the next turn'],
    ['cancelled', 'Not applied'],
    ['rejected', 'Not applied'],
  ] as const)('renders %s as a small lifecycle label', async (disposition, label) => {
    const { app, host } = await renderDisposition(disposition)

    const status = host.querySelector('.msg-user-steer-status')
    expect(host.querySelector('.msg-user')?.classList.contains('msg-user--steer')).toBe(true)
    expect(status?.textContent).toContain(label)
    expect(status?.classList.contains(`msg-user-steer-status--${disposition}`)).toBe(true)
    app.unmount()
  })

  it('keeps the stable Steer identity and delays the waiting detail', async () => {
    const { app, host } = await renderDisposition('steering')

    const status = host.querySelector('.msg-user-steer-status')
    expect(status?.textContent).toBe('Steer')

    await vi.advanceTimersByTimeAsync(699)
    expect(status?.textContent).toBe('Steer')

    await vi.advanceTimersByTimeAsync(1)
    expect(status?.textContent).toBe('Steer · Waiting to apply')
    expect(status?.classList.contains('msg-user-steer-status--steering')).toBe(true)
    app.unmount()
  })

  it('does not reveal waiting detail after the steer is applied', async () => {
    const { app, host, message } = await renderDisposition('steering')

    message.inputDisposition = 'applied'
    await nextTick()
    await vi.advanceTimersByTimeAsync(700)

    expect(host.querySelector('.msg-user-steer-status')?.textContent).toBe('Steer')
    app.unmount()
  })
})
