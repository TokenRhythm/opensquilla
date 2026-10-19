import { computed, ref } from 'vue'
import { usageSession } from '@/testing/usage.test-helper'
import { describe, expect, it } from 'vitest'
import { useUsageSessionRows } from './useUsageSessionRows'

describe('useUsageSessionRows', () => {
  it('keeps partial and attributed parent groups distinct even if they share a root generation', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => ['complete', 'legacy'].map(taskGroupId => usageSession({
        taskGroupId, sessionKind: 'root', sessionId: 'root', sessionEpoch: 0,
      }))),
      sortCol: ref('updated_at'), sortAsc: ref(false),
      sessionTimestamp: () => null, relTime: () => '-', sortVal: () => 0,
      taskName: () => 'Parent task',
    })
    expect(sortedRows.value.map(row => row.rowIdentity)).toEqual(['task:complete', 'task:legacy'])
  })

  it('opens single capability details while preserving the single LLM row behavior', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [
        { modelBreakdown: [{ model: 'wan3.0-video', capability: 'video_generation' }] },
        { modelBreakdown: [{ model: 'legacy-text-model' }] },
        { modelBreakdown: [{ model: 'text-model', capability: 'llm', meters: [] }] },
      ].map(usageSession)),
      sortCol: ref('updated_at'), sortAsc: ref(false),
      sessionTimestamp: () => null, relTime: () => '-', sortVal: () => 0, taskName: () => 'Task',
    })
    expect(sortedRows.value.map(row => row.hasModelBreakdown)).toEqual([true, false, false])
  })

  it('separates reset and recreated sessions that reuse the same navigation key', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [
        { sessionKey: 'reused-key', sessionId: 'original', sessionEpoch: 0 },
        { sessionKey: 'reused-key', sessionId: 'original', sessionEpoch: 1 },
        { sessionKey: 'reused-key', sessionId: 'recreated', sessionEpoch: 0 },
      ].map(usageSession)),
      sortCol: ref('updated_at'), sortAsc: ref(false),
      sessionTimestamp: () => null, relTime: () => '-', sortVal: () => 0,
      taskName: () => 'Repeated task',
    })
    expect(sortedRows.value.map(row => row.rowIdentity)).toEqual([
      'original:0', 'original:1', 'recreated:0',
    ])
  })

  it('uses the shared task name for the first table column', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [{
        sessionKey: 'agent:main:webchat:private-id',
        title: 'Inspect the long-running launch readiness checklist and summarize risks',
      }].map(usageSession)),
      sortCol: ref('updated_at'),
      sortAsc: ref(false),
      sessionTimestamp: () => null,
      relTime: () => '-',
      sortVal: () => 0,
      taskName: row => String(row.title || 'Untitled task'),
    })

    expect(sortedRows.value[0].sessionLabel).toBe(
      'Inspect the long-running launch readiness checklist and summarize risks',
    )
    expect(sortedRows.value[0].sessionKey).toBe('agent:main:webchat:private-id')
    expect(sortedRows.value[0].rowIdentity).toBe('agent:main:webchat:private-id')
  })

  it('keeps rows with multiple models expandable', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [{
        sessionKey: 'agent:main:webchat:multi-model',
        modelBreakdown: [
          { model: 'provider/primary-model' },
          { model: 'provider/helper-model' },
        ],
      }].map(usageSession)),
      sortCol: ref('updated_at'),
      sortAsc: ref(false),
      sessionTimestamp: () => null,
      relTime: () => '-',
      sortVal: () => 0,
      taskName: () => 'Multi-model task',
    })

    expect(sortedRows.value[0].hasModelBreakdown).toBe(true)
  })

  it('keeps same-label deleted sessions independently expandable', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [
        {
          sessionKey: '',
          sessionId: 'deleted-session-a',
          modelBreakdown: [{ model: 'provider/a' }, { model: 'provider/b' }],
        },
        {
          sessionKey: '',
          sessionId: 'deleted-session-b',
          modelBreakdown: [{ model: 'provider/a' }, { model: 'provider/b' }],
        },
      ].map(usageSession)),
      sortCol: ref('updated_at'),
      sortAsc: ref(false),
      sessionTimestamp: () => null,
      relTime: () => '-',
      sortVal: () => 0,
      taskName: () => 'Untitled task',
    })

    expect(sortedRows.value.map(row => row.sessionLabel)).toEqual([
      'Untitled task',
      'Untitled task',
    ])
    expect(sortedRows.value.map(row => row.rowIdentity)).toEqual([
      'deleted-session-a',
      'deleted-session-b',
    ])

    const expandedSessions = new Set([sortedRows.value[0].rowIdentity])
    expect(sortedRows.value.map(row => expandedSessions.has(row.rowIdentity))).toEqual([
      true,
      false,
    ])
  })

  it('falls back to legacy non-empty session identities', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [
        { sessionKey: '', session: 'legacy-session' },
        { sessionKey: 'legacy-key' },
      ].map(usageSession)),
      sortCol: ref('updated_at'),
      sortAsc: ref(false),
      sessionTimestamp: () => null,
      relTime: () => '-',
      sortVal: () => 0,
      taskName: () => 'Untitled task',
    })

    expect(sortedRows.value.map(row => row.rowIdentity)).toEqual([
      'legacy-session',
      'legacy-key',
    ])
  })
})
