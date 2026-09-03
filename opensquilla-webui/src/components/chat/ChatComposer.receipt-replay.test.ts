// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: '',
  'onUpdate:modelValue': () => {},
  attachments: [],
  busySendMode: 'queue',
  hasSendContent: false,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Retry',
  runMode: 'safe',
  allowedRunModes: ['safe', 'full'],
  runModeLocked: false,
  runModeLockMessage: '',
  sessionRoutingMode: 'off',
  sessionRoutingBusy: false,
  sessionRoutingAvailable: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

async function mount(overrides: Record<string, unknown> = {}) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, { ...BASE_PROPS, ...overrides })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer exact receipt replay admission', () => {
  it('keeps only Retry enabled while fresh composer mutation is policy-blocked', async () => {
    const onSend = vi.fn()
    const onFileChange = vi.fn()
    const { app, el } = await mount({
      freshInputDisabled: true,
      goalDraftArmed: true,
      planModeAvailable: true,
      collaborationMode: 'plan',
      onSend,
      onFileChange,
    })

    expect(el.querySelector<HTMLTextAreaElement>('.chat-textarea')?.disabled).toBe(true)
    expect(el.querySelector<HTMLButtonElement>('.chat-plus-btn')?.disabled).toBe(true)
    expect(el.querySelector<HTMLInputElement>('input[type="file"]')?.disabled).toBe(true)
    const routing = el.querySelector<HTMLButtonElement>('.chat-model-routing-btn')
    expect(routing?.disabled).toBe(true)
    expect(routing?.getAttribute('aria-disabled')).toBe('true')
    expect(el.querySelector<HTMLButtonElement>('.chat-run-mode-btn')?.disabled).toBe(true)
    expect(el.querySelector<HTMLButtonElement>('.chat-more-actions-btn')?.disabled).toBe(true)
    expect(el.querySelector<HTMLButtonElement>('.composer-goal-mode__toggle')?.disabled).toBe(true)
    expect(el.querySelector<HTMLButtonElement>('.composer-plan-mode__toggle')?.disabled).toBe(true)

    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')
    fileInput?.dispatchEvent(new Event('change'))
    expect(onFileChange).not.toHaveBeenCalled()

    const retry = el.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(retry?.disabled).toBe(false)
    retry?.click()
    expect(onSend).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('keeps normal interactive composer input and file selection available', async () => {
    const onSend = vi.fn()
    const onFileChange = vi.fn()
    const { app, el } = await mount({ onSend, onFileChange })

    expect(el.querySelector<HTMLTextAreaElement>('.chat-textarea')?.disabled).toBe(false)
    expect(el.querySelector<HTMLButtonElement>('.chat-plus-btn')?.disabled).toBe(false)
    const fileInput = el.querySelector<HTMLInputElement>('input[type="file"]')
    expect(fileInput?.disabled).toBe(false)
    fileInput?.dispatchEvent(new Event('change'))
    expect(onFileChange).toHaveBeenCalledOnce()

    el.querySelector<HTMLButtonElement>('.chat-send-btn')?.click()
    expect(onSend).toHaveBeenCalledOnce()
    app.unmount()
  })
})
