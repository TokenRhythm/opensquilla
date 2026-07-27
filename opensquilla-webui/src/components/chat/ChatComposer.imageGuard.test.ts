// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: 'describe this image',
  'onUpdate:modelValue': () => {},
  attachments: [],
  busySendMode: 'queue',
  hasSendContent: true,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Send',
  runMode: 'trusted',
  allowedRunModes: ['standard', 'trusted', 'full'],
  modelRoutingMode: 'llm_ensemble',
  modelRoutingSettingsBusy: false,
  routerVisualEffectsEnabled: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer image-send guard', () => {
  it('announces the block accessibly and prevents the send control from firing', async () => {
    const onSend = vi.fn()
    const message = 'Ensemble image input is unavailable.'
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sendBlockedMessage: message,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const status = el.querySelector<HTMLElement>('#chat-composer-image-send-status')
    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')

    expect(status?.textContent).toBe(message)
    expect(status?.getAttribute('role')).toBe('status')
    expect(status?.getAttribute('aria-live')).toBe('polite')
    expect(status?.getAttribute('aria-atomic')).toBe('true')
    expect(textarea?.getAttribute('aria-describedby')).toBe(status?.id)
    expect(send?.getAttribute('aria-describedby')).toBe(status?.id)
    expect(send?.title).toBe(message)
    expect(send?.disabled).toBe(true)
    send?.click()
    expect(onSend).not.toHaveBeenCalled()

    app.unmount()
  })

  it('keeps the send control enabled when no guard message is present', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, onSend })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(el.querySelector('#chat-composer-image-send-status')).toBeNull()
    expect(send?.disabled).toBe(false)
    send?.click()
    expect(onSend).toHaveBeenCalledOnce()

    app.unmount()
  })
})
