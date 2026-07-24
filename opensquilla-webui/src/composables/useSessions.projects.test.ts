import { describe, expect, it } from 'vitest'
import {
  arrangeSidebarSections,
  normalizeSessionItem,
  type SessionItem,
} from './useSessions'
import type { ProjectWorkspaceItem } from './useProjectWorkspaces'
import type { RawSessionItem } from '@/types/rpc'

function session(raw: RawSessionItem): SessionItem {
  const item = normalizeSessionItem(raw)
  if (!item) throw new Error('fixture did not normalize')
  return item
}

describe('persisted project sidebar arrangement', () => {
  it('keeps fixed projects first, includes empty projects, and leaves ordinary tasks last', () => {
    const projects: ProjectWorkspaceItem[] = [
      { id: 'project-b', name: 'Project B', path: '/repo/b', taskCount: 0, pinned: true, available: true },
      { id: 'project-a', name: 'Project A', path: '/repo/a', taskCount: 2, pinned: false, available: true },
    ]
    const rows = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:a-old', title: 'A old', updatedAt: 100, workspaceId: 'project-a' }),
      session({ key: 'agent:main:webchat:a-new', title: 'A new', updatedAt: 300, workspaceId: 'project-a' }),
      session({ key: 'agent:main:webchat:ordinary', title: 'Ordinary', updatedAt: 400 }),
    ], projects)[0].rows

    expect(rows.map(row => ({ kind: row.rowKind, key: row.key, depth: row.depth }))).toEqual([
      { kind: 'workspace', key: 'workspace:project-b', depth: 0 },
      { kind: 'workspace-empty', key: 'workspace:project-b:empty', depth: 1 },
      { kind: 'workspace', key: 'workspace:project-a', depth: 0 },
      { kind: 'session', key: 'agent:main:webchat:a-new', depth: 1 },
      { kind: 'session', key: 'agent:main:webchat:a-old', depth: 1 },
      { kind: 'session', key: 'agent:main:webchat:ordinary', depth: 0 },
    ])
  })

  it('never reorders projects from child activity', () => {
    const projects: ProjectWorkspaceItem[] = [
      { id: 'b', name: 'B', path: '/b', taskCount: 1, pinned: true, available: true },
      { id: 'a', name: 'A', path: '/a', taskCount: 1, pinned: false, available: true },
    ]
    const rows = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:a', title: 'A task', updatedAt: 9_999, workspaceId: 'a' }),
      session({ key: 'agent:main:webchat:b', title: 'B task', updatedAt: 1, workspaceId: 'b' }),
    ], projects)[0].rows

    expect(rows.filter(row => row.rowKind === 'workspace').map(row => row.key))
      .toEqual(['workspace:b', 'workspace:a'])
  })

  it('keeps workspace-bound tasks visible while the canonical project list is unavailable', () => {
    const rows = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:project-task',
        title: 'Project task',
        updatedAt: 100,
        workspaceId: 'project-a',
      }),
    ], undefined)[0].rows

    expect(rows.map(row => row.key)).toContain('agent:main:webchat:project-task')
  })
})
