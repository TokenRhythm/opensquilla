import { describe, expect, it } from 'vitest'

import type { ActivitySnapshotV2, ChatMessage, ChatStreamTimelineItem } from '@/types/chat'
import {
  activityReasoningBlocks,
  activitySnapshotMatchesMessage,
  activityStatusHistory,
  applyActivityOrdersToTimeline,
  normalizeActivitySnapshot,
  restoreActivityInterruptTimeline,
} from './activitySnapshot'

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
})
