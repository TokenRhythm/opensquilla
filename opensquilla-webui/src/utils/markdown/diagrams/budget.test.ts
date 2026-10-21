import { describe, expect, it } from 'vitest'
import { checkGraphvizBudget } from './budget'

describe('Graphviz input budgets', () => {
  it('accepts ordinary graphs and DOT subgraphs', () => {
    expect(checkGraphvizBudget('digraph { a -> b }')).toBe(true)
    expect(checkGraphvizBudget('digraph { subgraph cluster_a { a; b; } a -> b; }')).toBe(true)
  })

  it('bounds source, edges and quadratic subgraph expansion before worker allocation', () => {
    expect(checkGraphvizBudget('x'.repeat(20_001))).toBe(false)
    expect(checkGraphvizBudget('x\n'.repeat(600))).toBe(false)
    expect(checkGraphvizBudget('digraph { ' + 'a -> b;'.repeat(251) + ' }')).toBe(false)
    expect(checkGraphvizBudget('digraph {' + '{'.repeat(21) + 'a' + '}'.repeat(22))).toBe(false)
    expect(checkGraphvizBudget('digraph { { a b c } -> { d e f } }')).toBe(false)
  })
})
