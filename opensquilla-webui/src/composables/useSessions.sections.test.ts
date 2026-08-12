import { describe, it, expect } from 'vitest'
import {
  arrangeSidebarSections,
  normalizeSessionItem,
  type SessionItem,
  type SidebarSection,
} from './useSessions'
import type { RawSessionItem } from '@/types/rpc'

// Build real SessionItems through the production normalizer so the test
// exercises the same sessionKind/surface/parent derivation the sidebar sees,
// rather than hand-rolling the normalized shape.
function session(raw: RawSessionItem): SessionItem {
  const item = normalizeSessionItem(raw)
  if (!item) throw new Error(`fixture did not normalize: ${JSON.stringify(raw)}`)
  return item
}

function sectionFor(sections: SidebarSection[], family: SidebarSection['family']): SidebarSection {
  const found = sections.find(s => s.family === family)
  if (!found) throw new Error(`missing section: ${family}`)
  return found
}

describe('arrangeSidebarSections — family bucketing', () => {
  it('buckets chat, channel, and cron sessions into their families', () => {
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:chat1', title: 'A chat', updatedAt: 100 }),
      session({ key: 'channel:slack:room1', sessionKind: 'channel', title: 'A channel', updatedAt: 90 }),
      session({ key: 'cron:nightly:run1', title: 'A cron run', updatedAt: 80 }),
    ])

    // The helper always returns all three families, in display order.
    expect(sections.map(s => s.family)).toEqual(['chats', 'channels', 'automations'])
    expect(sections.map(s => s.label)).toEqual(['Tasks', 'Channels', 'Automations'])

    expect(sectionFor(sections, 'chats').rows.map(r => r.title)).toEqual(['A chat'])
    expect(sectionFor(sections, 'channels').rows.map(r => r.title)).toEqual(['A channel'])
    expect(sectionFor(sections, 'automations').rows.map(r => r.title)).toEqual(['A cron run'])
  })

  it('drops cli/subagent chat surfaces from the chats family', () => {
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:cli:abc', sessionKind: 'chat', surface: 'cli', title: 'CLI session', updatedAt: 50 }),
    ])
    expect(sectionFor(sections, 'chats').rows).toHaveLength(0)
  })
})

describe('arrangeSidebarSections cancel stop labels', () => {
  it('shows how long a cancelled turn ran when task timing is available', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:stopped',
        title: 'Stopped chat',
        updatedAt: 100,
        runStatus: 'cancelled',
        last_task: {
          status: 'cancelled',
          started_at: 1_000,
          finished_at: 2_240,
        },
      }),
    ])

    const [row] = sectionFor(sections, 'chats').rows
    expect(row.runStatus).toBe('cancelled')
    expect(row.runLabel).toBe('Stopped after 1s')
  })

  it('falls back to a stopped label when a cancelled turn has no timing', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:stopped',
        title: 'Stopped chat',
        updatedAt: 100,
        runStatus: 'cancelled',
        last_task: { status: 'cancelled' },
      }),
    ])

    const [row] = sectionFor(sections, 'chats').rows
    expect(row.runLabel).toBe('Stopped')
  })
})

describe('arrangeSidebarSections task attention', () => {
  it('folds queued and running rows into one running indicator state', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:queued',
        title: 'Queued',
        updatedAt: 300,
        runStatus: 'queued',
        active_task: { status: 'queued' },
      }),
      session({
        key: 'agent:main:webchat:running',
        title: 'Running',
        updatedAt: 200,
        runStatus: 'running',
        active_task: { status: 'running' },
      }),
      session({
        key: 'agent:main:webchat:idle',
        title: 'Idle',
        updatedAt: 100,
        runStatus: 'idle',
      }),
    ])

    expect(sectionFor(sections, 'chats').rows.map(row => row.taskAttention)).toEqual([
      'running',
      'running',
      'none',
    ])
  })
})

describe('arrangeSidebarSections — subagent nesting', () => {
  it('nests a subagent under its parent chat at depth 1', () => {
    const parentKey = 'agent:main:webchat:parent'
    const sections = arrangeSidebarSections([
      session({ key: parentKey, title: 'Parent chat', updatedAt: 200 }),
      session({
        key: 'agent:main:subagent:child',
        title: 'Subagent task',
        updatedAt: 150,
        parent: { key: parentKey, title: 'Parent chat', spawnDepth: 1 },
      }),
    ])

    const rows = sectionFor(sections, 'chats').rows
    expect(rows.map(r => ({ title: r.title, depth: r.depth }))).toEqual([
      { title: 'Parent chat', depth: 0 },
      { title: 'Subagent task', depth: 1 },
    ])
    expect(rows[1].sessionKind).toBe('task')
  })

  it('indents an orphan subagent (parent absent) at depth 1', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:subagent:orphan',
        title: 'Orphan task',
        updatedAt: 120,
        parent: { key: 'agent:main:webchat:gone', title: 'Gone parent', spawnDepth: 1 },
      }),
    ])

    const rows = sectionFor(sections, 'chats').rows
    expect(rows).toHaveLength(1)
    expect(rows[0].title).toBe('Orphan task')
    expect(rows[0].depth).toBe(1)
  })

  it('keeps a numbered fork title flat while preserving the parent title', () => {
    const parentKey = 'agent:main:webchat:parent'
    const parentTitle = 'Release planning notes'
    const sections = arrangeSidebarSections([
      session({ key: parentKey, title: parentTitle, updatedAt: 100 }),
      session({
        key: 'agent:main:webchat:fork',
        title: `${parentTitle} (2)`,
        updatedAt: 200,
        forked_from_parent: true,
        parent: { key: parentKey, title: parentTitle },
      }),
      session({
        key: 'agent:main:webchat:fork-2',
        title: `${parentTitle} (3)`,
        updatedAt: 300,
        forked_from_parent: true,
        parent: { key: parentKey, title: parentTitle },
      }),
    ])

    expect(sectionFor(sections, 'chats').rows.map(row => ({ title: row.title, depth: row.depth }))).toEqual([
      { title: `${parentTitle} (3)`, depth: 0 },
      { title: `${parentTitle} (2)`, depth: 0 },
      { title: parentTitle, depth: 0 },
    ])
  })
})

describe('arrangeSidebarSections — workspace grouping', () => {
  it('groups chat sessions by explicit workspace and keeps sessions without workspace flat', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:project1-session1',
        title: 'Session 1',
        updatedAt: 400,
        workspace: '/repo/project1',
        workspaceLabel: 'project1',
        workspaceDisplayPath: '/repo/project1',
      } as RawSessionItem),
      session({
        key: 'agent:main:webchat:project1-session2',
        title: 'Session 2',
        updatedAt: 300,
        workspace: '/repo/project1',
        workspaceLabel: 'project1',
        workspaceDisplayPath: '/repo/project1',
      } as RawSessionItem),
      session({
        key: 'agent:main:webchat:project2-session3',
        title: 'Session 3',
        updatedAt: 200,
        workspace: '/repo/project2',
        workspaceLabel: 'project2',
        workspaceDisplayPath: '/repo/project2',
      } as RawSessionItem),
      session({
        key: 'agent:main:webchat:session4',
        title: 'Session 4',
        updatedAt: 100,
      }),
    ])

    expect(sectionFor(sections, 'chats').rows.map(r => ({
      rowKind: r.rowKind,
      key: r.key,
      title: r.title,
      depth: r.depth,
    }))).toEqual([
      { rowKind: 'workspace', key: 'workspace:/repo/project1', title: 'project1', depth: 0 },
      { rowKind: 'session', key: 'agent:main:webchat:project1-session1', title: 'Session 1', depth: 1 },
      { rowKind: 'session', key: 'agent:main:webchat:project1-session2', title: 'Session 2', depth: 1 },
      { rowKind: 'workspace', key: 'workspace:/repo/project2', title: 'project2', depth: 0 },
      { rowKind: 'session', key: 'agent:main:webchat:project2-session3', title: 'Session 3', depth: 1 },
      { rowKind: 'session', key: 'agent:main:webchat:session4', title: 'Session 4', depth: 0 },
    ])
  })

  it('nests subagent rows one level deeper inside their workspace group', () => {
    const parentKey = 'agent:main:webchat:workspace-parent'
    const sections = arrangeSidebarSections([
      session({
        key: parentKey,
        title: 'Parent chat',
        updatedAt: 200,
        workspace: '/repo/project',
        workspaceLabel: 'project',
      } as RawSessionItem),
      session({
        key: 'agent:main:subagent:workspace-child',
        title: 'Subagent task',
        updatedAt: 150,
        workspace: '/repo/project',
        workspaceLabel: 'project',
        parent: { key: parentKey, title: 'Parent chat', spawnDepth: 1 },
      } as RawSessionItem),
    ])

    expect(sectionFor(sections, 'chats').rows.map(r => ({
      rowKind: r.rowKind,
      key: r.key,
      depth: r.depth,
    }))).toEqual([
      { rowKind: 'workspace', key: 'workspace:/repo/project', depth: 0 },
      { rowKind: 'session', key: parentKey, depth: 1 },
      { rowKind: 'session', key: 'agent:main:subagent:workspace-child', depth: 2 },
    ])
  })
})

describe('arrangeSidebarSections — recency ordering', () => {
  it('orders rows within a family newest-first', () => {
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:old', title: 'Older', updatedAt: 10 }),
      session({ key: 'agent:main:webchat:new', title: 'Newer', updatedAt: 30 }),
      session({ key: 'agent:main:webchat:mid', title: 'Middle', updatedAt: 20 }),
    ])
    expect(sectionFor(sections, 'chats').rows.map(r => r.title)).toEqual(['Newer', 'Middle', 'Older'])
  })

  it('orders a running chat by backend lastActivityAt', () => {
    const sections = arrangeSidebarSections([
      session({
        key: 'agent:main:webchat:finished',
        title: 'Finished',
        updatedAt: 900,
        runStatus: 'idle',
      }),
      session({
        key: 'agent:main:webchat:running',
        title: 'Running',
        updatedAt: 100,
        lastActivityAt: 1000,
        runStatus: 'running',
        active_task: { status: 'running' },
      }),
    ])

    const rows = sectionFor(sections, 'chats').rows
    expect(rows.map(r => ({ title: r.title, runStatus: r.runStatus }))).toEqual([
      { title: 'Running', runStatus: 'running' },
      { title: 'Finished', runStatus: 'idle' },
    ])
  })
})

describe('arrangeSidebarSections — manual session ordering', () => {
  it('applies a saved order while keeping newly-created sessions above it', () => {
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:new', title: 'New', updatedAt: 300 }),
      session({ key: 'agent:main:webchat:a', title: 'A', updatedAt: 200 }),
      session({ key: 'agent:main:webchat:b', title: 'B', updatedAt: 100 }),
    ], undefined, [
      'agent:main:webchat:b',
      'agent:main:webchat:a',
    ])

    expect(sectionFor(sections, 'chats').rows.map(row => row.title)).toEqual(['New', 'B', 'A'])
  })

  it('places pinned sessions above newer unpinned sessions', () => {
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:new', title: 'New', updatedAt: 300 }),
      session({ key: 'agent:main:webchat:pinned', title: 'Pinned', updatedAt: 100 }),
      session({ key: 'agent:main:webchat:old', title: 'Old', updatedAt: 50 }),
    ], undefined, [], ['agent:main:webchat:pinned'])

    expect(sectionFor(sections, 'chats').rows.map(row => ({ title: row.title, pinned: row.pinned }))).toEqual([
      { title: 'Pinned', pinned: true },
      { title: 'New', pinned: false },
      { title: 'Old', pinned: false },
    ])
  })

  it('keeps a subagent attached to its parent after the parent is reordered', () => {
    const parentKey = 'agent:main:webchat:parent'
    const sections = arrangeSidebarSections([
      session({ key: 'agent:main:webchat:other', title: 'Other', updatedAt: 300 }),
      session({ key: parentKey, title: 'Parent', updatedAt: 200 }),
      session({
        key: 'agent:main:subagent:child',
        title: 'Child',
        updatedAt: 100,
        parent: { key: parentKey, spawnDepth: 1 },
      }),
    ], undefined, [parentKey, 'agent:main:webchat:other'])

    expect(sectionFor(sections, 'chats').rows.map(row => ({ title: row.title, depth: row.depth }))).toEqual([
      { title: 'Parent', depth: 0 },
      { title: 'Child', depth: 1 },
      { title: 'Other', depth: 0 },
    ])
  })
})
