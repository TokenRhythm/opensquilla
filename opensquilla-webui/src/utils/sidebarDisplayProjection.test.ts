import { describe, expect, it } from 'vitest'
import type {
  SidebarSection,
  SidebarSectionFamily,
  SidebarSectionRow,
} from '@/composables/useSessions'
import {
  buildSidebarDisplayProjection,
  isSidebarSessionOrderable,
  sidebarSessionOrderKeys,
} from './sidebarDisplayProjection'

function session(
  key: string,
  overrides: Partial<SidebarSectionRow> = {},
): SidebarSectionRow {
  return {
    rowKind: 'session',
    key,
    title: key,
    effectiveAgentId: 'main',
    agentName: 'Main',
    sessionKind: 'chat',
    depth: 0,
    runStatus: 'idle',
    runLabel: 'Idle',
    taskAttention: 'none',
    updatedAt: 100,
    hasContractGaps: false,
    ...overrides,
  }
}

function project(id: string, title: string): SidebarSectionRow {
  return {
    ...session(`workspace:${id}`, {
      title,
      workspaceId: id,
      workspaceTaskCount: 1,
    }),
    rowKind: 'workspace',
    sessionKind: 'workspace',
  }
}

function section(
  family: SidebarSectionFamily,
  rows: SidebarSectionRow[],
): SidebarSection {
  return { family, label: family, rows }
}

describe('buildSidebarDisplayProjection', () => {
  it('extracts pinned rows once and resolves project names from project headers', () => {
    const result = buildSidebarDisplayProjection([
      section('chats', [
        project('p1', 'OpenSquilla'),
        session('project-pin', { workspaceId: 'p1', pinned: true }),
        session('project-live', { workspaceId: 'p1' }),
        session('recent-pin', { pinned: true }),
        session('recent-live'),
      ]),
    ], ['recent-pin', 'project-pin'])

    expect(result.pinned.map(row => [row.key, row.displayProjectName])).toEqual([
      ['recent-pin', ''],
      ['project-pin', 'OpenSquilla'],
    ])
    expect(result.projects.map(row => row.key)).toEqual([
      'workspace:p1',
      'project-live',
    ])
    expect(result.recents[0]?.rows.map(row => row.key)).toEqual(['recent-live'])
    expect(result.allRows.filter(row => row.key === 'project-pin')).toHaveLength(1)
    expect(result.allRows.filter(row => row.key === 'recent-pin')).toHaveLength(1)
  })

  it('computes independent counts from the displayed collections', () => {
    const result = buildSidebarDisplayProjection([
      section('chats', [
        project('p1', 'One'),
        session('p1-pin', { workspaceId: 'p1', pinned: true }),
        project('p2', 'Two'),
        session('p2-live', { workspaceId: 'p2' }),
        session('recent-pin', { pinned: true }),
        session('recent-live'),
      ]),
    ], ['recent-pin', 'p1-pin'])

    expect(result.pinned).toHaveLength(2)
    expect(result.projectCount).toBe(2)
    expect(result.recentCount).toBe(1)
  })

  it('returns unpinned rows to their canonical project or Recents collection', () => {
    const result = buildSidebarDisplayProjection([
      section('chats', [
        project('p1', 'OpenSquilla'),
        session('project-task', { workspaceId: 'p1', pinned: false }),
        session('recent-task', { pinned: false }),
      ]),
    ])

    expect(result.projects.find(row => row.key === 'project-task')).toMatchObject({
      displayZone: 'projects',
      displayProjectName: 'OpenSquilla',
    })
    expect(result.recents[0]?.rows.find(row => row.key === 'recent-task')).toMatchObject({
      displayZone: 'recents',
      displayProjectName: '',
    })
    expect(result.pinned).toHaveLength(0)
  })

  it('uses one persisted order for pinned sessions from different families', () => {
    const result = buildSidebarDisplayProjection([
      section('chats', [session('chat-pin', { pinned: true })]),
      section('automations', [
        session('cron-pin', { sessionKind: 'cron', pinned: true }),
      ]),
    ], ['cron-pin', 'chat-pin'])

    expect(result.pinned.map(row => row.key)).toEqual(['cron-pin', 'chat-pin'])
    expect(result.pinned.map(row => row.displayFamily)).toEqual([
      'automations',
      'chats',
    ])
  })

  it('orders pinned sessions across every pinnable family without enabling regular non-chat drag', () => {
    const sections = [
      section('chats', [
        session('chat-pin', { pinned: true }),
        session('chat-live'),
      ]),
      section('channels', [
        session('channel-pin', { sessionKind: 'channel', pinned: true }),
        session('channel-live', { sessionKind: 'channel' }),
      ]),
      section('automations', [
        session('cron-pin', { sessionKind: 'cron', pinned: true }),
        session('cron-live', { sessionKind: 'cron' }),
      ]),
    ]

    expect(sidebarSessionOrderKeys(
      sections,
      ['cron-pin', 'channel-pin', 'chat-pin', 'chat-live'],
    )).toEqual([
      'cron-pin',
      'channel-pin',
      'chat-pin',
      'chat-live',
    ])
    expect(isSidebarSessionOrderable(sections[1]!.rows[0]!)).toBe(true)
    expect(isSidebarSessionOrderable(sections[1]!.rows[1]!)).toBe(false)
    expect(isSidebarSessionOrderable(sections[2]!.rows[0]!)).toBe(true)
    expect(isSidebarSessionOrderable(sections[2]!.rows[1]!)).toBe(false)
  })

  it('does not infer a project name from an ordinary session workspace path', () => {
    const result = buildSidebarDisplayProjection([
      section('chats', [
        session('ordinary', {
          pinned: true,
          workspace: 'D:\\repos\\not-a-persisted-project',
          workspaceLabel: 'not-a-persisted-project',
        }),
      ]),
    ])

    expect(result.pinned[0]?.displayProjectName).toBe('')
  })
})
