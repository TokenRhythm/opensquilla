import { describe, expect, it } from 'vitest'
import { usageSession, usageSnapshot, usageTotals } from '@/testing/usage.test-helper'
import { usageTaskRows } from './usageTaskRows'

describe('usage parent task rows', () => {
  it('uses each parent total and merged model breakdown once, including roots without direct calls', () => {
    const totals = usageTotals({ cost: 0.7, eventCount: 5, sessions: 4, costUsdExact: '0.70000000001' })
    const modelBreakdown = [{ provider: 'tokenrhythm', model: 'image', costUsd: 0.7,
      capability: 'image_generation', meters: [{ metric: 'output_images', unit: 'image',
        quantity: '1', dimensions: {}, unknownCount: 0, measurementCount: 1 }] }]
    const snapshot = usageSnapshot({ totals, sessions: [usageSession({ sessionId: 'child', costUsd: 0.7 })],
      taskGroups: [{ taskGroupId: 'task', rootSessionId: 'root', rootSessionEpoch: 2,
        sessionKey: 'parent-key', title: 'Renamed parent', attributionStatus: 'complete',
        firstUsageAtMs: 100, lastUsageAtMs: 200, modelBreakdown, totals,
        branches: { self: usageTotals(), inlineSubagents: usageTotals(), subsessions: totals,
          sideChats: usageTotals(), unattributed: usageTotals() }, runs: [] }] })
    expect(usageTaskRows(snapshot)).toHaveLength(1)
    expect(usageTaskRows(snapshot)[0]).toMatchObject({ sessionId: 'root', sessionEpoch: 2,
      sessionKey: 'parent-key', title: 'Renamed parent', costUsd: 0.7, model: 'image',
      costUsdExact: '0.70000000001', startedAt: 100, updatedAt: 200, modelBreakdown })
    expect(snapshot.totals.cost).toBe(0.7)
  })

  it('does not expose child rows when the parent projection is empty', () => {
    const sessions = [usageSession({ sessionId: 'child', costUsd: 1 })]
    expect(usageTaskRows(usageSnapshot({ taskGroups: [], sessions }))).toEqual([])
    expect(usageTaskRows(usageSnapshot({ sessions }))).toBe(sessions)
  })
})
