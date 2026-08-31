import { describe, expect, it } from 'vitest'
import {
  buildFileTreeModel,
  flattenFileTreeModel,
  flattenLiveFileTreeModel,
  normalizeFileTreePath,
  type FileNode,
} from './fileTreeModel'

describe('normalizeFileTreePath', () => {
  it('normalizes windows separators, duplicate and edge slashes', () => {
    expect(normalizeFileTreePath('src\\lib\\a.ts')).toBe('src/lib/a.ts')
    expect(normalizeFileTreePath('/src//lib/b.ts/')).toBe('src/lib/b.ts')
    expect(normalizeFileTreePath('src/./lib/a.ts')).toBe('src/./lib/a.ts')
    expect(normalizeFileTreePath('')).toBe('')
  })
})

describe('buildFileTreeModel', () => {
  it('builds a sorted tree and flattens expanded directories', () => {
    const model = buildFileTreeModel(['src/z.ts', 'src/lib/b.ts', 'src/lib/a.ts', 'README.md', 'docs/guide.md'])

    expect(model.total).toBe(8)
    const rows = flattenFileTreeModel(model, () => true).map((row) => [row.node.path, row.node.type, row.level])
    expect(rows).toEqual([
      ['docs', 'directory', 0],
      ['docs/guide.md', 'file', 1],
      ['src', 'directory', 0],
      ['src/lib', 'directory', 1],
      ['src/lib/a.ts', 'file', 2],
      ['src/lib/b.ts', 'file', 2],
      ['src/z.ts', 'file', 1],
      ['README.md', 'file', 0],
    ])
  })

  it('skips children of collapsed directories', () => {
    const model = buildFileTreeModel(['src/lib/a.ts', 'src/z.ts'])
    // The collapsed directory itself still renders; only its children skip.
    const rows = flattenFileTreeModel(model, (path) => path !== 'src/lib').map((row) => row.node.path)
    expect(rows).toEqual(['src', 'src/lib', 'src/z.ts'])
  })

  it('normalizes duplicate and messy paths without duplicating nodes', () => {
    const model = buildFileTreeModel(['src\\lib\\a.ts', 'src/lib/a.ts', '/src//lib/b.ts/'])
    expect(model.total).toBe(4)
    const rows = flattenFileTreeModel(model, () => true).map((row) => row.node.path)
    expect(rows).toEqual(['src', 'src/lib', 'src/lib/a.ts', 'src/lib/b.ts'])
  })

  it('sorts case-insensitively among siblings', () => {
    const model = buildFileTreeModel(['readme.md', 'Zeta.ts', 'alpha.ts'])
    const rows = flattenFileTreeModel(model, () => true).map((row) => row.node.name)
    // alpha < readme < zeta, regardless of case.
    expect(rows).toEqual(['alpha.ts', 'readme.md', 'Zeta.ts'])
  })
})

describe('flattenLiveFileTreeModel', () => {
  it('walks lazily-loaded children only through expanded directories', () => {
    const nodes: Record<string, FileNode[]> = {
      '': [
        { name: 'docs', path: 'docs', type: 'directory' },
        { name: 'app.ts', path: 'app.ts', type: 'file' },
      ],
      docs: [{ name: 'guide.md', path: 'docs/guide.md', type: 'file' }],
    }
    const rows = flattenLiveFileTreeModel((path) => nodes[path] ?? [], () => true).map(
      (row) => [row.node.path, row.level] as const,
    )
    expect(rows).toEqual([
      ['docs', 0],
      ['docs/guide.md', 1],
      ['app.ts', 0],
    ])

    const collapsed = flattenLiveFileTreeModel((path) => nodes[path] ?? [], (path) => path !== 'docs').map(
      (row) => row.node.path,
    )
    expect(collapsed).toEqual(['docs', 'app.ts'])
  })
})
