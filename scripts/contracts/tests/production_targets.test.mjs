import assert from 'node:assert/strict'
import test from 'node:test'
import { evaluateProductionTargets } from '../check_gateway_production_targets.mjs'
import { repositoryRoot } from '../gateway_contract_inventory.mjs'

const manifest = {
  format: 1,
  targets: [{ kind: 'method', wireName: 'config.get', roles: ['result'] }],
}
const modulePath = '../../contracts/generated/v4/configGetValidators.mjs'
const evaluate = (text, path = 'src/adapters/gateway/example.ts') => evaluateProductionTargets({
  manifest, sources: [{ path, text }],
})

test('production cannot import or re-export the verification compiler', () => {
  const loader = '../../../../scripts/contracts/gateway_contract_verification.mjs'
  for (const source of [
    `import { loadContractValidators } from '${loader}'`,
    `export { loadContractValidators } from '${loader}'`,
    `await import('${loader}')`,
  ]) assert.ok(evaluate(source).failures.some(failure => /test-only/.test(failure)))
})

test('production references exactly match the reviewed target policy', () => {
  const result = evaluateProductionTargets()
  assert.deepEqual(result.failures, [])
  assert.equal(result.targets.length, 210)
})

test('named import aliases preserve original validator identity in TS, JS and Vue', () => {
  const source = `import { validateResult as validate } from '${modulePath}'; validate({})`
  for (const extension of ['ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'vue']) {
    const text = extension === 'vue' ? `<template/><script setup>${source}</script>` : source
    assert.deepEqual(evaluate(text, `src/adapters/gateway/example.${extension}`).failures, [])
  }
})

test('test-only consumers do not approve production entry points', () => {
  const result = evaluate(`import { validateResult } from '${modulePath}'`, 'src/example.test.ts')
  assert.ok(result.failures.some(failure => failure.includes('unused production target')))
})

test('namespace, re-export, require and dynamic validators fail closed', () => {
  for (const source of [
    `import * as validators from '${modulePath}'`,
    `export { validateResult } from '${modulePath}'`,
    `export * from '${modulePath}'`,
    `const validators = require('${modulePath}')`,
    `await import('${modulePath}')`,
    `const path = '${modulePath}'; await import(path)`,
    'await import(`../../contracts/generated/v4/${name}Validators.mjs`)',
  ]) {
    assert.ok(evaluate(source).failures.some(failure => /named|dynamic|require|computed/.test(failure)))
  }
})

test('an unapproved role and duplicated policy cannot silently widen production output', () => {
  assert.ok(evaluate(`import { validateParams } from '${modulePath}'`).failures.length > 0)
  assert.throws(() => evaluateProductionTargets({
    sources: [], manifest: { format: 1, targets: [...manifest.targets, ...manifest.targets] },
  }), /duplicate/)
})

test('ordinary literal lazy imports remain supported', () => {
  const source = `import { validateResult } from '${modulePath}'; await import('@/views/ChatView.vue')`
  assert.deepEqual(evaluate(source).failures, [])
})

test('Vite root-relative imports retain role identity and reject dynamic validators', () => {
  for (const rootPath of [
    '/src/contracts/generated/v4/configGetValidators.mjs',
    `/@fs/${repositoryRoot.replace(/\\/g, '/')}/opensquilla-webui/src/contracts/generated/v4/configGetValidators.mjs`,
  ]) {
  const approved = `import { validateResult } from '${rootPath}';`
  assert.deepEqual(evaluate(approved).failures, [])
  for (const source of [
    `import { validateParams } from '${rootPath}'`,
    `await import('${rootPath}')`,
    "import.meta.glob('/src/contracts/generated/v4/*Validators.mjs')",
  ]) assert.ok(evaluate(approved + source).failures.length > 0)
  }
})
