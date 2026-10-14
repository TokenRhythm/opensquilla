// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { normalizeVisualizeContext, readVisualizeContext, saveVisualizeContext } from './context'
import { readVisualizeState, saveVisualizeState } from './state'

const keys = ['context-test-a', 'context-test-b']
afterEach(() => { keys.forEach(key => saveVisualizeContext(key, null)); sessionStorage.clear() })

describe('Visualize host context snapshots', () => {
  it('copies plain JSON and treats null as an explicit clear', () => {
    const source = { summary: '  4 nights  ', data: { nights: 4, selected: ['A'] }, suggestedPrompt: ' Compare hotels ' }
    const copied = normalizeVisualizeContext(source)!
    source.data.selected.push('B')
    expect(copied).toEqual({ summary: '4 nights', data: { nights: 4, selected: ['A'] }, suggestedPrompt: 'Compare hotels' })
    expect(normalizeVisualizeContext(null)).toBeNull()
  })
  it('rejects instructions outside the schema and non-JSON or excessive payloads', () => {
    for (const input of [undefined, [], { summary: '' }, { summary: 'x', send: true }, { summary: 'x', data: Infinity }, { summary: 'x', data: new Date() }, { summary: 'x', data: { fn() {} } }]) {
      expect(() => normalizeVisualizeContext(input)).toThrow()
    }
    expect(() => normalizeVisualizeContext({ summary: 'x'.repeat(401) })).toThrow('summary')
    expect(() => normalizeVisualizeContext({ summary: 'x', suggestedPrompt: 'x'.repeat(2001) })).toThrow('suggestedPrompt')
    expect(() => normalizeVisualizeContext({ summary: 'x', data: '汉'.repeat(3000) })).toThrow('8 KiB')
    const circular: Record<string, unknown> = {}; circular.self = circular
    expect(() => normalizeVisualizeContext({ summary: 'x', data: circular })).toThrow('complex')
  })
  it('persists context by scope separately from legacy state and deletes cleared snapshots', () => {
    saveVisualizeState(keys[0]!, { nights: 4 })
    saveVisualizeContext(keys[0]!, { summary: '4 nights', data: { nights: 4 } })
    const first = readVisualizeContext(keys[0]!)!
    first.summary = 'mutated'
    expect(readVisualizeContext(keys[0]!)).toEqual({ summary: '4 nights', data: { nights: 4 } })
    expect(readVisualizeContext(keys[1]!)).toBeNull()
    expect(readVisualizeState(keys[0]!)).toEqual({ nights: 4 })
    saveVisualizeContext(keys[0]!, null)
    expect(readVisualizeContext(keys[0]!)).toBeNull()
    expect(sessionStorage.getItem('opensquilla.visualize-context.v1.' + keys[0])).toBeNull()
    expect(readVisualizeState(keys[0]!)).toEqual({ nights: 4 })
  })
})
