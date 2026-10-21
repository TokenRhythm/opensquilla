export const MAX_DIAGRAM_CHARS = 20_000
export const MAX_DIAGRAM_LINES = 600
export const MAX_SVG_CHARS = 1_000_000
export const GRAPHVIZ_TIMEOUT_MS = 8_000

// These bounds run before loading Graphviz. DOT subgraph endpoints can
// expand a short expression into a quadratic number of edges; ordinary
// subgraphs remain supported, but those endpoints deliberately stay as code.
export function checkGraphvizBudget(source: string): boolean {
  if (!source.trim() || source.length > MAX_DIAGRAM_CHARS) return false
  if (source.split(/\r\n|\r|\n/).length > MAX_DIAGRAM_LINES) return false
  if ((source.match(/;|\n/g)?.length ?? 0) > 600) return false
  if ((source.match(/--?>|--|==>|\.->|=>/g)?.length ?? 0) > 250) return false
  if ((source.match(/[\[({]/g)?.length ?? 0) > 300) return false
  {
    const structural = source.replace(/"(?:\\.|[^"\\])*"|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, '""')
    if (/}\s*(?:->|--)|(?:->|--)\s*(?:subgraph\b|\{)/i.test(structural)) return false
    if ((structural.match(/[A-Za-z_\u0080-\uFFFF][\w\u0080-\uFFFF]*/g)?.length ?? 0) > 1_200) return false
    let depth = 0
    for (const char of structural) {
      if (char === '{' && ++depth > 20) return false
      if (char === '}') depth--
    }
  }
  return true
}
