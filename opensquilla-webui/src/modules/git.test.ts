import { describe, expect, it } from 'vitest'
import { groupChangedFiles, type GitChangedFile } from './git'

function entry(path: string, status: GitChangedFile['status'], staged: boolean, previousPath?: string): GitChangedFile {
  return previousPath === undefined ? { path, status, staged } : { path, status, staged, previousPath }
}

describe('groupChangedFiles', () => {
  it('splits staged, unstaged and untracked entries', () => {
    const grouped = groupChangedFiles([
      entry('src/a.ts', 'modified', true),
      entry('src/b.ts', 'modified', false),
      entry('src/c.ts', 'untracked', false),
    ])
    expect(grouped.staged.map(file => file.path)).toEqual(['src/a.ts'])
    expect(grouped.unstaged.map(file => file.path)).toEqual(['src/b.ts'])
    expect(grouped.untracked.map(file => file.path)).toEqual(['src/c.ts'])
  })

  it('keeps staged renames and unstaged conflicts out of the untracked bucket', () => {
    const grouped = groupChangedFiles([
      entry('src/new.ts', 'renamed', true, 'src/old.ts'),
      entry('src/conflict.ts', 'conflicted', false),
      entry('src/added.ts', 'added', true),
    ])
    expect(grouped.untracked).toEqual([])
    expect(grouped.staged.map(file => file.status)).toEqual(['renamed', 'added'])
    expect(grouped.unstaged.map(file => file.status)).toEqual(['conflicted'])
  })

  it('reports an untracked entry only once, even when Git marks it staged', () => {
    const untracked = entry('src/x.ts', 'untracked', true)
    expect(groupChangedFiles([untracked])).toEqual({ staged: [], unstaged: [], untracked: [untracked] })
  })

  it('returns empty groups for a clean tree', () => {
    expect(groupChangedFiles([])).toEqual({ staged: [], unstaged: [], untracked: [] })
  })
})
