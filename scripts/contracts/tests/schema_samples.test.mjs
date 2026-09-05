import assert from 'node:assert/strict'
import test from 'node:test'
import { schemaSamples, mutationSamples } from '../verification_samples.mjs'

test('deterministic probes traverse refs, unions, optional fields and numeric bounds', () => {
  const schema = { $defs: { value: { anyOf: [
    { type: 'null' },
    { type: 'object', required: ['n'], properties: {
      n: { type: 'integer', minimum: 1 }, text: { type: 'string', enum: ['中文😀'] },
    } },
  ] } } }
  const samples = schemaSamples(schema, { $ref: '#/$defs/value' })
  assert.deepEqual(samples, schemaSamples(schema, { $ref: '#/$defs/value' }))
  assert.ok(samples.some(value => value === null))
  assert.ok(samples.some(value => value?.n === 1 && value.text === '中文😀'))
  const mutations = mutationSamples({ n: 1, nested: { value: 'x' } })
  assert.ok(mutations.some(value => value?.nested && !Object.hasOwn(value, 'n')))
  assert.ok(mutations.some(value => value?.n === true))
  assert.ok(mutations.some(value => value?.nested?.value === null))
})

test('branch constraints retain required siblings and nonempty maps', () => {
  const schema = { type: 'object', required: ['fields'], properties: {
    fields: { type: 'object', minProperties: 1, additionalProperties: true },
    left: { type: 'string', minLength: 1 }, right: { type: 'string', minLength: 1 },
  }, oneOf: [{ required: ['left'] }, { required: ['right'] }] }
  assert.ok(schemaSamples(schema, schema).some(value => (
    Object.keys(value.fields).length > 0 && value.left && !Object.hasOwn(value, 'right')
  )))
})

test('recursive and non-local references do not fetch resources or recurse forever', () => {
  const schema = { $defs: { node: { type: 'object', properties: { next: { $ref: '#/$defs/node' } } } } }
  assert.ok(schemaSamples(schema, { $ref: '#/$defs/node' }).length > 0)
  assert.throws(() => schemaSamples(schema, { $ref: 'https://example.invalid/schema' }), /local/)
})
