// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import SidebarConversations, { type SidebarSectionRow } from './SidebarConversations.vue'

const mounted: App[] = []

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

function taskRow(
  key: string,
  taskAttention: SidebarSectionRow['taskAttention'],
): SidebarSectionRow {
  return {
    rowKind: 'session',
    key,
    title: key,
    effectiveAgentId: 'main',
    agentName: 'Main',
    sessionKind: 'chat',
    depth: 0,
    runStatus: taskAttention === 'running' ? 'running' : 'idle',
    runLabel: taskAttention === 'running' ? 'Running' : 'Idle',
    taskAttention,
    updatedAt: Date.now(),
    hasContractGaps: false,
  }
}

async function mountSidebar(rows: SidebarSectionRow[]) {
  i18n.global.locale.value = 'en'
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(SidebarConversations, {
    sections: [{ family: 'chats', label: 'Tasks', rows }],
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: '⌘K',
  })
  app.use(i18n)
  app.mount(root)
  mounted.push(app)
  await nextTick()
  return root
}

describe('SidebarConversations task attention', () => {
  it('renders right-side running, completed, failed, and reserved empty states', async () => {
    const root = await mountSidebar([
      taskRow('running-task', 'running'),
      taskRow('completed-task', 'completed'),
      taskRow('failed-task', 'failed'),
      taskRow('idle-task', 'none'),
    ])
    const indicators = [...root.querySelectorAll<HTMLElement>('[data-testid="sidebar-task-attention"]')]

    expect(indicators).toHaveLength(4)
    expect(indicators[0].classList).toContain('sidebar-task-attention--running')
    expect(indicators[0].getAttribute('aria-label')).toBe('Task running')
    expect(indicators[1].classList).toContain('sidebar-task-attention--completed')
    expect(indicators[1].getAttribute('aria-label')).toBe('Task completed, result not viewed')
    expect(indicators[2].classList).toContain('sidebar-task-attention--failed')
    expect(indicators[2].getAttribute('aria-label')).toBe('Task unfinished, details not viewed')
    expect(indicators[3].classList).toContain('sidebar-task-attention--none')
    expect(indicators[3].getAttribute('aria-hidden')).toBe('true')
    expect(root.querySelector('.sidebar-history-run')).toBeNull()
  })
})

function groupRow(
  parentKey: string,
  count: number,
  attention: SidebarSectionRow['taskAttention'],
  children: SidebarSectionRow[],
): SidebarSectionRow {
  return {
    rowKind: 'session',
    key: parentKey,
    title: parentKey,
    effectiveAgentId: 'main',
    agentName: 'Main',
    sessionKind: 'chat',
    depth: 0,
    runStatus: 'idle',
    runLabel: 'Idle',
    taskAttention: 'none',
    updatedAt: Date.now(),
    hasContractGaps: false,
    subagentGroup: { count, attention, children },
  }
}

describe('SidebarConversations folded subagent groups', () => {
  it('renders a collapsed group header with the child count by default', async () => {
    const children = [0, 1, 2, 3].map(index => ({
      ...taskRow(`child-${index}`, 'none'),
      depth: 1,
    }))
    const root = await mountSidebar([groupRow('parent', 4, 'none', children)])

    const groupHead = root.querySelector<HTMLElement>('.sidebar-subagent-group-head')!
    expect(groupHead).not.toBeNull()
    expect(groupHead.getAttribute('aria-expanded')).toBe('false')
    expect(root.querySelector('[data-testid="sidebar-subagent-group-count"]')!.textContent).toBe('4')
    // Children stay hidden while collapsed.
    expect(root.querySelector('[data-session-key="child-0"]')).toBeNull()
  })

  it('expands the group on click and hides it again on a second click', async () => {
    const children = [0, 1, 2, 3].map(index => ({
      ...taskRow(`child-${index}`, 'none'),
      depth: 1,
    }))
    const root = await mountSidebar([groupRow('parent', 4, 'none', children)])
    const groupHead = root.querySelector<HTMLElement>('.sidebar-subagent-group-head')!

    groupHead.click()
    await nextTick()
    expect(groupHead.getAttribute('aria-expanded')).toBe('true')
    expect(root.querySelector('[data-session-key="child-0"]')).not.toBeNull()
    expect(root.querySelector('[data-session-key="child-3"]')).not.toBeNull()

    groupHead.click()
    await nextTick()
    // The TransitionGroup leave animation keeps a leaving row in the DOM for
    // one frame; wait for it to settle before asserting the collapse.
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
    expect(groupHead.getAttribute('aria-expanded')).toBe('false')
    expect(root.querySelector('[data-session-key="child-0"]')).toBeNull()
  })

  it('surfaces a running group attention marker in the collapsed header', async () => {
    const children = [0, 1, 2, 3].map(index => ({
      ...taskRow(`child-${index}`, index === 1 ? 'running' : 'none'),
      depth: 1,
    }))
    const root = await mountSidebar([groupRow('parent', 4, 'running', children)])

    const marker = root.querySelector<HTMLElement>('[data-testid="sidebar-task-attention"]')!
    expect(marker).not.toBeNull()
    expect(marker.classList).toContain('sidebar-task-attention--running')
    expect(marker.getAttribute('aria-label')).toBe('Task running')
  })

  it('keeps a plain row interactive when it is not a group', async () => {
    const root = await mountSidebar([taskRow('plain', 'none')])
    expect(root.querySelector('.sidebar-subagent-group-head')).toBeNull()
    expect(root.querySelector('[data-session-key="plain"]')).not.toBeNull()
  })
})
