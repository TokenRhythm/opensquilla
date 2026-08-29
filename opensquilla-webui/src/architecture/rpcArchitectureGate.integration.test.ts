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

  it('analyzes RPC provenance from Vue script setup blocks', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/views/FeatureView.vue': `
        <template><main /></template>
        <script setup lang="ts">
        import { useRpcStore } from '../stores/rpc'
        const rpc = useRpcStore()
        rpc.call('feature.get')
        </script>
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/views/FeatureView.vue: unexpected raw transport call (1); add a domain Adapter instead.',
    )
  })

  it('keeps sessions.search wire literals inside the Contract Adapter', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      useRpcStore().call('sessions.search')
    `)
    expect(evaluateRpcArchitectureGate({ root, debtLanes: oneCallLane }).failures).toContain(
      'src/feature.ts: sessions.search wire literal is allowed only in its Contract Adapter.',
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

  it.each([
    {
      label: 'anonymous default return',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
        'src/wrapper.ts': `
          import { useRpcStore } from './stores/rpc'
          export default () => useRpcStore()
        `,
        'src/feature.ts': `
          import backend from './wrapper'
          backend().call('feature.get')
        `,
      },
      expected: 'src/feature.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    },
    {
      label: 'index barrel and local factory alias',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
        'src/stores/index.ts': `export { useRpcStore } from './rpc'`,
        'src/feature.ts': `
          import { useRpcStore } from './stores'
          const make = useRpcStore
          make().call('feature.get')
        `,
      },
      expected: 'src/feature.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    },
    {
      label: 'CommonJS bracket member',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
        'src/feature.js': `
          const rpc = require('./stores/rpc')['useRpcStore']()
          rpc.call('feature.get')
        `,
      },
      expected: 'src/feature.js: unexpected raw transport call (1); add a domain Adapter instead.',
    },
    {
      label: 'nested object and array argument',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
        'src/consumer.ts': `
          export function consume({ nested: [{ rpc }] }: any) {
            rpc.call('feature.get')
          }
        `,
        'src/feature.ts': `
          import { useRpcStore } from './stores/rpc'
          import { consume } from './consumer'
          consume({ nested: [{ rpc: useRpcStore() }] })
        `,
      },
      expected: 'src/consumer.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    },
  ])('rejects an unapproved raw call through $label', ({ files, expected }) => {
    const root = fixture(files as unknown as Record<string, string>)
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(expected)
  })

  it('does not merge same-named values from separate lexical scopes', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      function seed() {
        const client = useRpcStore()
        return client
      }
      function cacheOnly() {
        const client = { call(key: string) { return key } }
        client.call('cache')
      }
      function shadowed(useRpcStore: () => { call(key: string): string }) {
        useRpcStore().call('cache')
      }
      void seed
      void cacheOnly
      void shadowed
    `)
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] })).toMatchObject({
      failures: [],
      rpcTotal: 0,
    })
  })

  it('rejects useRpcStore imported into an Adapter through an index barrel', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/stores/bridge.ts': `export { useRpcStore as backendStore } from './rpc'`,
      'src/stores/index.ts': `export { backendStore as useRpcStore } from './bridge'`,
      'src/adapters/gateway/bypass.ts': `
        import { useRpcStore } from '../../stores'
        export const bypass = () => useRpcStore()
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/adapters/gateway/bypass.ts: Gateway Adapters must consume the private transport Interface instead of useRpcStore.',
    )
  })

  it('rejects a namespace import of useRpcStore through an index barrel', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/stores/index.ts': `export * from './rpc'`,
      'src/adapters/gateway/bypass.ts': `
        import * as stores from '../../stores'
        export const bypass = () => stores.useRpcStore()
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/adapters/gateway/bypass.ts: Gateway Adapters must consume the private transport Interface instead of useRpcStore.',
    )
  })

  it('allows ordinary store barrels to re-export the RPC factory', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
      'src/stores/index.ts': `export * from './rpc'`,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).not.toContain(
      'src/stores/index.ts: RPC store factory modules must not be re-exported through a barrel.',
    )
  })

  it('fences private symbols returned from exported class expressions', () => {
    const root = fixture({
      'src/adapters/gateway/privateTransports.ts': 'export const hidden = 1',
      'src/adapters/gateway/classExpressionLeak.ts': `
        import { hidden } from './privateTransports'
        export const PublicClient = class {
          field = hidden
          get() { return hidden }
        }
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/adapters/gateway/classExpressionLeak.ts: exported declaration exposes private Gateway transport symbols.',
    )
  })

  it('fences private symbols through multiple ESM barrel re-exports', () => {
    const root = fixture({
      'src/adapters/gateway/privateTransports.ts': 'export const hidden = 1',
      'src/adapters/gateway/index.ts': `export { hidden as h } from './privateTransports'`,
      'src/adapters/gateway/leak.ts': `export { h as publicHidden } from './index'`,
    })
    const failures = evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures
    expect(failures).toEqual(expect.arrayContaining([
      'src/adapters/gateway/index.ts: private Gateway transport modules must not be re-exported through a barrel.',
      'src/adapters/gateway/leak.ts: private Gateway transport modules must not be re-exported through a barrel.',
    ]))
  })

  it('fails fast when the canonical RPC store loses its named ESM seed export', () => {
    const root = fixture({
      'src/stores/rpc.ts': `
        function useRpcStore() { return {} }
        module.exports.useRpcStore = useRpcStore
      `,
    })
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/stores/rpc.ts: RPC provenance seed must remain an ESM named export "useRpcStore".',
    )
  })

  it('rejects function and CommonJS private transport exports from an Adapter', () => {
    const root = fixture({
      'src/adapters/gateway/privateTransports.ts': `
        export interface RpcTransport { request(method: string): unknown }
      `,
      'src/adapters/gateway/functionLeak.ts': `
        import type { RpcTransport } from './privateTransports'
        export function expose(value: RpcTransport): RpcTransport { return value }
      `,
      'src/adapters/gateway/cjsLeak.js': `
        module.exports['transport'] = require('./privateTransports')['RpcTransport']
      `,
    })
    const failures = evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures
    expect(failures).toEqual(expect.arrayContaining([
      'src/adapters/gateway/functionLeak.ts: exported declaration exposes private Gateway transport symbols.',
      'src/adapters/gateway/cjsLeak.js: CommonJS export exposes private Gateway transport symbols.',
    ]))
  })

  it('terminates recursive shape analysis while preserving reachable depth', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/wrapper.ts': `
        export function wrap(rpc: unknown, depth: number): unknown {
          if (depth <= 0) return { rpc }
          return { next: wrap(rpc, depth - 1) }
        }
      `,
      'src/feature.ts': `
        import { useRpcStore } from './stores/rpc'
        import { wrap } from './wrapper'
        const wrapped = wrap(useRpcStore(), 2) as any
        wrapped.next.next.rpc.call('feature.get')
      `,
    })
    const started = performance.now()
    expect(evaluateRpcArchitectureGate({ root, debtLanes: [] }).failures).toContain(
      'src/feature.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    )
    expect(performance.now() - started).toBeLessThan(500)
  }, 1_000)
})
