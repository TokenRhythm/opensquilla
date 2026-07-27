// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import SidebarConversations from './SidebarConversations.vue'
import type { SidebarSection, SidebarSectionRow } from '@/composables/useSessions'

const confirm = vi.hoisted(() => vi.fn(async () => true))

vi.mock('@/composables/useConfirm', () => ({
  useConfirm: () => ({ confirm }),
}))

const mountedApps: App<Element>[] = []

function projectRow(overrides: Partial<SidebarSectionRow> = {}): SidebarSectionRow {
  return {
    rowKind: 'workspace',
    key: 'workspace:project-a',
    title: 'Project A',
    effectiveAgentId: '',
    agentName: '',
    sessionKind: 'workspace',
    depth: 0,
    runStatus: 'idle',
    runLabel: '',
    updatedAt: 0,
    hasContractGaps: false,
    workspace: 'D:\\repos\\project-a',
    workspaceId: 'project-a',
    workspaceLabel: 'Project A',
    workspaceDisplayPath: 'D:\\repos\\project-a',
    workspaceTaskCount: 2,
    workspacePinned: false,
    workspaceAvailable: true,
    ...overrides,
  } as SidebarSectionRow
}

function taskRow(overrides: Partial<SidebarSectionRow> = {}): SidebarSectionRow {
  return {
    rowKind: 'session',
    key: 'agent:main:webchat:task-a',
    title: 'Project task',
    effectiveAgentId: 'main',
    agentName: 'Main',
    sessionKind: 'chat',
    depth: 1,
    runStatus: 'idle',
    runLabel: 'Idle',
    updatedAt: 1,
    hasContractGaps: false,
    workspaceId: 'project-a',
    ...overrides,
  }
}

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        chrome: { searchChats: 'Search tasks' },
        sessions: { filter: { chats: 'Tasks' } },
        shared: {
          sidebar: {
            recentConversations: 'Recent tasks',
            recents: 'Tasks',
            refresh: 'Refresh',
            enterSelectionMode: 'Select tasks',
            rowActions: 'Actions for {title}',
            statusLabel: '{status}',
            rename: 'Rename',
            delete: 'Delete',
          },
        },
        workspaces: {
          projectInfo: '{path}; {count} tasks',
          taskCount: '{count} tasks',
          newTask: 'New project task',
          unavailableProjectCannotStartTask: 'This project directory is unavailable',
          moreActions: 'Project actions',
          pin: 'Pin project',
          unpin: 'Unpin project',
          editProject: 'Edit project',
          deleteHistory: 'Delete project task history',
          removeProject: 'Remove project',
          deleteHistoryTitle: 'Delete project task history?',
          deleteHistoryBody: 'Delete {count} tasks from {name}.',
          deleteHistoryConfirm: 'Delete history',
          unavailable: 'Directory unavailable',
        },
      },
    },
  })
}

async function mountSidebar(
  rows: SidebarSectionRow[],
  canManageProjects = true,
) {
  const sections: SidebarSection[] = [{ family: 'chats', label: 'Tasks', rows }]
  const events = {
    select: vi.fn(),
    newProjectTask: vi.fn(),
    projectPin: vi.fn(),
    projectEdit: vi.fn(),
    projectDeleteHistory: vi.fn(),
    projectRemove: vi.fn(),
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(SidebarConversations, {
    sections,
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: 'Ctrl+K',
    canManageProjects,
    onSelect: events.select,
    onNewProjectTask: events.newProjectTask,
    onProjectPin: events.projectPin,
    onProjectEdit: events.projectEdit,
    onProjectDeleteHistory: events.projectDeleteHistory,
    onProjectRemove: events.projectRemove,
  }))
  const app = createApp(Root)
  app.use(i18n())
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  return { host, events }
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  localStorage.clear()
  confirm.mockClear()
})

describe('SidebarConversations project workspaces', () => {
  it('toggles a project without selecting it and keeps project details visible', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const disclosure = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-disclosure"]')
    const info = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-info"]')

    expect(disclosure).toBeTruthy()
    expect(disclosure?.getAttribute('aria-expanded')).toBe('true')
    expect(info).toBeTruthy()
    expect(info?.innerHTML).toContain('M3 6.5')
    expect(host.textContent).toContain('D:\\repos\\project-a')
    expect(host.textContent).toContain('2 tasks')

    disclosure?.click()
    await nextTick()
    await new Promise<void>(resolve => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    })
    await nextTick()

    expect(events.select).not.toHaveBeenCalled()
    expect(
      host.querySelector('[data-testid="project-workspace-disclosure"]')?.getAttribute('aria-expanded'),
    ).toBe('false')
    expect(host.querySelector('[data-session-key="agent:main:webchat:task-a"]')).toBeNull()
  })

  it('creates a project task from the pencil action', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const pencil = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-new-task"]')

    expect(pencil).toBeTruthy()
    pencil?.click()
    await nextTick()

    expect(events.newProjectTask).toHaveBeenCalledWith('project-a')
    expect(events.select).not.toHaveBeenCalled()
  })

  it('disables the new-task action for an unavailable project', async () => {
    const { host, events } = await mountSidebar([
      projectRow({ workspaceAvailable: false }),
      taskRow(),
    ])
    const pencil = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-new-task"]')

    expect(pencil?.disabled).toBe(true)
    expect(pencil?.getAttribute('title')).toBe('This project directory is unavailable')
    pencil?.click()
    await nextTick()

    expect(events.newProjectTask).not.toHaveBeenCalled()
  })

  it('exposes pin, edit, delete-history, and remove through the project menu', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const more = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-more"]')

    more?.click()
    await nextTick()
    document.body.querySelector<HTMLButtonElement>('[data-project-action="pin"]')?.click()
    expect(events.projectPin).toHaveBeenCalledWith({ workspaceId: 'project-a', pinned: true })

    more?.click()
    await nextTick()
    document.body.querySelector<HTMLButtonElement>('[data-project-action="edit"]')?.click()
    expect(events.projectEdit).toHaveBeenCalledWith('project-a')

    more?.click()
    await nextTick()
    document.body.querySelector<HTMLButtonElement>('[data-project-action="delete-history"]')?.click()
    await nextTick()
    expect(confirm).toHaveBeenCalled()
    expect(events.projectDeleteHistory).toHaveBeenCalledWith('project-a')

    more?.click()
    await nextTick()
    document.body.querySelector<HTMLButtonElement>('[data-project-action="remove"]')?.click()
    expect(events.projectRemove).toHaveBeenCalledWith('project-a')
  })

  it('keeps project navigation but hides management actions for non-owners', async () => {
    const { host, events } = await mountSidebar(
      [projectRow(), taskRow()],
      false,
    )

    expect(host.querySelector('[data-testid="project-workspace-disclosure"]')).toBeTruthy()
    expect(host.querySelector('[data-testid="project-workspace-new-task"]')).toBeNull()
    expect(host.querySelector('[data-testid="project-workspace-more"]')).toBeNull()

    host.querySelector<HTMLButtonElement>(
      '[data-session-key="agent:main:webchat:task-a"] .sidebar-history-item',
    )?.click()
    await nextTick()
    expect(events.select).toHaveBeenCalledWith('agent:main:webchat:task-a')
  })

})
