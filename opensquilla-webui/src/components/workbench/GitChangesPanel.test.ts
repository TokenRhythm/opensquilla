// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import en from '@/locales/en.json'
import {
  GIT_ACCESS_KEY,
  type GitAccess,
  type GitDiffPayload,
  type GitStatusPayload,
} from '@/modules/git'
import type { WorkbenchItem, WorkbenchPanelRenderState } from '@/workbench/types'
import GitChangesPanel from './GitChangesPanel.vue'
import { createGitChangesWorkbenchDefinition, gitChangesWorkspaceId } from './gitChangesWorkbenchProvider'

const cleanups: Array<() => void> = []
afterEach(() => {
  for (const cleanup of cleanups.splice(0)) cleanup()
  vi.restoreAllMocks()
})

function statusPayload(patch: Partial<GitStatusPayload> = {}): GitStatusPayload {
  return {
    available: true,
    workspacePath: '/repo',
    branch: 'main',
    detached: false,
    unborn: false,
    headOid: 'abcdef1234567890',
    ahead: 0,
    behind: 0,
    clean: false,
    files: [],
    reason: null,
    ...patch,
  }
}

function diffPayload(patch: Partial<GitDiffPayload> = {}): GitDiffPayload {
  return {
    path: 'src/App.vue',
    diff: '',
    binary: false,
    truncated: false,
    additions: 0,
    deletions: 0,
    ...patch,
  }
}

function mountPanel(
  access: GitAccess,
  props: { workspaceId?: string | null; visible?: boolean } = { workspaceId: 'ws-1' },
): HTMLElement {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp({ render: () => h(GitChangesPanel, props) })
  app.provide(GIT_ACCESS_KEY, access)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } })).mount(element)
  cleanups.push(() => { app.unmount(); element.remove() })
  return element
}

function rows(element: HTMLElement): HTMLElement[] {
  return [...element.querySelectorAll<HTMLElement>('[data-testid="git-changes-file"]')]
}

function text(element: Element | null): string {
  return element?.textContent?.replace(/\s+/g, ' ').trim() ?? ''
}

describe('GitChangesPanel', () => {
  it('renders staged, unstaged and untracked groups from one status read', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({
        ahead: 2,
        behind: 1,
        files: [
          { path: 'src/App.vue', status: 'modified', staged: true },
          { path: 'src/modules/git.ts', status: 'modified', staged: false },
          { path: 'docs/notes.md', status: 'untracked', staged: false },
        ],
      })),
      diff: vi.fn(async () => diffPayload()),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(rows(element)).toHaveLength(3))

    expect(access.status).toHaveBeenCalledTimes(1)
    expect(text(element.querySelector('[data-testid="git-changes-panel"] .git-changes__branch'))).toContain('main')
    expect(element.querySelectorAll('.git-changes__tracking')).toHaveLength(2)
    expect(text(element.querySelector('.git-changes__count'))).toContain('3 changed')

    const staged = element.querySelector('[data-testid="git-changes-section-staged"]')
    const unstaged = element.querySelector('[data-testid="git-changes-section-unstaged"]')
    const untracked = element.querySelector('[data-testid="git-changes-section-untracked"]')
    expect(text(staged)).toContain('Staged')
    expect(text(staged)).toContain('App.vue')
    expect(text(staged)).toContain('src')
    expect(text(unstaged)).toContain('Unstaged')
    expect(text(unstaged)).toContain('git.ts')
    expect(text(untracked)).toContain('Untracked')
    expect(text(untracked)).toContain('notes.md')

    // Status letters: M for a modification, ? for an untracked entry.
    expect([...rows(element)].map(row => text(row.querySelector('.git-changes__status'))))
      .toEqual(['M', 'M', '?'])
  })

  it('reports an unusable repository instead of rendering an empty list', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({ available: false, branch: null, reason: 'not_repository' })),
      diff: vi.fn(async () => diffPayload()),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-unavailable"]')).toBeTruthy())
    expect(text(element.querySelector('[data-testid="git-changes-unavailable"]')))
      .toContain('not a Git repository')
    expect(rows(element)).toHaveLength(0)
    expect(element.querySelector('[data-testid="git-changes-clean"]')).toBeNull()
  })

  it('separates "git missing" from "not a repository"', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({ available: false, reason: 'git_unavailable' })),
      diff: vi.fn(async () => diffPayload()),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-unavailable"]')).toBeTruthy())
    expect(text(element.querySelector('[data-testid="git-changes-unavailable"]')))
      .toContain('Git is not available')
  })

  it('says the tree is clean when a usable repository has no changes', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({ clean: true, files: [] })),
      diff: vi.fn(async () => diffPayload()),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-clean"]')).toBeTruthy())
    expect(rows(element)).toHaveLength(0)
  })

  it('requests the clicked file diff with its group and renders +/- lines', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({
        files: [{ path: 'src/App.vue', status: 'modified', staged: true }],
      })),
      diff: vi.fn(async () => diffPayload({
        diff: [
          'diff --git a/src/App.vue b/src/App.vue',
          '--- a/src/App.vue',
          '+++ b/src/App.vue',
          '@@ -1,2 +1,2 @@',
          ' unchanged',
          '-old line',
          '+new line',
          '',
        ].join('\n'),
        additions: 1,
        deletions: 1,
      })),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(rows(element)).toHaveLength(1))
    rows(element)[0].click()

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-diff-code"]')).toBeTruthy())
    expect(access.diff).toHaveBeenCalledWith(expect.objectContaining({
      path: 'src/App.vue',
      staged: true,
      workspaceId: 'ws-1',
      signal: expect.anything(),
    }))
    expect(text(element.querySelector('.git-changes__diff-line.is-add'))).toBe('+new line')
    expect(text(element.querySelector('.git-changes__diff-line.is-del'))).toBe('-old line')
    expect(element.querySelectorAll('.git-changes__diff-line')).toHaveLength(7)
    expect(text(element.querySelector('[data-testid="git-changes-diff"] .git-changes__badge'))).toBe('Staged')
  })

  it('keeps a binary diff readable and flags a truncated one', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload({
        files: [{ path: 'assets/logo.png', status: 'untracked', staged: false }],
      })),
      diff: vi.fn(async () => diffPayload({ binary: true, truncated: true })),
    }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(rows(element)).toHaveLength(1))
    rows(element)[0].click()

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-diff-binary"]')).toBeTruthy())
    expect(text(element.querySelector('[data-testid="git-changes-diff-binary"]'))).toContain('Binary file')
    expect(access.diff).toHaveBeenCalledWith(expect.objectContaining({ path: 'assets/logo.png', staged: false }))
  })

  it('offers a retry after a failed read and recovers on the second attempt', async () => {
    const status = vi.fn()
      .mockRejectedValueOnce(new Error('git.status failed: boom'))
      .mockResolvedValueOnce(statusPayload({ files: [{ path: 'src/App.vue', status: 'modified', staged: false }] }))
    const access: GitAccess = { status, diff: vi.fn(async () => diffPayload()) }
    const element = mountPanel(access)

    await vi.waitFor(() => expect(element.querySelector('[data-testid="git-changes-error"]')).toBeTruthy())
    expect(text(element.querySelector('[data-testid="git-changes-error"]'))).toContain('boom')

    element.querySelector<HTMLButtonElement>('.git-changes__retry')!.click()
    await vi.waitFor(() => expect(rows(element)).toHaveLength(1))
    expect(element.querySelector('[data-testid="git-changes-error"]')).toBeNull()
    expect(status).toHaveBeenCalledTimes(2)
  })

  it('does not read Git while the tab is hidden', async () => {
    const access: GitAccess = {
      status: vi.fn(async () => statusPayload()),
      diff: vi.fn(async () => diffPayload()),
    }
    mountPanel(access, { workspaceId: 'ws-1', visible: false })

    await nextTick()
    expect(access.status).not.toHaveBeenCalled()
  })
})

describe('createGitChangesWorkbenchDefinition', () => {
  const item: WorkbenchItem = {
    id: 'git-changes',
    kind: 'git-changes',
    title: 'Source control',
    scope: { type: 'session', id: 'session-a' },
    hostKind: 'dom',
    retention: 'keep-alive',
    payload: { workspaceId: 'ws-1' },
  }
  const state: WorkbenchPanelRenderState = {
    active: true,
    visible: false,
    hostAvailable: true,
    nativeSurface: false,
    runtimeState: {},
  }

  it('derives the header and the workspace from the item payload', () => {
    const definition = createGitChangesWorkbenchDefinition(
      { status: vi.fn(async () => statusPayload()), diff: vi.fn(async () => diffPayload()) },
      key => key,
    )

    expect(definition.kind).toBe('git-changes')
    expect(definition.supports?.(item)).toBe(true)
    expect(definition.getHeader?.(item, state)).toEqual({
      title: 'workbench.gitChanges.title',
      subtitle: 'workbench.gitChanges.subtitle',
      icon: 'fork',
    })
    // `visible` from the host wins; without it the panel follows activate+host.
    expect(definition.getProps?.(item, state)).toEqual({ workspaceId: 'ws-1', visible: false })
    expect(definition.getProps?.(item, { ...state, visible: undefined, active: true })).toEqual({
      workspaceId: 'ws-1',
      visible: true,
    })
    expect(definition.getProps?.({ ...item, payload: {} }, state)).toEqual({ workspaceId: null, visible: false })
  })

  it('declines the item when no Git owner was injected', () => {
    const definition = createGitChangesWorkbenchDefinition(null, key => key)
    expect(definition.supports?.(item)).toBe(false)
  })

  it('ignores a non-string payload workspace', () => {
    expect(gitChangesWorkspaceId({ ...item, payload: { workspaceId: 42 } })).toBeNull()
    expect(gitChangesWorkspaceId({ ...item, payload: { workspaceId: '' } })).toBeNull()
    expect(gitChangesWorkspaceId(item)).toBe('ws-1')
  })
})
