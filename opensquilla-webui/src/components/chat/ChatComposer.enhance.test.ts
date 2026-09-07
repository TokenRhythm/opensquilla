// @vitest-environment happy-dom
import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

function composerProps(overrides: Record<string, unknown> = {}) {
  return {
    modelValue: '',
    'onUpdate:modelValue': (v: string) => { props.modelValue = v },
    attachments: [],
    promptAnnotations: [],
    busySendMode: 'queue',
    hasSendContent: false,
    isStreaming: false,
    canStop: false,
    isNewLanding: false,
    placeholder: 'Send a message',
    sendButtonTitle: 'Send',
    runMode: 'safe',
    allowedRunModes: ['safe', 'full'],
    runModeLocked: false,
    runModeLockMessage: '',
    modelRoutingMode: 'off',
    modelRoutingSettingsBusy: false,
    routerVisualEffectsEnabled: true,
    codingModeEnabled: false,
    codingModeSettingsBusy: false,
    voiceBusy: false,
    voiceRecording: false,
    voiceReady: true,
    floating: true,
    ...overrides,
  }
}

const props = { modelValue: '', 'onUpdate:modelValue': (_: string) => undefined }

function mountComposer(overrides: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp(ChatComposer, composerProps(overrides))
  app.use(i18n)
  app.mount(host)
  const btn = () => host.querySelector<HTMLButtonElement>('.chat-enhance-btn')
  const textarea = () => host.querySelector<HTMLTextAreaElement>('textarea.chat-textarea')
  return { app, btn, textarea }
}

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ChatComposer prompt enhancement', () => {
  it('renders an enhance button that is disabled while the draft is empty', async () => {
    const { app, btn } = mountComposer({ hasSendContent: false })
    await nextTick()

    const button = btn()
    expect(button).not.toBeNull()
    expect(button?.disabled).toBe(true)
    expect(button?.getAttribute('aria-label')).toContain('Enhance')
    app.unmount()
  })

  it('enables the enhance button once the parent reports sendable content', async () => {
    const { app, btn } = mountComposer({ hasSendContent: false })
    await nextTick()
    expect(btn()?.disabled).toBe(true)

    // The parent owns the draft; it reports readiness through hasSendContent.
    app.unmount()
    const { btn: btn2 } = mountComposer({ hasSendContent: true, modelValue: '写一个抽奖程序' })
    await nextTick()
    expect(btn2()?.disabled).toBe(false)
  })

  it('emits an enhanced prompt through v-model and never sends', async () => {
    const updates: string[] = []
    const sendSpy = vi.fn()
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      modelValue: '写一个抽奖程序',
      hasSendContent: true,
      'onUpdate:modelValue': (v: string) => { updates.push(v) },
      'onSend': sendSpy,
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    host.querySelector<HTMLButtonElement>('.chat-enhance-btn')?.click()
    await nextTick()

    expect(updates.length).toBe(1)
    const enhanced = updates[0]
    // i18n default locale in the test harness is 'en', so the scaffolding is
    // English; the original body must survive inside it verbatim.
    expect(enhanced).toContain('Complete the following task:')
    expect(enhanced).toContain('写一个抽奖程序')
    expect(enhanced).toContain('Output requirements:')

    // The whole point: enhancement must never auto-send.
    expect(sendSpy).not.toHaveBeenCalled()
    app.unmount()
  })

  it('is a no-op when the draft is empty even if the button is somehow clicked', async () => {
    const updates: string[] = []
    const host = document.createElement('div')
    document.body.append(host)
    const app = createApp(ChatComposer, composerProps({
      modelValue: '',
      hasSendContent: false,
      'onUpdate:modelValue': (v: string) => { updates.push(v) },
    }))
    app.use(i18n)
    app.mount(host)
    await nextTick()

    host.querySelector<HTMLButtonElement>('.chat-enhance-btn')?.click()
    await nextTick()

    expect(updates.length).toBe(0)
    app.unmount()
  })
})
