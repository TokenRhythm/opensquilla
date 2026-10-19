// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import i18n from '@/i18n'
import ChatComposer from './ChatComposer.vue'

const base = {
  modelValue: 'Preserve this draft', attachments: [], busySendMode: 'queue' as const,
  hasSendContent: true, isStreaming: false, canStop: false, isNewLanding: false,
  placeholder: 'Send', sendButtonTitle: 'Send', runMode: 'safe' as const,
  allowedRunModes: ['safe' as const], runModeLocked: false, runModeLockMessage: '',
  sessionRoutingMode: 'off' as const, sessionRoutingBusy: false,
  voiceBusy: false, voiceRecording: false, voiceReady: true,
}
const cleanups: (() => void)[] = []
function mount(overrides: Record<string, unknown> = {}) {
  const props = reactive({ ...base, sendPending: false, sendBusy: false,
    recoveryAction: null as 'retry' | 'check' | null, recoveryPending: false,
    recoveryDisabled: false, ...overrides })
  const onSend = vi.fn(), onStop = vi.fn(), onRecover = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp({ render: () => h(ChatComposer, { ...props, onSend, onStop, onRecover }) })
  app.use(i18n)
  app.mount(el)
  cleanups.push(() => { app.unmount(); el.remove() })
  return { props, el, onSend, onStop, onRecover,
    button: () => el.querySelector<HTMLButtonElement>('.chat-send-control .chat-send-btn')! }
}
afterEach(() => { cleanups.splice(0).forEach(cleanup => cleanup()); vi.restoreAllMocks() })

describe('composer acceptance and quiet recovery controls', () => {
  it('ends admission motion at acceptance without releasing the transaction lock', async () => {
    const subject = mount({ sendPending: true, sendBusy: true })
    await nextTick()
    expect(subject.button().disabled).toBe(true)
    expect(subject.button().querySelector('.loading-spinner')).not.toBeNull()
    subject.button().click()
    expect(subject.onSend).not.toHaveBeenCalled()
    subject.props.sendBusy = false
    await nextTick()
    expect(subject.button().querySelector('.loading-spinner')).toBeNull()
    expect(subject.button().getAttribute('aria-busy')).toBe('false')
    expect(subject.button().disabled).toBe(true)
    subject.props.sendPending = false
    await nextTick()
    subject.button().click()
    expect(subject.onSend).toHaveBeenCalledOnce()
  })

  it('replaces the admission spinner with actual execution Stop at acceptance', async () => {
    const subject = mount({ sendPending: true, sendBusy: true })
    await nextTick()
    expect(subject.el.querySelector('.chat-stop-btn')).toBeNull()
    subject.props.sendBusy = false
    subject.props.canStop = true
    await nextTick()
    expect(subject.button().classList.contains('chat-stop-btn')).toBe(true)
    expect(subject.button().disabled).toBe(false)
    expect(subject.button().querySelector('.loading-spinner')).toBeNull()
    subject.button().click()
    expect(subject.onStop).toHaveBeenCalledOnce()
    expect(subject.onSend).not.toHaveBeenCalled()
  })

  it('preserves actual task Stop during a pending follow-up submission', async () => {
    const subject = mount({ canStop: true, sendPending: true, sendBusy: true })
    await nextTick()
    expect(subject.button().classList.contains('chat-stop-btn')).toBe(true)
    subject.button().click()
    expect(subject.onStop).toHaveBeenCalledOnce()
    expect(subject.onSend).not.toHaveBeenCalled()
  })

  it.each(['retry', 'check'] as const)('uses the existing primary action for %s without status copy', async action => {
    const subject = mount({ recoveryAction: action, canStop: true,
      sendBlockedMessage: 'Recovery detail must not become visible.', showSendBlockedTooltip: true })
    await nextTick()
    expect(subject.button().dataset.recoveryAction).toBe(action)
    expect(subject.el.querySelector('.chat-send-tooltip')).toBeNull()
    expect(subject.el.textContent).not.toContain('Recovery detail')
    expect(subject.button().querySelector('.loading-spinner')).toBeNull()
    subject.button().click()
    expect(subject.onRecover).toHaveBeenCalledOnce()
    expect(subject.onSend).not.toHaveBeenCalled()
    expect(subject.onStop).not.toHaveBeenCalled()
    subject.props.recoveryPending = true
    await nextTick()
    expect(subject.button().disabled).toBe(true)
    expect(subject.button().querySelector('.loading-spinner')).not.toBeNull()
    subject.button().click()
    expect(subject.onRecover).toHaveBeenCalledOnce()
    expect(subject.el.querySelector('.chat-inline-editor')?.getAttribute('contenteditable')).toBe('true')
    subject.props.recoveryPending = false
    subject.props.recoveryDisabled = true
    await nextTick()
    expect(subject.button().disabled).toBe(true)
    expect(subject.button().querySelector('.loading-spinner')).toBeNull()
  })
})
