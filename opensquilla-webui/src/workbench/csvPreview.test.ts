import { describe, expect, it } from 'vitest'
import { parseCsvPreview } from './csvPreview'

describe('bounded table preview parsing', () => {
  it('preserves quoted newlines, separators, empty cells and literal formulas', () => {
    const parsed = parseCsvPreview('name,notes,value\r\n"a,b","first\nsecond",\r\nformula,"=SUM(A1:A2)",42')
    expect(parsed.rows).toEqual([
      ['name', 'notes', 'value'], ['a,b', 'first\nsecond', ''], ['formula', '=SUM(A1:A2)', '42'],
    ])
    expect(parsed.truncated).toBe(false)
  })

  it('does not misdetect a tab-separated file containing commas', () => {
    expect(parseCsvPreview('name\tnotes\nitem\t"a,b,c"', '\t').rows).toEqual([
      ['name', 'notes'], ['item', 'a,b,c'],
    ])
  })

  it('keeps a bounded prefix and reports that the complete file must be downloaded', () => {
    const parsed = parseCsvPreview(Array.from({ length: 11000 }, (_, index) => `${index},value`).join('\n'))
    expect(parsed.rows).toHaveLength(10000)
    expect(parsed.rows[0]).toEqual(['0', 'value'])
    expect(parsed.rows[9999]).toEqual(['9999', 'value'])
    expect(parsed.truncated).toBe(true)
  })

  it('bounds wide tables by the total number of cells without accepting partial rows', () => {
    const row = Array.from({ length: 200 }, (_, index) => String(index)).join(',')
    const parsed = parseCsvPreview(Array(1001).fill(row).join('\n'))
    expect(parsed.rows).toHaveLength(1000)
    expect(parsed.rows.every(cells => cells.length === 200)).toBe(true)
    expect(parsed.columns).toBe(200)
    expect(parsed.truncated).toBe(true)
    expect(parseCsvPreview(`${row},extra`)).toEqual({ rows: [], columns: 0, truncated: true })
  })
})
