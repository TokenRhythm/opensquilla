// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref, type App } from 'vue'
import i18n from '@/i18n'
import { useChatComposerShortcuts } from '@/composables/chat/useChatComposerShortcuts'
import ChatComposer from './ChatComposer.vue'

const mounted: App[] = []
afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function setup(showSendBlockedTooltip = true) {
  const draft = ref('Keep this unsent draft.')
  const blocked = ref('Restoring live updates. Your draft is preserved.')
  const composing = ref(false)
  const slashOpen = ref(false)
  const send = vi.fn()
  const completeSlashCmd = vi.fn()
  const activateSlashCmd = vi.fn()
  const handle = ref<{ showSendBlockedHint: () => void } | null>(null)
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(defineComponent({ setup() {
    const shortcuts = useChatComposerShortcuts({
      inputText: draft, composing, messages: ref([]), pendingQueue: ref([]),
      canQueueMore: ref(true), slashOpen, slashIdx: ref(0),
      filteredSlashCmds: ref([{ name: 'help', cmd: '/help', label: 'Help', desc: '', aliases: [] }]),
      isStreaming: ref(false), autoResizeTextarea: vi.fn(), handleSlashInput: vi.fn(),
      closeSlashMenu: vi.fn(), completeSlashCmd, activateSlashCmd,
      popPendingTail: () => false, enqueuePendingInput: () => false,
      sendCurrentInput: () => {
        if (blocked.value) { handle.value?.showSendBlockedHint(); return }
        send()
      },
    })
    return () => h(ChatComposer, {
      ref: handle, modelValue: draft.value,
      'onUpdate:modelValue': value => { draft.value = value },
      attachments: [], busySendMode: 'queue', hasSendContent: Boolean(draft.value),
      isStreaming: false, canStop: false, isNewLanding: false,
      placeholder: 'Send a message', sendButtonTitle: 'Send',
      sendBlockedMessage: blocked.value,
      showSendBlockedTooltip,
      runMode: 'safe', allowedRunModes: ['safe', 'full'], runModeLocked: false, runModeLockMessage: '',
      sessionRoutingMode: 'off',
      sessionRoutingBusy: false, routerVisualEffectsEnabled: true,
      voiceBusy: false, voiceRecording: false, voiceReady: true,
      onKeydown: shortcuts.onTextareaKeydown,
      onCompositionChange: value => { composing.value = value },
    })
  } })).use(i18n)
  mounted.push(app)
  app.mount(host)
  await nextTick()
  const textarea = host.querySelector<HTMLTextAreaElement>('.chat-textarea')!
  const control = host.querySelector<HTMLElement>('.chat-send-control')!
  textarea.focus()
  textarea.setSelectionRange(5, 5)
  function key(key: string, init: KeyboardEventInit = {}) {
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
    textarea.dispatchEvent(event)
    return event
  }
  return { draft, blocked, composing, slashOpen, send, activateSlashCmd, host, textarea, control, key }
}

describe('blocked keyboard Send feedback', () => {
  it('does not duplicate an inline explanation when Enter attempts a blocked send', async () => {
    const test = await setup(false)
    test.key('Enter')
    await nextTick()
    expect(test.host.querySelector('.chat-send-tooltip')).toBeNull()
    expect(test.control.getAttribute('aria-describedby')).toBeNull()
    expect(test.textarea.getAttribute('aria-describedby')).toBeNull()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.send).not.toHaveBeenCalled()
    expect(test.draft.value).toBe('Keep this unsent draft.')
    expect(document.activeElement).toBe(test.textarea)
  })

  it('opens the existing hint immediately on Enter and preserves the draft, caret and focus', async () => {
    const test = await setup()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.key('Enter').defaultPrevented).toBe(true)
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(true)
    expect(test.host.querySelector('.chat-send-tooltip')?.textContent).toBe(test.blocked.value)
    expect(test.send).not.toHaveBeenCalled()
    expect(test.textarea.value).toBe('Keep this unsent draft.')
    expect(test.textarea.selectionStart).toBe(5)
    expect(document.activeElement).toBe(test.textarea)

    expect(test.key('Escape').defaultPrevented).toBe(true)
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.draft.value).toBe('Keep this unsent draft.')
    test.key('Enter')
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(true)
    expect(test.control.classList.contains('is-hint-dismissed')).toBe(false)
  })

  it('clears an action hint after editing or recovery without automatically sending', async () => {
    const test = await setup()
    test.key('Enter')
    await nextTick()
    test.textarea.value = 'Keep this revised draft.'
    test.textarea.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    test.key('Enter')
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(true)
    test.blocked.value = ''
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.host.querySelector('.chat-send-tooltip')).toBeNull()
    expect(test.send).not.toHaveBeenCalled()
    expect(test.draft.value).toBe('Keep this revised draft.')
    test.key('Enter')
    expect(test.send).toHaveBeenCalledOnce()
  })

  it('preserves Shift+Enter and IME confirmation instead of opening a blocked-send hint', async () => {
    const test = await setup()
    expect(test.key('Enter', { shiftKey: true }).defaultPrevented).toBe(false)
    expect(test.key('Enter', { isComposing: true }).defaultPrevented).toBe(false)
    test.textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    expect(test.key('Enter').defaultPrevented).toBe(false)
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.send).not.toHaveBeenCalled()
    test.textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }))
    test.key('Enter')
    await nextTick()
    expect(test.control.classList.contains('is-hint-requested')).toBe(true)
  })

  it('lets the slash menu consume Enter without exposing a connection hint', async () => {
    const test = await setup()
    test.slashOpen.value = true
    test.key('Enter')
    await nextTick()
    expect(test.activateSlashCmd).toHaveBeenCalledOnce()
    expect(test.control.classList.contains('is-hint-requested')).toBe(false)
    expect(test.send).not.toHaveBeenCalled()
  })
})
