// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n, { loadLocaleMessages, SUPPORTED_LOCALES, type LocaleCode } from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const mountedApps: App[] = []

async function mountComposer(isNewLanding: boolean) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const state = reactive({ isNewLanding, draft: '' })
  const app = createApp({
    render: () => h(ChatComposer, {
      modelValue: state.draft,
      'onUpdate:modelValue': (value: string) => { state.draft = value },
      attachments: [],
      busySendMode: 'queue',
      hasSendContent: state.draft.trim().length > 0,
      isStreaming: false,
      canStop: false,
      isNewLanding: state.isNewLanding,
      placeholder: 'Send a message',
      sendButtonTitle: 'Send',
      runMode: 'safe',
      allowedRunModes: ['safe', 'full'],
      runModeLocked: false,
      runModeLockMessage: '',
      sessionRoutingMode: 'off',
      sessionRoutingBusy: false,
      voiceBusy: false,
      voiceRecording: false,
      voiceReady: true,
    }),
  })
  app.use(i18n)
  mountedApps.push(app)
  app.mount(el)
  await nextTick()
  return { el, state }
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer AI disclaimer', () => {
  it('shows on the landing page, survives typing, and returns when starting another new chat', async () => {
    const { el, state } = await mountComposer(true)
    const note = el.querySelector('.chat-ai-disclaimer')
    expect(note?.getAttribute('role')).toBe('note')
    expect(note?.textContent).toBe('AI can make mistakes. Verify important information.')

    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')!
    textarea.focus()
    textarea.value = 'Keep this draft'
    textarea.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(state.draft).toBe('Keep this draft')
    expect(el.querySelector('.chat-ai-disclaimer')).toBe(note)

    state.isNewLanding = false
    await nextTick()
    expect(el.querySelector('.chat-ai-disclaimer')).toBeNull()
    expect(el.querySelector('.chat-collapse-region--disclaimer')).toBeNull()

    state.draft = ''
    state.isNewLanding = true
    await nextTick()
    expect(el.querySelector('.chat-ai-disclaimer')?.textContent)
      .toBe('AI can make mistakes. Verify important information.')
  })

  it('does not mount a disclaimer or an empty disclaimer region for an existing chat', async () => {
    const { el } = await mountComposer(false)
    expect(el.querySelector('.chat-ai-disclaimer')).toBeNull()
    expect(el.querySelector('.chat-collapse-region--disclaimer')).toBeNull()
  })

  it('updates the mounted landing note when switching among every supported locale', async () => {
    const expected: Record<LocaleCode, string> = {
      en: 'AI can make mistakes. Verify important information.',
      'zh-Hans': 'AI 可能出错，请核实重要信息。',
      ja: 'AI は間違えることがあります。重要な情報は確認してください。',
      fr: 'L’IA peut se tromper. Vérifiez les informations importantes.',
      de: 'KI kann Fehler machen. Prüfen Sie wichtige Informationen.',
      es: 'La IA puede cometer errores. Verifica la información importante.',
    }
    const { el } = await mountComposer(true)

    for (const locale of SUPPORTED_LOCALES) {
      await loadLocaleMessages(locale)
      i18n.global.locale.value = locale
      await nextTick()
      expect(i18n.global.te('chat.aiDisclaimer', locale)).toBe(true)
      expect(el.querySelector('.chat-ai-disclaimer')?.textContent).toBe(expected[locale])
    }
  })
})
