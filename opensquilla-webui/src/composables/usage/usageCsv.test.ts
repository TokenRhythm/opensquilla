import Papa from 'papaparse'
import { describe, expect, it } from 'vitest'
import { usageSession, usageSnapshot, usageTotals } from '@/testing/usage.test-helper'
import { buildUsageCsv } from './usageCsv'

describe('usage CSV accounting projections', () => {
  it('keeps parent and capability projections as metadata without adding charges', () => {
    const sessions = [
      usageSession({ session: 'main', sessionId: 'main', sessionEpoch: 0, taskGroupId: 'task', costUsd: 0.2 }),
      usageSession({ session: 'side', sessionId: 'side', sessionEpoch: 0, taskGroupId: 'task',
        parentSessionId: 'main', parentSessionEpoch: 0, costUsd: 0.3 }),
    ]
    const totals = usageTotals({ cost: 0.5, costUsdExact: '0.5', sessions: 2, eventCount: 2 })
    const snapshot = usageSnapshot({ sessions, totals, taskGroups: [{
      taskGroupId: 'task', rootSessionId: 'main', rootSessionEpoch: 0,
      sessionKey: null, title: 'Parent task', attributionStatus: 'complete', totals,
      branches: { self: usageTotals({ cost: 0.2 }), sideChats: usageTotals({ cost: 0.3 }),
        inlineSubagents: usageTotals(), subsessions: usageTotals(), unattributed: usageTotals() },
      runs: [],
    }], capabilities: [{ capability: 'image_generation', totals, meters: [], tokenUnknownItemCount: 2 }] })
    const parsed = Papa.parse<Record<string, string>>(buildUsageCsv(snapshot, sessions, 6.975), { header: true })
    expect(parsed.errors).toEqual([])
    expect(parsed.data.map(row => row.row_type)).toEqual(['summary', 'task'])
    expect(parsed.data[0].cost_usd).toBe('0.500000000')
    expect(parsed.data[0].cost_cny).toBe('3.487500000')
    expect(parsed.data.slice(1).reduce((sum, row) => sum + Number(row.cost_usd), 0)).toBe(0.5)
    expect(JSON.parse(parsed.data[0].task_groups_json)[0].totals.cost).toBe(0.5)
    expect(JSON.parse(parsed.data[0].capabilities_json)[0].totals.cost).toBe(0.5)
    expect(parsed.data.slice(1).map(row => row.task_groups_json)).toEqual([''])
    expect(parsed.data[1].session_id).toBe('main')
    expect(parsed.data[1].session).toBe('Parent task')
    expect(parsed.data[1].parent_session_id).toBe('')
    expect(parsed.data[0].task_count).toBe('1')
    expect(parsed.data[1].cost_usd_exact).toBe('0.5')
  })

  it('keeps legacy session exports but never falls back to child rows for an empty task projection', () => {
    const sessions = [usageSession({ sessionId: 'child', session: 'child', costUsd: 1 })]
    const legacy = Papa.parse<Record<string, string>>(buildUsageCsv(usageSnapshot({ sessions }), sessions), { header: true })
    expect(legacy.data.map(row => row.row_type)).toEqual(['summary', 'session'])
    const parentEmpty = Papa.parse<Record<string, string>>(buildUsageCsv(usageSnapshot({
      sessions, taskGroups: [], totals: usageTotals({ cost: 3, taskCount: 0 }),
    }), sessions), { header: true })
    expect(parentEmpty.data.map(row => row.row_type)).toEqual(['summary'])
    expect(parentEmpty.data[0].cost_usd).toBe('3.000000000')
    expect(parentEmpty.data[0].task_count).toBe('0')
  })
})
