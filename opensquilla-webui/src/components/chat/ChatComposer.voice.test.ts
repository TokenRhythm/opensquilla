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
  runMode: 'safe',
  allowedRunModes: ['safe', 'full'],
  sessionRoutingMode: 'off',
  sessionRoutingBusy: false,
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
async function micButton(el: HTMLElement): Promise<HTMLButtonElement | null> {
  const more = el.querySelector<HTMLButtonElement>(
    `button[aria-label="${i18n.global.t('chrome.more')}"]`,
  )
  expect(more).toBeTruthy()
  more?.click()
  await nextTick()
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
  it('disables drafting and sending while a Plan questionnaire owns the input area', async () => {
    const { app, el } = await mount({ inputDisabled: true })
    const textarea = el.querySelector<HTMLTextAreaElement>('textarea')
    const send = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.send')}"]`,
    )

    expect(textarea?.disabled).toBe(true)
    expect(send?.disabled).toBe(true)
    app.unmount()
  })

  it('keeps Stop available while a background subagent group is active', async () => {
    const onStop = vi.fn()
    const { app, el } = await mount({ canStop: true, isStreaming: false, onStop })
    const stop = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.stopResponse')}"]`,
    )
    const send = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.send')}"]`,
    )

    expect(stop).toBeTruthy()
    expect(send).toBeNull()
    stop?.click()
    await nextTick()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('shows Send instead of Stop when no response can be stopped', async () => {
    const { app, el } = await mount({ canStop: false })
    const send = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.send')}"]`,
    )
    const stop = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.stopResponse')}"]`,
    )

    expect(send).toBeTruthy()
    expect(stop).toBeNull()
    app.unmount()
  })

  it('names Stop as ending the durable plan execution when it targets a PlanRun', async () => {
    const onStop = vi.fn()
    const { app, el } = await mount({
      canStop: true,
      stopTargetsPlanRun: true,
      onStop,
    })
    const stop = el.querySelector<HTMLButtonElement>(
      `button[aria-label="${i18n.global.t('chat.planRun.stopExecution')}"]`,
    )

    expect(stop).toBeTruthy()
    expect(stop?.getAttribute('title'))
      .toBe(i18n.global.t('chat.planRun.stopExecutionEsc'))
    stop?.click()
    await nextTick()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('records when ready: enabled, normal label, emits voiceInput', async () => {
    const onVoiceInput = vi.fn()
    const onVoiceSetup = vi.fn()
    const { app, el } = await mount({ voiceReady: true, onVoiceInput, onVoiceSetup })
    const btn = await micButton(el)
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
    const btn = await micButton(el)
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
    expect((await micButton(el))?.disabled).toBe(true)
    app.unmount()
  })
})
