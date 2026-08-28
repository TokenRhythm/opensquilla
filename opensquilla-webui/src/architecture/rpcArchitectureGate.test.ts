import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  boundaryReexportViolation,
  collectBoundaryArchitectureViolations,
  callMemberReceiverText,
  callMemberReferenceReceiverText,
  destructuredCallSourceText,
  destructuredMemberSourceText,
  generatedContractImportViolation,
  gatewayAdapterRpcStoreImportViolation,
  isDirectCallMemberReference,
  isKnownNonRpcCallReceiver,
  isRpcCapabilityReceiverText,
  moduleReferenceSpecifier,
  namedMemberCallReceiverText,
  namedMemberReferenceReceiverText,
  privateGatewayTransportImportViolation,
  resolveSourceImport,
} from '../../scripts/lib/rpc-architecture-imports.mjs'
import { collectHttpBoundaryOperations } from '../../scripts/lib/http-architecture-provenance.mjs'
import { exactTransportDebtFailures } from '../../scripts/lib/exact-transport-debt.mjs'
import { collectRpcTransportOperations } from '../../scripts/lib/rpc-symbol-provenance.mjs'

const fixtureRoot = resolve('rpc-architecture-fixture')
const require = createRequire(import.meta.url)
const ts = require('typescript') as typeof import('typescript')

function source(code: string) {
  return ts.createSourceFile(
    'fixture.ts',
    code,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  )
}

function nodes(code: string) {
  const parsed = source(code)
  const result: import('typescript').Node[] = []
  function visit(node: import('typescript').Node) {
    result.push(node)
    ts.forEachChild(node, visit)
  }
  visit(parsed)
  return { parsed, result }
}

describe('RPC generated Contract import fence', () => {
  it.each([
    {
      importer: 'src/contracts/manualWire.ts',
      specifier: './generated/v4/sessionsList',
    },
    {
      importer: 'src/views/SessionsView.vue',
      specifier: '../contracts/generated/v4/sessionsList',
    },
    {
      importer: 'src/modules/sessionDirectory.ts',
      specifier: '@/contracts/generated/v4/sessionsList',
    },
  ])('rejects generated wire imports resolved from $specifier', ({ importer, specifier }) => {
    expect(generatedContractImportViolation({
      root: fixtureRoot,
      importer,
      specifier,
    })).toBe(
      `${importer}: generated wire Contract import "${specifier}" is allowed only in a Gateway Adapter or test.`,
    )
  })

  it.each([
    {
      importer: 'src/adapters/gateway/sessionDirectoryV4.ts',
      specifier: '../../contracts/generated/v4/sessionsList',
    },
    {
      importer: 'src/contracts/sessionsList.contract.test.ts',
      specifier: './generated/v4/sessionsList',
    },
    {
      importer: 'src/contracts/generated/v4/reexport.ts',
      specifier: './sessionsList',
    },
  ])('preserves the existing Adapter/test/generated exemption for $importer', ({ importer, specifier }) => {
    expect(generatedContractImportViolation({
      root: fixtureRoot,
      importer,
      specifier,
    })).toBeNull()
  })

  it('normalizes traversal and does not confuse a similarly named directory', () => {
    expect(resolveSourceImport(
      fixtureRoot,
      'src/views/SessionsView.vue',
      '../contracts/./generated/v4/sessionsList?raw',
    )).toBe(resolve(fixtureRoot, 'src/contracts/generated/v4/sessionsList'))
    expect(generatedContractImportViolation({
      root: fixtureRoot,
      importer: 'src/views/SessionsView.vue',
      specifier: '../contracts/generatedish/v4/sessionsList',
    })).toBeNull()
  })

  it.each([
    `export type { SessionRow } from '@/contracts/generated/v4/sessionsList'`,
    'void import(`@/contracts/generated/v4/sessionsList`)',
    `type Row = import('@/contracts/generated/v4/sessionsList').SessionRow`,
    `type Mod = typeof import('@/contracts/generated/v4/sessionsList')`,
    `import wire = require('@/contracts/generated/v4/sessionsList')`,
    `const wire = require('@/contracts/generated/v4/sessionsList')`,
  ])('finds generated wire module references in %s', (code) => {
    const { result } = nodes(code)
    expect(result.map(node => moduleReferenceSpecifier(ts, node)).filter(Boolean)).toEqual([
      '@/contracts/generated/v4/sessionsList',
    ])
  })
})

describe('private Gateway transport import fence', () => {
  it.each([
    'src/main.ts',
    'src/views/SessionsView.vue',
    'src/composables/useSessions.ts',
    'src/modules/sessionDirectory.ts',
    'src/stores/app.ts',
    'src/adapters/platform/nativeHost.ts',
  ])('rejects generic transport imports from %s', (importer) => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer,
      specifier: '@/adapters/gateway/privateTransports',
    })).toBe(
      `${importer}: private Gateway transports may be imported only by a Gateway Adapter or test.`,
    )
  })

  it.each([
    ['src/adapters/gateway/sessionDirectoryV4.ts', './privateTransports.js'],
    ['src/adapters/gateway/privateTransports.test.ts', './privateTransports'],
  ])('allows the explicit transport boundary in %s', (importer, specifier) => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer,
      specifier,
    })).toBeNull()
  })

  it('does not block similarly named modules', () => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer: 'src/views/SessionsView.vue',
      specifier: '@/adapters/gateway/privateTransportsFixture',
    })).toBeNull()
  })

  it.each([
    ['src/adapters/gateway/index.ts', './privateTransports.js'],
    ['src/contracts/index.ts', './generated/v4/sessionsList'],
  ])('forbids boundary barrel re-exports from %s', (importer, specifier) => {
    expect(boundaryReexportViolation({ root: fixtureRoot, importer, specifier })).toBe(
      `${importer}: ${specifier.includes('private') ? 'private Gateway transport' : 'generated Contract'} modules must not be re-exported through a barrel.`,
    )
  })

  it('applies direct barrel fences to production JavaScript', () => {
    const parsed = ts.createSourceFile(
      'barrel.js',
      `
        export { SessionRow } from './contracts/generated/v4/sessionsList.js'
      `,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.JS,
    )
    const directExport = parsed.statements.find(statement => (
      ts.isExportDeclaration(statement) && statement.moduleSpecifier
    ))
    if (!directExport) throw new Error('missing JavaScript export fixture')
    const specifier = moduleReferenceSpecifier(ts, directExport)
    expect(specifier).toBe('./contracts/generated/v4/sessionsList.js')
    expect(boundaryReexportViolation({
      root: fixtureRoot,
      importer: 'src/barrel.js',
      specifier: String(specifier),
    })).toBe(
      'src/barrel.js: generated Contract modules must not be re-exported through a barrel.',
    )
  })

  it.each([
    ['src/adapters/platform/nativeHost.ts', '@/contracts/generated/v4/sessionsList'],
    ['src/views/SessionsView.js', '@/contracts/generated/v4/sessionsList'],
  ])('does not exempt non-Gateway production inputs such as %s', (importer, specifier) => {
    expect(generatedContractImportViolation({ root: fixtureRoot, importer, specifier })).not.toBeNull()
  })

  it('applies the private transport fence to production JavaScript', () => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer: 'src/views/SessionsView.js',
      specifier: '@/adapters/gateway/privateTransports',
    })).not.toBeNull()
  })

  it('allows only the private transport composition root to import useRpcStore', () => {
    expect(gatewayAdapterRpcStoreImportViolation({
      root: fixtureRoot,
      importer: 'src/adapters/gateway/sessionDirectoryV4.ts',
      specifier: '@/stores/rpc.js',
    })).toBe(
      'src/adapters/gateway/sessionDirectoryV4.ts: Gateway Adapters must consume the private transport Interface instead of useRpcStore.',
    )
    expect(gatewayAdapterRpcStoreImportViolation({
      root: fixtureRoot,
      importer: 'src/adapters/gateway/privateTransports.ts',
      specifier: '../../stores/rpc.js',
    })).toBeNull()
    expect(gatewayAdapterRpcStoreImportViolation({
      root: fixtureRoot,
      importer: 'src/views/ChatView.vue',
      specifier: '@/stores/rpc',
    })).toBeNull()
  })

  it.each([
    'src/views/SessionsView.vue',
    'src/composables/useApprovals.ts',
    'src/modules/sessionDirectory.ts',
    'src/stores/app.ts',
    'src/adapters/platform/nativeHost.ts',
  ])('keeps the private HTTP transport out of %s', (importer) => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer,
      specifier: '@/adapters/gateway/privateHttpTransport',
    })).toBe(
      `${importer}: private Gateway HTTP transport may be imported only by a Gateway Adapter, composition root, or test.`,
    )
  })

  it.each([
    'src/main.ts',
    'src/adapters/gateway/approvalCenterV4.ts',
    'src/adapters/gateway/privateHttpTransport.test.ts',
  ])('allows the HTTP seam at %s', (importer) => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer,
      specifier: '@/adapters/gateway/privateHttpTransport',
    })).toBeNull()
  })

  it('does not block a similarly named HTTP helper', () => {
    expect(privateGatewayTransportImportViolation({
      root: fixtureRoot,
      importer: 'src/views/SessionsView.vue',
      specifier: '@/adapters/gateway/privateHttpTransportFixture',
    })).toBeNull()
  })

  it('forbids re-exporting the private HTTP transport from the composition root', () => {
    expect(boundaryReexportViolation({
      root: fixtureRoot,
      importer: 'src/main.ts',
      specifier: '@/adapters/gateway/privateHttpTransport',
    })).toBe(
      'src/main.ts: private Gateway transport modules must not be re-exported through a barrel.',
    )
  })
})

describe('Gateway HTTP boundary debt syntax', () => {
  it('tracks API path fragments, auth storage, and both protected headers', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      sources: provenanceSources({
        'src/consumer.ts': `
          const endpoint = \`/api/v1/sessions/\${sessionId}\`
          const token = sessionStorage.getItem('opensquilla.wsToken')
          const headers = { Authorization: \`Bearer \${token}\` }
          headers['x-opensquilla-session-key'] = sessionId
          void fetch(endpoint, { headers })
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/consumer.ts', kind: 'httpRequest' },
      { rel: 'src/consumer.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/consumer.ts', kind: 'httpAuthToken' },
      { rel: 'src/consumer.ts', kind: 'httpAuthorizationHeader' },
      { rel: 'src/consumer.ts', kind: 'httpSessionKeyHeader' },
    ])
  })

  it('tracks Headers method syntax and case-insensitive names once each', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      sources: provenanceSources({
        'src/consumer.ts': `
          const headers = new Headers()
          headers.set('authorization', token)
          headers.append('X-OpenSquilla-Session-Key', sessionId)
          void fetch('/api/value', { headers })
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/consumer.ts', kind: 'httpRequest' },
      { rel: 'src/consumer.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/consumer.ts', kind: 'httpAuthorizationHeader' },
      { rel: 'src/consumer.ts', kind: 'httpSessionKeyHeader' },
    ])
  })

  it('does not count asset, data, blob, external fetches, comments, or regexes', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      sources: provenanceSources({
        'src/assets.ts': `
          // fetch('/api/comment-only')
          const apiMatcher = /^\\/api\\/v1\\//
          void apiMatcher
          void fetch('/assets/logo.svg')
          void fetch('data:image/png;base64,AA==')
          void fetch('blob:https://control.example/id')
          void fetch('https://cdn.example/api/image.png')
        `,
      }),
    })

    expect(operations).toEqual([])
  })

  it('does not treat similarly named application fields as protected headers', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      sources: provenanceSources({
        'src/domain.ts': `
          const authorizationState = 'ready'
          const sessionKeyHeaderVisible = true
          void authorizationState
          void sessionKeyHeaderVisible
        `,
      }),
    })

    expect(operations).toEqual([])
  })

  it('rejects both unapproved growth and stale HTTP debt entries', () => {
    expect(exactTransportDebtFailures(
      'httpApiEndpoint',
      new Map([['src/old.ts', 1]]),
      new Map([['src/new.ts', 1]]),
    )).toEqual([
      'src/new.ts: unexpected raw transport httpApiEndpoint (1); add a domain Adapter instead.',
      'src/old.ts: stale raw transport httpApiEndpoint debt (1); remove it from its lane file.',
    ])
  })
})

describe('raw RPC call ledger syntax', () => {
  it.each([
    ['renamed.call("sessions.list")', 'renamed'],
    ['gateway["call"]("sessions.list")', 'gateway'],
    ['useRpcStore().call("sessions.list")', 'useRpcStore()'],
    ['(client.call)("sessions.list")', 'client'],
    ['(client["call"])("sessions.list")', 'client'],
  ])('tracks receiver aliases in %s', (code, expected) => {
    const { parsed, result } = nodes(code)
    expect(
      result
        .map(node => callMemberReceiverText(ts, node, parsed))
        .filter(Boolean),
    ).toEqual([expected])
  })

  it('distinguishes the known hasOwnProperty helper from RPC receivers', () => {
    const { parsed, result } = nodes('Object.prototype.hasOwnProperty.call(value, "field")')
    expect(
      result
        .map(node => callMemberReceiverText(ts, node, parsed))
        .filter(Boolean),
    ).toEqual(['Object.prototype.hasOwnProperty'])
    expect(isKnownNonRpcCallReceiver('Object.prototype.hasOwnProperty')).toBe(true)
    expect(isKnownNonRpcCallReceiver('Object.prototype.toString')).toBe(true)
    expect(isKnownNonRpcCallReceiver('client')).toBe(false)
  })

  it.each([
    ['const { call } = useRpcStore()', 'useRpcStore()'],
    ['const { call: invoke } = gateway', 'gateway'],
    ['function run({ call }: RpcClient) {}', 'RpcClient'],
  ])('detects destructured RPC call capabilities in %s', (code, expected) => {
    const { parsed, result } = nodes(code)
    const sources = result
      .map(node => destructuredCallSourceText(ts, node, parsed))
      .filter((value): value is string => Boolean(value))
    expect(sources).toEqual([expected])
    expect(sources.every(isRpcCapabilityReceiverText)).toBe(true)
  })

  it('does not classify unrelated call data destructuring as RPC', () => {
    const { parsed, result } = nodes('function render({ call }: ToolTraceItem) {}')
    const sources = result
      .map(node => destructuredCallSourceText(ts, node, parsed))
      .filter((value): value is string => Boolean(value))
    expect(sources).toEqual(['ToolTraceItem'])
    expect(sources.some(isRpcCapabilityReceiverText)).toBe(false)
  })

  it.each([
    ['const invoke = client.call; invoke("sessions.list")', 'client'],
    ['const invoke = client.call.bind(client); invoke("sessions.list")', 'client'],
  ])('detects extracted RPC call capabilities in %s', (code, expected) => {
    const { parsed, result } = nodes(code)
    const references = result
      .map(node => ({
        node,
        receiver: callMemberReferenceReceiverText(ts, node, parsed),
      }))
      .filter(item => item.receiver && !isDirectCallMemberReference(ts, item.node))
      .map(item => item.receiver)
    expect(references).toEqual([expected])
    expect(references.every(value => isRpcCapabilityReceiverText(String(value)))).toBe(true)
  })
})

describe('raw RPC member debt syntax', () => {
  it.each([
    ['rpc.on("sessions.changed", handler)', 'on', 'rpc'],
    ['rpc["supportsMethod"]("sessions.list")', 'supportsMethod', 'rpc'],
    ['options.rpc.waitForConnection()', 'waitForConnection', 'options.rpc'],
    ['gateway.markMethodUnavailable("legacy")', 'markMethodUnavailable', 'gateway'],
  ])('tracks %s', (code, memberName, expectedReceiver) => {
    const { parsed, result } = nodes(code)
    expect(
      result
        .map(node => namedMemberCallReceiverText(ts, node, parsed, memberName))
        .filter(Boolean),
    ).toEqual([expectedReceiver])
  })

  it('detects extracted and destructured event capabilities', () => {
    const { parsed, result } = nodes(`
      const listen = rpc.on
      const { supportsMethod } = gateway
    `)
    const references = result
      .map(node => namedMemberReferenceReceiverText(ts, node, parsed, 'on'))
      .filter(Boolean)
    const destructured = result
      .map(node => destructuredMemberSourceText(ts, node, parsed, 'supportsMethod'))
      .filter(Boolean)
    expect(references).toEqual(['rpc'])
    expect(destructured).toEqual(['gateway'])
  })
})

function provenanceSources(entries: Record<string, string>) {
  return Object.entries(entries).map(([rel, code]) => ({
    rel,
    source: ts.createSourceFile(
      rel,
      code,
      ts.ScriptTarget.Latest,
      true,
      rel.endsWith('.js') ? ts.ScriptKind.JS : ts.ScriptKind.TS,
    ),
  }))
}

describe('HTTP symbol provenance', () => {
  it('follows global Fetch through objects, nested destructuring, and an imported wrapper', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/wrapper.ts': `
          export function invoke(client: (endpoint: string, init: object) => unknown, endpoint: string, init: object) {
            return client(endpoint, init)
          }
        `,
        'src/consumer.ts': `
          import { invoke } from './wrapper'
          const platform = { network: { fetch: globalThis.fetch } }
          const { network: { fetch: request } } = platform
          const AUTH = 'Authorization'
          const headers = new Headers()
          headers.set(AUTH, sessionStorage.getItem('opensquilla.wsToken'))
          headers.set('x-opensquilla-session-key', 'session-a')
          void invoke(request, '/api/items', { headers })
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/wrapper.ts', kind: 'httpRequest' },
      { rel: 'src/wrapper.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/wrapper.ts', kind: 'httpAuthToken' },
      { rel: 'src/wrapper.ts', kind: 'httpAuthorizationHeader' },
      { rel: 'src/wrapper.ts', kind: 'httpSessionKeyHeader' },
    ])
  })

  it('expands a two-hop imported callable chain to a fixed point', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/consumer.ts': `
          import { bridge } from './bridge'
          void bridge(globalThis.fetch, '/api/two-hop', {})
        `,
        'src/bridge.ts': `
          import { forward } from './forward'
          export function bridge(client: (value: string, init: object) => unknown, value: string, init: object) {
            return forward(client, value, init)
          }
        `,
        'src/forward.ts': `
          export function forward(client: (value: string, init: object) => unknown, value: string, init: object) {
            return client(value, init)
          }
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/forward.ts', kind: 'httpRequest' },
      { rel: 'src/forward.ts', kind: 'httpApiEndpoint' },
    ])
  })

  it('discovers a callable returned through an imported object property', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/consumer.ts': `
          import { make } from './factory'
          void make(globalThis.fetch).invoke('/api/object-wrapper', {})
        `,
        'src/factory.ts': `
          export function make(client: (value: string, init: object) => unknown) {
            return {
              invoke(value: string, init: object) {
                return client(value, init)
              },
            }
          }
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/factory.ts', kind: 'httpRequest' },
      { rel: 'src/factory.ts', kind: 'httpApiEndpoint' },
    ])
  })

  it('counts a Request-to-fetch pipeline once and follows cyclic RequestInit aliases', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/request.ts': `
          const NativeRequest = globalThis.Request
          const headers = new globalThis.Headers()
          headers.set('Authorization', sessionStorage.getItem('opensquilla.wsToken'))
          let left: RequestInit = {}
          let right: RequestInit = left
          left = right
          right = left
          const request = new NativeRequest('/api/items', left)
          void globalThis.fetch(request, { headers })
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/request.ts', kind: 'httpRequest' },
      { rel: 'src/request.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/request.ts', kind: 'httpAuthToken' },
      { rel: 'src/request.ts', kind: 'httpAuthorizationHeader' },
    ])
  })

  it('uses lexical symbol identity instead of names from another scope', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/shadowed.ts': `
          function unrelated(fetch: (value: string) => void, globalThis: { fetch(value: string): void }) {
            fetch('/api/not-network')
            globalThis.fetch('/api/not-network-either')
          }
          const headers = { Authorization: 'not-attached' }
          void headers
          void unrelated
          void globalThis.fetch('/api/real')
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/shadowed.ts', kind: 'httpRequest' },
      { rel: 'src/shadowed.ts', kind: 'httpApiEndpoint' },
    ])
  })

  it('terminates recursive wrappers and counts their authored Fetch site once', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/recursive.ts': `
          function request(url: string): Promise<Response> {
            if (!url) return request(url)
            return fetch(url)
          }
          void request('/api/value')
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/recursive.ts', kind: 'httpRequest' },
      { rel: 'src/recursive.ts', kind: 'httpApiEndpoint' },
    ])
  })

  it('normalizes traversal, keeps dynamic targets conservative, and exempts proven resources', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/targets.ts': `
          void fetch('/static/logo.svg')
          void fetch('/assets/theme.css')
          void fetch('data:image/png;base64,AA==')
          void fetch('blob:https://control.example/id')
          void fetch('https://cdn.example/api/image.png')
          void fetch('http://127.0.0.1:8765/api/private')
          void fetch('/static/../api/private')
          void fetch('/static/%2e%2e/api/private')
          declare const runtimeTarget: string
          void fetch(runtimeTarget)
        `,
      }),
    })

    expect(operations).toEqual([
      { rel: 'src/targets.ts', kind: 'httpRequest' },
      { rel: 'src/targets.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/targets.ts', kind: 'httpRequest' },
      { rel: 'src/targets.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/targets.ts', kind: 'httpRequest' },
      { rel: 'src/targets.ts', kind: 'httpApiEndpoint' },
      { rel: 'src/targets.ts', kind: 'httpRequest' },
      { rel: 'src/targets.ts', kind: 'httpApiEndpoint' },
    ])
  })

  it('does not count Request construction or detached header data without a network call', () => {
    const operations = collectHttpBoundaryOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/detached.ts': `
          const headers = { Authorization: sessionStorage.getItem('opensquilla.wsToken') }
          const request = new Request('/api/items', { headers })
          void request
        `,
      }),
    })
    expect(operations).toEqual([])
  })
})

describe('RPC import and symbol provenance', () => {
  it('follows renamed factories and store aliases through a barrel', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/rpc-barrel.ts': `export { useRpcStore as makeBridge } from './stores/rpc'`,
        'src/consumer.ts': `
          import { makeBridge } from './rpc-barrel'
          const bridge = makeBridge()
          const transport = bridge
          transport.call('sessions.list')
          transport.on('sessions.changed', () => {})
        `,
      }),
    })
    expect(operations).toEqual([
      { rel: 'src/consumer.ts', kind: 'call' },
      { rel: 'src/consumer.ts', kind: 'on' },
    ])
  })

  it('finds destructured capabilities from a proven store', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          const bridge = useRpcStore()
          const { call: invoke, waitForConnection: ready } = bridge
          void invoke
          void ready
        `,
      }),
    })
    expect(operations).toEqual([
      { rel: 'src/consumer.ts', kind: 'callReference' },
      { rel: 'src/consumer.ts', kind: 'waitForConnectionReference' },
    ])
  })

  it('finds destructured capabilities from an imported RPC client type', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/lib/rpc.ts': 'export class RpcClient {}',
        'src/consumer.ts': `
          import type { RpcClient as WireClient } from './lib/rpc'
          function consume({ call: invoke, on: listen }: WireClient) {
            void invoke
            void listen
          }
        `,
      }),
    })
    expect(operations).toEqual([
      { rel: 'src/consumer.ts', kind: 'callReference' },
      { rel: 'src/consumer.ts', kind: 'onReference' },
    ])
  })

  it('does not classify an unrelated analytics client by receiver spelling', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          const bridge = useRpcStore()
          const analyticsClient = { on(_name: string, _handler: () => void) {} }
          analyticsClient.on('page.view', () => {})
          bridge.on('sessions.changed', () => {})
        `,
      }),
    })
    expect(operations).toEqual([{ rel: 'src/consumer.ts', kind: 'on' }])
  })

  it('parses production JavaScript inputs with the same provenance rules', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/consumer.js': `
          import { useRpcStore as makeBridge } from './stores/rpc.js'
          const bridge = makeBridge()
          bridge.call('sessions.list')
        `,
      }),
    })
    expect(operations).toEqual([{ rel: 'src/consumer.js', kind: 'call' }])
  })

  it('follows a seed through an object property and imported factory return', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/wrapper.ts': `
          export function createOptions(rpc: unknown) {
            return {
              transport: {
                call: (...args: unknown[]) => rpc.call(...args),
                waitForConnection: () => rpc.waitForConnection(),
              },
            }
          }
        `,
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc.js'
          import { createOptions } from './wrapper.js'
          const options = createOptions(useRpcStore())
          options.transport.call('sessions.list')
          options.transport.waitForConnection()
        `,
      }),
    })
    expect(operations).toEqual([
      { rel: 'src/wrapper.ts', kind: 'call' },
      { rel: 'src/wrapper.ts', kind: 'waitForConnection' },
      { rel: 'src/consumer.ts', kind: 'call' },
      { rel: 'src/consumer.ts', kind: 'waitForConnection' },
    ])
  })

  it('follows a seed into an imported composable parameter', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/feature.ts': `
          export function consume(options: { rpc: { call(method: string): unknown } }) {
            return options.rpc.call('sessions.list')
          }
        `,
        'src/feature-barrel.ts': `export { consume as run } from './feature.js'`,
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          import { run } from './feature-barrel.js'
          run({ rpc: useRpcStore() })
        `,
      }),
    })
    expect(operations).toEqual([{ rel: 'src/feature.ts', kind: 'call' }])
  })

  it('does not infer provenance from a local same-shaped call/wait interface', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/cache.ts': `
          interface CacheClient {
            call(key: string): unknown
            waitForConnection(): unknown
          }
          export function useCache(cache: CacheClient) {
            cache.call('entry')
            cache.waitForConnection()
          }
          useCache({ call: () => null, waitForConnection: () => null })
        `,
      }),
    })
    expect(operations).toEqual([])
  })

  it.each([
    {
      label: 'anonymous default wrapper',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/wrapper.ts': `
          import { useRpcStore } from './stores/rpc'
          export default () => useRpcStore()
        `,
        'src/consumer.ts': `
          import backend from './wrapper'
          backend().call('feature.get')
        `,
      },
      operationRel: 'src/consumer.ts',
    },
    {
      label: 'local and exported callable aliases',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/wrapper.ts': `
          import { useRpcStore } from './stores/rpc'
          function backend() { return useRpcStore() }
          const first = backend
          const second = first
          export { second as makeBackend }
        `,
        'src/consumer.ts': `
          import { makeBackend } from './wrapper'
          makeBackend().call('feature.get')
        `,
      },
      operationRel: 'src/consumer.ts',
    },
    {
      label: 'directory index barrel',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/stores/index.ts': `export { useRpcStore } from './rpc'`,
        'src/consumer.ts': `
          import { useRpcStore } from './stores'
          useRpcStore().call('feature.get')
        `,
      },
      operationRel: 'src/consumer.ts',
    },
    {
      label: 'CommonJS member require',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/consumer.js': `
          const rpc = require('./stores/rpc')['useRpcStore']()
          rpc.call('feature.get')
        `,
      },
      operationRel: 'src/consumer.js',
    },
    {
      label: 'nested object and array destructuring',
      files: {
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/feature.ts': `
          export function consume({ nested: [{ rpc }] }) {
            rpc.call('feature.get')
          }
        `,
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          import { consume } from './feature'
          consume({ nested: [{ rpc: useRpcStore() }] })
        `,
      },
      operationRel: 'src/feature.ts',
    },
  ])('follows $label through the declaration graph', ({ files, operationRel }) => {
    expect(collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources(files as unknown as Record<string, string>),
    })).toEqual([{ rel: operationRel, kind: 'call' }])
  })

  it('keeps factory values distinct from clients and respects lexical scope', () => {
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          function invokeFactory(factory: typeof useRpcStore) {
            factory.call(null)
          }
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
          void invokeFactory
          void seed
          void cacheOnly
          void shadowed
        `,
      }),
    })
    expect(operations).toEqual([])
  })

  it('bounds recursive return shapes by paths the AST can consume', () => {
    const started = performance.now()
    const operations = collectRpcTransportOperations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/stores/rpc.ts': 'export function useRpcStore() { return {} }',
        'src/wrapper.ts': `
          export function wrap(rpc: unknown, depth: number): unknown {
            if (depth <= 0) return { rpc }
            return { next: wrap(rpc, depth - 1) }
          }
        `,
        'src/consumer.ts': `
          import { useRpcStore } from './stores/rpc'
          import { wrap } from './wrapper'
          const wrapped = wrap(useRpcStore(), 2)
          wrapped.next.next.rpc.call('feature.get')
        `,
      }),
    })
    expect(operations).toEqual([{ rel: 'src/consumer.ts', kind: 'call' }])
    expect(performance.now() - started).toBeLessThan(500)
  }, 1_000)
})

describe('whole-program boundary export fence', () => {
  it('rejects exported function signatures and CommonJS member leaks from an Adapter', () => {
    const sources = provenanceSources({
      'src/adapters/gateway/privateTransports.ts': `
        export interface RpcTransport { request(method: string): unknown }
        export const PRIVATE_TRANSPORT = 1
      `,
      'src/adapters/gateway/functionLeak.ts': `
        import type { RpcTransport } from './privateTransports'
        export function expose(value: RpcTransport): RpcTransport { return value }
      `,
      'src/adapters/gateway/bodyLeak.ts': `
        import { PRIVATE_TRANSPORT } from './privateTransports'
        export function expose() { return PRIVATE_TRANSPORT }
      `,
      'src/adapters/gateway/cjsLeak.js': `
        module.exports['transport'] = require('./privateTransports')['RpcTransport']
      `,
      'src/adapters/gateway/cjsAssignLeak.js': `
        Object.assign(exports, {
          transport: require('./privateTransports').RpcTransport,
        })
      `,
      'src/adapters/gateway/aliasLeak.ts': `
        import {
          PRIVATE_TRANSPORT as HiddenValue,
          type RpcTransport as HiddenType,
        } from './privateTransports'
        const first = HiddenValue
        const second = first
        type Alias = HiddenType
        export const PublicValue = second
        export type PublicType = Alias
        export interface PublicInterface extends HiddenType {}
        export default second
      `,
      'src/adapters/gateway/classLeak.ts': `
        import type { RpcTransport } from './privateTransports'
        export class PublicTransport {
          constructor(readonly transport: RpcTransport) {}
        }
      `,
    })
    expect(collectBoundaryArchitectureViolations({
      ts,
      root: fixtureRoot,
      sources,
    })).toEqual(expect.arrayContaining([
      'src/adapters/gateway/functionLeak.ts: exported declaration exposes private Gateway transport symbols.',
      'src/adapters/gateway/bodyLeak.ts: exported declaration exposes private Gateway transport symbols.',
      'src/adapters/gateway/cjsLeak.js: CommonJS export exposes private Gateway transport symbols.',
      'src/adapters/gateway/cjsAssignLeak.js: CommonJS export exposes private Gateway transport symbols.',
      'src/adapters/gateway/aliasLeak.ts: exported declaration exposes private Gateway transport symbols.',
      'src/adapters/gateway/aliasLeak.ts: default export exposes private Gateway transport symbols.',
      'src/adapters/gateway/classLeak.ts: exported declaration exposes private Gateway transport symbols.',
    ]))
  })

  it('keeps generated-to-generated composition legal', () => {
    const failures = collectBoundaryArchitectureViolations({
      ts,
      root: fixtureRoot,
      sources: provenanceSources({
        'src/contracts/generated/wire.ts': 'export interface Wire { id: string }',
        'src/contracts/generated/index.ts': `export type { Wire } from './wire'`,
      }),
    })
    expect(failures).toEqual([])
  })
})
