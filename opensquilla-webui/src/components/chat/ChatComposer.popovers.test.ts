// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

function pointerDown(target: EventTarget) {
  target.dispatchEvent(new Event('pointerdown', { bubbles: true, composed: true }))
}

async function mountComposer() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(ChatComposer, {
    modelValue: '',
    'onUpdate:modelValue': () => {},
    attachments: [],
    busySendMode: 'queue',
    hasSendContent: false,
    isStreaming: false,
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
  })
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app: app as App<Element>, el }
}

async function clickButton(el: HTMLElement, label: string) {
  const button = el.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`)
  expect(button).toBeTruthy()
  button?.click()
  await nextTick()
}

async function clickMoreAction(el: HTMLElement, label: string) {
  await clickButton(el, 'More')
  expectPopover(el, '.chat-more-actions-menu', true)
  await clickButton(el, label)
}

function expectPopover(el: HTMLElement, selector: string, visible: boolean) {
  expect(Boolean(el.querySelector(selector))).toBe(visible)
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('ChatComposer popovers', () => {
  it('closes the more-actions menu on outside pointerdown', async () => {
    const { app, el } = await mountComposer()

    await clickButton(el, 'More')
    expectPopover(el, '.chat-more-actions-menu', true)
    pointerDown(document.body)
    await nextTick()
    expectPopover(el, '.chat-more-actions-menu', false)

    app.unmount()
  })

  it.each([
    ['Composer settings', '.composer-settings'],
    ['Model routing', '.composer-model-routing'],
    ['Execution mode', '.composer-run-mode'],
  ])('closes %s on outside pointerdown', async (label, selector) => {
    const { app, el } = await mountComposer()

    if (label === 'Composer settings') {
      await clickMoreAction(el, label)
    } else {
      await clickButton(el, label)
    }
    expectPopover(el, selector, true)
    pointerDown(document.body)
    await nextTick()
    expectPopover(el, selector, false)

    app.unmount()
  })

  it('keeps the active popover open when clicking inside it', async () => {
    const { app, el } = await mountComposer()

    await clickMoreAction(el, 'Composer settings')
    const popover = el.querySelector<HTMLElement>('.composer-settings')
    expect(popover).toBeTruthy()
    if (popover) pointerDown(popover)
    await nextTick()
    expectPopover(el, '.composer-settings', true)

    app.unmount()
  })

  it('keeps only one composer popover open at a time', async () => {
    const { app, el } = await mountComposer()

    await clickMoreAction(el, 'Composer settings')
    expectPopover(el, '.composer-settings', true)
    await clickButton(el, 'Model routing')
    expectPopover(el, '.composer-settings', false)
    expectPopover(el, '.composer-model-routing', true)
    await clickButton(el, 'Execution mode')
    expectPopover(el, '.composer-model-routing', false)
    expectPopover(el, '.composer-run-mode', true)

    app.unmount()
  })

  it('exports from the more-actions menu and closes the menu', async () => {
    let exports = 0
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      modelValue: '',
      'onUpdate:modelValue': () => {},
      attachments: [],
      busySendMode: 'queue',
      hasSendContent: false,
      isStreaming: false,
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
      onExportMarkdown: () => { exports += 1 },
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    await clickMoreAction(el, 'Export as Markdown')
    expect(exports).toBe(1)
    expectPopover(el, '.chat-more-actions-menu', false)

    app.unmount()
  })
})
