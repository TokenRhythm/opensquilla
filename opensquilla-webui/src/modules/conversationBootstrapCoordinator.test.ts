import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createConversationBootstrapPhase,
  createConversationBootstrapCoordinator,
  type ConversationBootstrapRunToken,
  waitForConversationBootstrapRetry,
} from './conversationBootstrapCoordinator'

type Run = ConversationBootstrapRunToken & { label: string }

afterEach(() => {
  vi.useRealTimers()
})

function coordinator(now = 1_000) {
  return createConversationBootstrapCoordinator<Run>({
    now: () => now,
    budgetMs: 15_000,
  })
}

describe('ConversationBootstrapCoordinator', () => {
  it('creates neutral phase bookkeeping without owning execution', async () => {
    const phase = createConversationBootstrapPhase(2_000, { ok: true })
    expect(phase.attempts).toBe(0)
    expect(phase.running).toBe(false)
    expect(phase.deadlineAt).toBe(2_000)
    expect(phase.result).toBeNull()
    expect(phase.skipSnapshot).toBe(false)
    await expect(phase.promise).resolves.toEqual({ ok: true })
  })

  it('fences predecessor runs and keeps one absolute deadline per run', () => {
    const api = coordinator()
    const first = api.start('session:a', true, () => ({ label: 'first' }))
    expect(first.generation).toBe(1)
    expect(first.deadlineAt).toBe(16_000)
    expect(api.current()).toBe(first)
    expect(api.isCurrent(first, 'session:a')).toBe(true)

    const second = api.start('session:b', false, () => ({ label: 'second' }))
    expect(first.controller.signal.aborted).toBe(true)
    expect(api.current()).toBe(second)
    expect(api.generation).toBe(2)
    expect(api.isCurrent(first, 'session:a')).toBe(false)
    expect(api.isCurrent(second, 'session:a')).toBe(false)
    expect(api.isCurrent(second, 'session:b')).toBe(true)
  })

  it('cancels the active run and invalidates callbacks exactly once', () => {
    const api = coordinator()
    const run = api.start('session:a', true, () => ({ label: 'run' }))
    const cancelled = api.cancel()

    expect(cancelled).toBe(run)
    expect(run.controller.signal.aborted).toBe(true)
    expect(api.current()).toBeNull()
    expect(api.generation).toBe(2)
    expect(api.isCurrent(run, 'session:a')).toBe(false)
    expect(api.cancel()).toBeNull()
    expect(api.generation).toBe(3)
  })

  it('defers and coalesces physical transitions until a handoff resolves', () => {
    const api = coordinator()
    expect(api.setHandoffTarget('session:b', 2)).toBe(true)
    expect(api.setHandoffTarget('session:c', 1)).toBe(false)
    expect(api.shouldDeferConnectionState('session:a')).toBe(true)
    expect(api.shouldDeferConnectionState('session:b')).toBe(false)

    api.deferConnectionState('connected', false)
    api.deferConnectionState('connected', true)
    api.deferConnectionState('disconnected', false)
    api.deferConnectionState('connected', true)

    const stale = api.resolveHandoff(1, 'failed')
    expect(stale).toEqual({ accepted: false, deferred: [] })
    expect(api.shouldDeferConnectionState('session:a')).toBe(true)

    const failed = api.resolveHandoff(2, 'failed')
    expect(failed.accepted).toBe(true)
    expect(failed.deferred).toEqual([
      { state: 'disconnected', includeHistory: false },
      { state: 'connected', includeHistory: true },
    ])
    expect(api.shouldDeferConnectionState('session:a')).toBe(false)
    expect(api.resolveHandoff(2, 'failed').deferred).toEqual([])
  })

  it('drops deferred transitions for a committed target', () => {
    const api = coordinator()
    api.setHandoffTarget('session:b', 1)
    api.deferConnectionState('disconnected', true)

    expect(api.resolveHandoff(1, 'committed')).toEqual({
      accepted: true,
      deferred: [],
    })
  })

  it('grants one recovery budget after an authoritative phase', () => {
    const api = coordinator()
    expect(api.consumeRecoveryBudget()).toBe(false)
    api.armRecovery()
    expect(api.consumeRecoveryBudget()).toBe(true)
    expect(api.consumeRecoveryBudget()).toBe(false)
    api.armRecovery()
    api.disarmRecovery()
    expect(api.consumeRecoveryBudget()).toBe(false)
  })

  it('does not require a transport or reactive runtime', () => {
    const api = coordinator()
    const abort = vi.fn()
    const run = api.start('session:a', true, token => ({
      label: 'pure',
      abort: () => abort(token.generation),
    }))
    run.abort()
    expect(abort).toHaveBeenCalledWith(1)
  })

  it('bounds retry timers by deadline and abort/current ownership', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const controller = new AbortController()
    let current = true
    const delayed = waitForConversationBootstrapRetry({
      delayMs: 100,
      deadlineAt: 500,
      signal: controller.signal,
      isCurrent: () => current,
    })
    await vi.advanceTimersByTimeAsync(99)
    current = false
    await vi.advanceTimersByTimeAsync(1)
    expect(await delayed).toBe(false)

    current = true
    const bounded = waitForConversationBootstrapRetry({
      delayMs: 500,
      deadlineAt: 200,
      signal: controller.signal,
      isCurrent: () => current,
    })
    await vi.advanceTimersByTimeAsync(200)
    expect(await bounded).toBe(true)

    const aborted = waitForConversationBootstrapRetry({
      delayMs: 100,
      deadlineAt: 500,
      signal: controller.signal,
      isCurrent: () => true,
    })
    controller.abort()
    expect(await aborted).toBe(false)
  })
})
