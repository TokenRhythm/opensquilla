// @vitest-environment happy-dom
//
// Regression: pasting text into the composer could leave the send button
// disabled until the next composed keystroke (issue #1017).
//
// Mechanism: Vue's vModelText input listener skips model updates while the
// element's internal IME-composition flag is set (see vModelText in
// @vue/runtime-dom — `if (e.target.composing) return`). On Windows, a paste
// can land while that flag is stale after a composition round-trip, so the
// v-model never observes the pasted text and `hasSendContent` (the send
// button's readiness) stays false.
//
// Timing matters: in real browsers the paste event fires BEFORE the default
// insertion mutates the DOM, so a paste-handler (or nextTick scheduled from
// it) reads an empty value. The input event with inputType "insertFromPaste"
// fires AFTER the browser has written the pasted text; the composer syncs
// the model from the DOM at that stage, restoring readiness even when
// vModelText skipped the update.
//
// The tests model the real Chromium order (paste → beforeinput → input, with
// textarea.value mutated between beforeinput and input) and assert the
// user-visible contract: the pasted text reaches the parent model and the
// send button becomes ready.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
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
  runModeLocked: false,
  runModeLockMessage: '',
  sessionRoutingMode: 'llm_ensemble',
  sessionRoutingBusy: false,
  routerVisualEffectsEnabled: true,
  codingModeEnabled: false,
  codingModeSettingsBusy: false,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

function mountComposer() {
  const updates: string[] = []
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, {
    ...BASE_PROPS,
    modelValue: '',
    'onUpdate:modelValue': (value: string) => {
      updates.push(value)
    },
    onSend: vi.fn(),
  })
  app.use(i18n)
  app.mount(el)
  const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
  if (!textarea) throw new Error('textarea not rendered')
  return { updates, textarea, el }
}

/** Parent wrapper mirroring ChatView's usage: send readiness derives from
 * the model, so a model sync must visibly enable the send path. */
const ComposerWrapper = defineComponent({
  components: { ChatComposer },
  template: `
    <ChatComposer
      v-model="text"
      :attachments="[]"
      :busy-send-mode="'queue'"
      :has-send-content="text.trim().length > 0"
      :is-streaming="false"
      :can-stop="false"
      :is-new-landing="false"
      placeholder="Send a message"
      send-button-title="Send"
      :run-mode="'trusted'"
      :allowed-run-modes="['standard', 'trusted', 'full']"
      :run-mode-locked="false"
      run-mode-lock-message=""
      :session-routing-mode="'llm_ensemble'"
      :session-routing-busy="false"
      :router-visual-effects-enabled="true"
      :coding-mode-enabled="false"
      :coding-mode-settings-busy="false"
      :voice-busy="false"
      :voice-recording="false"
      :voice-ready="true"
      @input="observedAtInput.push(text)"
      @send="sent.push(text)"
    />`,
  setup() {
    const text = ref('')
    const observedAtInput = ref<string[]>([])
    const sent = ref<string[]>([])
    return { observedAtInput, sent, text }
  },
})

function mountWrapper() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ComposerWrapper)
  app.use(i18n)
  const instance = app.mount(el) as unknown as {
    observedAtInput: string[]
    sent: string[]
    text: string
  }
  const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
  if (!textarea) throw new Error('textarea not rendered')
  return { textarea, el, instance }
}

/** Model the real Chromium paste order: paste → beforeinput → input. The
 * browser's default insertion mutates textarea.value between beforeinput
 * and input — the DOM value only exists at the input stage. */
async function simulateRealPaste(textarea: HTMLTextAreaElement, text: string) {
  textarea.dispatchEvent(new Event('paste'))
  textarea.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertFromPaste' }))
  textarea.value = text // browser default insertion (between beforeinput and input)
  textarea.dispatchEvent(new InputEvent('input', { inputType: 'insertFromPaste' }))
  await nextTick()
}

function sendButtonReady(el: HTMLElement): boolean {
  const button = el.querySelector<HTMLButtonElement>('.chat-send-btn')
  if (!button) throw new Error('send button not rendered')
  return button.classList.contains('is-ready')
}

function clickSend(el: HTMLElement) {
  const button = el.querySelector<HTMLButtonElement>('.chat-send-btn')
  if (!button) throw new Error('send button not rendered')
  button.click()
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer paste → model sync', () => {
  it('updates the parent model on a plain paste', async () => {
    const { updates, textarea } = mountComposer()
    await simulateRealPaste(textarea, 'hello from clipboard')
    expect(updates[updates.length - 1]).toBe('hello from clipboard')
  })

  it('restores send readiness on paste during stale composition', async () => {
    const { textarea, el, instance } = mountWrapper()

    // Stale-composition simulation: Vue's compositionstart listener sets the
    // element's internal `composing` flag, which makes vModelText skip the
    // following input event — the exact state that stranded pasted text on
    // Windows.
    textarea.dispatchEvent(new Event('compositionstart'))
    await simulateRealPaste(textarea, 'pasted during stale composition')

    // User-visible contract: the pasted text reached the parent model…
    expect(instance.text).toBe('pasted during stale composition')
    // …parent input consumers observed the reconciled value…
    expect(instance.observedAtInput).toEqual(['pasted during stale composition'])
    // …and the send button is ready.
    expect(sendButtonReady(el)).toBe(true)

    clickSend(el)
    expect(instance.sent).toEqual(['pasted during stale composition'])
  })

  it('preserves the normal IME composition lifecycle', async () => {
    const { textarea, instance } = mountWrapper()

    textarea.dispatchEvent(new Event('compositionstart'))
    textarea.value = '输入'
    textarea.dispatchEvent(new InputEvent('input', { inputType: 'insertCompositionText' }))
    await nextTick()
    expect(instance.text).toBe('')

    textarea.dispatchEvent(new Event('compositionend'))
    await nextTick()
    expect(instance.text).toBe('输入')
  })
})
