import { computed, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import { useUsageSessionRows } from './useUsageSessionRows'

describe('useUsageSessionRows', () => {
  it('uses the shared task name for the first table column', () => {
    const { sortedRows } = useUsageSessionRows({
      visibleSessions: computed(() => [{
        sessionKey: 'agent:main:webchat:private-id',
        title: 'Inspect the long-running launch readiness checklist and summarize risks',
      }]),
      rangeHiddenHint: computed(() => ''),
      sortCol: ref('updated_at'),
      sortAsc: ref(false),
      rowVal: (row, ...keys) => keys.map(key => row[key]).find(value => value != null),
      numericRowVal: () => null,
      sessionTimestamp: () => null,
      relTime: () => '-',
      sortVal: () => 0,
      taskName: row => String(row.title || 'Untitled task'),
    })

    expect(sortedRows.value[0].sessionLabel).toBe(
      'Inspect the long-running launch readiness checklist and summarize risks',
    )
    expect(sortedRows.value[0].sessionKey).toBe('agent:main:webchat:private-id')
  })
})
