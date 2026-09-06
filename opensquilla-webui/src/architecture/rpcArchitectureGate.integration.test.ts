import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { evaluateRpcArchitectureGate } from '../../scripts/lib/rpc-architecture-gate.mjs'

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

describe('transport architecture hard-zero integration', () => {
  it('rejects generated wire types through data-only facades and Adapter re-exports', () => {
    const root = fixture({
      'src/contracts/generated/v4/routerFeedbackSubmit.ts': `
        export interface Result {
          accepted: boolean
          reason?: string | null
          recorded?: string | null
          [key: string]: unknown
        }
      `,
      'src/contracts/publicData.ts': `
        import type { Result as WireResult } from './generated/v4/routerFeedbackSubmit'
        export type RouteFeedbackResult = Readonly<Pick<WireResult, 'accepted' | 'reason' | 'recorded'>>
      `,
      'src/adapters/gateway/leak.ts': `
        export type { Result } from '../../contracts/generated/v4/routerFeedbackSubmit'
      `,
      'src/modules/leak.ts': `
        import type { Result } from '../adapters/gateway/leak'
        export interface LeakedFeedback { submit(): Promise<Result> }
      `,
      'src/feature.ts': `
        import type { Result } from './contracts/generated/v4/routerFeedbackSubmit'
      `,
    })
    const failures = evaluateRpcArchitectureGate({ root }).failures
    expect(failures).toContain(
      'src/contracts/publicData.ts: generated wire Contract import "./generated/v4/routerFeedbackSubmit" is allowed only in a Gateway Adapter or test.',
    )
    expect(failures.some(failure => failure.startsWith('src/modules/leak.ts:'))).toBe(true)
    expect(failures.some(failure => failure.startsWith('src/feature.ts:'))).toBe(true)
  })

  const requesterFactoryFiles = {
    'src/adapters/gateway/privateTransports.ts': `
      export interface RpcTransport { request(method: string): Promise<unknown> }
      export type RpcRequester = Pick<RpcTransport, 'request'>
    `,
    'src/modules/example.ts': 'export interface Example { read(): Promise<unknown> }',
  }

  it('allows a typed Adapter factory to consume its narrow request dependency', () => {
    const root = fixture({
      ...requesterFactoryFiles,
      'src/adapters/gateway/example.ts': `
        import type { RpcRequester as Requester } from './privateTransports'
        import type { Example } from '../../modules/example'
        export function createExample(rpc: Requester): Example {
          return { read: () => rpc.request('example.read') }
        }
      `,
    })
    expect(evaluateRpcArchitectureGate({ root }).failures).toEqual([])
  })

  it.each([
    'return rpc',
    'const alias = rpc; return alias',
    'return { rpc }',
    'return { read: () => rpc }',
    'return rpc.request',
    "return rpc['request']",
    'return { read: rpc.request.bind(rpc) }',
    'return (() => rpc)()',
    'const leak = () => rpc.request; return leak()',
    'return { read: () => (() => rpc)() }',
  ])('rejects private values returned from request-consuming factories (%#)', (body) => {
    const root = fixture({
      ...requesterFactoryFiles,
      'src/adapters/gateway/example.ts': `
        import type { RpcRequester as Requester } from './privateTransports'
        import type { Example } from '../../modules/example'
        export function createExample(rpc: Requester): Example { ${body} }
      `,
    })
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/example.ts: exported declaration exposes private Gateway transport symbols.',
    )
  })

  it('still rejects request types exposed by modules or Adapter aliases', () => {
    const root = fixture({
      ...requesterFactoryFiles,
      'src/adapters/gateway/leak.ts': `
        import type { RpcRequester } from './privateTransports'
        export type Leaked = RpcRequester
      `,
      'src/modules/leak.ts': `
        import type { RpcRequester } from '../adapters/gateway/privateTransports'
        export function consume(rpc: RpcRequester): Promise<unknown> { return rpc.request('x') }
      `,
    })
    const failures = evaluateRpcArchitectureGate({ root }).failures
    expect(failures).toContain('src/adapters/gateway/leak.ts: exported declaration exposes private Gateway transport symbols.')
    expect(failures.some(failure => failure.startsWith('src/modules/leak.ts:'))).toBe(true)
  })

  it('rejects every raw RPC operation outside its private boundary', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      const rpc = useRpcStore()
      rpc.call('feature.get')
    `)
    const result = evaluateRpcArchitectureGate({ root })

    expect(result).toMatchObject({
      total: 1,
      rpcTotal: 1,
      httpTotal: 0,
    })
    expect(Object.keys(result).sort()).toEqual([
      'failures',
      'httpTotal',
      'rpcTotal',
      'total',
    ])
    expect(result.failures).toContain(
      'src/feature.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    )
  })

  it('groups every forbidden operation by file and kind', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      const rpc = useRpcStore()
      rpc.call('feature.get')
      rpc.call('feature.refresh')
    `)
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/feature.ts: unexpected raw transport call (2); add a domain Adapter instead.',
    )
  })

  it('sorts whole-tree failures independently of source creation order', () => {
    const root = fixture({
      'src/zeta.ts': `
        import type { RpcClient } from './lib/rpc'
        declare const rpc: RpcClient
        rpc.call('zeta.get')
      `,
      'src/lib/rpc.ts': `
        export interface RpcClient { call(method: string): unknown }
      `,
      'src/alpha.ts': `
        import type { RpcClient } from './lib/rpc'
        declare const rpc: RpcClient
        rpc.call('alpha.get')
      `,
    })
    expect(evaluateRpcArchitectureGate({ root }).failures).toEqual([
      'src/alpha.ts: lib/rpc may be imported only by the RPC store or private Gateway transport.',
      'src/alpha.ts: unexpected raw transport call (1); add a domain Adapter instead.',
      'src/zeta.ts: lib/rpc may be imported only by the RPC store or private Gateway transport.',
      'src/zeta.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    ])
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/views/FeatureView.vue: unexpected raw transport call (1); add a domain Adapter instead.',
    )
  })

  it('keeps sessions.search wire literals inside the Contract Adapter', () => {
    const root = seededFixture(`
      import { useRpcStore } from './stores/rpc'
      useRpcStore().call('sessions.search')
    `)
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/feature.ts: sessions.search wire literal is allowed only in its Contract Adapter.',
    )
  })

  it.each([
    'session.event.text_delta',
    'task.succeeded',
  ])('keeps %s wire literals inside Gateway Adapters', (wireName) => {
    const root = seededFixture(`export const leakedEvent = '${wireName}'`, {
      'src/adapters/gateway/eventsV4.ts': `export const wireEvent = '${wireName}'`,
    })
    const failures = evaluateRpcArchitectureGate({ root }).failures

    expect(failures).toContain(
      `src/feature.ts: ${wireName} wire literal is allowed only in a Gateway Adapter, generated Contract, or test.`,
    )
    expect(failures.some(failure => failure.includes('src/adapters/gateway/eventsV4.ts')))
      .toBe(false)
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
    expect(evaluateRpcArchitectureGate({ root })).toMatchObject({
      failures: [],
      total: 0,
    })
  })

  it('keeps HTTP boundary hard-zero outside the private transport and static assets', () => {
    const root = fixture({
      'src/platform/staticAssets.ts': `
        export async function readStaticJson(path: string) {
          const url = new URL(path, location.href)
          if (url.origin !== location.origin || url.pathname.startsWith('/api/')) return null
          return await fetch(url)
        }
      `,
      'src/composables/copiedAssetReader.ts': `
        export async function copied(path: string) {
          return await fetch(path)
        }
      `,
      'src/adapters/gateway/legacyHttpV4.ts': `
        export async function request(path: string) {
          return await fetch(path)
        }
      `,
      'src/adapters/gateway/privateHttpTransport.ts': `
        export async function request(path: string) {
          return await fetch(path)
        }
      `,
    })
    const result = evaluateRpcArchitectureGate({ root })

    expect(result.failures).toContain(
      'src/composables/copiedAssetReader.ts: unexpected raw transport httpRequest (1); add a domain Adapter instead.',
    )
    expect(result.failures).toContain(
      'src/adapters/gateway/legacyHttpV4.ts: unexpected raw transport httpRequest (1); add a domain Adapter instead.',
    )
    expect(result.failures.some(failure => failure.includes('src/platform/staticAssets.ts'))).toBe(false)
    expect(result.failures.some(failure => failure.includes('src/adapters/gateway/privateHttpTransport.ts')))
      .toBe(false)
    expect(result).toMatchObject({ httpTotal: 4, total: 4 })
  })

  it('rejects an Adapter that bypasses the private transport composition', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore(): any { return {} }',
      'src/adapters/gateway/bypass.ts': `
        import { useRpcStore } from '../../stores/rpc.js'
        export const bypass = () => useRpcStore()
      `,
    })
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/bypass.ts: useRpcStore may be imported only by the composition root or tests.',
    )
  })

  it('does not exempt copied Gateway Adapters from raw RPC operations', () => {
    const root = fixture({
      'src/lib/rpc.ts': `
        export interface RpcClient { call(method: string): unknown }
      `,
      'src/adapters/gateway/copiedConversationV4.ts': `
        import type { RpcClient } from '../../lib/rpc'
        declare const rpc: RpcClient
        rpc.call('session.get')
      `,
    })

    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/copiedConversationV4.ts: unexpected raw transport call (1); add a domain Adapter instead.',
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
  ])('rejects a forbidden raw call through $label', ({ files, expected }) => {
    const root = fixture(files as unknown as Record<string, string>)
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(expected)
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
    expect(evaluateRpcArchitectureGate({ root })).toMatchObject({
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/bypass.ts: useRpcStore may be imported only by the composition root or tests.',
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/bypass.ts: useRpcStore may be imported only by the composition root or tests.',
    )
  })

  it('forbids ordinary store barrels from re-exporting the RPC factory', () => {
    const root = fixture({
      'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
      'src/stores/index.ts': `export * from './rpc'`,
    })
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/adapters/gateway/classExpressionLeak.ts: exported declaration exposes private Gateway transport symbols.',
    )
  })

  it('fences private symbols through multiple ESM barrel re-exports', () => {
    const root = fixture({
      'src/adapters/gateway/privateTransports.ts': 'export const hidden = 1',
      'src/adapters/gateway/index.ts': `export { hidden as h } from './privateTransports'`,
      'src/adapters/gateway/leak.ts': `export { h as publicHidden } from './index'`,
    })
    const failures = evaluateRpcArchitectureGate({ root }).failures
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
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
    const failures = evaluateRpcArchitectureGate({ root }).failures
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
    expect(evaluateRpcArchitectureGate({ root }).failures).toContain(
      'src/feature.ts: unexpected raw transport call (1); add a domain Adapter instead.',
    )
    expect(performance.now() - started).toBeLessThan(500)
  }, 1_000)
})
