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
// button's readiness) stays false. The next composed keystroke's
// compositionend clears the flag and re-dispatches an input event, which is
// why typing one character "fixed" it. The fix force-syncs the model to the
// textarea's real DOM value on every paste.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
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
  modelRoutingMode: 'llm_ensemble',
  modelRoutingSettingsBusy: false,
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

async function simulatePaste(textarea: HTMLTextAreaElement, text: string) {
  // Real browser paste order: paste → beforeinput → input.
  textarea.value = text
  textarea.dispatchEvent(new Event('paste'))
  textarea.dispatchEvent(new InputEvent('input', { inputType: 'insertFromPaste' }))
  await nextTick()
  await nextTick()
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer paste → model sync', () => {
  it('updates the model on a plain paste', async () => {
    const { updates, textarea } = mountComposer()
    await simulatePaste(textarea, 'hello from clipboard')
    expect(updates[updates.length - 1]).toBe('hello from clipboard')
  })

  it('updates the model when pasting while the IME composition flag is stale', async () => {
    const { updates, textarea } = mountComposer()

    // Stale-composition simulation: Vue's compositionstart listener sets the
    // element's internal `composing` flag, which makes vModelText skip the
    // following input event — the exact state that stranded pasted text on
    // Windows. The paste handler must sync the model regardless.
    textarea.dispatchEvent(new Event('compositionstart'))
    await simulatePaste(textarea, 'pasted during stale composition')

    expect(updates[updates.length - 1]).toBe('pasted during stale composition')
  })
})
