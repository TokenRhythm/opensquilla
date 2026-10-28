// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, type App } from 'vue'

import appSource from './App.vue?raw'
import GitBranchChip from './components/chat/GitBranchChip.vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string, params?: Record<string, unknown>) => (
      params?.branch ? `${key}|${String(params.branch)}` : key
    ),
  }),
}))

const apps: App[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function mountChip(initial: { label: string; dirtyCount: number; loading?: boolean }) {
  const props = reactive({ ...initial })
  let opened = 0
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(defineComponent(() => () => h(GitBranchChip, {
    ...props,
    onOpen: () => { opened += 1 },
  })))
  apps.push(app)
  app.mount(root)
  await nextTick()
  return { root, props, opened: () => opened, button: () => root.querySelector('button')! }
}

describe('App git branch chip contract', () => {
  it('mounts the branch chip as the first topbar-right control on chat routes', () => {
    const rightStart = appSource.indexOf('<div class="topbar-right">')
    const rightEnd = appSource.indexOf('<WorkbenchToggle', rightStart)
    const right = appSource.slice(rightStart, rightEnd)

    expect(right).toContain('<GitBranchChip')
    expect(right.trimStart().startsWith('<div class="topbar-right">\n        <GitBranchChip')).toBe(true)
    expect(right).toContain('v-if="isChatRoute && gitStatus.branchLabel.value"')
    expect(right).toContain(':dirty-count="gitStatus.dirtyCount.value"')
    expect(right).toContain(':loading="gitStatus.loading.value"')
    expect(right).toContain('@open="openGitChanges"')
  })

  it('reads git status for the active chat session and opens a stable workbench tab', () => {
    expect(appSource).toContain("import GitBranchChip from './components/chat/GitBranchChip.vue'")
    expect(appSource).toContain('const gitAccess = useGitAccess()')
    expect(appSource).toContain('chatRouteHeader.model.workspaceId.value ?? null')
    expect(appSource).toContain('active: computed(() => isChatRoute.value)')

    const handlerStart = appSource.indexOf('function openGitChanges()')
    const handlerEnd = appSource.indexOf('\n}', handlerStart)
    const handler = appSource.slice(handlerStart, handlerEnd)
    expect(handler).toContain("id: 'git-changes'")
    expect(handler).toContain("kind: 'git-changes'")
    expect(handler).toContain("title: t('workbench.gitChanges.title')")
    expect(handler).toContain('workbenchStore.openItem(item, { activate: true })')
    expect(handler).toContain('workbenchStore.setExpanded(true)')
  })
})

describe('GitBranchChip', () => {
  it('renders the branch name and opens the workbench on click', async () => {
    const { button, opened } = await mountChip({ label: 'main', dirtyCount: 0 })

    expect(button().getAttribute('type')).toBe('button')
    expect(button().textContent).toContain('main')
    expect(button().getAttribute('title')).toBe('workbench.gitChanges.openHint|main')
    expect(button().getAttribute('aria-label')).toBe('workbench.gitChanges.openHint|main')
    expect(button().querySelector('.git-branch-chip__dirty')).toBeNull()

    button().click()
    await nextTick()
    expect(opened()).toBe(1)
  })

  it('marks a dirty working tree with a star after the branch name', async () => {
    const { button } = await mountChip({ label: 'main', dirtyCount: 3 })

    expect(button().querySelector('.git-branch-chip__dirty')?.textContent).toBe('*')
    expect(button().textContent?.trim()).toBe('main*')
  })

  it('stays quiet while a read is in flight instead of flashing', async () => {
    const { button, props, root } = await mountChip({ label: 'main', dirtyCount: 0, loading: true })

    expect(button().getAttribute('aria-busy')).toBe('true')
    expect(button().classList.contains('git-branch-chip--loading')).toBe(true)
    expect(root.querySelector('.git-branch-chip__label')?.textContent).toBe('main')

    props.loading = false
    await nextTick()
    expect(button().getAttribute('aria-busy')).toBeNull()
    expect(button().classList.contains('git-branch-chip--loading')).toBe(false)
  })
})
