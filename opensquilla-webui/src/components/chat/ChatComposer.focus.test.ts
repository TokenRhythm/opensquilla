// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
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

async function mountComposer() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, BASE_PROPS)
  app.use(i18n)
  const vm = app.mount(el) as unknown as { focusTextarea: () => void }
  await nextTick()
  const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')
  expect(textarea).toBeTruthy()
  return { app, textarea: textarea as HTMLTextAreaElement, vm }
}

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ChatComposer focus contract', () => {
  it('focuses the textarea while the page is visible and active', async () => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    await nextTick()

    expect(focus).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    ['hidden', true],
    ['visible', false],
  ] as const)('does not focus when visibility is %s and window focus is %s', async (visibility, hasFocus) => {
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(visibility)
    vi.spyOn(document, 'hasFocus').mockReturnValue(hasFocus)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    await nextTick()

    expect(focus).not.toHaveBeenCalled()
    app.unmount()
  })

  it('rechecks page activity before the deferred focus runs', async () => {
    let visibility: DocumentVisibilityState = 'visible'
    let hasFocus = true
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility)
    vi.spyOn(document, 'hasFocus').mockImplementation(() => hasFocus)
    const { app, textarea, vm } = await mountComposer()
    const focus = vi.spyOn(textarea, 'focus')

    vm.focusTextarea()
    visibility = 'hidden'
    hasFocus = false
    await nextTick()

    expect(focus).not.toHaveBeenCalled()
    app.unmount()
  })
})
