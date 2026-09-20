// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'
import ChatComposerModelRouting from './ChatComposerModelRouting.vue'

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
    runModeLocked: false,
    runModeLockMessage: '',
    ...overrides,
  })
  app.use(i18n)
  const vm = app.mount(el) as unknown as { canCollapse: () => boolean }
  await nextTick()
  return { app: app as App<Element>, el, vm }
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
  expect(Boolean((selector === '.composer-model-routing' ? document.body : el).querySelector(selector))).toBe(visible)
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})

describe('ChatComposer popovers', () => {
  it.each([
    { mode: 'off', pin: null, defaultModel: { model: 'base', provider: 'provider-a' }, label: 'Base model', badge: true },
    { mode: 'off', pin: { model: 'base', provider: 'provider-a' }, defaultModel: { model: 'base', provider: 'provider-a' }, label: 'Base model', badge: false },
    { mode: 'squilla_router', pin: null, defaultModel: { model: 'base', provider: 'provider-a' }, label: 'Intelligent model routing', badge: false },
    { mode: 'llm_ensemble', pin: null, defaultModel: { model: 'base', provider: 'provider-a' }, label: 'Model ensemble', badge: false },
    { mode: 'off', pin: null, defaultModel: null, label: 'Fixed model', badge: true },
  ])('shows the active selection without confusing defaults and pins: $mode / $label / $badge', async ({ mode, pin, defaultModel, label, badge }) => {
    const { app, el } = await mountComposer({
      modelSelectionAvailable: true,
      availableModels: [{ id: 'base', provider: 'provider-a', name: 'Base model' }],
      modelSelection: pin,
      defaultModel: defaultModel,
      sessionRoutingMode: mode,
    })
    expect(el.querySelector('.chat-model-routing-btn__label')?.textContent).toBe(label)
    expect(Boolean(el.querySelector('.chat-model-routing-btn__default'))).toBe(badge)
    app.unmount()
  })
  it.each([
    ['safe', 'Safe'],
    ['full', 'Full Access'],
  ])('shows the current %s execution permission before opening its menu', async (runMode, label) => {
    const { app, el } = await mountComposer({ runMode })
    const trigger = el.querySelector<HTMLButtonElement>('.chat-run-mode-btn')!
    expect(trigger.textContent?.trim()).toBe(label)
    expect(trigger.getAttribute('aria-label')).toBe(`Execution mode: ${label}`)
    expect(trigger.getAttribute('aria-haspopup')).toBe('dialog')
    trigger.click()
    await nextTick()
    expect(el.querySelector('[role="radio"][aria-checked="true"]')?.textContent).toContain(label)
    app.unmount()
  })

  it('shows a persisted session model without a default badge or new-task selector', async () => {
    const { app, el } = await mountComposer({ modelSelectionAvailable: false, sessionModelName: 'bound-model' })
    expect(el.querySelector('.chat-model-routing-btn__label')?.textContent).toBe('bound-model')
    expect(el.querySelector('.chat-model-routing-btn__default')).toBeNull()
    await clickButton(el, 'Models & routing')
    expect(document.body.querySelector('.routing-mode__model-name')?.textContent).toBe('bound-model')
    expect(document.body.querySelector('[aria-haspopup="listbox"]')).toBeNull()
    app.unmount()
  })
  it('forwards a model change for an existing conversation through the unified menu', async () => {
    const selected = vi.fn(), refresh = vi.fn()
    const { app, el } = await mountComposer({
      isNewTask: false,
      modelSelectionAvailable: true,
      modelSelection: { model: 'base', provider: 'provider-a' },
      availableModels: [
        { id: 'base', provider: 'provider-a', name: 'Base model' },
        { id: 'next', provider: 'provider-b', name: 'Next model' },
      ],
      onSelectModel: selected,
      onRefreshModels: refresh,
    })
    await clickButton(el, 'Models & routing')
    expect(refresh).toHaveBeenCalledOnce()
    document.body.querySelector<HTMLButtonElement>('[data-mode="off"]')!.click()
    await nextTick()
    expect(document.body.querySelector('.new-task-model-menu strong')?.textContent).toBe('Conversation model')
    const nextModel = Array.from(document.body.querySelectorAll<HTMLButtonElement>('[role="option"]'))
      .find(row => row.textContent?.includes('Next model'))!
    nextModel.click()
    expect(selected).toHaveBeenCalledWith({ model: 'next', provider: 'provider-b' })
    await nextTick()
    expect(document.body.querySelector('.composer-model-routing')).toBeNull()
    app.unmount()
  })
  it('keeps the focused routing option mounted while a mutation is busy', async () => {
    const setMode = vi.fn()
    const props = reactive({
      modelRoutingMode: 'squilla_router',
      busy: false,
      onSetSessionRoutingMode: setMode,
    })
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp({ render: () => h(ChatComposerModelRouting, props as any) })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const selected = document.body.querySelector<HTMLButtonElement>('[role="menuitemradio"][aria-checked="true"]')
    expect(selected).toBeTruthy()
    selected?.focus()
    props.busy = true
    await nextTick()

    expect(selected?.disabled).toBe(false)
    expect(selected?.getAttribute('aria-disabled')).toBe('true')
    expect(document.activeElement).toBe(selected)
    selected?.click()
    expect(setMode).not.toHaveBeenCalled()

    app.unmount()
  })

  it('keeps floating visuals opt-in for non-ChatView consumers', async () => {
    const { app, el } = await mountComposer()
    const root = el.querySelector('.chat-composer')

    expect(root?.classList.contains('chat-composer--docked')).toBe(true)
    expect(root?.classList.contains('chat-composer--floating')).toBe(false)
    app.unmount()
  })

  it('requests expansion before pointer, focus, and input interactions', async () => {
    const expand = vi.fn()
    const { app, el } = await mountComposer({ collapsed: true, onExpand: expand })
    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')!

    pointerDown(textarea)
    textarea.focus()
    textarea.dispatchEvent(new Event('beforeinput', { bubbles: true }))

    expect(expand).toHaveBeenCalledTimes(3)
    app.unmount()
  })

  it('prevents collapse while a composer control owns focus or a popover is open', async () => {
    const { app, el, vm } = await mountComposer()
    expect(vm.canCollapse()).toBe(true)

    const textarea = el.querySelector<HTMLTextAreaElement>('.chat-textarea')!
    textarea.focus()
    expect(vm.canCollapse()).toBe(true)
    textarea.blur()

    const more = el.querySelector<HTMLButtonElement>('button[aria-label="More"]')!
    more.focus()
    expect(vm.canCollapse()).toBe(false)
    more.blur()

    await clickButton(el, 'More')
    expect(vm.canCollapse()).toBe(false)
    pointerDown(document.body)
    await nextTick()
    more.blur()
    expect(vm.canCollapse()).toBe(true)
    app.unmount()
  })

  it('retains attachment DOM while the visual region retracts', async () => {
    const { app, el } = await mountComposer({
      collapsed: true,
      attachments: [{
        kind: 'inline',
        local_id: 1,
        name: 'synthetic.txt',
        mime: 'text/plain',
        size: 12,
        data: 'c3ludGhldGlj',
      }],
    })

    expect(el.querySelector('.attachment-chip__name')?.textContent).toBe('synthetic.txt')
    expect(el.querySelector('.chat-attachments')?.closest('.chat-collapse-region')).toBeTruthy()
    app.unmount()
  })

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
    ["Models & routing", '.composer-model-routing'],
    ['Execution mode: Safe', '.composer-run-mode'],
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
    await clickButton(el, "Models & routing")
    expectPopover(el, '.chat-more-actions-menu', false)
    expectPopover(el, '.composer-model-routing', true)
    await clickButton(el, 'Execution mode: Safe')
    expectPopover(el, '.composer-model-routing', false)
    expectPopover(el, '.composer-run-mode', true)

    app.unmount()
  })

  it('keeps routing choices read-only while a Goal is materializing', async () => {
    const setMode = vi.fn()
    const { app, el } = await mountComposer({
      sessionRoutingControlBlocked: true,
      onSetSessionRoutingMode: setMode,
    })

    await clickButton(el, "Models & routing")
    const option = document.body.querySelector<HTMLButtonElement>('[role="menuitemradio"]')
    expect(option?.getAttribute('aria-disabled')).toBe('true')
    option?.click()
    await nextTick()

    expect(setMode).not.toHaveBeenCalled()
    app.unmount()
  })

  it('closes every open popover when the composer collapses', async () => {
    const props = reactive({
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
      runModeLocked: false,
      runModeLockMessage: '',
      collapsed: false,
    })
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp({ render: () => h(ChatComposer, props as any) })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const popovers = [
      ['Add', '.composer-add-menu'],
      ['More', '.chat-more-actions-menu'],
      ["Models & routing", '.composer-model-routing'],
      ['Execution mode: Safe', '.composer-run-mode'],
    ] as const
    for (const [label, selector] of popovers) {
      props.collapsed = false
      await nextTick()
      await clickButton(el, label)
      expectPopover(el, selector, true)

      props.collapsed = true
      await nextTick()
      expectPopover(el, selector, false)
    }

    // re-expanding keeps the menu closed
    props.collapsed = false
    await nextTick()
    expectPopover(el, '.chat-more-actions-menu', false)

    app.unmount()
  })

  it('shows a custom lock tooltip without the native title while the session is active', async () => {
    const lockMessage = 'Run mode cannot be changed while a task is running.'
    const { app, el } = await mountComposer({
      runModeLocked: true,
      runModeLockMessage: lockMessage,
    })
    const button = el.querySelector<HTMLButtonElement>(
      'button[aria-label="Execution mode: Safe"]',
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
      sessionRoutingMode: 'off',
      sessionRoutingBusy: false,
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

  it('shows keepalive only when supported and enables it after the session is materialized', async () => {
    const unsupported = await mountComposer()
    await clickButton(unsupported.el, 'More')
    expect(unsupported.el.querySelector('[data-testid="chat-composer-action-keepalive"]')).toBeNull()
    unsupported.app.unmount()

    const openKeepalive = vi.fn()
    const draft = await mountComposer({
      promptCacheKeepaliveAvailable: true,
      promptCacheKeepaliveSessionReady: false,
      onOpenPromptCacheKeepalive: openKeepalive,
    })
    await clickButton(draft.el, 'More')
    const disabledAction = draft.el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-composer-action-keepalive"]',
    )
    expect(disabledAction?.disabled).toBe(true)
    expect(disabledAction?.textContent).toContain('Available after the first message is sent')
    disabledAction?.click()
    expect(openKeepalive).not.toHaveBeenCalled()
    expectPopover(draft.el, '.chat-more-actions-menu', true)
    draft.app.unmount()

    const ready = await mountComposer({
      promptCacheKeepaliveAvailable: true,
      promptCacheKeepaliveSessionReady: true,
      onOpenPromptCacheKeepalive: openKeepalive,
    })
    await clickMoreAction(ready.el, 'Prompt cache keepalive')
    expect(openKeepalive).toHaveBeenCalledTimes(1)
    expectPopover(ready.el, '.chat-more-actions-menu', false)
    ready.app.unmount()
  })

  it('refreshes and shows the current keepalive state inside the existing menu', async () => {
    const refreshKeepalive = vi.fn()
    const { app, el } = await mountComposer({
      promptCacheKeepaliveAvailable: true,
      promptCacheKeepaliveSessionReady: true,
      promptCacheKeepaliveStatus: {
        enabled: true,
        ttlSeconds: 300,
        intervalSeconds: 240,
        idleTimeoutSeconds: 3_600,
        idleExpiresAt: Date.now() + 3_600_000,
        state: 'scheduled',
        reason: null,
        hasSnapshot: true,
        nextProbeAt: Date.now() + 240_000,
        lastProbeAt: null,
        lastCacheHitTokens: 0,
        provider: 'synthetic',
        model: 'synthetic-model',
      },
      onRefreshPromptCacheKeepalive: refreshKeepalive,
    })

    await clickButton(el, 'More')

    expect(refreshKeepalive).toHaveBeenCalledTimes(1)
    const action = el.querySelector<HTMLButtonElement>(
      '[data-testid="chat-composer-action-keepalive"]',
    )
    expect(action?.textContent).toContain('Scheduled')
    expect(action?.getAttribute('aria-label')).toBe('Prompt cache keepalive. Scheduled')
    expect(
      action?.querySelector('[data-state="scheduled"] .chat-more-actions-menu__status-dot'),
    ).toBeTruthy()
    app.unmount()
  })
})
