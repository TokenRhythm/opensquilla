import { effectScope, nextTick, reactive, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ConversationEvent } from '@/modules/conversationEvents'
import type { SessionProcess, SessionProcessIdentity, SessionProcessLog, SessionProcesses } from '@/modules/sessionProcesses'
import { createConversationEventsTestHarness } from '@/testing/conversationEvents.test-helper'
import { useSessionProcesses } from './useSessionProcesses'

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).forEach(close => close()); vi.useRealTimers() })
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); await nextTick() }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
const owner = (key = 'a', epoch = 0): SessionProcessIdentity => ({ sessionKey: key, sessionId: `${key}-${epoch}`, sessionEpoch: epoch })
const process = (status: SessionProcess['status'] = 'running', executionId = 'p1'): SessionProcess => ({
  executionId, command: 'python -m uvicorn app:app', status, returncode: status === 'done' ? 0 : null,
  taskId: 'old-turn', startedAt: 10, endedAt: status === 'running' ? null : 20,
})
const snapshot = (items = [process()], key = 'a', epoch = 0) => ({ ...owner(key, epoch), processes: items })
const output = (key = 'a', epoch = 0): SessionProcessLog => ({ ...owner(key, epoch), executionId: 'p1', status: 'running', output: 'ready', truncated: false })
function harness(available = true) {
  vi.useFakeTimers()
  const scope = effectScope()
  const sessionKey = ref('a')
  const gateway = reactive({ isAvailable: true, subscriptionEpoch: 1 })
  const events = createConversationEventsTestHarness()
  const list = vi.fn<SessionProcesses['list']>().mockResolvedValue(snapshot())
  const log = vi.fn<SessionProcesses['log']>().mockResolvedValue(output())
  const stop = vi.fn<SessionProcesses['stop']>().mockResolvedValue({ ...owner(), process: process('killed') })
  const port = reactive({ available, canStop: true, list, log, stop })
  const state = scope.run(() => useSessionProcesses({ sessionKey, gateway, processes: port, events: events.events }))!
  cleanups.push(() => { scope.stop(); events.events.dispose() })
  function emit(semanticKind: 'process-completed' | 'turn-completed' | 'session-epoch-changed', key = 'a', epoch = 0) {
    const event = { kind: 'conversation', event: { kind: 'known', semanticKind,
      sessionKey: key, taskId: 'old-turn', turnId: 'old-turn', streamGeneration: 'old-generation',
      streamSeq: 1, connectionSeq: null, generationEpoch: 0, meta: {},
      payload: semanticKind === 'process-completed'
        ? { executionId: 'p1', status: 'done', returncode: 0, sessionId: `${key}-${epoch}`, sessionEpoch: epoch }
        : { epoch },
    } } as ConversationEvent
    events.emit(event)
  }
  return { state, port, list, log, stop, gateway, sessionKey, events, emit }
}

describe('session processes independent of the agent turn', () => {
  it('restores a live service without requiring an active turn and keeps it on turn completion', async () => {
    const h = harness()
    await flush()
    expect(h.state.runningCount.value).toBe(1)
    h.emit('turn-completed')
    await flush()
    expect(h.list).toHaveBeenCalledTimes(2)
    expect(h.state.processes.value[0]?.status).toBe('running')
  })

  it('discovers new processes from tool receipts and refreshes a completed process from an older turn', async () => {
    const h = harness()
    h.list.mockResolvedValue(snapshot([]))
    await h.state.refresh()
    h.list.mockResolvedValue(snapshot())
    h.events.emitToolResult({ key: 'a', name: 'exec_command', task_id: 'launch' })
    await flush()
    expect(h.state.runningCount.value).toBe(1)
    h.list.mockResolvedValue(snapshot([process('done')]))
    h.emit('process-completed')
    await flush()
    expect(h.state.processes.value[0]?.returncode).toBe(0)
    expect(h.state.runningCount.value).toBe(0)
  })

  it('rejects an older running snapshot after a newer terminal snapshot', async () => {
    const h = harness()
    await flush()
    const old = deferred<Awaited<ReturnType<SessionProcesses['list']>>>()
    h.list.mockReturnValueOnce(old.promise).mockResolvedValue(snapshot([process('done')]))
    const pending = h.state.refresh()
    h.emit('process-completed')
    await flush()
    old.resolve(snapshot())
    await pending
    expect(h.state.processes.value[0]?.status).toBe('done')
  })

  it('clears old-session state immediately and rejects late snapshots and logs after switching', async () => {
    const h = harness()
    await flush()
    const oldList = deferred<Awaited<ReturnType<SessionProcesses['list']>>>()
    const oldLog = deferred<SessionProcessLog>()
    h.list.mockReturnValueOnce(oldList.promise).mockResolvedValue(snapshot([process('done', 'b1')], 'b'))
    h.log.mockReturnValueOnce(oldLog.promise)
    const pendingList = h.state.refresh()
    const pendingLog = h.state.inspect('p1')
    h.sessionKey.value = 'b'
    expect(h.state.processes.value).toEqual([])
    expect(h.state.selectedId.value).toBeNull()
    await flush()
    oldList.resolve(snapshot())
    oldLog.resolve(output())
    await Promise.all([pendingList, pendingLog])
    expect(h.state.processes.value.map(item => item.executionId)).toEqual(['b1'])
    expect(h.state.log.value).toBeNull()
    const calls = h.list.mock.calls.length
    h.emit('process-completed', 'a')
    await flush()
    expect(h.list).toHaveBeenCalledTimes(calls)
  })

  it('invalidates pending reads and logs on a same-key session reset', async () => {
    const h = harness()
    await flush()
    const oldLog = deferred<SessionProcessLog>()
    const oldList = deferred<Awaited<ReturnType<SessionProcesses['list']>>>()
    h.log.mockReturnValueOnce(oldLog.promise)
    h.list.mockReturnValueOnce(oldList.promise).mockResolvedValue(snapshot([], 'a', 1))
    const pendingLog = h.state.inspect('p1')
    const pendingList = h.state.refresh()
    h.emit('session-epoch-changed', 'a', 1)
    expect(h.state.processes.value).toEqual([])
    expect(h.state.selectedId.value).toBeNull()
    await flush()
    oldLog.resolve(output())
    oldList.resolve(snapshot())
    await Promise.all([pendingLog, pendingList])
    expect(h.state.processes.value).toEqual([])
    expect(h.state.log.value).toBeNull()
    const calls = h.list.mock.calls.length
    h.emit('process-completed', 'a', 0)
    await flush()
    expect(h.list).toHaveBeenCalledTimes(calls)
  })

  it('stops polling offline and restores an authoritative snapshot on reconnect', async () => {
    const h = harness()
    await flush()
    const old = deferred<Awaited<ReturnType<SessionProcesses['list']>>>()
    h.list.mockReturnValueOnce(old.promise)
    const pending = h.state.refresh()
    h.gateway.isAvailable = false
    expect(h.state.processes.value).toEqual([])
    await vi.advanceTimersByTimeAsync(15000)
    expect(h.list).toHaveBeenCalledTimes(2)
    h.list.mockResolvedValue(snapshot([process('done')]))
    h.gateway.subscriptionEpoch++
    h.gateway.isAvailable = true
    await flush()
    old.resolve(snapshot())
    await pending
    expect(h.state.processes.value[0]?.status).toBe('done')
  })

  it('reads bounded output and refreshes process state after stop', async () => {
    const h = harness()
    await flush()
    await h.state.inspect('p1')
    expect(h.state.log.value?.output).toBe('ready')
    h.list.mockResolvedValue(snapshot([process('killed')]))
    await h.state.stop('p1')
    expect(h.stop).toHaveBeenCalledWith('a', 'p1')
    expect(h.state.processes.value[0]?.status).toBe('killed')
    expect(h.state.stopping.value).toBeNull()
  })

  it('does not query unsupported/unauthorized gateways or stop without write access', async () => {
    const h = harness(false)
    await flush()
    expect(h.list).not.toHaveBeenCalled()
    h.port.available = true
    await flush()
    h.port.canStop = false
    await h.state.stop('p1')
    expect(h.stop).not.toHaveBeenCalled()
  })

  it('polls only while the authoritative snapshot has a running process', async () => {
    const h = harness()
    await flush()
    h.list.mockResolvedValue(snapshot([process('done')]))
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.list).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(15000)
    expect(h.list).toHaveBeenCalledTimes(2)
  })
})
