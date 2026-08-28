import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { evaluateRpcArchitectureGate } from '../../scripts/lib/rpc-architecture-gate.mjs'

type Debt = Record<string, Record<string, number>>
type DebtLane = { lane: string; debt: Debt }

const roots: string[] = []

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true })
})

function fixture(files: Record<string, string>): string {
  const root = mkdtempSync(join(tmpdir(), 'opensquilla-rpc-gate-'))
  roots.push(root)
  for (const [rel, contents] of Object.entries(files)) {
    const path = join(root, rel)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, contents)
  }
  return root
}

function seededFixture(feature: string, extra: Record<string, string> = {}): string {
  return fixture({
    'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
    'src/feature.ts': feature,
    ...extra,
  })
}

const oneCallLane: DebtLane[] = [{
  lane: 'fixture',
  debt: { 'src/feature.ts': { call: 1 } },
}]

describe('transport architecture gate ledger integration', () => {
  it('accepts an exact seeded ledger', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      const rpc = useRpcStore()
      rpc.call('feature.get')
    `)
    expect(evaluateRpcArchitectureGate({ root, debtLanes: oneCallLane })).toMatchObject({
      failures: [],
      total: 1,
      rpcTotal: 1,
      httpTotal: 0,
    })
  })

  it('fails when the count changes', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      const rpc = useRpcStore()
      rpc.call('feature.get')
      rpc.call('feature.refresh')
    `)
    expect(evaluateRpcArchitectureGate({ root, debtLanes: oneCallLane }).failures).toContain(
      'src/feature.ts: raw transport call count is 2; lane debt requires 1.',
    )
  })

  it('fails on an unapproved new file', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      useRpcStore().call('feature.get')
    `, {
      'src/extra.ts': `
        import { useRpcStore } from './stores/rpc'
        useRpcStore().call('extra.get')
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: oneCallLane }).failures).toContain(
      'src/extra.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    )
  })

  it('fails when a paid-down entry remains in the ledger', () => {
    const root = seededFixture('export const value = 1')
    expect(evaluateRpcArchitectureGate({ root, debtLanes: oneCallLane }).failures).toContain(
      'src/feature.ts: stale raw transport call debt (1); remove it from its lane file.',
    )
  })

  it('fails when two lanes claim the same file', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      useRpcStore().call('feature.get')
    `)
    const failures = evaluateRpcArchitectureGate({
      root,
      debtLanes: [
        ...oneCallLane,
        { lane: 'duplicate', debt: { 'src/feature.ts': { call: 1 } } },
      ],
    }).failures
    expect(failures).toContain(
      'src/feature.ts: transport debt is owned by both fixture and duplicate.',
    )
  })

  it('does not charge a local same-named call/wait interface', () => {
    const root = fixture({
      'src/cache.ts': `
        interface CacheClient {
          call(key: string): unknown
          waitForConnection(): unknown
        }
        const cache: CacheClient = {
          call: key => key,
          waitForConnection: () => undefined,
        }
        cache.call('entry')
        cache.waitForConnection()
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] })).toMatchObject({
      failures: [],
      total: 0,
    })
  })

  it('rejects an Adapter that bypasses the private transport composition', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/adapters/gateway/bypass.ts': `
        import { useRpcStore } from '../../stores/rpc.js'
        export const bypass = () => useRpcStore()
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/adapters/gateway/bypass.ts: Gateway Adapters must consume the private transport Interface instead of useRpcStore.',
    )
  })
})
