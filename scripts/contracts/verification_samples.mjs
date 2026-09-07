// Bounded, deterministic differential-test probes, not a validator or proof of equivalence.
export const jsonProbes = [
  null, false, true, -1, 0, 1, 1.0, 1.5, '', '0', '1', 'synthetic', '中文😀',
  '\ud800', [], [null], [0], {}, { extra: true },
]
const unique = values => [...new Map(values.map(value => [JSON.stringify(value), value])).values()]
const mergeConstraints = (left, right) => ({
  ...left, ...right,
  ...(left.properties || right.properties ? { properties: { ...left.properties, ...right.properties } } : {}),
  ...(left.required || right.required ? { required: [...new Set([...(left.required ?? []), ...(right.required ?? [])])] } : {}),
})

/** Produce candidate seeds from local constraints; validators decide acceptance. */
export function schemaSamples(root, schema, depth = 0) {
  if (depth > 12 || schema === false) return [null]
  if (!schema || schema === true) return jsonProbes
  if (schema.$ref) {
    if (!schema.$ref.startsWith('#/')) throw new Error('test samples require local references')
    const resolved = schema.$ref.slice(2).split('/').reduce(
      (value, key) => value?.[key.replace(/~1/g, '/').replace(/~0/g, '~')], root,
    )
    if (resolved === undefined) throw new Error(`missing sample reference: ${schema.$ref}`)
    const { $ref, ...siblings } = schema
    return schemaSamples(root, mergeConstraints(resolved, siblings), depth + 1)
  }
  if (Object.hasOwn(schema, 'const')) return [schema.const]
  if (schema.enum) return schema.enum
  if (schema.anyOf || schema.oneOf) {
    const { anyOf, oneOf, ...siblings } = schema
    return unique((anyOf ?? oneOf).flatMap(branch => (
      schemaSamples(root, mergeConstraints(siblings, branch), depth + 1)
    )))
  }
  if (schema.allOf) {
    const { allOf, ...siblings } = schema
    const parts = allOf.map(branch => schemaSamples(root, mergeConstraints(siblings, branch), depth + 1))
    return parts.reduce((combined, values) => unique(combined.flatMap(left => values.map(right => (
      left && right && typeof left === 'object' && typeof right === 'object'
        ? { ...left, ...right } : right
    )))).slice(0, 256), [{}])
  }
  if (Array.isArray(schema.type)) return unique(schema.type.flatMap(type => (
    schemaSamples(root, { ...schema, type }, depth + 1)
  )))
  switch (schema.type ?? (schema.properties || schema.required ? 'object' : undefined)) {
    case 'null': return [null]
    case 'boolean': return [false, true]
    case 'integer':
    case 'number': {
      const minimum = schema.minimum ?? ((schema.exclusiveMinimum ?? -1) + 1)
      const maximum = schema.maximum ?? ((schema.exclusiveMaximum ?? minimum + 2) - 1)
      const multiple = schema.multipleOf ?? 1
      return unique([Math.ceil(minimum / multiple) * multiple, maximum, 0, 1, 1.5])
    }
    case 'string': {
      const candidates = ['', 'synthetic', '中文😀', 'a'.repeat(schema.minLength ?? 1),
        '00000000', '0'.repeat(64), '00000000-0000-4000-8000-000000000000',
        'task.running', 'session.event.synthetic', 'chat.done']
      const matching = candidates.filter(value => value.length >= (schema.minLength ?? 0)
        && value.length <= (schema.maxLength ?? Infinity)
        && (!schema.pattern || new RegExp(schema.pattern, 'u').test(value)))
      return unique([...matching, ...candidates])
    }
    case 'array': {
      const seeds = schemaSamples(root, schema.items ?? true, depth + 1)
      const length = schema.minItems ?? 0
      return [Array.from({ length }, (_, index) => seeds[index % seeds.length]), [], [seeds[0]]]
    }
    case 'object': {
      const minimal = {}
      const complete = {}
      const properties = Object.entries(schema.properties ?? {})
      const samples = new Map(properties.map(([key, value]) => [key, schemaSamples(root, value, depth + 1)]))
      for (const [key] of properties) {
        complete[key] = samples.get(key)[0]
        if (schema.required?.includes(key)) minimal[key] = complete[key]
      }
      for (const key of schema.required ?? []) if (!Object.hasOwn(minimal, key)) minimal[key] = null
      if (schema.additionalProperties !== false) {
        for (let index = Object.keys(minimal).length; index < (schema.minProperties ?? 0); index++) {
          minimal[`synthetic_${index}`] = schemaSamples(root, schema.additionalProperties ?? true, depth + 1)[0]
        }
      }
      const variants = [minimal, complete]
      // Exercise unions and nullable/optional fields without a Cartesian explosion.
      for (const [key, values] of samples) {
        for (const value of values.slice(0, 16)) variants.push({ ...minimal, [key]: value })
      }
      return unique(variants).slice(0, 256)
    }
    default: return jsonProbes
  }
}

/** Perturb nested values and remove/add keys while preserving the source seed. */
export function mutationSamples(seed, depth = 0) {
  if (seed === null || typeof seed !== 'object' || depth > 8) return jsonProbes
  const results = [...jsonProbes]
  if (Array.isArray(seed)) {
    results.push([...seed, null], seed.slice(1))
    if (seed.length) for (const value of mutationSamples(seed[0], depth + 1)) {
      results.push([value, ...seed.slice(1)])
    }
  } else {
    results.push({ ...seed, __unexpected: 'synthetic' })
    for (const key of Object.keys(seed)) {
      const without = { ...seed }
      delete without[key]
      results.push(without)
      for (const value of mutationSamples(seed[key], depth + 1)) results.push({ ...seed, [key]: value })
    }
  }
  return unique(results).slice(0, 2048)
}
