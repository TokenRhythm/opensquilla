import { describe, it, expect } from 'vitest'
import { encodeRouterShape, decodeRouterShape, type RouterShape } from './routerShapeCache'

function shape(overrides: Partial<RouterShape> = {}): RouterShape {
  return {
    enabled: true,
    slots: ['light', 'standard', 'heavy'],
    models: { light: 'a/x', standard: 'b/y', heavy: 'c/z' },
    configs: {
      light: { model: 'a/x', supportsImage: false, imageOnly: false, ensembleEnabled: false },
      standard: { model: 'b/y', supportsImage: true, imageOnly: false, ensembleEnabled: false },
      heavy: { model: 'c/z', supportsImage: false, imageOnly: false, ensembleEnabled: true },
    },
    ...overrides,
  }
}

describe('routerShapeCache — round-trip', () => {
  it('decodes exactly what it encoded', () => {
    const s = shape()
    expect(decodeRouterShape(encodeRouterShape(s))).toEqual(s)
  })

  it('preserves enabled=false', () => {
    const decoded = decodeRouterShape(encodeRouterShape(shape({ enabled: false })))
    expect(decoded?.enabled).toBe(false)
  })
})

describe('routerShapeCache — rejects unusable input (returns null)', () => {
  it('null / empty / garbage', () => {
    expect(decodeRouterShape(null)).toBeNull()
    expect(decodeRouterShape(undefined)).toBeNull()
    expect(decodeRouterShape('')).toBeNull()
    expect(decodeRouterShape('not json')).toBeNull()
    expect(decodeRouterShape('[]')).toBeNull()
    expect(decodeRouterShape('"str"')).toBeNull()
  })

  it('wrong / missing version', () => {
    expect(decodeRouterShape(JSON.stringify({ v: 2, models: { a: 'b' }, slots: [], configs: {} }))).toBeNull()
    expect(decodeRouterShape(JSON.stringify({ models: { a: 'b' }, slots: [], configs: {} }))).toBeNull()
  })

  it('empty models — a tier-less shape would seed a <=1-cell reserve', () => {
    expect(decodeRouterShape(JSON.stringify({ v: 1, enabled: true, slots: [], models: {}, configs: {} }))).toBeNull()
  })

  it('malformed field types', () => {
    expect(decodeRouterShape(JSON.stringify({ v: 1, models: 'nope', slots: [], configs: {} }))).toBeNull()
    expect(decodeRouterShape(JSON.stringify({ v: 1, models: { a: 1 }, slots: [], configs: {} }))).toBeNull()
    expect(decodeRouterShape(JSON.stringify({ v: 1, models: { a: 'b' }, slots: [1], configs: {} }))).toBeNull()
    expect(decodeRouterShape(JSON.stringify({ v: 1, models: { a: 'b' }, slots: [], configs: { a: 'x' } }))).toBeNull()
  })
})

describe('routerShapeCache — forward compatibility + normalization', () => {
  it('tolerates an unknown extra field', () => {
    const raw = JSON.stringify({ ...JSON.parse(encodeRouterShape(shape())), future: 'whatever' })
    expect(decodeRouterShape(raw)).toEqual(shape())
  })

  it('normalizes tier-config booleans and missing model', () => {
    const decoded = decodeRouterShape(JSON.stringify({
      v: 1,
      enabled: true,
      slots: ['standard'],
      models: { standard: 'b/y' },
      configs: { standard: { /* no model */ supportsImage: true } },
    }))
    expect(decoded?.configs.standard).toEqual({ model: '', supportsImage: true, imageOnly: false })
  })

  it('preserves the tier-scoped ensemble flag', () => {
    expect(decodeRouterShape(encodeRouterShape(shape()))?.configs.heavy?.ensembleEnabled).toBe(true)
  })
})
