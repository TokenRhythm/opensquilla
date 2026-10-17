import { effectScope, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import { useStableStopControl } from './useStableStopControl'

const scopes: ReturnType<typeof effectScope>[] = []
afterEach(() => scopes.splice(0).forEach(scope => scope.stop()))

function harness() {
  const state = reactive({ identity: 'gateway/user/A/1', taskId: 'task-a', canStop: true,
    hydrating: false, terminal: false })
  const scope = effectScope()
  scopes.push(scope)
  const control = scope.run(() => useStableStopControl({
    identity: () => state.identity, taskId: () => state.taskId,
    canStop: () => state.canStop, hydrating: () => state.hydrating,
    terminal: () => state.terminal,
  }))!
  return { state, ...control }
}

describe('useStableStopControl', () => {
  it('retains the same task appearance through live and metadata recovery without changing permission', async () => {
    const h = harness()
    h.state.hydrating = true
    h.state.canStop = false
    expect(h.showStop.value).toBe(true)
    expect(h.state.canStop).toBe(false)
    await nextTick()
    // Reconstructing the current task from a snapshot may briefly remove its ID.
    h.state.taskId = ''
    await nextTick()
    expect(h.showStop.value).toBe(true)
    h.state.taskId = 'task-a'
    h.state.hydrating = false
    h.state.canStop = true
    await nextTick()
    expect(h.showStop.value).toBe(true)
  })

  it('does not turn an unknown cold read into a stoppable-looking task', async () => {
    const h = harness()
    h.state.identity = 'gateway/user/B/1'
    h.state.taskId = ''
    h.state.hydrating = true
    h.state.canStop = false
    expect(h.showStop.value).toBe(false)
    await nextTick()
    h.state.taskId = 'task-b'
    await nextTick()
    expect(h.showStop.value).toBe(false)
  })

  it.each(['gateway/user/B/1', 'other-gateway/user/A/1', 'gateway/other-user/A/1', 'gateway/user/A/2', ''])(
    'retires presentation when its scope changes to %s', async identity => {
      const h = harness()
      h.state.identity = identity
      h.state.hydrating = true
      h.state.canStop = false
      expect(h.showStop.value).toBe(false)
      await nextTick()
      h.state.identity = 'gateway/user/A/1'
      await nextTick()
      expect(h.showStop.value).toBe(false)
    },
  )

  it('does not attach an outgoing task to a new session during synchronous reset ordering', async () => {
    const h = harness()
    h.state.identity = 'gateway/user/B/1'
    // This is the real switch order: key changes before runtime ownership resets.
    h.state.canStop = false
    h.state.taskId = ''
    h.state.hydrating = true
    await nextTick()
    h.state.taskId = 'task-a'
    await nextTick()
    expect(h.showStop.value).toBe(false)
  })

  it('does not lend task A appearance to an unconfirmed successor B', async () => {
    const h = harness()
    h.state.hydrating = true
    h.state.canStop = false
    h.state.taskId = 'task-b'
    expect(h.showStop.value).toBe(false)
    await nextTick()
    h.state.taskId = ''
    await nextTick()
    expect(h.showStop.value).toBe(false)
  })

  it('clears an explicit terminal immediately, even while metadata is pending', async () => {
    const h = harness()
    h.state.hydrating = true
    h.state.canStop = false
    h.state.terminal = true
    expect(h.showStop.value).toBe(false)
    // A successor read can reset the transient terminal flag in the same tick.
    h.state.terminal = false
    h.state.taskId = ''
    await nextTick()
    expect(h.showStop.value).toBe(false)
  })

  it('clears the retained shape when authoritative recovery says there is no stoppable task', async () => {
    const h = harness()
    h.state.hydrating = true
    h.state.canStop = false
    await nextTick()
    expect(h.showStop.value).toBe(true)
    h.state.hydrating = false
    h.state.taskId = ''
    expect(h.showStop.value).toBe(false)
    await nextTick()
    h.state.hydrating = true
    await nextTick()
    expect(h.showStop.value).toBe(false)
  })
})
