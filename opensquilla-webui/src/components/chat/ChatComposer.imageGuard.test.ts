// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const BASE_PROPS = {
  modelValue: 'describe this image',
  'onUpdate:modelValue': () => {},
  attachments: [],
  busySendMode: 'queue',
  hasSendContent: true,
  isStreaming: false,
  canStop: false,
  isNewLanding: false,
  placeholder: 'Send a message',
  sendButtonTitle: 'Send',
  runMode: 'safe',
  allowedRunModes: ['safe', 'full'],
  runModeLocked: false,
  runModeLockMessage: '',
  sessionRoutingMode: 'llm_ensemble',
  sessionRoutingBusy: false,
  routerVisualEffectsEnabled: true,
  voiceBusy: false,
  voiceRecording: false,
  voiceReady: true,
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('ChatComposer image-send guard', () => {
  it('shows a spinner and prevents duplicate submission without pending copy', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, sendPending: true, onSend })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(send.disabled).toBe(true)
    expect(send.getAttribute('aria-busy')).toBe('true')
    expect(send.querySelector('.loading-spinner')).not.toBeNull()
    expect(send.hasAttribute('title')).toBe(false)
    expect(send.getAttribute('aria-describedby')).toBeNull()
    expect(el.querySelector('.chat-send-tooltip')).toBeNull()
    expect(el.querySelector('.chat-composer-send-pending')).toBeNull()
    expect(el.querySelector('.chat-composer-status-announcement')).toBeNull()
    expect(el.textContent).not.toContain(i18n.global.t('chat.sendPending'))
    send.click()
    expect(onSend).not.toHaveBeenCalled()
    app.unmount()
  })

  it.each([
    { sendPending: true },
    { sessionRoutingBusy: true },
    { sendBlockedMessage: 'Wait for the session to reconnect.' },
    { showImageInputWarning: true, sendBlockedMessage: 'Image input is unavailable.' },
  ])('preserves normal Stop controls while another send is blocked: %j', async overrides => {
    const onStop = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, canStop: true, onStop, ...overrides })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const control = el.querySelector<HTMLElement>('.chat-send-control')!
    const stop = control.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(control.tabIndex).toBe(-1)
    expect(stop.disabled).toBe(false)
    expect(stop.getAttribute('aria-label')).toBe(i18n.global.t('chat.stopResponse'))
    expect(stop.title).toBe(i18n.global.t('chat.stopResponseEsc'))
    expect(stop.getAttribute('aria-describedby')).toBeNull()
    expect(el.querySelector('.chat-send-tooltip')).toBeNull()
    expect(el.querySelector('.chat-composer-send-status')).toBeNull()
    expect(el.querySelector('.chat-composer-status-announcement')).toBeNull()
    const stopShortcut = vi.fn()
    el.addEventListener('keydown', stopShortcut)
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    stop.dispatchEvent(escape)
    expect(escape.defaultPrevented).toBe(false)
    expect(stopShortcut).toHaveBeenCalledOnce()
    stop.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('moves unsupported-image warnings exclusively to the disabled send tooltip', async () => {
    const onSend = vi.fn()
    const message = 'Ensemble image input is unavailable.'
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sendBlockedMessage: message,
      showImageInputWarning: true,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const control = el.querySelector<HTMLElement>('.chat-send-control')!
    const tooltip = control.querySelector<HTMLElement>('.chat-send-tooltip')!
    const textarea = el.querySelector<HTMLDivElement>('.chat-inline-editor')!
    const send = control.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(el.querySelector('.chat-composer-send-status')).toBeNull()
    expect(el.querySelector('.chat-composer-status-announcement')).toBeNull()
    expect(el.querySelector('#chat-composer-send-status')).toBeNull()
    expect(tooltip.textContent).toBe(message)
    expect(tooltip.getAttribute('role')).toBe('tooltip')
    expect(control.getAttribute('role')).toBe('group')
    expect(control.tabIndex).toBe(0)
    expect(textarea.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(send.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(send.hasAttribute('title')).toBe(false)
    expect(send.disabled).toBe(true)
    send.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(el.querySelector('.chat-ai-disclaimer')).toBeNull()
    app.unmount()
  })

  it.each([
    'Restoring live updates. Your draft is preserved.',
    'The selected model requires direct routing.',
    'Model routing is being updated. Wait before sending images.',
  ])('explains a non-image restriction through the existing send tooltip: %s', async message => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sendBlockedMessage: message,
      showSendBlockedTooltip: true,
      sendButtonTitle: message,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const control = el.querySelector<HTMLElement>('.chat-send-control')!
    const tooltip = control.querySelector<HTMLElement>('.chat-send-tooltip')!
    const send = control.querySelector<HTMLButtonElement>('.chat-send-btn')!
    const textarea = el.querySelector<HTMLDivElement>('.chat-inline-editor')!
    expect(control.tabIndex).toBe(0)
    expect(control.getAttribute('role')).toBe('group')
    expect(control.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(tooltip.getAttribute('role')).toBe('tooltip')
    expect(tooltip.textContent).toBe(message)
    expect(send.disabled).toBe(true)
    expect(send.hasAttribute('title')).toBe(false)
    expect(send.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(textarea.getAttribute('aria-describedby')).toBe(tooltip.id)
    expect(el.querySelector('.chat-composer-send-status')).toBeNull()
    expect(el.querySelector('.chat-composer-status-announcement')).toBeNull()
    const escaped = vi.fn()
    el.addEventListener('keydown', escaped)
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    textarea.dispatchEvent(escape)
    expect(escape.defaultPrevented).toBe(false)
    expect(escaped).toHaveBeenCalledOnce()
    send.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(textarea.textContent).toBe(BASE_PROPS.modelValue)
    app.unmount()
  })

  it.each([
    { showImageInputWarning: true, sendBlockedMessage: 'Image input is unavailable.' },
    { sendBlockedMessage: 'Wait for the session to reconnect.' },
  ])('lets keyboard and touch users revisit a dismissed send hint: %j', async restriction => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      showSendBlockedTooltip: true,
      ...restriction,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const control = el.querySelector<HTMLElement>('.chat-send-control')!
    const escaped = vi.fn()
    el.addEventListener('keydown', escaped)
    control.focus()
    await nextTick()
    expect(document.activeElement).toBe(control)
    expect(control.classList.contains('is-hint-dismissed')).toBe(false)

    const dismiss = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    control.dispatchEvent(dismiss)
    await nextTick()
    expect(dismiss.defaultPrevented).toBe(true)
    expect(escaped).not.toHaveBeenCalled()
    expect(control.classList.contains('is-hint-dismissed')).toBe(true)

    const subsequent = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    control.dispatchEvent(subsequent)
    expect(subsequent.defaultPrevented).toBe(false)
    expect(escaped).toHaveBeenCalledOnce()

    control.blur()
    control.focus()
    await nextTick()
    expect(control.classList.contains('is-hint-dismissed')).toBe(false)
    control.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(control.classList.contains('is-hint-dismissed')).toBe(true)
    control.blur()
    control.dispatchEvent(new PointerEvent('pointerdown', { pointerType: 'touch', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(control)
    expect(control.classList.contains('is-hint-dismissed')).toBe(false)
    expect(onSend).not.toHaveBeenCalled()
    app.unmount()
  })

  it.each([
    { showImageInputWarning: true, sendBlockedMessage: 'Image input is unavailable.' },
    { sendBlockedMessage: 'Wait for the session to reconnect.' },
  ])('dismisses a hovered send hint before Escape can reach the focused editor draft shortcut: %j', async restriction => {
    const clearDraft = vi.fn((event: KeyboardEvent) => {
      if (event.key === 'Escape') (event.target as HTMLDivElement).textContent = ''
    })
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      showSendBlockedTooltip: true,
      modelValue: 'Keep this unsent draft.',
      ...restriction,
      onKeydown: clearDraft,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const control = el.querySelector<HTMLElement>('.chat-send-control')!
    const tooltip = control.querySelector<HTMLElement>('.chat-send-tooltip')!
    const textarea = el.querySelector<HTMLDivElement>('.chat-inline-editor')!
    textarea.focus()
    control.dispatchEvent(new MouseEvent('mouseenter'))
    // happy-dom does not resolve :hover; expose the real tooltip for the capture handler.
    tooltip.style.visibility = 'visible'
    const dismiss = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    textarea.dispatchEvent(dismiss)
    await nextTick()

    expect(document.activeElement).toBe(textarea)
    expect(dismiss.defaultPrevented).toBe(true)
    expect(control.classList.contains('is-hint-dismissed')).toBe(true)
    expect(clearDraft).not.toHaveBeenCalled()
    expect(textarea.textContent).toBe('Keep this unsent draft.')

    const subsequent = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    textarea.dispatchEvent(subsequent)
    expect(subsequent.defaultPrevented).toBe(false)
    expect(clearDraft).toHaveBeenCalledOnce()
    expect(textarea.textContent).toBe('')
    app.unmount()
  })

  it('keeps the send control enabled when no guard message is present', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, { ...BASE_PROPS, onSend })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')
    expect(el.querySelector('#chat-composer-send-status')).toBeNull()
    expect(el.querySelector('.chat-send-tooltip')).toBeNull()
    expect(el.querySelector<HTMLElement>('.chat-send-control')?.tabIndex).toBe(-1)
    expect(send?.disabled).toBe(false)
    expect(send?.title).toBe('Send')
    send?.click()
    expect(onSend).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('disables sending during a routing mutation without displaying update copy', async () => {
    const onSend = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      sessionRoutingBusy: true,
      onSend,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()

    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn')!
    expect(el.querySelector('.chat-composer-status-announcement')).toBeNull()
    expect(el.querySelector('.chat-send-tooltip')).toBeNull()
    expect(el.textContent).not.toContain(i18n.global.t('chat.composer.routingUpdateBlocked'))
    expect(send.disabled).toBe(true)
    expect(send.getAttribute('aria-busy')).toBe('true')
    expect(send.hasAttribute('title')).toBe(false)
    expect(send.getAttribute('aria-describedby')).toBeNull()
    send.click()
    expect(onSend).not.toHaveBeenCalled()
    app.unmount()
  })
})

describe('ChatComposer selected skill queue controls', () => {
  const selectedSkills = [{ name: 'synthetic-table', instanceId: 'instance-a', digest: 'digest-a' }]

  async function mountBusyComposer(overrides: Record<string, unknown> = {}) {
    const onSend = vi.fn()
    const onStop = vi.fn()
    const el = document.createElement('div')
    document.body.appendChild(el)
    const app = createApp(ChatComposer, {
      ...BASE_PROPS,
      selectedSkills,
      isStreaming: true,
      canStop: true,
      busySendMode: 'steer',
      sendButtonTitle: 'Steer the current response',
      onSend,
      onStop,
      ...overrides,
    })
    app.use(i18n)
    app.mount(el)
    await nextTick()
    return { app, el, onSend, onStop }
  }

  it('allows a busy skill draft to send to the queue and keeps Stop available', async () => {
    const { app, el, onSend, onStop } = await mountBusyComposer()
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')
    const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')
    expect(send?.disabled).toBe(false)
    expect(send?.title).toBe(i18n.global.t('chat.sendQueues'))
    expect(stop?.disabled).toBe(false)
    send?.click()
    expect(onSend).toHaveBeenCalledOnce()
    expect(onStop).not.toHaveBeenCalled()
    stop?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    { sendPending: true },
    { sessionRoutingBusy: true },
    { inputDisabled: true },
    { sendBlockedMessage: 'Synthetic send restriction' },
  ])('preserves Stop while the skill queue send is blocked: %j', async overrides => {
    const { app, el, onSend, onStop } = await mountBusyComposer(overrides)
    const send = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')
    const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')
    expect(send?.disabled).toBe(true)
    send?.click()
    expect(onSend).not.toHaveBeenCalled()
    expect(stop?.disabled).toBe(false)
    stop?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    { selectedSkills: [] },
    { modelValue: '', hasSendContent: false },
    { stopTargetsPlanRun: true },
    { replanActive: true },
  ])('retains the existing stop-only controls outside skill queue input: %j', async overrides => {
    const { app, el, onStop } = await mountBusyComposer(overrides)
    expect(el.querySelector('.chat-send-btn.btn--primary')).toBeNull()
    el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')?.click()
    expect(onStop).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each(['hover', 'focus'] as const)(
    'preserves the active Stop shortcut while dismissing a skill queue image hint: %s',
    async mode => {
      const { app, el, onStop } = await mountBusyComposer({
        showImageInputWarning: true,
        sendBlockedMessage: 'Image input is unavailable.',
      })
      const control = el.querySelector<HTMLElement>('.chat-send-control')!
      const tooltip = control.querySelector<HTMLElement>('.chat-send-tooltip')!
      const textarea = el.querySelector<HTMLDivElement>('.chat-inline-editor')!
      const stop = el.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')!
      const target = mode === 'hover' ? textarea : control
      target.focus()
      // happy-dom does not resolve :hover/:focus-within; exercise the capture handler.
      tooltip.style.visibility = 'visible'

      // ChatView consumes unhandled Escape at the document to stop the active turn.
      const stopShortcut = vi.fn((event: KeyboardEvent) => {
        if (event.key !== 'Escape' || event.defaultPrevented) return
        event.preventDefault()
        stop.click()
      })
      document.addEventListener('keydown', stopShortcut)
      try {
        target.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Escape', bubbles: true, cancelable: true,
        }))
        await nextTick()

        expect(control.classList.contains('is-hint-dismissed')).toBe(true)
        expect(stopShortcut).toHaveBeenCalledOnce()
        expect(onStop).toHaveBeenCalledOnce()
      } finally {
        document.removeEventListener('keydown', stopShortcut)
        app.unmount()
      }
    },
  )
})
