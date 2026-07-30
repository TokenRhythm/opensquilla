import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatPlans } from './useChatPlans'

const SESSION_ONE = 'agent:main:webchat:one'
const SESSION_TWO = 'agent:main:webchat:two'

function deferred<T = unknown>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, reject, resolve }
}

function revision(
  revisionId = 'revision-2',
  overrides: Record<string, unknown> = {},
) {
  return {
    revisionId,
    planId: 'plan-1',
    generation: 2,
    title: 'Ship plan mode',
    markdown: 'A complete plan.',
    steps: [{ stepId: 'inspect', title: 'Inspect' }],
    current: true,
    createdAt: 200,
    ...overrides,
  }
}

function run(
  status = 'running',
  overrides: Record<string, unknown> = {},
) {
  return {
    runId: 'run-1',
    planRevisionId: 'revision-2',
    status,
    currentStepId: 'inspect',
    stateRevision: 3,
    createdAt: 300,
    updatedAt: 303,
    steps: [{ stepId: 'inspect', title: 'Inspect', status: 'in_progress' }],
    ...overrides,
  }
}

function harness({ draft = false }: { draft?: boolean } = {}) {
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const rpc = {
    call: vi.fn(),
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      handlers.set(event, handler)
      return vi.fn()
    }),
  }
  const sessionKey = ref(SESSION_ONE)
  const currentEpoch = ref(0)
  const isStreaming = ref(false)
  const inputText = ref('')
  const switchToSession = vi.fn()
  const notifyError = vi.fn()
  const onMutationAccepted = vi.fn()
  const api = useChatPlans({
    rpc,
    sessionKey,
    currentEpoch,
    isStreaming,
    inputText,
    createSessionKey: () => SESSION_TWO,
    agentId: () => 'main',
    switchToSession,
    focusComposer: vi.fn(),
    notifyError,
    onMutationAccepted,
    isDraft: () => draft,
  })
  return {
    api,
    handlers,
    rpc,
    sessionKey,
    currentEpoch,
    isStreaming,
    inputText,
    switchToSession,
    notifyError,
    onMutationAccepted,
  }
}

describe('useChatPlans', () => {
  it('hydrates collaboration, current revision, and active run from bootstrap', () => {
    const { api } = harness()

    api.applyBootstrap({
      key: SESSION_ONE,
      collaboration: { mode: 'plan', revision: 4 },
      currentPlan: revision(),
      activePlanRun: run() as never,
    })

    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 4 })
    expect(api.currentPlan.value?.revisionId).toBe('revision-2')
    expect(api.activePlanRun.value?.runId).toBe('run-1')
  })

  it('consumes wrapped plan and collaboration stream events for only this session', () => {
    const { api, handlers } = harness()
    api.subscribe()

    handlers.get('session.event.plan_revision')?.({
      session_key: SESSION_ONE,
      plan_revision: revision('revision-3'),
      collaboration: { mode: 'plan', revision: 5 },
    })
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', { planRevisionId: 'revision-3' }),
    })
    handlers.get('session.event.collaboration_mode')?.({
      session_key: SESSION_ONE,
      collaboration: { mode: 'plan', revision: 6 },
    })
    handlers.get('session.event.plan_revision')?.({
      session_key: SESSION_TWO,
      plan_revision: revision('wrong-session'),
    })

    expect(api.currentPlan.value?.revisionId).toBe('revision-3')
    expect(api.activePlanRun.value?.runId).toBe('run-1')
    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 6 })
  })

  it('does not let an older plan generation or collaboration revision replace current state', () => {
    const { api, handlers } = harness()
    api.subscribe()

    handlers.get('session.event.plan_revision')?.({
      session_key: SESSION_ONE,
      plan_revision: revision('revision-3', {
        generation: 3,
        parentRevisionId: 'revision-2',
        createdAt: 300,
      }),
      collaboration: { mode: 'plan', revision: 7 },
    })
    handlers.get('session.event.plan_revision')?.({
      session_key: SESSION_ONE,
      plan_revision: revision('revision-2', {
        generation: 2,
        createdAt: 200,
      }),
      collaboration: { mode: 'default', revision: 6 },
    })
    // Even a payload claiming a later generation is stale when its enclosing
    // collaboration CAS revision predates the accepted pointer.
    handlers.get('session.event.plan_revision')?.({
      session_key: SESSION_ONE,
      plan_revision: revision('revision-4', {
        generation: 4,
        parentRevisionId: 'revision-3',
        createdAt: 400,
      }),
      collaboration: { mode: 'default', revision: 6 },
    })
    handlers.get('session.event.collaboration_mode')?.({
      session_key: SESSION_ONE,
      collaboration: { mode: 'default', revision: 5 },
    })

    expect(api.currentPlan.value).toMatchObject({
      revisionId: 'revision-3',
      generation: 3,
    })
    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 7 })
  })

  it('only adopts runs for the current plan revision', () => {
    const { api, handlers } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      currentPlan: revision(),
    })
    api.subscribe()

    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', {
        runId: 'wrong-plan-run',
        planRevisionId: 'revision-1',
      }),
    })

    expect(api.activePlanRun.value).toBeNull()

    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run(),
    })

    expect(api.activePlanRun.value?.runId).toBe('run-1')
  })

  it('rejects lower state revisions and cannot resurrect a terminal run', () => {
    const { api, handlers } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      currentPlan: revision(),
    })
    api.subscribe()

    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', { stateRevision: 5, updatedAt: 305 }),
    })
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('paused', { stateRevision: 4, updatedAt: 304 }),
    })
    expect(api.activePlanRun.value).toMatchObject({
      status: 'running',
      stateRevision: 5,
    })

    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('completed', {
        stateRevision: 6,
        updatedAt: 306,
        terminalReason: 'all_steps_completed',
      }),
    })
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', { stateRevision: 7, updatedAt: 307 }),
    })

    expect(api.activePlanRun.value).toMatchObject({
      status: 'completed',
      stateRevision: 6,
      terminalReason: 'all_steps_completed',
    })
  })

  it('uses the server-disambiguated creation order without inventing UUID order', () => {
    const { api, handlers } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      currentPlan: revision(),
      activePlanRun: run('paused', {
        runId: 'run-old',
        stateRevision: 8,
        createdAt: 500,
        updatedAt: 500,
      }) as never,
    })
    api.subscribe()

    // A legacy/ambiguous equal timestamp is not enough to infer ordering.
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', {
        runId: 'run-ambiguous',
        stateRevision: 1,
        createdAt: 500,
        updatedAt: 500,
      }),
    })
    expect(api.activePlanRun.value?.runId).toBe('run-old')

    // The storage contract serializes same-millisecond starts as 500 -> 501.
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('running', {
        runId: 'run-new',
        stateRevision: 1,
        createdAt: 501,
        updatedAt: 501,
      }),
    })
    handlers.get('session.event.plan_run')?.({
      session_key: SESSION_ONE,
      plan_run: run('completed', {
        runId: 'run-old',
        stateRevision: 99,
        createdAt: 500,
        updatedAt: 599,
      }),
    })

    expect(api.activePlanRun.value).toMatchObject({
      runId: 'run-new',
      status: 'running',
      stateRevision: 1,
    })
  })

  it('clears old pointers synchronously before hydrating a newly selected task', async () => {
    const { api, sessionKey } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      currentPlan: revision(),
      activePlanRun: run() as never,
    })

    sessionKey.value = SESSION_TWO
    expect(api.currentPlan.value).toBeNull()
    expect(api.activePlanRun.value).toBeNull()

    api.applyBootstrap({
      key: SESSION_TWO,
      currentPlan: revision('revision-new'),
    })
    await nextTick()

    expect(api.currentPlan.value?.revisionId).toBe('revision-new')
  })

  it('clears plan state before applying a revision-zero bootstrap from a newer epoch', () => {
    const { api, currentEpoch } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 3,
      collaboration: { mode: 'plan', revision: 4 },
      currentPlan: revision(),
      activePlanRun: run() as never,
    })

    // The epoch event may arrive before the authoritative subscribe snapshot.
    currentEpoch.value = 4
    expect(api.currentPlan.value).toBeNull()
    expect(api.activePlanRun.value).toBeNull()

    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 4,
      collaboration: { mode: 'default', revision: 0 },
      currentPlan: null,
      activePlanRun: null,
    })

    expect(api.collaboration.value).toEqual({ mode: 'default', revision: 0 })
    expect(api.currentPlan.value).toBeNull()
    expect(api.activePlanRun.value).toBeNull()

    // A delayed snapshot from the archived epoch cannot restore its plan.
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 3,
      collaboration: { mode: 'plan', revision: 5 },
      currentPlan: revision('stale-revision', { generation: 5 }),
      activePlanRun: run() as never,
    })
    expect(api.collaboration.value).toEqual({ mode: 'default', revision: 0 })
    expect(api.currentPlan.value).toBeNull()
  })

  it('recognizes a reset from the subscribe snapshot when the epoch event was missed', () => {
    const { api, currentEpoch } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 7,
      collaboration: { mode: 'plan', revision: 9 },
      currentPlan: revision(),
      activePlanRun: run() as never,
    })

    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 8,
      collaboration: { mode: 'default', revision: 0 },
      currentPlan: null,
      activePlanRun: null,
    })

    expect(currentEpoch.value).toBe(8)
    expect(api.collaboration.value).toEqual({ mode: 'default', revision: 0 })
    expect(api.currentPlan.value).toBeNull()
    expect(api.activePlanRun.value).toBeNull()
  })

  it('keeps a draft mode selection local and exposes it for the atomic first send', async () => {
    const { api, rpc } = harness({ draft: true })

    await expect(api.setMode('plan')).resolves.toBe(true)

    expect(rpc.call).not.toHaveBeenCalled()
    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 0 })
    expect(api.initialCollaborationMode.value).toBe('plan')

    await expect(api.setMode('default')).resolves.toBe(true)

    expect(rpc.call).not.toHaveBeenCalled()
    expect(api.collaboration.value).toEqual({ mode: 'default', revision: 0 })
    expect(api.initialCollaborationMode.value).toBe('default')
  })

  it('marks a busy-turn mode change as applying to the next turn', async () => {
    const { api, rpc, isStreaming } = harness()
    isStreaming.value = true
    rpc.call.mockResolvedValue({
      collaboration: { mode: 'plan', revision: 1 },
    })

    await api.setMode('plan')

    expect(rpc.call).toHaveBeenCalledWith('plans.setMode', {
      sessionKey: SESSION_ONE,
      mode: 'plan',
      expectedRevision: 0,
    })
    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 1 })
    expect(api.modeAppliesNextTurn.value).toBe(true)

    isStreaming.value = false
    await nextTick()
    expect(api.modeAppliesNextTurn.value).toBe(false)
  })

  it('treats repeated Plan activation as an idempotent enter operation', async () => {
    const { api, rpc } = harness()
    rpc.call.mockResolvedValue({
      collaboration: { mode: 'plan', revision: 1 },
    })

    await expect(api.setMode('plan')).resolves.toBe(true)
    await expect(api.setMode('plan')).resolves.toBe(true)

    expect(rpc.call).toHaveBeenCalledTimes(1)
    expect(api.collaboration.value).toEqual({ mode: 'plan', revision: 1 })
  })

  it('does not leave a stale next-turn notice when the active turn settles during the RPC', async () => {
    const { api, rpc, isStreaming } = harness()
    let resolveMode!: (value: unknown) => void
    rpc.call.mockImplementation(() => new Promise(resolve => { resolveMode = resolve }))
    isStreaming.value = true

    const pending = api.setMode('plan')
    isStreaming.value = false
    await nextTick()
    resolveMode({ collaboration: { mode: 'plan', revision: 1 } })
    await pending

    expect(api.modeAppliesNextTurn.value).toBe(false)
  })

  it('does not let an older mode mutation response overwrite a newer event', async () => {
    const { api, handlers, rpc } = harness()
    let resolveMode!: (value: unknown) => void
    rpc.call.mockImplementation(() => new Promise(resolve => { resolveMode = resolve }))
    api.subscribe()

    const pending = api.setMode('plan')
    handlers.get('session.event.collaboration_mode')?.({
      session_key: SESSION_ONE,
      collaboration: { mode: 'default', revision: 2 },
    })
    resolveMode({ collaboration: { mode: 'plan', revision: 1 } })
    await pending

    expect(api.collaboration.value).toEqual({ mode: 'default', revision: 2 })
    expect(api.modeAppliesNextTurn.value).toBe(false)
  })

  it('does not let a stale epoch mode request unlock or report into a newer request', async () => {
    const { api, currentEpoch, notifyError, rpc } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 1,
      collaboration: { mode: 'default', revision: 0 },
    })
    const oldRequest = deferred()
    const newRequest = deferred()
    rpc.call
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)

    const oldMutation = api.setMode('plan')
    currentEpoch.value = 2
    const newMutation = api.setMode('plan')

    oldRequest.reject(new Error('stale epoch failure'))
    await oldMutation
    expect(notifyError).not.toHaveBeenCalled()
    expect(api.modeBusy.value).toBe(true)

    newRequest.resolve({ collaboration: { mode: 'plan', revision: 1 } })
    await newMutation
    expect(api.modeBusy.value).toBe(false)
  })

  it('serializes collaboration mutations so mode and run actions cannot race', async () => {
    const { api, rpc } = harness()
    let resolveMode!: (value: unknown) => void
    rpc.call.mockImplementation(() => new Promise(resolve => { resolveMode = resolve }))

    const pendingMode = api.setMode('plan')
    await api.implement({ planId: 'plan-1', revisionId: 'revision-2' }, false)

    expect(rpc.call).toHaveBeenCalledTimes(1)
    resolveMode({ collaboration: { mode: 'plan', revision: 1 } })
    await pendingMode
  })

  it('implements in the current task and adopts the returned run snapshot', async () => {
    const { api, rpc, onMutationAccepted } = harness()
    rpc.call.mockResolvedValue({
      planRevision: revision(),
      planRun: run(),
    })

    await api.implement({ planId: 'plan-1', revisionId: 'revision-2' }, false)

    expect(rpc.call).toHaveBeenCalledWith('plans.implement', {
      sessionKey: SESSION_ONE,
      planRevisionId: 'revision-2',
      clientRequestId: expect.any(String),
    })
    expect(api.activePlanRun.value?.runId).toBe('run-1')
    expect(onMutationAccepted).toHaveBeenCalledOnce()
  })

  it('implements in a newly generated task before switching the visible session', async () => {
    const { api, rpc, switchToSession } = harness()
    rpc.call.mockResolvedValue({ sessionKey: SESSION_TWO })

    await api.implement({ planId: 'plan-1', revisionId: 'revision-2' }, true)

    expect(rpc.call).toHaveBeenCalledWith('plans.implement', {
      sessionKey: SESSION_TWO,
      planRevisionId: 'revision-2',
      clientRequestId: expect.any(String),
      intent: 'new_chat',
    })
    expect(switchToSession).toHaveBeenCalledWith(SESSION_TWO)
  })

  it('keeps a newer epoch implement locked when the old implement returns late', async () => {
    const { api, currentEpoch, rpc } = harness()
    api.applyBootstrap({ key: SESSION_ONE, epoch: 1 })
    const oldRequest = deferred()
    const newRequest = deferred()
    rpc.call
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)
    const target = { planId: 'plan-1', revisionId: 'revision-2' }

    const oldMutation = api.implement(target, false)
    currentEpoch.value = 2
    const newMutation = api.implement(target, false)
    oldRequest.resolve({})
    await oldMutation

    expect(api.pendingAction.value).toBe('implement-current')
    newRequest.resolve({})
    await newMutation
    expect(api.pendingAction.value).toBeNull()
  })

  it('uses the composer draft for replan and clears it only after acceptance', async () => {
    const { api, rpc, onMutationAccepted } = harness()
    const target = { planId: 'plan-1', revisionId: 'revision-2' }
    api.beginReplan(target)
    rpc.call.mockResolvedValue({
      collaboration: { mode: 'plan', revision: 1 },
    })

    const accepted = await api.revise({ ...target, prompt: 'Keep the API compatible.' })

    expect(accepted).toBe(true)
    expect(rpc.call).toHaveBeenCalledWith('plans.revise', {
      sessionKey: SESSION_ONE,
      planRevisionId: 'revision-2',
      prompt: 'Keep the API compatible.',
      clientRequestId: expect.any(String),
    })
    expect(api.replanActive.value).toBe(false)
    expect(api.collaboration.value.mode).toBe('plan')
    expect(onMutationAccepted).toHaveBeenCalledOnce()
  })

  it('keeps a newer epoch replan locked when the old replan returns late', async () => {
    const { api, currentEpoch, rpc } = harness()
    api.applyBootstrap({ key: SESSION_ONE, epoch: 1 })
    const oldRequest = deferred()
    const newRequest = deferred()
    rpc.call
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)
    const target = {
      planId: 'plan-1',
      revisionId: 'revision-2',
      prompt: 'Keep compatibility.',
    }

    const oldMutation = api.revise(target)
    currentEpoch.value = 2
    const newMutation = api.revise(target)
    oldRequest.resolve({ collaboration: { mode: 'plan', revision: 1 } })
    await oldMutation

    expect(api.pendingAction.value).toBe('revise')
    newRequest.resolve({ collaboration: { mode: 'plan', revision: 1 } })
    await newMutation
    expect(api.pendingAction.value).toBeNull()
  })

  it('cancels the authoritative run with its state revision', async () => {
    const { api, rpc } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      currentPlan: revision(),
      activePlanRun: run() as never,
    })
    rpc.call.mockResolvedValue({
      planRun: run('cancelled'),
    })

    await api.cancelRun()

    expect(rpc.call).toHaveBeenCalledWith('plans.cancelRun', {
      sessionKey: SESSION_ONE,
      runId: 'run-1',
      expectedStateRevision: 3,
    })
    expect(api.activePlanRun.value?.status).toBe('cancelled')
  })

  it('keeps a newer epoch cancellation locked when the old cancellation returns late', async () => {
    const { api, currentEpoch, rpc } = harness()
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 1,
      currentPlan: revision(),
      activePlanRun: run() as never,
    })
    const oldRequest = deferred()
    const newRequest = deferred()
    rpc.call
      .mockImplementationOnce(() => oldRequest.promise)
      .mockImplementationOnce(() => newRequest.promise)

    const oldMutation = api.cancelRun()
    currentEpoch.value = 2
    api.applyBootstrap({
      key: SESSION_ONE,
      epoch: 2,
      currentPlan: revision(),
      activePlanRun: run() as never,
    })
    const newMutation = api.cancelRun()
    oldRequest.resolve({ planRun: run('cancelled') })
    await oldMutation

    expect(api.pendingAction.value).toBe('cancel-run')
    newRequest.resolve({ planRun: run('cancelled') })
    await newMutation
    expect(api.pendingAction.value).toBeNull()
  })
})
