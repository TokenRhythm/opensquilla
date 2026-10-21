// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'
import ChatComposer from './ChatComposer.vue'
import { getIconSvg } from '@/utils/icons'

const apps: App[] = []
let panelWidth = 700
let resizeCallbacks: ResizeObserverCallback[] = []
let frameCallbacks: FrameRequestCallback[] = []

beforeEach(() => {
  panelWidth = 700
  resizeCallbacks = []
  frameCallbacks = []
  i18n.global.locale.value = 'en'
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: ResizeObserverCallback) { resizeCallbacks.push(callback) }
    observe() {}
    disconnect() {}
  })
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frameCallbacks.push(callback))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const width = this.matches('.chat-input-panel, .chat-input-footer') ? panelWidth : 180
    return { x: 0, y: 0, left: 0, top: 0, width, height: 34, right: width, bottom: 34, toJSON() {} }
  })
})

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  i18n.global.locale.value = 'en'
  document.body.replaceChildren()
})

async function resize(width: number) {
  panelWidth = width
  resizeCallbacks.forEach(callback => callback([], {} as ResizeObserver))
  const callbacks = frameCallbacks.splice(0)
  callbacks.forEach(callback => callback(0))
  await nextTick()
}

async function mount(overrides: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.append(host)
  const onSend = vi.fn(), onStop = vi.fn(), onSetRunMode = vi.fn()
  const props = reactive({
    modelValue: 'Draft kept while resizing', 'onUpdate:modelValue': () => {}, attachments: [],
    busySendMode: 'queue', hasSendContent: true, isStreaming: false, canStop: false,
    isNewLanding: false, placeholder: 'Send a message', sendButtonTitle: 'Send',
    runMode: 'safe', runModeLocked: false, runModeLockMessage: '', allowedRunModes: ['safe', 'full'], sessionRoutingMode: 'off',
    sessionRoutingAvailable: true, sessionRoutingBusy: false, modelSelectionAvailable: true,
    modelSelection: { model: 'long-model', provider: 'provider' },
    availableModels: [{ id: 'long-model', provider: 'provider', name: 'A very long model name with all of its version details' }],
    voiceBusy: false, voiceRecording: false, voiceReady: true,
    onSend, onStop, onSetRunMode, ...overrides,
  })
  const app = createApp({ render: () => h(ChatComposer, props as any) }).use(i18n)
  apps.push(app)
  app.mount(host)
  await nextTick()
  return { host, props, onSend, onStop, onSetRunMode }
}

describe('ChatComposer responsive control identity', () => {
  it('keeps active planning controls inside the shared footer through density changes', async () => {
    const { host } = await mount({ planModeAvailable: true, collaborationMode: 'plan' })
    const plan = host.querySelector('.composer-plan-mode')!
    expect(plan).not.toBeNull()
    const footer = host.querySelector('.chat-input-footer')
    expect(plan.closest('.chat-input-footer')).toBe(footer)
    expect(plan.closest('.chat-input-actions--left')).not.toBeNull()
    await resize(360)
    expect(host.querySelector('.composer-plan-mode')).toBe(plan)
    expect(plan.closest('.chat-input-footer')).toBe(footer)
  })

  it.each(['off', 'squilla_router', 'llm_ensemble'] as const)(
    'keeps the %s routing icon, open menu, focused item and trigger through resizing', async (sessionRoutingMode) => {
    const { host } = await mount({ sessionRoutingMode })
    const route = host.querySelector<HTMLButtonElement>('.chat-model-routing-btn')!
    const plus = host.querySelector('.chat-plus-btn')
    const mode = host.querySelector('.chat-run-mode-btn')
    const editor = host.querySelector('[contenteditable="true"]')
    if (sessionRoutingMode === 'off') {
      expect(route.querySelector('.chat-model-routing-btn__dot')).not.toBeNull()
      expect(route.querySelector('.icon svg rect')).toBeNull()
      expect(route.title).toContain('A very long model name')
      expect(route.getAttribute('aria-description')).toContain('A very long model name')
    } else {
      expect(route.querySelector('.chat-model-routing-btn__dot')).toBeNull()
      const expectedIcon = document.createElement('span')
      expectedIcon.innerHTML = getIconSvg(sessionRoutingMode === 'squilla_router' ? 'router' : 'fork', 17)
      expect(route.querySelector('.icon svg')?.isEqualNode(expectedIcon.firstElementChild)).toBe(true)
    }
    route.click()
    await nextTick()
    const menu = document.querySelector('.composer-model-routing')
    const focused = document.activeElement
    expect(menu).not.toBeNull()
    await resize(360)
    expect(host.querySelector<HTMLElement>('.chat-input-footer')?.dataset.composerDensity).toBe('icons')
    expect(document.querySelector('.composer-model-routing')).toBe(menu)
    expect(document.activeElement).toBe(focused)
    await resize(700)
    expect(host.querySelector<HTMLElement>('.chat-input-footer')?.dataset.composerDensity).toBe('wide')
    expect(host.querySelector('.chat-model-routing-btn')).toBe(route)
    expect(host.querySelector('.chat-plus-btn')).toBe(plus)
    expect(host.querySelector('.chat-run-mode-btn')).toBe(mode)
    expect(host.querySelector('[contenteditable="true"]')).toBe(editor)
    expect(route.getAttribute('aria-expanded')).toBe('true')
    expect(document.querySelectorAll('.chat-input-footer')).toHaveLength(1)
  })

  it('preserves Add, permission, Send and Stop actions with long localized labels in icons mode', async () => {
    await loadLocaleMessages('de')
    i18n.global.locale.value = 'de'
    panelWidth = 360
    const test = await mount()
    const footer = test.host.querySelector<HTMLElement>('.chat-input-footer')!
    expect(footer.dataset.composerDensity).toBe('icons')
    const plus = test.host.querySelector<HTMLButtonElement>('.chat-plus-btn')!
    plus.click()
    await nextTick()
    expect(document.querySelector('.composer-add-menu')).not.toBeNull()
    plus.click()
    await nextTick()
    const permission = test.host.querySelector<HTMLButtonElement>('.chat-run-mode-btn')!
    expect(permission.title).not.toBe('')
    expect(permission.getAttribute('aria-label')).not.toBe('')
    permission.click()
    await nextTick()
    expect(permission.getAttribute('aria-expanded')).toBe('true')
    permission.click()
    await nextTick()
    test.host.querySelector<HTMLButtonElement>('.chat-send-btn')!.click()
    expect(test.onSend).toHaveBeenCalledOnce()
    test.props.canStop = true
    test.props.isStreaming = true
    await nextTick()
    test.host.querySelector<HTMLButtonElement>('.chat-stop-btn')!.click()
    expect(test.onStop).toHaveBeenCalledOnce()
    expect(footer.dataset.composerDensity).toBe('icons')
  })
})
