import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useMetaRuns } from './useMetaRuns'
import { counterText, ribbonCopy } from '@/utils/chat/metaRibbon'

type RpcCall = (
  method: string,
  params?: Record<string, unknown>,
) => Promise<unknown>

function makeOptions(call: RpcCall) {
  const sendComposerText = vi.fn()
  const sendHiddenReplay = vi.fn()
  const sendHiddenConfirmation = vi.fn()
  const pushToast = vi.fn()
  const sessionKey = ref('agent:main:replay-session')
  const handlers = new Map<string, (...args: unknown[]) => void>()
  const api = useMetaRuns({
    rpc: {
      call: <T = unknown>(
        method: string,
        params?: Record<string, unknown>,
      ): Promise<T> => call(method, params) as Promise<T>,
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        handlers.set(event, handler)
        return () => handlers.delete(event)
      }),
    },
    sessionKey,
    currentEpoch: ref(1),
    lastStreamSeq: ref(0),
    sendHiddenConfirmation,
    sendHiddenReplay,
    scrollToStepCard: vi.fn(),
    sendComposerText,
    lastUserMessageText: () => 'original request',
    pushToast,
  })
  return {
    api,
    handlers,
    sendComposerText,
    sendHiddenConfirmation,
    sendHiddenReplay,
    pushToast,
    sessionKey,
  }
}

function recoveryPayload(runId: string) {
  return {
    recovery: {
      run_id: runId,
      announced: {
        run_id: runId,
        meta_skill_name: 'meta-paper-write',
        language: 'en',
        steps: [
          { id: 'draft', label: 'Draft', kind: 'agent', depends_on: [] },
          {
            id: 'publication_quality_gate',
            label: 'Publication quality gate',
            kind: 'skill_exec',
            depends_on: ['draft'],
          },
        ],
        total: 2,
      },
      step_states: [
        { run_id: runId, step_id: 'draft', state: 'succeeded' },
        {
          run_id: runId,
          step_id: 'publication_quality_gate',
          state: 'failed',
          error: 'PDF length gate rejected the one-page artifact',
          rescue: {
            actions: [{ id: 'retry-step', label: 'Retry failed step' }],
          },
        },
      ],
      completed: {
        run_id: runId,
        outcome: 'failed',
        completed_steps: ['draft'],
        failed_steps: ['publication_quality_gate'],
        recovered_steps: [],
        skipped_steps: [],
      },
    },
  }
}

function paper36RecoveryPayload() {
  const skippedIds = new Set(['paper_clarify', 'final_manuscript_package'])
  const stepIds = Array.from({ length: 36 }, (_, index) => {
    let id = `paper_step_${index + 1}`
    if (index === 1) id = 'paper_clarify'
    if (index === 26) id = 'final_manuscript_package'
    if (index === 30) id = 'publication_quality_gate'
    return id
  })
  const steps = stepIds.map((id, index) => ({
    id,
    label: id,
    kind: 'agent',
    depends_on: index ? [stepIds[index - 1]] : [],
  }))
  const completedSteps = steps
    .slice(0, 30)
    .filter((step) => !skippedIds.has(step.id))
    .map((step) => step.id)
  return {
    recovery: {
      announced: {
        run_id: 'paper-36-run',
        meta_skill_name: 'meta-paper-write',
        language: 'en',
        steps,
        total: 36,
      },
      step_states: steps.map((step, index) => ({
        run_id: 'paper-36-run',
        step_id: step.id,
        state: skippedIds.has(step.id)
          ? 'skipped'
          : index < 30
            ? 'succeeded'
            : index === 30
              ? 'failed'
              : 'pending',
        rescue: index === 30
          ? { actions: [{ id: 'retry-step', label: 'Retry failed step' }] }
          : {},
      })),
      completed: {
        run_id: 'paper-36-run',
        outcome: 'failed',
        completed_steps: completedSteps,
        failed_steps: ['publication_quality_gate'],
        recovered_steps: [],
        skipped_steps: [...skippedIds],
      },
    },
  }
}

describe('useMetaRuns replay actions', () => {
  it('retries the selected run with the exact canonical command from the ledger', async () => {
    const canonicalMessage = '/meta meta-paper-write -- Write a cited paper\nfor reviewers.'
    const call = vi.fn(async () => ({ replay: { message: canonicalMessage } }))
    const { api, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-run',
      stepId: null,
      runId: 'run-source',
    })

    expect(call).toHaveBeenCalledOnce()
    expect(call).toHaveBeenCalledWith('meta.runs.replay', {
      sessionKey: 'agent:main:replay-session',
      runId: 'run-source',
      run_id: 'run-source',
      mode: 'run',
    })
    expect(sendComposerText).toHaveBeenCalledWith(canonicalMessage)
    expect(sendHiddenReplay).not.toHaveBeenCalled()
  })

  it.each([
    ['missing', { replay: {} }],
    ['invalid', { replay: { message: 'a newer, unrelated visible message' } }],
  ])('does not send when the selected run replay command is %s', async (_label, response) => {
    const call = vi.fn(async () => response)
    const { api, sendComposerText, pushToast } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-run',
      stepId: null,
      runId: 'run-source',
    })

    expect(sendComposerText).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledTimes(1)
    expect(pushToast.mock.calls[0]?.[0]).toEqual(expect.any(String))
    expect(pushToast.mock.calls[0]?.[1]).toEqual({ tone: 'danger' })
  })

  it('prepares and commits a live replay before dispatching the token-free sentinel', async () => {
    const replayToken = 'server-only-ticket'
    const replayLaunchText = '/meta-replay 0123456789abcdef0123456789abcdef'
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      if (!params?.replayToken) {
        return {
          replay: {
            message: '/meta meta-paper-write -- Write a cited paper',
            live_replay: { available: true, replay_token: replayToken },
          },
        }
      }
      return {
        replay: {
          launch_text: replayLaunchText,
          display_text: 'Retry failed step · meta-paper-write',
          live_replay: { available: true, committed: true },
        },
      }
    })
    const { api, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-step',
      stepId: 'compile_pdf',
      runId: 'run-source',
    })

    expect(call).toHaveBeenCalledTimes(2)
    expect(call).toHaveBeenNthCalledWith(1, 'meta.runs.replay', expect.objectContaining({
      sessionKey: 'agent:main:replay-session',
      runId: 'run-source',
      mode: 'failed-step',
      prepareLive: true,
    }))
    expect(call).toHaveBeenNthCalledWith(2, 'meta.runs.replay', expect.objectContaining({
      sessionKey: 'agent:main:replay-session',
      runId: 'run-source',
      mode: 'failed-step',
      replayToken,
    }))
    expect(sendHiddenReplay).toHaveBeenCalledWith(
      replayLaunchText,
      'Retry failed step · meta-paper-write',
    )
    expect(JSON.stringify(sendHiddenReplay.mock.calls)).not.toContain(replayToken)
    expect(sendComposerText).not.toHaveBeenCalled()
  })

  it('does not send a retry-run result into a session selected during the RPC', async () => {
    let resolveReplay: ((value: unknown) => void) | undefined
    const call = vi.fn(() => new Promise((resolve) => { resolveReplay = resolve }))
    const { api, sendComposerText, sessionKey } = makeOptions(call)

    const action = api.onRibbonAction({
      action: 'retry-run',
      stepId: null,
      runId: 'run-source',
    })
    sessionKey.value = 'agent:main:other-session'
    await nextTick()
    resolveReplay?.({ replay: { message: '/meta meta-paper-write -- original' } })
    await action

    expect(call).toHaveBeenCalledWith('meta.runs.replay', expect.objectContaining({
      sessionKey: 'agent:main:replay-session',
    }))
    expect(sendComposerText).not.toHaveBeenCalled()
  })

  it('does not dispatch a live replay committed after the user switches sessions', async () => {
    let resolveCommit: ((value: unknown) => void) | undefined
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      if (!params?.replayToken) {
        return { replay: { live_replay: { replay_token: 'server-only-ticket' } } }
      }
      return new Promise((resolve) => { resolveCommit = resolve })
    })
    const { api, sendComposerText, sendHiddenReplay, sessionKey } = makeOptions(call)

    const action = api.onRibbonAction({
      action: 'retry-step',
      stepId: 'compile_pdf',
      runId: 'run-source',
    })
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(2))
    sessionKey.value = 'agent:main:other-session'
    await nextTick()
    resolveCommit?.({
      replay: {
        launch_text: '/meta-replay 0123456789abcdef0123456789abcdef',
        display_text: 'Retry failed step',
        live_replay: { committed: true },
      },
    })
    await action

    expect(call).toHaveBeenNthCalledWith(2, 'meta.runs.replay', expect.objectContaining({
      sessionKey: 'agent:main:replay-session',
    }))
    expect(sendHiddenReplay).not.toHaveBeenCalled()
    expect(sendComposerText).not.toHaveBeenCalled()
  })

  it('uses the canonical /meta fallback returned by an older gateway', async () => {
    const call = vi.fn(async () => ({
      replay: {
        message: '/meta meta-paper-write -- Write a cited paper',
        replay_kind: 'draft',
        live_replay: { available: false },
      },
    }))
    const { api, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-step',
      stepId: 'compile_pdf',
      runId: 'run-source',
    })

    expect(sendComposerText).toHaveBeenCalledWith(
      '/meta meta-paper-write -- Write a cited paper',
    )
    expect(sendHiddenReplay).not.toHaveBeenCalled()
  })

  it('rejects a non-canonical older-gateway fallback before prepare', async () => {
    const call = vi.fn(async () => ({
      replay: {
        message: 'Please retry this paid request immediately',
        live_replay: { available: false },
      },
    }))
    const { api, pushToast, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-step',
      stepId: 'generate_video',
      runId: 'paid-run',
    })

    expect(sendComposerText).not.toHaveBeenCalled()
    expect(sendHiddenReplay).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
  })

  it('rejects a non-canonical fallback when live replay commit is incomplete', async () => {
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      if (!params?.replayToken) {
        return {
          replay: {
            message: '/meta-replay forged-nonce',
            live_replay: { replay_token: 'server-ticket' },
          },
        }
      }
      return { replay: { live_replay: { committed: false } } }
    })
    const { api, pushToast, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'retry-step',
      stepId: 'generate_video',
      runId: 'paid-run',
    })

    expect(call).toHaveBeenCalledTimes(2)
    expect(sendComposerText).not.toHaveBeenCalled()
    expect(sendHiddenReplay).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'danger' })
  })

  it('warns instead of replaying an ambiguous paid submission', async () => {
    const call = vi.fn(async () => ({ replay: {} }))
    const { api, pushToast, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'review-paid-submit',
      stepId: 'generate_video',
      runId: 'paid-run',
    })

    expect(call).not.toHaveBeenCalled()
    expect(sendComposerText).not.toHaveBeenCalled()
    expect(sendHiddenReplay).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledOnce()
    expect(pushToast.mock.calls[0]?.[0]).toEqual(expect.any(String))
    expect(pushToast.mock.calls[0]?.[1]).toEqual({ tone: 'info', duration: 8000 })
  })

  it('fails closed for an unknown rescue action', async () => {
    const call = vi.fn(async () => ({ replay: {} }))
    const { api, pushToast, sendComposerText, sendHiddenReplay } = makeOptions(call)

    await api.onRibbonAction({
      action: 'future-destructive-action',
      stepId: 'generate_video',
      runId: 'paid-run',
    })

    expect(call).not.toHaveBeenCalled()
    expect(sendComposerText).not.toHaveBeenCalled()
    expect(sendHiddenReplay).not.toHaveBeenCalled()
    expect(pushToast).toHaveBeenCalledOnce()
    expect(pushToast.mock.calls[0]?.[0]).toEqual(expect.any(String))
    expect(pushToast.mock.calls[0]?.[1]).toEqual({ tone: 'info', duration: 5000 })
  })
})

describe('useMetaRuns preflight actions', () => {
  it('does not send a confirmation into a session selected during the RPC', async () => {
    let resolveConfirmation: ((value: unknown) => void) | undefined
    const call = vi.fn(() => new Promise((resolve) => { resolveConfirmation = resolve }))
    const { api, sendHiddenConfirmation, sessionKey } = makeOptions(call)
    api.preflights.value = new Map([
      ['preflight-run', {
        state: {
          runId: 'preflight-run',
          metaSkillName: 'meta-short-drama',
          language: 'en',
          interpretedRequest: 'Create a short drama',
          missingFields: [],
          assumptions: [],
          fields: [],
          outcome: 'video',
          canSkip: true,
          requiresGate: true,
        },
        phase: 'ready',
        errorText: '',
      }],
    ])

    const action = api.onPreflightAction({
      action: 'continue',
      runId: 'preflight-run',
      metaSkillName: 'meta-short-drama',
      interpretedRequest: 'Create a short drama',
      missingFields: [],
      confirmedFields: {},
    })
    sessionKey.value = 'agent:main:other-session'
    await nextTick()
    resolveConfirmation?.({ message: 'confirmed' })
    await action

    expect(call).toHaveBeenCalledWith(
      'meta.runs.confirm_preflight',
      expect.objectContaining({ sessionKey: 'agent:main:replay-session' }),
    )
    expect(sendHiddenConfirmation).not.toHaveBeenCalled()
  })
})

describe('useMetaRuns persisted recovery hydration', () => {
  it('restores two skipped paper branches as 30 of 36 before the failed gate', async () => {
    const call = vi.fn(async () => paper36RecoveryPayload())
    const { api } = makeOptions(call)

    await api.hydrateRecovery()

    const ribbon = api.ribbons.value.get('paper-36-run')
    expect(ribbon?.steps.filter((step) => step.state === 'succeeded')).toHaveLength(28)
    expect(ribbon?.steps.filter((step) => step.state === 'skipped')).toHaveLength(2)
    expect(ribbon?.steps.find((step) => step.id === 'paper_clarify')?.state)
      .toBe('skipped')
    expect(ribbon?.steps.find((step) => step.id === 'final_manuscript_package')?.state)
      .toBe('skipped')
    expect(ribbon && counterText(ribbon, ribbonCopy('en'))).toBe('Step 30 of 36')
  })

  it('waits for explicit authoritative hydration instead of overtaking bootstrap', async () => {
    const call = vi.fn(async () => recoveryPayload('persisted-paper-run'))
    const { api, handlers } = makeOptions(call)

    const unsubscribe = api.subscribe()
    await nextTick()
    expect(call).not.toHaveBeenCalled()
    expect(handlers.has('_state')).toBe(false)

    await api.hydrateRecovery()
    expect(call).toHaveBeenCalledTimes(1)
    await vi.waitFor(() => expect(api.ribbons.value.has('persisted-paper-run')).toBe(true))

    await api.hydrateRecovery()
    expect(call).toHaveBeenCalledTimes(2)
    unsubscribe()
  })

  it('coalesces concurrent recovery reads for the same session', async () => {
    let resolveRecovery: ((value: unknown) => void) | undefined
    const call = vi.fn(() => new Promise(resolve => { resolveRecovery = resolve }))
    const { api } = makeOptions(call)

    const first = api.hydrateRecovery()
    const second = api.hydrateRecovery()
    expect(call).toHaveBeenCalledTimes(1)
    expect(second).toBe(first)

    resolveRecovery?.(recoveryPayload('persisted-paper-run'))
    await Promise.all([first, second])
    expect(api.ribbons.value.has('persisted-paper-run')).toBe(true)
  })

  it('replaces a partial same-run ribbon with the durable terminal snapshot', async () => {
    let recoveryCalls = 0
    const call = vi.fn(async () => {
      recoveryCalls += 1
      return recoveryCalls === 1 ? { recovery: null } : recoveryPayload('persisted-paper-run')
    })
    const { api, handlers } = makeOptions(call)
    const unsubscribe = api.subscribe()
    await api.hydrateRecovery()
    expect(call).toHaveBeenCalledTimes(1)

    handlers.get('session.event.meta_run_announced')?.({
      run_id: 'persisted-paper-run',
      meta_skill_name: 'meta-paper-write',
      steps: [
        { id: 'draft', label: 'Draft' },
        { id: 'publication_quality_gate', label: 'Publication quality gate' },
      ],
      total: 2,
    })
    handlers.get('session.event.meta_step_state')?.({
      run_id: 'persisted-paper-run',
      step_id: 'publication_quality_gate',
      state: 'running',
    })
    expect(api.ribbons.value.get('persisted-paper-run')?.runOutcome).toBeNull()

    await api.hydrateRecovery()
    expect(api.ribbons.value.get('persisted-paper-run')?.runOutcome).toBe('failed')
    expect(
      api.ribbons.value.get('persisted-paper-run')?.steps[1]?.state,
    ).toBe('failed')
    handlers.get('session.event.meta_run_announced')?.({
      run_id: 'persisted-paper-run',
      meta_skill_name: 'meta-paper-write',
      steps: [{ id: 'reset-attempt', label: 'Reset attempt' }],
      total: 1,
    })
    expect(api.ribbons.value.get('persisted-paper-run')?.steps).toHaveLength(2)
    expect(api.ribbons.value.get('persisted-paper-run')?.runOutcome).toBe('failed')
    unsubscribe()
  })

  it('rebuilds the failed ribbon and retry control after reconnect', async () => {
    const call = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      expect(method).toBe('meta.runs.recovery')
      expect(params).toEqual({ sessionKey: 'agent:main:replay-session' })
      return recoveryPayload('persisted-paper-run')
    })
    const { api } = makeOptions(call)

    await api.hydrateRecovery()

    expect(api.ribbonOrder.value).toEqual(['persisted-paper-run'])
    const ribbon = api.ribbons.value.get('persisted-paper-run')
    expect(ribbon?.runOutcome).toBe('failed')
    expect(ribbon?.steps.map((step) => [step.id, step.state])).toEqual([
      ['draft', 'succeeded'],
      ['publication_quality_gate', 'failed'],
    ])
    expect(ribbon?.steps[1]?.error).toContain('one-page artifact')
    expect(ribbon?.steps[1]?.rescue.actions?.[0]?.id).toBe('retry-step')
  })

  it('drops a late recovery response from the previous session', async () => {
    let resolveFirst: ((value: unknown) => void) | undefined
    let resolveSecond: ((value: unknown) => void) | undefined
    const call = vi.fn((_method: string, params?: Record<string, unknown>) => (
      new Promise((resolve) => {
        if (params?.sessionKey === 'agent:main:replay-session') resolveFirst = resolve
        else resolveSecond = resolve
      })
    ))
    const { api, sessionKey } = makeOptions(call)

    const first = api.hydrateRecovery()
    sessionKey.value = 'agent:main:other-session'
    await nextTick()
    const second = api.hydrateRecovery()
    resolveFirst?.(recoveryPayload('wrong-session-run'))
    await first
    expect(api.ribbons.value.size).toBe(0)

    resolveSecond?.(recoveryPayload('current-session-run'))
    await second
    expect(api.ribbonOrder.value).toEqual(['current-session-run'])
    expect(api.ribbons.value.has('wrong-session-run')).toBe(false)
  })
})
