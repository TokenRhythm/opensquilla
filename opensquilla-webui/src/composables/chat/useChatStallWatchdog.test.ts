import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { ref, nextTick, effectScope, type EffectScope } from 'vue'
import { conversationSemanticEventKind } from '@/adapters/gateway/conversationEventsV4'
import {
  useChatStallWatchdog,
  SOFT_STALL_THRESHOLD_MS as THRESHOLD,
} from './useChatStallWatchdog'

function harness(streamIdleGraceMs = 0) {
  const isStreaming = ref(false)
  const negotiatedGrace = ref(streamIdleGraceMs)
  const scope: EffectScope = effectScope()
  let api!: Omit<ReturnType<typeof useChatStallWatchdog>, 'noteEvent'> & {
    noteEvent(eventName: string, payload?: unknown): void
  }
  scope.run(() => {
    const semanticApi = useChatStallWatchdog({ isStreaming, streamIdleGraceMs: negotiatedGrace })
    api = {
      ...semanticApi,
      noteEvent: (eventName: string, payload?: unknown) => (
        semanticApi.noteEvent(conversationSemanticEventKind(eventName), payload)
      ),
    }
  })
  return { isStreaming, streamIdleGraceMs: negotiatedGrace, api, scope }
}

async function startStreaming(h: ReturnType<typeof harness>) {
  h.isStreaming.value = true
  await nextTick()
}

describe('useChatStallWatchdog', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('stays quiet before the threshold and activates once content goes silent past it', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)

    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    expect(h.api.stallSeconds.value).toBe(THRESHOLD / 1000)

    // Live seconds keep advancing while the stall persists.
    vi.advanceTimersByTime(5000)
    expect(h.api.stallSeconds.value).toBe(THRESHOLD / 1000 + 5)
    h.scope.stop()
  })

  it('uses the larger negotiated WebUI grace as its soft threshold', async () => {
    const negotiated = THRESHOLD + 15 * 60_000
    const h = harness(negotiated)
    await startStreaming(h)

    expect(h.api.effectiveThresholdMs.value).toBe(negotiated)
    vi.advanceTimersByTime(THRESHOLD)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(negotiated - THRESHOLD)
    expect(h.api.stallActive.value).toBe(true)

    h.streamIdleGraceMs.value = THRESHOLD + 30 * 60_000
    expect(h.api.effectiveThresholdMs.value).toBe(THRESHOLD + 30 * 60_000)
    h.scope.stop()
  })

  it('resets the silence clock on content events (text, thinking, router decision)', async () => {
    const h = harness()
    await startStreaming(h)

    for (const event of [
      'session.event.text_delta',
      'session.event.thinking',
      'session.event.router_decision',
    ]) {
      vi.advanceTimersByTime(THRESHOLD - 1000)
      expect(h.api.stallActive.value).toBe(false)
      h.api.noteEvent(event, {})
    }
    // Clock was just reset by the last content event: still below threshold.
    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('a content event clears an already-active banner', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD)
    expect(h.api.stallActive.value).toBe(true)

    h.api.noteEvent('session.event.text_delta', {})
    expect(h.api.stallActive.value).toBe(false)
    h.scope.stop()
  })

  it('run_heartbeat and other liveness events do NOT reset the clock', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD / 2)
    h.api.noteEvent('session.event.run_heartbeat', {})
    h.api.noteEvent('session.event.state_change', { to_state: 'thinking' })
    h.api.noteEvent('tick', {})

    vi.advanceTimersByTime(THRESHOLD / 2)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('suppresses the soft warning while proposer and aggregator phases are active', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'proposer_start', proposer_index: 0, proposer_label: 'anchor', proposer_model: 'model-a',
    })
    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'proposer_start', proposer_index: 1, proposer_label: 'critic', proposer_model: 'model-b',
    })
    expect(h.api.suspendReason.value).toBe('ensemble-running')
    vi.advanceTimersByTime(THRESHOLD * 2)
    expect(h.api.stallActive.value).toBe(false)

    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'proposer_finish', proposer_index: 0, proposer_label: 'anchor', proposer_model: 'model-a',
    })
    expect(h.api.suspendReason.value).toBe('ensemble-running')
    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'proposer_finish', proposer_index: 1, proposer_label: 'critic', proposer_model: 'model-b',
    })
    expect(h.api.suspendReason.value).toBe(null)

    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'aggregator_start', proposer_index: -1, proposer_label: 'aggregator', proposer_model: 'model-z',
    })
    expect(h.api.suspendReason.value).toBe('ensemble-running')
    vi.advanceTimersByTime(THRESHOLD * 2)
    expect(h.api.stallActive.value).toBe(false)

    h.api.noteEvent('session.event.ensemble_progress', {
      event_type: 'aggregator_finish', proposer_index: -1, proposer_label: 'aggregator', proposer_model: 'model-z',
    })
    expect(h.api.suspendReason.value).toBe(null)
    vi.advanceTimersByTime(THRESHOLD)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('treats ensemble phase heartbeats as backend-deadline-owned activity', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.run_heartbeat', { phase: 'ensemble_proposers_wait' })
    expect(h.api.suspendReason.value).toBe('ensemble-running')
    vi.advanceTimersByTime(THRESHOLD * 2)
    expect(h.api.stallActive.value).toBe(false)

    h.api.noteEvent('session.event.done', {})
    expect(h.api.suspendReason.value).toBe(null)
    expect(h.api.stallActive.value).toBe(false)
    h.scope.stop()
  })

  it('compaction and ensemble progress frames count as content activity', async () => {
    const h = harness()
    await startStreaming(h)

    for (const event of [
      'session.event.compaction',
      'session.event.ensemble_progress',
    ]) {
      vi.advanceTimersByTime(THRESHOLD - 1000)
      expect(h.api.stallActive.value).toBe(false)
      h.api.noteEvent(event, {})
    }
    // The last progress frame reset the clock: still below threshold.
    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('suspends while a tool call is in flight and resumes after its result', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.tool_use_start', { tool_use_id: 't1' })
    expect(h.api.suspendReason.value).toBe('tool-running')

    // A long tool run emits nothing — the banner must never fire.
    vi.advanceTimersByTime(THRESHOLD * 4)
    expect(h.api.stallActive.value).toBe(false)

    h.api.noteEvent('session.event.tool_result', { tool_use_id: 't1' })
    expect(h.api.suspendReason.value).toBe(null)

    // The tool_result is itself content: silence restarts from it.
    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('stays suspended until every in-flight tool has its matching result', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.tool_use_start', { tool_use_id: 't1' })
    h.api.noteEvent('session.event.tool_use_start', { toolUseId: 't2' })
    h.api.noteEvent('session.event.tool_result', { tool_use_id: 't1' })
    expect(h.api.suspendReason.value).toBe('tool-running')

    h.api.noteEvent('session.event.tool_result', { toolUseId: 't2' })
    expect(h.api.suspendReason.value).toBe(null)
    h.scope.stop()
  })

  it('suspends while an approval is pending and re-measures silence after it resolves', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('exec.approval.requested', { approval_id: 'a1', session_key: 's1' })
    expect(h.api.suspendReason.value).toBe('approval-pending')

    vi.advanceTimersByTime(THRESHOLD * 6)
    expect(h.api.stallActive.value).toBe(false)

    // Resolving unblocks the run; the banner must not fire instantly off the
    // pre-approval silence.
    h.api.noteEvent('exec.approval.resolved', { approval_id: 'a1', approved: true })
    expect(h.api.suspendReason.value).toBe(null)
    expect(h.api.stallActive.value).toBe(false)

    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('tracks plugin-namespace approvals too', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('plugin.approval.requested', { approvalId: 'p1' })
    expect(h.api.suspendReason.value).toBe('approval-pending')
    h.api.noteEvent('plugin.approval.resolved', { approvalId: 'p1' })
    expect(h.api.suspendReason.value).toBe(null)
    h.scope.stop()
  })

  it('prefers approval-pending when both gates hold', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.tool_use_start', { tool_use_id: 't1' })
    h.api.noteEvent('exec.approval.requested', { approval_id: 'a1' })
    expect(h.api.suspendReason.value).toBe('approval-pending')

    h.api.noteEvent('exec.approval.resolved', { approval_id: 'a1' })
    expect(h.api.suspendReason.value).toBe('tool-running')
    h.scope.stop()
  })

  it('dismiss hides the banner for the whole current silence episode', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD)
    expect(h.api.stallActive.value).toBe(true)

    h.api.dismiss()
    expect(h.api.stallActive.value).toBe(false)

    vi.advanceTimersByTime(THRESHOLD * 3)
    expect(h.api.stallActive.value).toBe(false)
    h.scope.stop()
  })

  it('content after a dismissal clears the re-arm window entirely', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD)
    h.api.dismiss()
    h.api.noteEvent('session.event.text_delta', {})

    // Genuine progress opens a new silence episode with a full threshold.
    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('terminal events clear the banner and per-turn tracking', async () => {
    for (const terminal of ['chat.done', 'session.event.done', 'session.event.error', 'task.cancelled']) {
      const h = harness()
      await startStreaming(h)

      h.api.noteEvent('session.event.tool_use_start', { tool_use_id: 't1' })
      h.api.noteEvent(terminal, {})
      expect(h.api.stallActive.value).toBe(false)
      // The orphaned tool from the ended turn no longer suspends anything.
      expect(h.api.suspendReason.value).toBe(null)

      vi.advanceTimersByTime(THRESHOLD - 1000)
      expect(h.api.stallActive.value).toBe(false)
      h.scope.stop()
    }
  })

  it('task_group checkpoints are neither terminal nor content', async () => {
    const h = harness()
    await startStreaming(h)

    vi.advanceTimersByTime(THRESHOLD / 2)
    h.api.noteEvent('session.event.task_group.done', {})
    h.api.noteEvent('session.event.task_group.failed', {})

    vi.advanceTimersByTime(THRESHOLD / 2)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('deactivates when streaming ends and re-arms cleanly on the next turn', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('session.event.tool_use_start', { tool_use_id: 'orphan' })
    vi.advanceTimersByTime(THRESHOLD * 2)
    expect(h.api.stallActive.value).toBe(false) // suspended by the tool

    h.isStreaming.value = false
    await nextTick()
    expect(h.api.stallActive.value).toBe(false)
    // The aborted turn's orphaned tool is forgotten.
    expect(h.api.suspendReason.value).toBe(null)

    await startStreaming(h)
    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('clears a pending approval when streaming ends so it cannot suspend the next turn', async () => {
    const h = harness()
    await startStreaming(h)

    // The resolved push never arrives (e.g. a WS reconnect ate it).
    h.api.noteEvent('exec.approval.requested', { approval_id: 'lost' })
    expect(h.api.suspendReason.value).toBe('approval-pending')

    h.isStreaming.value = false
    await nextTick()
    expect(h.api.suspendReason.value).toBe(null)

    // The next turn's watchdog is live again, not suspended forever.
    await startStreaming(h)
    expect(h.api.suspendReason.value).toBe(null)
    vi.advanceTimersByTime(THRESHOLD)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('never activates while not streaming', async () => {
    const h = harness()
    vi.advanceTimersByTime(THRESHOLD * 3)
    expect(h.api.stallActive.value).toBe(false)
    h.scope.stop()
  })

  it('reset() clears everything (session switch)', async () => {
    const h = harness()
    await startStreaming(h)

    h.api.noteEvent('exec.approval.requested', { approval_id: 'a1' })
    vi.advanceTimersByTime(THRESHOLD)
    h.api.reset()
    expect(h.api.stallActive.value).toBe(false)
    expect(h.api.suspendReason.value).toBe(null)

    vi.advanceTimersByTime(THRESHOLD - 1000)
    expect(h.api.stallActive.value).toBe(false)
    vi.advanceTimersByTime(1000)
    expect(h.api.stallActive.value).toBe(true)
    h.scope.stop()
  })

  it('stops evaluating after scope dispose', async () => {
    const h = harness()
    await startStreaming(h)
    h.scope.stop()
    vi.advanceTimersByTime(THRESHOLD * 2)
    expect(h.api.stallActive.value).toBe(false)
  })
})
