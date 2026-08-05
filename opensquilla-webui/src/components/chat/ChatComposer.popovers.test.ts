// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

function pointerDown(target: EventTarget) {
  target.dispatchEvent(new Event('pointerdown', { bubbles: true, composed: true }))
}

async function mountComposer(overrides: Record<string, unknown> = {}) {
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
    runMode: 'safe',
    allowedRunModes: ['safe', 'full'],
    modelRoutingMode: 'off',
    modelRoutingSettingsBusy: false,
    routerVisualEffectsEnabled: true,
    codingModeEnabled: false,
    codingModeSettingsBusy: false,
    voiceBusy: false,
    voiceRecording: false,
    voiceReady: true,
    runModeLocked: false,
    runModeLockMessage: '',
    ...overrides,
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
  it('shows an accessible Coding ON chip that requests disabling the global mode', async () => {
    const setCodingModeEnabled = vi.fn()
    const { app, el } = await mountComposer({
      codingModeEnabled: true,
      onSetCodingModeEnabled: setCodingModeEnabled,
    })

    const chip = el.querySelector<HTMLButtonElement>('.chat-coding-mode-chip')
    expect(chip?.textContent).toContain('Coding ON')
    expect(chip?.getAttribute('aria-label')).toBe('Disable Coding mode')
    chip?.click()
    await nextTick()
    expect(setCodingModeEnabled).toHaveBeenCalledWith(false)

    app.unmount()
  })

  it('hides the Coding mode chip while off and disables it during a pending update', async () => {
    const { app, el } = await mountComposer()
    expect(el.querySelector('.chat-coding-mode-chip')).toBeNull()
    app.unmount()

    const busy = await mountComposer({
      codingModeEnabled: true,
      codingModeSettingsBusy: true,
    })
    const chip = busy.el.querySelector<HTMLButtonElement>('.chat-coding-mode-chip')
    expect(chip?.disabled).toBe(true)
    expect(chip?.getAttribute('aria-busy')).toBe('true')
    busy.app.unmount()
  })

  it('preserves the original single stop control while streaming', async () => {
    const { app, el } = await mountComposer({
      isStreaming: true,
      canStop: true,
      hasSendContent: true,
    })

    expect(el.querySelector('.chat-busy-mode')).toBeNull()
    expect(el.querySelector('.chat-input-actions--right .btn--primary')).toBeNull()
    expect(el.querySelectorAll('.chat-input-actions--right .btn--danger')).toHaveLength(1)
    app.unmount()
  })

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
    ['Model routing', '.composer-model-routing'],
    ['Execution mode', '.composer-run-mode'],
  ])('closes %s on outside pointerdown', async (label, selector) => {
    const { app, el } = await mountComposer()

    await clickButton(el, label)
    expectPopover(el, selector, true)
    pointerDown(document.body)
    await nextTick()
    expectPopover(el, selector, false)

    app.unmount()
  })

  it('keeps the more-actions menu open when clicking inside it', async () => {
    const { app, el } = await mountComposer()

    await clickButton(el, 'More')
    const popover = el.querySelector<HTMLElement>('.chat-more-actions-menu')
    expect(popover).toBeTruthy()
    if (popover) pointerDown(popover)
    await nextTick()
    expectPopover(el, '.chat-more-actions-menu', true)

    app.unmount()
  })

  it('keeps only one composer popover open at a time', async () => {
    const { app, el } = await mountComposer()

    await clickButton(el, 'More')
    expectPopover(el, '.chat-more-actions-menu', true)
    await clickButton(el, 'Model routing')
    expectPopover(el, '.chat-more-actions-menu', false)
    expectPopover(el, '.composer-model-routing', true)
    await clickButton(el, 'Execution mode')
    expectPopover(el, '.composer-model-routing', false)
    expectPopover(el, '.composer-run-mode', true)

    app.unmount()
  })

  it('shows a custom lock tooltip without the native title while the session is active', async () => {
    const lockMessage = 'Run mode cannot be changed while a task is running.'
    const { app, el } = await mountComposer({
      runModeLocked: true,
      runModeLockMessage: lockMessage,
    })
    const button = el.querySelector<HTMLButtonElement>(
      'button[aria-label="Execution mode"]',
    )
    const tooltip = el.querySelector<HTMLElement>('[role="tooltip"]')

    expect(button?.disabled).toBe(true)
    expect(button?.hasAttribute('title')).toBe(false)
    expect(button?.classList.contains('is-locked')).toBe(true)
    expect(tooltip?.classList.contains('chat-run-mode-lock-tip')).toBe(true)
    expect(tooltip?.textContent?.trim()).toBe(lockMessage)
    expect(button?.getAttribute('aria-describedby')).toBe(tooltip?.id)
    button?.click()
    await nextTick()
    expectPopover(el, '.composer-run-mode', false)

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
      runMode: 'safe',
      allowedRunModes: ['safe', 'full'],
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
