import { effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import type { NativeBrowserAutomationState, NativeWorkbenchApi } from '@/platform/types'
import type { ChatRunStatus } from '@/types/chat'
import { useBrowserAutomationState } from './useBrowserAutomationState'

function fixture(native?: NativeWorkbenchApi) {
  const update = vi.fn(async (_state: NativeBrowserAutomationState) => ({ ok: true }))
  const bridge = native ?? { setBrowserAutomationState: update } as unknown as NativeWorkbenchApi
  const state = {
    native: bridge,
    sessionKey: ref('session-a'),
    connected: ref(true),
    isStreaming: ref(false),
    runStatus: ref<ChatRunStatus>({ status: 'idle', label: '', task: null }),
    activeStreamTaskId: ref(''),
    activeStreamSessionKey: ref(''),
  }
  const scope = effectScope()
  scope.run(() => useBrowserAutomationState(state))
  return { ...state, scope, update }
}

describe('browser automation visual lifecycle', () => {
  it('keeps the same task active across streaming and approval gaps, then ends it', async () => {
    const f = fixture()
    expect(f.update).not.toHaveBeenCalled()
    f.runStatus.value = { status: 'running', label: '', task: { taskId: 'task-a' } }
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: true })
    f.isStreaming.value = true
    f.activeStreamTaskId.value = 'task-a'
    f.activeStreamSessionKey.value = 'session-a'
    await nextTick()
    f.isStreaming.value = false
    f.runStatus.value = { status: 'approval_pending', label: '', task: { taskId: 'task-a' } }
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(1)
    f.runStatus.value = { status: 'idle', label: '', task: { taskId: 'task-a', status: 'succeeded' } }
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: false })
    f.scope.stop()
    expect(f.update).toHaveBeenCalledTimes(2)
  })

  it('retires the old session and ignores its lingering stream while the next session hydrates', async () => {
    const f = fixture()
    f.isStreaming.value = true
    f.activeStreamTaskId.value = 'task-a'
    f.activeStreamSessionKey.value = 'session-a'
    await nextTick()
    f.sessionKey.value = 'session-b'
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: false })
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(2)
    f.runStatus.value = { status: 'running', label: '', task: { taskId: 'task-b' } }
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-b', taskId: 'task-b', active: true })
    f.scope.stop()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-b', taskId: 'task-b', active: false })
  })

  it('clears on disconnect and restores only a still-active task on reconnect', async () => {
    const f = fixture()
    f.runStatus.value = { status: 'running', label: '', task: { task_id: 'task-a' } }
    await nextTick()
    f.connected.value = false
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: false })
    f.connected.value = true
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: true })
    f.runStatus.value = { status: 'cancelled', label: '', task: { task_id: 'task-a' } }
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: false })
    f.scope.stop()
  })

  it('retains a provisional visual identity when its backend task arrives, but replaces it for a new task', async () => {
    const f = fixture()
    f.isStreaming.value = true
    await nextTick()
    const provisional = f.update.mock.calls[0]![0]
    expect(provisional).toMatchObject({ sessionKey: 'session-a', active: true })
    expect(provisional!.taskId).toMatch(/^browser-visual-/)
    f.runStatus.value = { status: 'running', label: '', task: null }
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(1)
    f.activeStreamTaskId.value = 'assigned-task'
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(1)
    f.activeStreamTaskId.value = 'next-task'
    await nextTick()
    expect(f.update.mock.calls.slice(-2).map(call => call[0])).toEqual([
      { ...provisional, active: false },
      { sessionKey: 'session-a', taskId: 'next-task', active: true },
    ])
    f.scope.stop()
  })

  it('resumes the same provisional activation after disconnect and late backend identity', async () => {
    const f = fixture()
    f.isStreaming.value = true
    await nextTick()
    const original = f.update.mock.calls[0]![0]
    f.connected.value = false
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith({ ...original, active: false })
    f.activeStreamTaskId.value = 'assigned-task'
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(2)
    f.connected.value = true
    await nextTick()
    expect(f.update).toHaveBeenLastCalledWith(original)
    f.isStreaming.value = false
    await nextTick()
    f.isStreaming.value = true
    f.activeStreamTaskId.value = ''
    await nextTick()
    expect(f.update.mock.lastCall![0].taskId).not.toBe(original.taskId)
    f.scope.stop()
  })

  it('does not let an old view teardown clear a successor rendering the same task', async () => {
    const old = fixture()
    old.runStatus.value = { status: 'running', label: '', task: { taskId: 'task-a' } }
    await nextTick()
    const next = fixture(old.native)
    next.runStatus.value = { status: 'running', label: '', task: { taskId: 'task-a' } }
    await nextTick()
    expect(old.update).toHaveBeenCalledTimes(2)
    old.scope.stop()
    expect(old.update).toHaveBeenCalledTimes(2)
    next.scope.stop()
    expect(old.update).toHaveBeenLastCalledWith({ sessionKey: 'session-a', taskId: 'task-a', active: false })
  })

  it('supports older clients and ignores visual IPC failures', async () => {
    const old = fixture({} as NativeWorkbenchApi)
    old.runStatus.value = { status: 'running', label: '', task: null }
    await nextTick()
    expect(old.update).not.toHaveBeenCalled()
    old.scope.stop()
    const f = fixture()
    f.update.mockRejectedValue(new Error('renderer closed'))
    f.isStreaming.value = true
    await nextTick()
    f.scope.stop()
    await nextTick()
    expect(f.update).toHaveBeenCalledTimes(2)
  })
})
