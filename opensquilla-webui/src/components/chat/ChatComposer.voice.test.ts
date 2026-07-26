// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
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
  sendButtonTitle: 'Send',
  runMode: 'trusted',
  allowedRunModes: ['standard', 'trusted', 'full'],
  modelRoutingMode: 'off',
  modelRoutingSettingsBusy: false,
  routerVisualEffectsEnabled: true,
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
  return { app: app as App<Element>, el }
}

// The mic button carries the recordVoice aria-label when ready and the
// "unavailable" hint when gated — resolve both from i18n so the test never
// hard-codes English copy.
function micButton(el: HTMLElement): HTMLButtonElement | null {
  const ready = i18n.global.t('chat.recordVoice')
  const gated = i18n.global.t('chat.voiceUnavailableHint')
  return (
    el.querySelector<HTMLButtonElement>(`button[aria-label="${ready}"]`) ??
    el.querySelector<HTMLButtonElement>(`button[aria-label="${gated}"]`)
  )
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('ChatComposer voice-input gate', () => {
  it('keeps Stop available while a background subagent group is active', async () => {
    const onStop = vi.fn()
    const { app, el } = await mount({ canStop: true, isStreaming: false, onStop })
    const stop = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.stopResponse')}"]`,
    )

    expect(stop).toBeTruthy()
    stop?.click()
    await nextTick()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('records when ready: enabled, normal label, emits voiceInput', async () => {
    const onVoiceInput = vi.fn()
    const onVoiceSetup = vi.fn()
    const { app, el } = await mount({ voiceReady: true, onVoiceInput, onVoiceSetup })
    const btn = micButton(el)
    expect(btn).toBeTruthy()
    expect(btn?.disabled).toBe(false)
    expect(btn?.getAttribute('aria-label')).toBe(i18n.global.t('chat.recordVoice'))
    btn?.click()
    await nextTick()
    expect(onVoiceInput).toHaveBeenCalledTimes(1)
    expect(onVoiceSetup).not.toHaveBeenCalled()
    app.unmount()
  })

  it('when not ready: stays clickable, is dimmed, explains why, and routes to setup instead of recording', async () => {
    const onVoiceInput = vi.fn()
    const onVoiceSetup = vi.fn()
    const { app, el } = await mount({ voiceReady: false, onVoiceInput, onVoiceSetup })
    const btn = micButton(el)
    expect(btn).toBeTruthy()
    // Not hard-disabled — the user can click it to be guided to configuration.
    expect(btn?.disabled).toBe(false)
    expect(btn?.classList.contains('chat-mic--needs-setup')).toBe(true)
    expect(btn?.getAttribute('aria-label')).toBe(i18n.global.t('chat.voiceUnavailableHint'))
    expect(btn?.getAttribute('title')).toBe(i18n.global.t('chat.voiceUnavailableHint'))
    btn?.click()
    await nextTick()
    expect(onVoiceSetup).toHaveBeenCalledTimes(1)
    expect(onVoiceInput).not.toHaveBeenCalled()
    app.unmount()
  })

  it('disables the mic button while a transcription is in flight', async () => {
    const { app, el } = await mount({ voiceReady: true, voiceBusy: true })
    expect(micButton(el)?.disabled).toBe(true)
    app.unmount()
  })
})
