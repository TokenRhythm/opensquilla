import { describe, expect, it } from 'vitest'
import { chatTaskId, useChatTaskOwnership } from './useChatTaskOwnership'
import type { ChatRunTask } from '@/types/chat'

describe('useChatTaskOwnership', () => {
  it.each<{ task: ChatRunTask | null | undefined; expected: string }>([
    { task: undefined, expected: '' },
    { task: null, expected: '' },
    { task: {}, expected: '' },
    { task: { task_id: 'task-A' }, expected: 'task-A' },
    { task: { taskId: 'task-A' }, expected: 'task-A' },
    { task: { task_id: '', turn_id: 'task-A' }, expected: 'task-A' },
    { task: { turnId: 'task-A' }, expected: 'task-A' },
    { task: { task_id: ' task-A ', taskId: 'task-B' }, expected: 'task-A' },
    { task: { task_id: ' ', taskId: 'task-B' }, expected: '' },
    { task: { ownershipTaskId: undefined, taskId: 'task-A' }, expected: 'task-A' },
    { task: { ownershipTaskId: '', task_id: 'task-B' }, expected: '' },
    { task: { ownershipTaskId: 'task-A', task_id: 'task-B' }, expected: 'task-A' },
  ])('preserves projected ownership authority and unprojected task fallback: %j', ({ task, expected }) => {
    expect(chatTaskId(task)).toBe(expected)
  })

  it('keeps Stop bound to A when B starts before A publishes its cancelled terminal', () => {
    const ownership = useChatTaskOwnership()

    expect(ownership.noteRunning({ task_id: 'task-A', status: 'running' })).toBe(true)
    ownership.noteQueued({ task_id: 'task-B', status: 'queued' })
    expect(ownership.beginStop()).toBe('task-A')

    // TaskRuntime can release A's execution lane before its terminal observer
    // finishes, so this order is valid and must not retarget the in-flight Stop.
    expect(ownership.noteRunning({ task_id: 'task-B', status: 'running' })).toBe(true)
    expect(ownership.runningTaskId.value).toBe('task-B')
    expect(ownership.stopRequestedTaskId.value).toBe('task-A')

    expect(ownership.noteTerminal('task-A')).toEqual({
      wasRunning: false,
      wasQueued: false,
      wasStopTarget: true,
    })
    expect(ownership.runningTaskId.value).toBe('task-B')
    expect(ownership.stopRequestedTaskId.value).toBe('')
    expect(ownership.stopTargetTaskId.value).toBe('task-B')
  })

  it('does not let a queued acceptance demote the running owner', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning({ task_id: 'task-A', status: 'running' })

    const accepted = ownership.noteAccepted('task-B', 'queued')

    expect(accepted).toEqual({ claimRender: false, renderTaskId: 'task-A' })
    expect(ownership.runningTaskId.value).toBe('task-A')
    expect([...ownership.queuedTaskIds.value]).toEqual(['task-B'])
    expect(ownership.stopTargetTaskId.value).toBe('task-A')
  })

  it('removes a cancelled queued task without closing the running owner', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-A')
    ownership.noteQueued('task-B')

    expect(ownership.noteTerminal('task-B')).toEqual({
      wasRunning: false,
      wasQueued: true,
      wasStopTarget: false,
    })
    expect(ownership.runningTaskId.value).toBe('task-A')
    expect(ownership.queuedTaskIds.value.size).toBe(0)
    expect(ownership.hasAuthoritativeWork.value).toBe(true)
  })

  it('keeps delivery blocked until deferred hydration resolves', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-stale')

    ownership.beginHydration()
    ownership.applySnapshot({ run_status: 'idle', active_task: null }, false)

    expect(ownership.hydrationResolved.value).toBe(false)
    expect(ownership.runningTaskId.value).toBe('task-stale')
    expect(ownership.hasAuthoritativeWork.value).toBe(true)

    ownership.applySnapshot({
      run_status: 'running',
      active_task: { task_id: 'task-live', status: 'running' },
      tasks: [
        { task_id: 'task-newest', status: 'queued' },
        { task_id: 'task-live', status: 'running' },
        { task_id: 'task-oldest', status: 'queued' },
      ],
    } as never, true)

    expect(ownership.hydrationResolved.value).toBe(true)
    expect(ownership.runningTaskId.value).toBe('task-live')
    expect([...ownership.queuedTaskIds.value]).toEqual(['task-newest', 'task-oldest'])
  })

  it('restores stopping from an additive active-task snapshot', () => {
    const ownership = useChatTaskOwnership(false)

    ownership.applySnapshot({
      run_status: 'running',
      active_task: {
        task_id: 'task-stopping',
        status: 'running',
        cancel_requested: true,
      },
      tasks: [{ task_id: 'task-stopping', status: 'running', cancel_requested: true }],
    } as never, true)

    expect(ownership.runningTaskId.value).toBe('task-stopping')
    expect(ownership.stopRequestedTaskId.value).toBe('task-stopping')

    ownership.applySnapshot({
      run_status: 'cancelled',
      active_task: null,
      last_task: { task_id: 'task-stopping', status: 'cancelled' },
      tasks: [{ task_id: 'task-stopping', status: 'cancelled' }],
    } as never, true)
    expect(ownership.stopRequestedTaskId.value).toBe('')
  })

  it('uses the authoritative queued foreground first after reconnect', () => {
    const ownership = useChatTaskOwnership(false)

    ownership.applySnapshot({
      run_status: 'queued',
      active_task: { task_id: 'task-oldest', status: 'queued' },
      // Gateway hydration returns task rows newest-first. active_task carries
      // TaskRuntime's FIFO foreground and must therefore stay first.
      tasks: [
        { task_id: 'task-newest', status: 'queued' },
        { task_id: 'task-oldest', status: 'queued' },
      ],
    } as never, true)

    expect([...ownership.queuedTaskIds.value]).toEqual(['task-oldest', 'task-newest'])
    expect(ownership.stopTargetTaskId.value).toBe('task-oldest')
    expect(ownership.beginStop()).toBe('task-oldest')
  })

  it('treats an old-Gateway statusless ACK as queued without stealing A', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-A')

    const accepted = ownership.noteAccepted('task-B')

    expect(accepted.claimRender).toBe(false)
    expect(ownership.runningTaskId.value).toBe('task-A')
    expect([...ownership.queuedTaskIds.value]).toEqual(['task-B'])
  })

  it('does not restore a task that terminal history already settled', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteTerminal('task-settled')

    ownership.applySnapshot({
      run_status: 'running',
      active_task: { task_id: 'task-settled', status: 'running' },
      tasks: [{ task_id: 'task-settled', status: 'running' }],
    } as never, true)

    expect(ownership.isSettled('task-settled')).toBe(true)
    expect(ownership.runningTaskId.value).toBe('')
    expect(ownership.hasAuthoritativeWork.value).toBe(false)
  })

  it.each(['idle', 'failed', 'timeout', 'cancelled', 'interrupted'])(
    'releases a stopped task missing from a complete %s snapshot so its successor can be stopped',
    runStatus => {
      const ownership = useChatTaskOwnership()
      ownership.noteRunning('task-A')
      ownership.beginStop()
      ownership.noteRunning('task-B')

      const revision = ownership.captureSnapshotRevision()
      expect(ownership.applySnapshot({
        run_status: runStatus, active_task: null, last_task: null,
      }, true, revision)).toBe(true)

      expect(ownership.stopRequestedTaskId.value).toBe('')
      expect(ownership.isSettled('task-A')).toBe(true)
      expect(ownership.noteRunning('task-C')).toBe(true)
      expect(ownership.stopTargetTaskId.value).toBe('task-C')
      expect(ownership.beginStop()).toBe('task-C')
    },
  )

  it.each([
    { run_status: 'idle' },
    { active_task: null, last_task: null },
    { run_status: 'idle', active_task: null },
    { run_status: 'failed', last_task: null },
    { run_status: 'cancelled', active_task: null },
    { run_status: 'unexpected', active_task: null, last_task: null },
    { run_status: 'running', active_task: null, last_task: null },
  ])('does not release Stop from incomplete, unknown, or live metadata: %j', (snapshot) => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-A')
    ownership.beginStop()

    ownership.applySnapshot(snapshot, true)

    expect(ownership.stopRequestedTaskId.value).toBe('task-A')
    expect(ownership.isSettled('task-A')).toBe(false)
  })

  it.each(['idle', 'failed', 'timeout', 'cancelled', 'interrupted'])(
    'keeps the old Stop target when %s metadata still contains running or queued work', runStatus => {
      const ownership = useChatTaskOwnership()
      ownership.noteRunning('task-A')
      ownership.beginStop()
      ownership.applySnapshot({
        run_status: 'running',
        active_task: { task_id: 'task-B', status: 'running' },
        last_task: null,
      }, true)
      expect(ownership.stopRequestedTaskId.value).toBe('task-A')
      expect(ownership.runningTaskId.value).toBe('task-B')

      ownership.applySnapshot({
        run_status: runStatus, active_task: null, last_task: null,
        queued_task_ids: ['task-C'],
      } as never, true)
      expect(ownership.stopRequestedTaskId.value).toBe('task-A')

      ownership.applySnapshot({
        run_status: runStatus, active_task: null, last_task: null,
        tasks: [{ task_id: 'task-B', status: 'running' }],
      } as never, true)
      expect(ownership.stopRequestedTaskId.value).toBe('task-A')
      expect(ownership.runningTaskId.value).toBe('task-B')
    },
  )

  it('keeps Stop through deferred failed hydration until a complete current read arrives', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-A')
    ownership.beginStop()
    const snapshot = { run_status: 'failed', active_task: null, last_task: null }
    expect(ownership.applySnapshot(snapshot, false, ownership.captureSnapshotRevision())).toBe(false)
    expect(ownership.stopRequestedTaskId.value).toBe('task-A')
    expect(ownership.runningTaskId.value).toBe('task-A')
    expect(ownership.hydrationResolved.value).toBe(false)
    expect(ownership.applySnapshot(snapshot, true, ownership.captureSnapshotRevision())).toBe(true)
    expect(ownership.stopRequestedTaskId.value).toBe('')
  })

  it.each(['idle', 'failed', 'timeout', 'cancelled', 'interrupted'])(
    'rejects a previously started %s read after a successor or newer Stop has been observed', runStatus => {
      const ownership = useChatTaskOwnership()
      ownership.noteRunning('task-A')
      ownership.beginStop()
      const revision = ownership.captureSnapshotRevision()
      ownership.noteTerminal('task-A')
      ownership.noteRunning('task-C')
      ownership.beginStop()

      expect(ownership.applySnapshot({
        run_status: runStatus, active_task: null, last_task: null,
      }, true, revision)).toBe(false)
      expect(ownership.runningTaskId.value).toBe('task-C')
      expect(ownership.stopRequestedTaskId.value).toBe('task-C')
      expect(ownership.isSettled('task-C')).toBe(false)
    },
  )

  it('does not demote the current task on a malformed statusless snapshot', () => {
    const ownership = useChatTaskOwnership()
    ownership.noteRunning('task-A')
    ownership.beginStop()

    expect(ownership.applySnapshot({ active_task: null, last_task: null }, true)).toBe(false)
    expect(ownership.runningTaskId.value).toBe('task-A')
    expect(ownership.stopRequestedTaskId.value).toBe('task-A')
  })
})
