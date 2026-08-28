import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  boundaryReexportViolation,
  callMemberReceiverText,
  callMemberReferenceReceiverText,
  destructuredCallSourceText,
  destructuredMemberSourceText,
  generatedContractImportViolation,
  isDirectCallMemberReference,
  isKnownNonRpcCallReceiver,
  isRpcCapabilityReceiverText,
  localBoundaryReexportViolations,
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

  it('forbids renamed boundary symbols from leaking through a barrel', () => {
    const parsed = source(`
      import { RpcTransport as HiddenTransport } from './adapters/gateway/privateTransports'
      import { SessionRow as HiddenWire } from './contracts/generated/v4/sessionsList'
      export { HiddenTransport as PublicTransport, HiddenWire }
    `)
    expect(localBoundaryReexportViolations(ts, parsed, {
      root: fixtureRoot,
      importer: 'src/index.ts',
    })).toEqual([
      'src/index.ts: private Gateway transport symbol HiddenTransport must not be re-exported through a barrel.',
      'src/index.ts: generated Contract symbol HiddenWire must not be re-exported through a barrel.',
    ])
  })

  it('applies direct and aliased barrel fences to production JavaScript', () => {
    const parsed = ts.createSourceFile(
      'barrel.js',
      `
        import { RpcTransport as HiddenTransport } from './adapters/gateway/privateTransports.js'
        export { HiddenTransport }
        export { SessionRow } from './contracts/generated/v4/sessionsList.js'
      `,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.JS,
    )
    expect(localBoundaryReexportViolations(ts, parsed, {
      root: fixtureRoot,
      importer: 'src/barrel.js',
    })).toEqual([
      'src/barrel.js: private Gateway transport symbol HiddenTransport must not be re-exported through a barrel.',
    ])
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
        `,
      }),
    })

    expect(operations).toEqual([
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
})
