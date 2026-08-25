import { describe, expect, it } from 'vitest'
import { useChatTaskOwnership } from './useChatTaskOwnership'

describe('useChatTaskOwnership', () => {
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
})
