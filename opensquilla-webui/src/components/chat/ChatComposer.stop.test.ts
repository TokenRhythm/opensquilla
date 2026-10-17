// @vitest-environment happy-dom
import { createApp, defineComponent, h, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import { useStableStopControl } from '@/composables/chat/useStableStopControl'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import ChatComposer from './ChatComposer.vue'

const apps: ReturnType<typeof createApp>[] = []
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function mountControl(initialCanStop = true) {
  const state = reactive({ identity: 'gateway:A:epoch1', taskId: initialCanStop ? 'task-a' : '',
    canStop: initialCanStop, hydrating: false, terminal: !initialCanStop, stopPending: false, cachedStop: false,
    skills: [] as SelectedSkillRef[], draft: '' })
  const stop = vi.fn()
  const send = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(defineComponent({ setup() {
    const { showStop } = useStableStopControl({
      identity: () => state.identity, taskId: () => state.taskId,
      canStop: () => state.canStop, hydrating: () => state.hydrating,
      terminal: () => state.terminal,
    })
    return () => h(ChatComposer, {
      modelValue: state.draft, attachments: [], busySendMode: 'queue',
      hasSendContent: Boolean(state.draft), selectedSkills: state.skills,
      isStreaming: !state.terminal, canStop: state.canStop, showStop: showStop.value || state.cachedStop,
      stopPending: state.stopPending, isNewLanding: false,
      placeholder: 'Send a message', sendButtonTitle: 'Send',
      runMode: 'safe', allowedRunModes: ['safe', 'full'], runModeLocked: false, runModeLockMessage: '',
      sessionRoutingMode: 'off', sessionRoutingBusy: false,
      voiceBusy: false, voiceRecording: false, voiceReady: false,
      onStop: stop, onSend: send,
    })
  } })).use(i18n)
  apps.push(app)
  app.mount(host)
  await nextTick()
  return { state, host, stop, send }
}

describe('ChatComposer Stop presentation', () => {
  it.each([true, false])('replaces Send with known Stop and back within one update when canStop=%s', async canStop => {
    const test = await mountControl(false)
    const originalSend = test.host.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')!
    expect(originalSend).not.toBeNull()

    // A warm session return can know the task's appearance before its stop
    // authority has recovered. Neither case should leave the previous Send
    // node on screen while an outgoing transition finishes.
    test.state.taskId = 'task-a'
    test.state.terminal = false
    test.state.hydrating = !canStop
    test.state.cachedStop = !canStop
    test.state.canStop = canStop
    await nextTick()

    const stop = test.host.querySelector<HTMLButtonElement>('.chat-stop-btn')
    expect(stop).not.toBeNull()
    expect(stop!.disabled).toBe(!canStop)
    expect(originalSend.isConnected).toBe(false)
    expect(test.host.querySelector('.chat-send-btn.btn--primary')).toBeNull()
    expect(test.host.querySelectorAll('.chat-send-control .chat-send-btn')).toHaveLength(1)
    expect(test.host.querySelector('[class*="composer-ctl-"]')).toBeNull()
    stop!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(test.stop).toHaveBeenCalledTimes(canStop ? 1 : 0)
    expect(test.send).not.toHaveBeenCalled()

    test.state.canStop = false
    test.state.cachedStop = false
    test.state.terminal = true
    test.state.taskId = ''
    test.state.hydrating = false
    await nextTick()

    expect(stop!.isConnected).toBe(false)
    expect(test.host.querySelector('.chat-stop-btn')).toBeNull()
    expect(test.host.querySelector('.chat-send-btn.btn--primary')).not.toBeNull()
    expect(test.host.querySelectorAll('.chat-send-control .chat-send-btn')).toHaveLength(1)
    expect(test.host.querySelector('[class*="composer-ctl-"]')).toBeNull()
  })

  it('keeps the same button through same-task hydration and blocks both Stop and Send until authority returns', async () => {
    const test = await mountControl()
    const original = test.host.querySelector<HTMLButtonElement>('.chat-stop-btn')!
    expect(original.disabled).toBe(false)
    test.state.hydrating = true
    test.state.canStop = false
    await nextTick()
    expect(test.host.querySelector('.chat-stop-btn')).toBe(original)
    expect(original.disabled).toBe(true)
    expect(original.getAttribute('aria-busy')).toBe('true')
    expect(original.className).not.toMatch(/composer-ctl-(leave|enter)/)
    expect(test.host.querySelector('.chat-send-btn.btn--primary')).toBeNull()
    original.click()
    // Even a synthetic click that bypasses the browser's disabled check is fenced.
    original.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    expect(test.stop).not.toHaveBeenCalled()
    expect(test.send).not.toHaveBeenCalled()

    test.state.taskId = ''
    await nextTick()
    expect(test.host.querySelector('.chat-stop-btn')).toBe(original)
    test.state.taskId = 'task-a'
    test.state.canStop = true
    test.state.hydrating = false
    await nextTick()
    expect(test.host.querySelector('.chat-stop-btn')).toBe(original)
    expect(original.disabled).toBe(false)
    original.click()
    expect(test.stop).toHaveBeenCalledOnce()
  })

  it('retires the appearance at terminal instead of keeping a dead Stop button', async () => {
    const test = await mountControl()
    test.state.hydrating = true
    test.state.canStop = false
    await nextTick()
    test.state.terminal = true
    test.state.taskId = ''
    await nextTick()
    expect(test.host.querySelector('.chat-stop-btn')).toBeNull()
    expect(test.host.querySelector('.chat-send-btn.btn--primary')).not.toBeNull()
    expect(test.stop).not.toHaveBeenCalled()
  })

  it('keeps the intentional skill queue Send and separate Stop controls available', async () => {
    const test = await mountControl()
    test.state.skills = [{ name: 'pptx', instanceId: 'skill-1', digest: 'hash-1' }]
    test.state.draft = 'Next task'
    await nextTick()
    expect(test.host.querySelector('.chat-stop-btn')).toBeNull()
    expect(test.host.querySelector('.chat-send-btn.btn--primary')).not.toBeNull()
    test.host.querySelector<HTMLButtonElement>('.chat-send-btn.btn--primary')!.click()
    expect(test.send).toHaveBeenCalledOnce()
    test.host.querySelector<HTMLButtonElement>('.chat-send-btn.btn--danger')!.click()
    expect(test.stop).toHaveBeenCalledOnce()
  })
})
