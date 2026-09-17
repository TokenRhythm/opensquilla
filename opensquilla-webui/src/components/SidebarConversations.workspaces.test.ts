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
    taskAttention: 'none',
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
    taskAttention: 'none',
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
            recents: 'Recents',
            pinned: 'Pinned',
            noConversations: 'No tasks yet.',
            refresh: 'Refresh',
            enterSelectionMode: 'Select tasks',
            rowActions: 'Actions for {title}',
            pinTask: 'Pin task',
            unpinTask: 'Unpin task',
            statusLabel: '{status}',
            rename: 'Rename',
            delete: 'Delete',
          },
        },
        workspaces: {
          projects: 'Projects',
          createProject: 'Create project',
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
          menuDeleteHistory: 'Delete history',
          menuRemove: 'Remove',
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
  canCreateProjects = canManageProjects,
  sessionOrder: string[] = [],
) {
  const sections: SidebarSection[] = [{ family: 'chats', label: 'Tasks', rows }]
  const events = {
    select: vi.fn(),
    newProject: vi.fn(),
    newProjectTask: vi.fn(),
    projectPin: vi.fn(),
    projectEdit: vi.fn(),
    projectDeleteHistory: vi.fn(),
    projectRemove: vi.fn(),
    reorder: vi.fn(),
    sessionPin: vi.fn(),
  }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const Root = defineComponent(() => () => h(SidebarConversations, {
    sections,
    sessionOrder,
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: 'Ctrl+K',
    canManageProjects,
    canCreateProjects,
    onSelect: events.select,
    onNewProject: events.newProject,
    onNewProjectTask: events.newProjectTask,
    onProjectPin: events.projectPin,
    onProjectEdit: events.projectEdit,
    onProjectDeleteHistory: events.projectDeleteHistory,
    onProjectRemove: events.projectRemove,
    onReorder: events.reorder,
    onSessionPin: events.sessionPin,
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
  it('creates projects from the section header while project-row plus actions remain task scoped', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const createProject = host.querySelector<HTMLButtonElement>('[data-testid="sidebar-create-project"]')
    const createTask = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-new-task"]')

    expect(createProject?.getAttribute('aria-label')).toBe('Create project')
    expect(createTask?.getAttribute('aria-label')).toBe('New project task')

    createProject?.click()
    await nextTick()

    expect(events.newProject).toHaveBeenCalledOnce()
    expect(events.newProjectTask).not.toHaveBeenCalled()
  })

  it('separates project work from ordinary recent tasks', async () => {
    const { host } = await mountSidebar([
      projectRow(),
      taskRow(),
      taskRow({
        key: 'agent:main:webchat:ordinary',
        title: 'Ordinary task',
        depth: 0,
        workspaceId: undefined,
      }),
    ])

    const projectHeading = host.querySelector('[data-sidebar-zone-heading="projects"]')
    expect(projectHeading?.querySelector('.sidebar-zone-heading__label')?.textContent).toBe('Projects')
    expect(projectHeading?.querySelector('.sidebar-zone-heading__count')?.textContent).toBe('1')
    expect(
      host.querySelector('[data-session-key="agent:main:webchat:task-a"]')
        ?.getAttribute('data-sidebar-zone'),
    ).toBe('projects')
    const ordinary = host.querySelector('[data-session-key="agent:main:webchat:ordinary"]')
    expect(ordinary?.getAttribute('data-sidebar-zone')).toBe('recents')
    expect(
      host.querySelector('[data-sidebar-zone-heading="recents"] .sidebar-zone-heading__label')
        ?.textContent,
    ).toBe('Recents')
  })

  it('renders peer zone headings with independent counts and unique pinned rows', async () => {
    const { host } = await mountSidebar([
      projectRow(),
      taskRow({ key: 'project-pin', pinned: true }),
      taskRow({ key: 'project-live', pinned: false }),
      taskRow({
        key: 'recent-pin',
        title: 'Pinned recent',
        workspaceId: undefined,
        depth: 0,
        pinned: true,
      }),
      taskRow({
        key: 'recent-live',
        title: 'Recent task',
        workspaceId: undefined,
        depth: 0,
        pinned: false,
      }),
    ], true, true, ['recent-pin', 'project-pin'])

    expect(
      Array.from(host.querySelectorAll<HTMLElement>('.sidebar-zone-heading'))
        .map(node => ({
          label: node.querySelector('.sidebar-zone-heading__label')?.textContent,
          count: node.querySelector('.sidebar-zone-heading__count')?.textContent,
        })),
    ).toEqual([
      { label: 'Pinned', count: '2' },
      { label: 'Projects', count: '1' },
      { label: 'Recents', count: '1' },
    ])
    expect(host.querySelectorAll('[data-session-key="project-pin"]')).toHaveLength(1)
    expect(host.querySelectorAll('[data-session-key="recent-pin"]')).toHaveLength(1)
    expect(
      host.querySelector('[data-session-key="project-pin"]')?.getAttribute('data-sidebar-zone'),
    ).toBe('pinned')
    expect(
      host.querySelector('[data-session-key="project-pin"]')?.getAttribute('data-depth'),
    ).toBe('0')
    expect(
      host.querySelector('[data-session-key="project-pin"] .sidebar-history-rail'),
    ).toBeNull()
    expect(
      host.querySelector('[data-session-key="recent-pin"]')?.getAttribute('data-sidebar-zone'),
    ).toBe('pinned')
    expect(
      host.querySelector('[data-session-key="project-live"]')?.getAttribute('data-sidebar-zone'),
    ).toBe('projects')
    expect(
      host.querySelector('[data-session-key="recent-live"]')?.getAttribute('data-sidebar-zone'),
    ).toBe('recents')
  })

  it('reorders pinned chats across their original project boundaries', async () => {
    const { host, events } = await mountSidebar([
      projectRow({
        key: 'workspace:a',
        title: 'A',
        workspaceId: 'a',
        workspace: '/a',
      }),
      taskRow({ key: 'a-pin', workspaceId: 'a', pinned: true }),
      projectRow({
        key: 'workspace:b',
        title: 'B',
        workspaceId: 'b',
        workspace: '/b',
      }),
      taskRow({ key: 'b-pin', workspaceId: 'b', pinned: true }),
    ], true, true, ['a-pin', 'b-pin'])
    const source = host.querySelector<HTMLElement>('[data-session-key="a-pin"]')
    const target = host.querySelector<HTMLElement>('[data-session-key="b-pin"]')

    vi.spyOn(document, 'elementFromPoint').mockReturnValue(target || null)
    source?.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      clientX: 10,
      clientY: 10,
    }))
    document.dispatchEvent(new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 20,
    }))
    document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
    await nextTick()

    expect(events.reorder).toHaveBeenCalledWith({
      draggedKey: 'a-pin',
      targetKey: 'b-pin',
      position: 'after',
    })
  })

  it('keeps pinned chats and automations in one reorderable collection', async () => {
    const { host, events } = await mountSidebar([
      taskRow({ key: 'chat-pin', workspaceId: undefined, depth: 0, pinned: true }),
      taskRow({
        key: 'cron-pin',
        workspaceId: undefined,
        depth: 0,
        sessionKind: 'cron',
        pinned: true,
      }),
    ], false, false, ['chat-pin', 'cron-pin'])
    const source = host.querySelector<HTMLElement>('[data-session-key="chat-pin"]')
    const target = host.querySelector<HTMLElement>('[data-session-key="cron-pin"]')

    expect(target?.classList.contains('is-reorderable')).toBe(true)
    vi.spyOn(document, 'elementFromPoint').mockReturnValue(target || null)
    source?.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      clientX: 10,
      clientY: 10,
    }))
    document.dispatchEvent(new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 20,
    }))
    document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
    await nextTick()

    expect(events.reorder).toHaveBeenCalledWith({
      draggedKey: 'chat-pin',
      targetKey: 'cron-pin',
      position: 'after',
    })
  })

  it('only enables non-chat drag after a channel or automation is pinned', async () => {
    const { host } = await mountSidebar([
      taskRow({
        key: 'channel-pin',
        workspaceId: undefined,
        depth: 0,
        sessionKind: 'channel',
        pinned: true,
      }),
      taskRow({
        key: 'channel-live',
        workspaceId: undefined,
        depth: 0,
        sessionKind: 'channel',
      }),
      taskRow({
        key: 'cron-live',
        workspaceId: undefined,
        depth: 0,
        sessionKind: 'cron',
      }),
    ])

    expect(
      host.querySelector('[data-session-key="channel-pin"]')?.classList.contains('is-reorderable'),
    ).toBe(true)
    expect(
      host.querySelector('[data-session-key="channel-live"]')?.classList.contains('is-reorderable'),
    ).toBe(false)
    expect(
      host.querySelector('[data-session-key="cron-live"]')?.classList.contains('is-reorderable'),
    ).toBe(false)
  })

  it('shows a project session hover card outside the sidebar with its project name', async () => {
    const { host } = await mountSidebar([projectRow(), taskRow()])
    const row = host.querySelector<HTMLElement>(
      '[data-session-key="agent:main:webchat:task-a"]',
    )
    Object.defineProperty(row, 'getBoundingClientRect', {
      value: () => ({ left: 12, right: 280, top: 40 }),
    })

    row?.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()

    const preview = document.body.querySelector('.sidebar-session-preview')
    expect(preview?.querySelector('.sidebar-session-preview__title')?.textContent)
      .toBe('Project task')
    expect(preview?.querySelector('[data-testid="sidebar-session-project"]')?.textContent)
      .toContain('Project A')
    expect(host.querySelector('.sidebar-session-preview')).toBeNull()
    expect(row?.querySelector('.sidebar-history-item')?.getAttribute('title')).toBeNull()
  })

  it('keeps a long mixed-language title and tooltip relationship intact', async () => {
    const title = '请执行一项 Deep Research 任务：分析 Fortinet 的长期经营表现与竞争格局'
    const { host } = await mountSidebar([projectRow(), taskRow({ title })])
    const row = host.querySelector<HTMLElement>(
      '[data-session-key="agent:main:webchat:task-a"]',
    )
    Object.defineProperty(row, 'getBoundingClientRect', {
      value: () => ({ left: 12, right: 280, top: 40 }),
    })

    row?.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()

    const trigger = row?.querySelector('.sidebar-history-item')
    const preview = document.body.querySelector('.sidebar-session-preview')
    expect(row?.querySelector('.sidebar-history-title')?.textContent).toBe(title)
    expect(preview?.querySelector('.sidebar-session-preview__title')?.textContent).toBe(title)
    expect(preview?.querySelector('.sidebar-session-preview__time')?.textContent).not.toBe('')
    expect(preview?.querySelector('[data-testid="sidebar-session-project"]')?.textContent)
      .toContain('Project A')
    expect(trigger?.getAttribute('aria-describedby')).toBe('sidebar-session-preview')
    expect(preview?.getAttribute('role')).toBe('tooltip')
  })

  it('keeps the hover card but omits the project row for an unbound session', async () => {
    const { host } = await mountSidebar([
      taskRow({
        key: 'recent-task',
        title: 'Recent task',
        workspaceId: undefined,
        depth: 0,
      }),
    ])
    const row = host.querySelector<HTMLElement>('[data-session-key="recent-task"]')
    Object.defineProperty(row, 'getBoundingClientRect', {
      value: () => ({ left: 12, right: 280, top: 40 }),
    })

    row?.dispatchEvent(new MouseEvent('mouseenter'))
    await nextTick()

    const preview = document.body.querySelector('.sidebar-session-preview')
    expect(preview?.querySelector('.sidebar-session-preview__title')?.textContent)
      .toBe('Recent task')
    expect(preview?.querySelector('[data-testid="sidebar-session-project"]')).toBeNull()
  })

  it('opens the session card on keyboard focus and closes it on focus loss', async () => {
    const { host } = await mountSidebar([projectRow(), taskRow()])
    const row = host.querySelector<HTMLElement>(
      '[data-session-key="agent:main:webchat:task-a"]',
    )
    Object.defineProperty(row, 'getBoundingClientRect', {
      value: () => ({ left: 12, right: 280, top: 40 }),
    })

    row?.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    await nextTick()
    expect(document.body.querySelector('.sidebar-session-preview')).toBeTruthy()

    row?.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
    await nextTick()
    expect(document.body.querySelector('.sidebar-session-preview')).toBeNull()
  })

  it('emits a reorder when one recent chat is dragged onto another', async () => {
    const first = taskRow({
      key: 'agent:main:webchat:first',
      title: 'First',
      depth: 0,
      workspaceId: undefined,
    })
    const second = taskRow({
      key: 'agent:main:webchat:second',
      title: 'Second',
      depth: 0,
      workspaceId: undefined,
    })
    const { host, events } = await mountSidebar([first, second])
    const source = host.querySelector<HTMLElement>(`[data-session-key="${first.key}"]`)
    const target = host.querySelector<HTMLElement>(`[data-session-key="${second.key}"]`)

    expect(source?.classList.contains('is-reorderable')).toBe(true)
    vi.spyOn(document, 'elementFromPoint').mockReturnValue(target || null)
    source?.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      clientX: 10,
      clientY: 10,
    }))
    document.dispatchEvent(new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 20,
    }))
    document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
    await nextTick()

    expect(events.reorder).toHaveBeenCalledWith({
      draggedKey: first.key,
      targetKey: second.key,
      position: 'after',
    })
  })

  it('pins and unpins a task from its row menu', async () => {
    const row = taskRow({ depth: 0, workspaceId: undefined })
    const { host, events } = await mountSidebar([row])
    const actions = host.querySelector<HTMLButtonElement>(`[aria-label="Actions for ${row.title}"]`)
    actions?.click()
    await nextTick()
    const pin = Array.from(document.body.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'))
      .find(button => button.textContent?.trim() === 'Pin task')
    pin?.click()
    await nextTick()

    expect(events.sessionPin).toHaveBeenCalledWith({ key: row.key, pinned: true })
  })

  it('does not reorder a project task into the recents group', async () => {
    const projectTask = taskRow()
    const recentTask = taskRow({
      key: 'agent:main:webchat:recent',
      title: 'Recent',
      depth: 0,
      workspaceId: undefined,
    })
    const { host, events } = await mountSidebar([projectRow(), projectTask, recentTask])
    const source = host.querySelector<HTMLElement>(`[data-session-key="${projectTask.key}"]`)
    const target = host.querySelector<HTMLElement>(`[data-session-key="${recentTask.key}"]`)

    vi.spyOn(document, 'elementFromPoint').mockReturnValue(target || null)
    source?.dispatchEvent(new MouseEvent('pointerdown', {
      bubbles: true,
      cancelable: true,
      button: 0,
      clientX: 10,
      clientY: 10,
    }))
    document.dispatchEvent(new MouseEvent('pointermove', {
      bubbles: true,
      cancelable: true,
      clientX: 20,
      clientY: 20,
    }))
    document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true }))
    await nextTick()

    expect(events.reorder).not.toHaveBeenCalled()
  })

  it('keeps an empty project compact and gives recents its own empty state', async () => {
    const { host } = await mountSidebar([
      projectRow({ workspaceTaskCount: 0 }),
      {
        ...taskRow(),
        rowKind: 'workspace-empty',
        key: 'workspace:project-a:empty',
        title: 'No tasks',
        sessionKind: 'workspace-empty',
      },
    ])

    expect(host.querySelector('.sidebar-workspace-empty')).toBeNull()
    expect(
      host.querySelector('[data-sidebar-zone-heading="recents"] .sidebar-zone-heading__label')
        ?.textContent,
    ).toBe('Recents')
    expect(host.querySelector('.sidebar-zone-empty__body')?.textContent).toBe('No tasks yet.')
  })

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

  it('creates a project task and expands its project from the persistent plus action', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const plus = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-new-task"]')
    const disclosure = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-disclosure"]')

    expect(plus).toBeTruthy()
    expect(plus?.classList.contains('sidebar-project-action--new-task')).toBe(true)
    expect(plus?.querySelector('svg')).not.toBeNull()
    disclosure?.click()
    await nextTick()
    expect(disclosure?.getAttribute('aria-expanded')).toBe('false')

    plus?.click()
    await nextTick()

    expect(events.newProjectTask).toHaveBeenCalledWith('project-a')
    expect(events.select).not.toHaveBeenCalled()
    expect(disclosure?.getAttribute('aria-expanded')).toBe('true')
    expect(host.querySelector('[data-session-key="agent:main:webchat:task-a"]')).not.toBeNull()
  })

  it('keeps a provisional project task current and free of persisted-task actions', async () => {
    const provisional = taskRow({
      key: 'draft:project:project-a:1',
      title: 'New task',
      provisional: true,
    })
    const { host, events } = await mountSidebar([projectRow(), provisional])
    const row = host.querySelector<HTMLElement>('[data-session-key="draft:project:project-a:1"]')

    row?.querySelector<HTMLButtonElement>('.sidebar-history-item')?.click()
    await nextTick()

    expect(events.select).not.toHaveBeenCalled()
    expect(row?.querySelector('.sidebar-row-menu-btn')).toBeNull()
    expect(row?.querySelector('.sidebar-agent-badge')).toBeNull()
  })

  it('disables the new-task action for an unavailable project', async () => {
    const { host, events } = await mountSidebar([
      projectRow({ workspaceAvailable: false }),
      taskRow(),
    ])
    const plus = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-new-task"]')

    expect(plus?.disabled).toBe(true)
    expect(plus?.getAttribute('title')).toBe('This project directory is unavailable')
    plus?.click()
    await nextTick()

    expect(events.newProjectTask).not.toHaveBeenCalled()
  })

  it('exposes pin, edit, delete-history, and remove through the project menu', async () => {
    const { host, events } = await mountSidebar([projectRow(), taskRow()])
    const more = host.querySelector<HTMLButtonElement>('[data-testid="project-workspace-more"]')
    Object.defineProperty(more, 'getBoundingClientRect', {
      value: () => ({
        x: 170,
        y: 20,
        width: 20,
        height: 20,
        top: 20,
        right: 190,
        bottom: 40,
        left: 170,
        toJSON: () => ({}),
      }),
    })

    more?.click()
    await nextTick()
    const menu = document.body.querySelector<HTMLElement>('.sidebar-project-menu')
    expect(menu?.classList.contains('sidebar-row-menu')).toBe(true)
    expect(menu?.querySelectorAll('.sidebar-row-menu__item')).toHaveLength(4)
    expect(menu?.style.left).toBe('196px')
    expect(menu?.style.transform).toBe('none')
    expect(
      Array.from(menu?.querySelectorAll('.sidebar-row-menu__item') || [])
        .map(item => item.textContent?.trim()),
    ).toEqual(['Pin project', 'Edit project', 'Delete history', 'Remove'])
    expect(
      menu?.querySelector('[data-project-action="delete-history"]')
        ?.classList.contains('sidebar-row-menu__item--danger'),
    ).toBe(false)
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
    expect(host.querySelector('[data-testid="sidebar-create-project"]')).toBeNull()
    expect(host.querySelector('[data-testid="project-workspace-new-task"]')).toBeNull()
    expect(host.querySelector('[data-testid="project-workspace-more"]')).toBeNull()

    host.querySelector<HTMLButtonElement>(
      '[data-session-key="agent:main:webchat:task-a"] .sidebar-history-item',
    )?.click()
    await nextTick()
    expect(events.select).toHaveBeenCalledWith('agent:main:webchat:task-a')
  })

  it('renders task rows without leading status dots', async () => {
    const { host } = await mountSidebar([
      projectRow(),
      taskRow(),
      taskRow({
        key: 'agent:main:webchat:running',
        title: 'Running task',
        runStatus: 'running',
        runLabel: 'Running',
      }),
    ])

    expect(host.querySelector('.sidebar-history-dot')).toBeNull()
    expect(host.querySelector('[data-session-key="agent:main:webchat:task-a"]')?.textContent)
      .toContain('Project task')
    expect(host.querySelector('[data-session-key="agent:main:webchat:running"]')?.textContent)
      .toContain('Running')
  })

})
