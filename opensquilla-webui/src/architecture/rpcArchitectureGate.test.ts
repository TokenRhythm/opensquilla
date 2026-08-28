import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import {
  callMemberReceiverText,
  callMemberReferenceReceiverText,
  destructuredCallSourceText,
  generatedContractImportViolation,
  isDirectCallMemberReference,
  isKnownNonRpcCallReceiver,
  isRpcCapabilityReceiverText,
  moduleReferenceSpecifier,
  resolveSourceImport,
} from '../../scripts/lib/rpc-architecture-imports.mjs'

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
      `${importer}: generated wire Contract import "${specifier}" is allowed only in an Adapter or test.`,
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
