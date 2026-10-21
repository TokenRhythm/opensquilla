import { describe, expect, it } from 'vitest'

import type { ActivitySnapshotV2, ChatMessage, ChatStreamTimelineItem } from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import {
  activityReasoningBlocks,
  activitySnapshotMatchesMessage,
  activityStatusHistory,
  applyActivityOrdersToTimeline,
  normalizeActivitySnapshot,
  restoreActivityInterruptTimeline,
} from './activitySnapshot'
import { projectAssistantActivityTimeline, providerActivityLabel } from './assistantActivity'

const snapshot: ActivitySnapshotV2 = {
  version: 2,
  taskId: 'turn-1',
  turnId: 'turn-1',
  complete: true,
  reasoningUtf16Length: 3,
  entries: [
    { type: 'phase', id: 'provider:requesting:4', order: 4, kind: 'provider', phase: 'requesting', at: 4_000, ended_at: 5_000 },
    { type: 'phase', id: 'provider:reasoning:5', order: 5, kind: 'provider', phase: 'reasoning', at: 5_000, ended_at: 31_000 },
    {
      type: 'reasoning',
      id: 'reasoning-1',
      order: 6,
      block_index: 0,
      started_at: 6_000,
      ended_at: 8_000,
      status: 'completed',
      content_kind: 'reasoning',
      text_start_utf16: 0,
      text_end_utf16: 3,
    },
    { type: 'phase', id: 'write:1:31', order: 31, kind: 'write', phase: 'writing', round: 1, at: 31_000, ended_at: 41_000 },
    { type: 'segment', id: 'text:0', order: 31, segment_type: 'text', text_index: 0, text_utf16_length: 5, at: 31_000, ended_at: 31_000 },
    { type: 'segment', id: 'tool:tool-1', order: 41, segment_type: 'tool', tool_use_id: 'tool-1', name: 'skill_view', started_at: 41_000 },
  ],
}

describe('activitySnapshot v2', () => {
  it('restores factual fallback legs and retains legacy snapshots without model names', () => {
    const raw = { version: 2, task_id: 'turn-1', turn_id: 'turn-1', complete: true, reasoning_utf16_length: 0, entries: [
      { type: 'phase', id: 'fallback-1', order: 1, kind: 'provider', phase: 'fallback', at: 1000, ended_at: 2000, model: 'model-b', from_model: 'model-a' },
      { type: 'phase', id: 'fallback-2', order: 2, kind: 'provider', phase: 'fallback', at: 2000, ended_at: 3000, model: 'model-c', from_model: 'model-b' },
      { type: 'phase', id: 'fallback-3', order: 3, kind: 'provider', phase: 'fallback', at: 3000, ended_at: 4000 },
    ] }
    const normalized = normalizeActivitySnapshot(raw)!
    expect(normalized).toBeDefined()
    const history = activityStatusHistory(normalized)
    expect(history[0]?.providerRecovery).toMatchObject({ model: 'model-b', fromModel: 'model-a' })
    expect(activityStatusHistory(normalizeActivitySnapshot({ ...raw, entries: normalized.entries })!)).toEqual(history)
    const steps = projectAssistantActivityTimeline([], { lifecycle: 'failed', statusHistory: history }).statusSteps
    expect(steps.map(step => providerActivityLabel(step).code)).toEqual(['chat.activity.provider.fallbackModelsPast', 'chat.activity.provider.fallbackModelsPast', 'chat.activity.provider.fallbackPast'])
    expect(providerActivityLabel(steps[1]!).params).toEqual({ from: 'model-b', to: 'model-c' })
    for (const fields of [{ model: 'a\nb' }, { from_model: 'a'.repeat(257) }, { phase: 'requesting', from_model: 'model-a' }]) {
      expect(normalizeActivitySnapshot({ ...raw, entries: [{ ...raw.entries[0], ...fields }] })).toBeUndefined()
    }
  })
  it('retains explicit HTTP provenance in history but never revives terminal recovery', () => {
    const raw = { version: 2, task_id: 'turn-1', turn_id: 'turn-1', complete: true, reasoning_utf16_length: 0, entries: [
      { type: 'phase', id: 'wait', order: 1, kind: 'provider', phase: 'retry_wait', at: 10_000, ended_at: 45_000, reason: 'provider_overloaded', retry_after_ms: 35_000, http_status: 503 },
      { type: 'maintenance', id: 'cmp', order: 2, maintenance_type: 'context_compaction', state: 'skipped', reason: 'summary_failed', at: 50_000, ended_at: 60_000, failure_detail: { failure_kind: 'rate_limited', http_status: 429 } },
    ] }
    const normalized = normalizeActivitySnapshot(raw)!
    expect(normalized).toBeDefined()
    const history = activityStatusHistory(normalized)
    expect(history[0]?.providerRecovery).toMatchObject({ reason: 'provider_overloaded', httpStatus: 503, startedAt: 10_000 })
    expect(history[1]).toMatchObject({ state: 'failed', httpStatus: 429, failureKind: 'rate_limited' })
    expect(history[1]?.providerRecovery).toBeUndefined()
    expect(activityStatusHistory(normalizeActivitySnapshot({ ...raw, entries: normalized.entries })!)).toEqual(history)
  })

  it.each(['summary_failed', 'suffix_summary_failed'])('retains an unclassified HTTP failure for refused compaction %s', reason => {
    const raw = { version: 2, task_id: 'turn-1', turn_id: 'turn-1', complete: true, reasoning_utf16_length: 0, entries: [
      { type: 'maintenance', id: 'cmp-unknown', order: 1, maintenance_type: 'context_compaction', state: 'skipped', reason, at: 50_000, ended_at: 60_000, failure_detail: { failure_kind: 'unknown', http_status: 418 } },
    ] }
    const normalized = normalizeActivitySnapshot(raw)!
    const history = activityStatusHistory(normalized)
    expect(history[0]).toMatchObject({ state: 'failed', httpStatus: 418 })
    expect(history[0]?.failureKind).toBeUndefined()
    expect(history[0]?.providerRecovery).toBeUndefined()
    expect(activityStatusHistory(normalizeActivitySnapshot({ ...raw, entries: normalized.entries })!)).toEqual(history)
  })

  it('keeps original chronology for explicitly shortened reasoning and text previews', () => {
    const message: ChatMessage = {
      role: 'assistant', text: 'fi', ts: 1,
      reasoning: { text: 'A', seconds: 0 },
      tool_calls: [
        { type: 'text', text: 'fi', presentation: 'intermediate' },
        { type: 'tool_use', tool_use_id: 'tool-1', name: 'skill_view', input: { text: 'preview' } },
      ],
      historyPayloadPreview: { detailsTruncated: true, reasoningUtf16Length: 3, textUtf16Lengths: [5] },
    }
    expect(activitySnapshotMatchesMessage(snapshot, message)).toBe(true)
    expect(activityReasoningBlocks(snapshot, 'A', 3)).toMatchObject([
      { text: 'A', activityOrder: 6, id: 'reasoning-1' },
    ])
    const timeline: ChatStreamTimelineItem[] = [{ type: 'text', key: 'first', rawText: 'fi', html: 'fi' }]
    expect(applyActivityOrdersToTimeline(timeline, snapshot, message.historyPayloadPreview?.textUtf16Lengths)[0]?.activityOrder).toBe(31)
    expect(activitySnapshotMatchesMessage(snapshot, { ...message, historyPayloadPreview: undefined })).toBe(false)
    expect(activitySnapshotMatchesMessage(snapshot, {
      ...message, historyPayloadPreview: { ...message.historyPayloadPreview, textUtf16Lengths: [6] },
    })).toBe(false)
    expect(activityReasoningBlocks(snapshot, 'A', 2)).toBeUndefined()
  })

  it('does not shift a later reasoning block into a clipped prefix', () => {
    const physical: ActivitySnapshotV2 = {
      ...snapshot, reasoningUtf16Length: 10,
      entries: [
        { ...snapshot.entries[2]!, text_start_utf16: 0, text_end_utf16: 3 },
        { ...snapshot.entries[2]!, id: 'reasoning-2', order: 42, block_index: 1, text_start_utf16: 4, text_end_utf16: 10 },
      ],
    }
    expect(activityReasoningBlocks(physical, 'A', 10)).toMatchObject([
      { text: 'A', activityOrder: 6 }, { text: '', activityOrder: 42 },
    ])
    expect(activityReasoningBlocks(physical, 'A😀 wrong', 10)).toBeUndefined()
  })
  it('retains chronology when persistence joins adjacent final answer and delivery text', () => {
    const coalesced: ActivitySnapshotV2 = {
      ...snapshot,
      entries: [...snapshot.entries,
        { type: 'segment', id: 'text:1', order: 51, segment_type: 'text', text_index: 1, text_utf16_length: 8 },
        { type: 'phase', id: 'write:3:61', order: 61, kind: 'write', phase: 'writing', round: 3 },
        { type: 'segment', id: 'text:2', order: 61, segment_type: 'text', text_index: 2, text_utf16_length: 4 },
      ],
    }
    const message: ChatMessage = {
      role: 'assistant', text: 'firstanswer😀note', ts: 1,
      reasoning: { text: 'A😀', seconds: 2 },
      tool_calls: [
        { type: 'text', text: 'first', presentation: 'intermediate' },
        { type: 'tool_use', tool_use_id: 'tool-1', name: 'skill_view', input: {} },
        { type: 'tool_result', tool_use_id: 'tool-1', name: 'skill_view', result: 'ok' },
        { type: 'text', text: 'answer😀note', presentation: 'answer' },
      ],
    }
    expect(activitySnapshotMatchesMessage(coalesced, message)).toBe(true)
    expect(activityReasoningBlocks(coalesced, 'A😀')?.[0]?.activityOrder).toBe(6)
    const textItems: ChatStreamTimelineItem[] = ['first', 'answer😀note'].map((rawText, index) => ({
      type: 'text', key: `text-${index}`, rawText, html: rawText,
      presentation: index === 1 ? 'answer' : 'intermediate',
    }))
    expect(applyActivityOrdersToTimeline(textItems, coalesced).map(item => item.activityOrder)).toEqual([31, 51])
    expect(textItems.map(item => item.type === 'text' ? item.rawText : '')).toEqual(['first', 'answer😀note'])

    // Matching lengths never authorizes crossing another kind of activity.
    for (const barrier of [
      { type: 'phase', kind: 'provider', phase: 'reasoning' },
      { type: 'segment', segment_type: 'tool' },
      { type: 'reasoning' }, { type: 'interrupt' }, { type: 'maintenance' },
    ] as const) {
      const separated = { ...coalesced, entries: coalesced.entries.map(entry =>
        entry.id === 'write:3:61' ? { ...entry, ...barrier } : entry) }
      expect(applyActivityOrdersToTimeline(textItems, separated)).toBe(textItems)
    }
    for (const presentation of ['intermediate', undefined] as const) {
      expect(activitySnapshotMatchesMessage(coalesced, {
        ...message, tool_calls: message.tool_calls?.map(segment =>
          segment.text === 'answer😀note' ? { ...segment, presentation } : segment),
      })).toBe(false)
    }
    expect(activitySnapshotMatchesMessage(coalesced, {
      ...message, tool_calls: message.tool_calls?.map(segment =>
        segment.text === 'answer😀note' ? { ...segment, text: 'answer😀' } : segment),
    })).toBe(false)
  })

  it('restores temporary maintenance without promoting it to a saved summary', () => {
    const normalized = normalizeActivitySnapshot({
      version: 2, task_id: 'turn-1', turn_id: 'turn-1',
      complete: true, reasoning_utf16_length: 0,
      entries: [{
        type: 'maintenance', id: 'cmp-temporary', order: 1,
        maintenance_type: 'context_compaction', state: 'completed',
        at: 1_000, ended_at: 2_000, source: 'automatic', durability: 'request_scoped',
      }],
    }, 'turn-1', 'turn-1')

    expect(normalized).toBeDefined()
    const history = activityStatusHistory(normalized!)
    expect(history).toMatchObject([{ state: 'completed', durability: 'request_scoped' }])
    const projection = projectAssistantActivityTimeline([], { lifecycle: 'settled', statusHistory: history })
    expect(projection.statusSteps[0]).toMatchObject({
      isCurrent: false, label: { code: 'chat.compact.temporarilyReduced' },
    })
  })

  it('restores connection recovery with its attempt and no artificial retry limit', () => {
    const normalized = normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: false,
      reasoning_utf16_length: 0,
      entries: [{
        type: 'phase', id: 'provider:retrying:7', order: 7, kind: 'provider',
        phase: 'retrying', at: 7_000, ended_at: 8_000,
        retry_attempt: 7, retry_limit: 0,
      }],
    }, 'turn-1', 'turn-1')

    expect(normalized).toBeDefined()
    expect(activityStatusHistory(normalized!)).toMatchObject([
      { action: 'provider:retrying:7:0', label: 'Retrying · attempt 7', activityOrder: 7 },
    ])
  })

  it('restores phase, UTF-16 reasoning, text, and tool order without timestamps', () => {
    const normalized = normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: true,
      reasoning_utf16_length: 3,
      entries: snapshot.entries,
    }, 'turn-1', 'turn-1')
    expect(normalized).toBeDefined()
    expect(activityStatusHistory(normalized!)).toMatchObject([
      { action: 'provider:requesting', activityOrder: 4 },
      { action: 'provider:reasoning', activityOrder: 5 },
      { action: 'write:1', activityOrder: 31 },
    ])
    expect(activityReasoningBlocks(normalized!, 'A😀')).toMatchObject([
      { id: 'reasoning-1', text: 'A😀', activityOrder: 6 },
    ])

    const timeline: ChatStreamTimelineItem[] = [
      { type: 'text', key: 'text-0', html: '<p>first</p>', rawText: 'first' },
      {
        type: 'tool-group',
        key: 'group-1',
        group: {
          groupId: 'group-1',
          operationKey: 'skill_view',
          label: 'Inspect',
          iconName: 'gear',
          calls: [{
            toolId: 'tool-1',
            renderKey: 'tool-1',
            name: 'skill_view',
            displayName: 'Inspect',
            inputPreview: '',
            isRunning: false,
            status: 'success',
            isError: false,
            result: '',
            resultPreview: '',
            isOpen: false,
          }],
          secondary: '',
          isRunning: false,
          isError: false,
          status: 'success',
        },
      },
    ]
    const ordered = applyActivityOrdersToTimeline(timeline, normalized)
    expect(ordered[0]?.activityOrder).toBe(31)
    expect(ordered[1]?.activityOrder).toBe(41)
    expect(ordered[1]?.type === 'tool-group' && ordered[1].group.calls[0]?.activityOrder).toBe(41)
  })

  it('rejects identity mismatch, descending order, and reasoning misalignment', () => {
    expect(normalizeActivitySnapshot({
      version: 2,
      task_id: 'other-task',
      turn_id: 'turn-1',
      entries: snapshot.entries,
    }, 'turn-1', 'turn-1')).toBeUndefined()
    expect(normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      entries: [...snapshot.entries].reverse(),
    })).toBeUndefined()
    expect(activityReasoningBlocks(snapshot, 'A')).toBeUndefined()
    expect(normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: true,
      reasoning_utf16_length: 3,
      entries: [{ ...snapshot.entries[0], raw_label: 'private' }],
    })).toBeUndefined()
    expect(normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: true,
      reasoning_utf16_length: 3,
      entries: snapshot.entries,
      checksum: 'not-a-checksum',
    })).toBeUndefined()
  })

  it('accepts only exact transcript references and rejects partial mixing', () => {
    const message: ChatMessage = {
      role: 'assistant',
      text: 'first',
      ts: 1,
      reasoning: { text: 'A😀', seconds: 2 },
      timeline: [{ type: 'text', raw: 'first' }],
      tool_calls: [{
        type: 'tool_use',
        tool_use_id: 'tool-1',
        name: 'skill_view',
      }],
    }
    expect(activitySnapshotMatchesMessage(snapshot, message)).toBe(true)
    expect(activitySnapshotMatchesMessage(snapshot, {
      ...message,
      timeline: [{ type: 'text', raw: 'different' }],
    })).toBe(false)
    expect(activitySnapshotMatchesMessage(snapshot, {
      ...message,
      tool_calls: [{
        type: 'tool_use',
        tool_use_id: 'tool-1',
        name: 'write_file',
      }],
    })).toBe(false)
  })

  it('restores multiple physical reasoning blocks across persisted newline separators', () => {
    const normalized = normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: true,
      reasoning_utf16_length: 10,
      entries: [
        {
          type: 'reasoning', id: 'reasoning-1', order: 1, block_index: 0,
          started_at: 1_000, ended_at: 2_000, status: 'completed',
          content_kind: 'reasoning', text_start_utf16: 0, text_end_utf16: 3,
        },
        {
          type: 'reasoning', id: 'reasoning-2', order: 2, block_index: 1,
          started_at: 3_000, ended_at: 4_000, status: 'completed',
          content_kind: 'reasoning', text_start_utf16: 4, text_end_utf16: 10,
        },
      ],
    }, 'turn-1', 'turn-1')

    expect(normalized).toBeDefined()
    expect(activityReasoningBlocks(normalized!, 'A😀\nsecond')).toMatchObject([
      { id: 'reasoning-1', text: 'A😀', activityOrder: 1 },
      { id: 'reasoning-2', text: 'second', activityOrder: 2 },
    ])
    expect(activityReasoningBlocks(normalized!, 'A😀 second')).toBeUndefined()
    expect(normalizeActivitySnapshot({
      version: 2,
      task_id: 'turn-1',
      turn_id: 'turn-1',
      complete: true,
      reasoning_utf16_length: 9,
      entries: [
        {
          type: 'reasoning', id: 'reasoning-1', order: 1, block_index: 0,
          started_at: 1_000, ended_at: 2_000, status: 'completed',
          content_kind: 'reasoning', text_start_utf16: 0, text_end_utf16: 3,
        },
        {
          type: 'reasoning', id: 'reasoning-2', order: 2, block_index: 1,
          started_at: 3_000, ended_at: 4_000, status: 'completed',
          content_kind: 'reasoning', text_start_utf16: 3, text_end_utf16: 9,
        },
      ],
    })).toBeUndefined()
  })

  it('falls back to persisted text segments when timeline has only tool placeholders', () => {
    const message: ChatMessage = {
      role: 'assistant',
      text: 'first',
      ts: 1,
      reasoning: { text: 'A😀', seconds: 2 },
      timeline: [{ type: 'tool-group', groupId: 'group-1' }],
      tool_calls: [
        { type: 'text', text: 'first' },
        { type: 'tool_use', tool_use_id: 'tool-1', name: 'skill_view' },
      ],
    }
    expect(activitySnapshotMatchesMessage(snapshot, message)).toBe(true)
  })

  it('restores a resolved approval at request order with no command or path', () => {
    const approvalSnapshot: ActivitySnapshotV2 = {
      version: 2,
      taskId: 'turn-1',
      turnId: 'turn-1',
      complete: true,
      reasoningUtf16Length: 0,
      entries: [{
        type: 'interrupt',
        id: 'approval:approval-1',
        order: 12,
        interrupt_type: 'approval',
        reference_id: 'approval-1',
        namespace: 'exec',
        approval_kind: 'sandbox_path',
        resolution: 'approved',
        started_at: 1_000,
        ended_at: 2_000,
      }],
    }
    const restored = restoreActivityInterruptTimeline([], [], approvalSnapshot, 'message-1')
    expect(restored.items).toMatchObject([{
      type: 'interrupt',
      approvalId: 'approval-1',
      activityOrder: 12,
    }])
    expect(restored.parts).toMatchObject([{
      type: 'interrupt',
      resolution: 'approved',
      approval: {
        toolName: 'sandbox_path',
        command: '',
        args: null,
      },
    }])
    expect(JSON.stringify(restored)).not.toContain('C:/')
    expect(restored.parts[0]?.type === 'interrupt'
      && restored.parts[0].approval?.displayTarget).toBeUndefined()
  })

  it('inserts a resolved clarify interrupt at its activity order', () => {
    const clarifyPart: ChatPart = {
      type: 'interrupt',
      interruptKind: 'clarify',
      key: 'message-1:interrupt:request-1',
      clarify: {
        intro: 'Choose a target.',
        fields: [],
        requestId: 'request-1',
        runId: 'run-1',
        step: 'choose_target',
      },
      resolution: 'replied',
      busy: false,
      error: '',
    }
    const toolItem: ChatStreamTimelineItem = {
      type: 'tool-group',
      key: 'tool-group-1',
      activityOrder: 100,
      group: {
        groupId: 'tool-group-1',
        operationKey: 'skill_view',
        label: 'Inspect',
        iconName: 'gear',
        calls: [],
        secondary: '',
        isRunning: false,
        isError: false,
        status: 'success',
      },
    }
    const restored = restoreActivityInterruptTimeline(
      [toolItem],
      [clarifyPart],
      {
        version: 2,
        taskId: 'run-1',
        turnId: 'run-1',
        complete: true,
        reasoningUtf16Length: 0,
        entries: [{
          type: 'interrupt',
          id: 'clarify:request-1',
          order: 50,
          interrupt_type: 'clarify',
          reference_id: 'request-1',
          started_at: 40,
          ended_at: 50,
        }],
      },
      'message-1',
    )

    expect(restored.items.map(item => item.type)).toEqual(['interrupt', 'tool-group'])
    expect(restored.items.map(item => item.activityOrder)).toEqual([50, 100])
  })
})
