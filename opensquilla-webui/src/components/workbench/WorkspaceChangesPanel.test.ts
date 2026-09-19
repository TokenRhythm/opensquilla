// @vitest-environment happy-dom

import { createApp, h, nextTick, reactive } from 'vue'
import type { Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import en from '@/locales/en.json'
import {
  WORKSPACE_CHANGES_KEY,
  type WorkspaceChanges,
  type WorkspaceChangesReader,
  type WorkspaceFileDiff,
} from '@/modules/workspaceChanges'
import WorkspaceChangesPanel from './WorkspaceChangesPanel.vue'

const confirmMock = vi.hoisted(() => vi.fn(async () => true))
vi.mock('@/composables/useConfirm', () => ({
  useConfirm: () => ({ confirm: confirmMock }),
}))

function changes(overrides: Partial<WorkspaceChanges> = {}): WorkspaceChanges {
  return {
    available: true,
    availabilityReason: null,
    branch: 'main',
    detached: false,
    upstream: null,
    ahead: 0,
    behind: 0,
    totalCount: 1,
    truncated: false,
    addedLines: 1,
    removedLines: 1,
    entries: [
      {
        path: 'src/a.ts',
        previousPath: null,
        changeType: 'modified',
        staged: false,
        unstaged: true,
        addedLines: 1,
        removedLines: 1,
      },
    ],
    ...overrides,
  }
}

/** Entries built by a test need the same shape the Contract requires. */
function entry(
  overrides: Partial<WorkspaceChanges['entries'][number]> & { path: string },
): WorkspaceChanges['entries'][number] {
  return {
    previousPath: null,
    changeType: 'modified',
    staged: false,
    unstaged: true,
    addedLines: 1,
    removedLines: 0,
    ...overrides,
  }
}

function diff(overrides: Partial<WorkspaceFileDiff> = {}): WorkspaceFileDiff {
  return {
    path: 'src/a.ts',
    staged: false,
    text: '@@ -1 +1 @@\n-const a = 1\n+const a = 2\n',
    truncated: false,
    binary: false,
    ...overrides,
  }
}

function reader(overrides: Partial<WorkspaceChangesReader> = {}): WorkspaceChangesReader {
  return {
    readChanges: vi.fn(async () => changes()),
    readDiff: vi.fn(async () => diff()),
    stagePaths: vi.fn(async request => ({
      staged: request.staged,
      affectedPaths: [...request.paths],
    })),
    discardPaths: vi.fn(async request => [...request.paths]),
    draftCommitMessage: vi.fn(async () => ({ subject: 'the drafted subject', body: '' })),
    commitIndex: vi.fn(async request => ({
      sha: 'a'.repeat(40),
      subject: request.message.split('\n')[0],
    })),
    pushBranch: vi.fn(async () => ({ upstream: 'origin/main', output: 'up to date' })),
    undoCommit: vi.fn(async () => ({ sha: 'b'.repeat(40), subject: 'the last one' })),
    ...overrides,
  }
}

async function settle() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountPanel(
  port: WorkspaceChangesReader | null,
  props: Record<string, unknown> = { workspaceId: 'workspace-1', workspaceName: 'Project A' },
) {
  const element = document.createElement('div')
  document.body.append(element)
  const state = reactive({ ...props })
  const app = createApp({
    render: () => h(WorkspaceChangesPanel as Component, state),
  })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  if (port) app.provide(WORKSPACE_CHANGES_KEY, port)
  app.mount(element)
  return {
    element,
    unmount: () => {
      app.unmount()
      element.remove()
    },
  }
}

function clickEntry(element: HTMLElement, path: string) {
  const button = [...element.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
    .find(candidate => candidate.textContent?.includes(path))
  if (!button) throw new Error(`no entry button for ${path}`)
  button.click()
}

/** The index action belongs to a row, so a lookup has to be row-scoped. */
function rowAction(element: HTMLElement, path: string): HTMLButtonElement {
  const row = [...element.querySelectorAll<HTMLElement>('.wb-changes__row')]
    .find(candidate => candidate.textContent?.includes(path))
  if (!row) throw new Error(`no row for ${path}`)
  const action = row.querySelector<HTMLButtonElement>('[data-testid="changes-index-action"]')
  if (!action) throw new Error(`no index action in the row for ${path}`)
  return action
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('WorkspaceChangesPanel', () => {
  it('lists changed files and shows the selected diff', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', changeType: 'modified', staged: false, unstaged: true }),
          entry({ path: 'src/new.ts', changeType: 'untracked', staged: false, unstaged: true }),
        ],
        totalCount: 2,
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    expect(mounted.element.textContent).toContain('src/a.ts')
    expect(mounted.element.textContent).toContain('src/new.ts')
    expect(mounted.element.textContent).toContain('main')
    expect(mounted.element.textContent).toContain('Select a file to review its diff.')
    // The truncation notice is a status for a bounded list, not a permanent banner.
    expect(mounted.element.textContent).not.toContain('Showing')

    clickEntry(mounted.element, 'src/new.ts')
    await settle()

    expect(port.readDiff).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      path: 'src/new.ts',
      staged: false,
    })
    expect(mounted.element.textContent).toContain('const a = 2')
    mounted.unmount()
  })

  it('reads the staged half when a file is only staged', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/staged.ts', changeType: 'added', staged: true, unstaged: false }),
        ],
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    clickEntry(mounted.element, 'src/staged.ts')
    await settle()

    expect(port.readDiff).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      path: 'src/staged.ts',
      staged: true,
    })
    mounted.unmount()
  })

  it('explains an unavailable repository instead of showing an empty list', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        available: false,
        availabilityReason: 'not_repository',
        branch: null,
        totalCount: 0,
        entries: [],
      })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('Git status is unavailable')
    expect(mounted.element.textContent).toContain('This workspace is not a Git repository.')
    mounted.unmount()
  })

  it('reports an unreadable working tree and offers a retry', async () => {
    const readChanges = vi.fn()
      .mockRejectedValueOnce(new Error('gateway offline'))
      .mockResolvedValueOnce(changes())
    const mounted = mountPanel(reader({ readChanges }))
    await settle()

    expect(mounted.element.textContent).toContain('gateway offline')
    const retry = [...mounted.element.querySelectorAll<HTMLButtonElement>('button')]
      .find(button => button.textContent?.includes('Retry'))
    retry?.click()
    await settle()

    expect(readChanges).toHaveBeenCalledTimes(2)
    expect(mounted.element.textContent).toContain('src/a.ts')
    mounted.unmount()
  })

  it('renders an empty working tree as a status, not an error', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ totalCount: 0, entries: [] })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('No changes in this workspace.')
    expect(mounted.element.querySelector('[role="alert"]')).toBeNull()
    mounted.unmount()
  })

  it('renders numbered code rows and keeps patch plumbing out of the line flow', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({
        text: [
          'diff --git a/src/a.ts b/src/a.ts',
          'index 1111111..2222222 100644',
          '--- a/src/a.ts',
          '+++ b/src/a.ts',
          '@@ -10,2 +10,2 @@',
          ' const keep = 1',
          '-const a = 1',
          '+const a = 2',
          '',
        ].join('\n'),
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    const row = (kind: string) => [...mounted.element.querySelectorAll('.wb-changes__line')]
      .find(node => node.getAttribute('data-kind') === kind)
    const gutters = (kind: string) => [...(row(kind)?.querySelectorAll('.wb-changes__gutter') || [])]
      .map(node => node.textContent)

    // The hunk starts at line 10, then one context line advances both sides.
    expect(row('removed')?.textContent).toContain('const a = 1')
    expect(gutters('removed')).toEqual(['11', ''])
    expect(gutters('added')).toEqual(['', '11'])
    // The +/- lives in the patch text itself, the same rendering the chat uses
    // for tool-result diffs. (The highlighter needs a real DOM sanitizer, so the
    // coloured span is verified in the browser, not here.)
    expect(row('added')?.textContent).toContain('+const a = 2')
    expect(row('removed')?.textContent).toContain('-const a = 1')
    // `diff --git` / `index` / `---` / `+++` duplicate the panel header, and
    // rendering them here is what left the numbered columns misaligned.
    expect(mounted.element.textContent).not.toContain('diff --git')
    expect(mounted.element.textContent).not.toContain('index 1111111')
    mounted.unmount()
  })

  it('still shows a rename that carries no hunks', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({
        text: [
          'diff --git a/src/a.ts b/src/b.ts',
          'similarity index 100%',
          'rename from src/a.ts',
          'rename to src/b.ts',
          '',
        ].join('\n'),
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    const notices = [...mounted.element.querySelectorAll('.wb-changes__line[data-kind="notice"]')]
      .map(node => node.textContent?.trim())
    expect(notices).toEqual(['rename from src/a.ts', 'rename to src/b.ts'])
    // The diff block must still render, or a pure rename would look empty.
    expect(mounted.element.querySelector('.wb-changes__code')).not.toBeNull()
    mounted.unmount()
  })

  it('wraps long patch lines by default and lets the reader turn that off', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({
        text: `@@ -1 +1 @@\n+${'x'.repeat(400)}\n`,
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    const root = mounted.element.querySelector('.wb-changes')
    const toggle = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-wrap-lines"]',
    )
    expect(toggle?.getAttribute('aria-pressed')).toBe('true')
    expect(root?.classList.contains('is-wrapped')).toBe(true)

    toggle?.click()
    await nextTick()

    expect(toggle?.getAttribute('aria-pressed')).toBe('false')
    expect(mounted.element.querySelector('.wb-changes')?.classList.contains('is-wrapped'))
      .toBe(false)
    mounted.unmount()
  })

  it('keeps numbered rows aligned and lets hunk rows span the width', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', changeType: 'modified', staged: false, unstaged: true }),
          entry({ path: 'src/b.ts', changeType: 'modified', staged: false, unstaged: true }),
        ],
        totalCount: 2,
      })),
      readDiff: vi.fn(async () => diff({
        text: '@@ -1,1 +1,1 @@\n-const a = 1\n+const a = 2\n',
      })),
    }))
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    // The number columns carry no header row: two columns of numbers are the
    // convention, and a label above them is one more thing to read.
    expect(mounted.element.querySelector('.wb-changes__line--head')).toBeNull()

    // A hunk band spans the row instead of leaving an empty gutter column.
    const hunk = [...mounted.element.querySelectorAll('.wb-changes__line')]
      .find(node => node.getAttribute('data-kind') === 'hunk')
    expect(hunk?.querySelectorAll('.wb-changes__gutter').length).toBe(0)
    expect(hunk?.textContent).toContain('@@ -1,1 +1,1 @@')
    mounted.unmount()
  })

  it('shows per-file line stats and keeps unknown counts unknown', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        addedLines: 7,
        removedLines: 2,
        totalCount: 2,
        entries: [
          entry({ path: 'src/a.ts', addedLines: 5, removedLines: 2 }),
          // A binary file has no countable lines; it must not claim 0/0.
          entry({ path: 'src/blob.bin', addedLines: null, removedLines: null }),
        ],
      })),
    }))
    await settle()

    const rowFor = (path: string) => [...mounted.element.querySelectorAll('.wb-changes__entry')]
      .find(node => node.textContent?.includes(path))
    expect(rowFor('src/a.ts')?.querySelector('.wb-changes__stats')?.textContent).toBe('+5-2')
    expect(rowFor('src/blob.bin')?.querySelector('.wb-changes__stats')).toBeNull()
    expect(mounted.element.querySelector('.wb-changes__bar')?.textContent).toContain('+7-2')
    mounted.unmount()
  })

  it('resizes the list/diff split and returns to the automatic one', async () => {
    const mounted = mountPanel(reader())
    await settle()

    const splitter = mounted.element.querySelector<HTMLElement>('[data-testid="changes-splitter"]')
    const body = mounted.element.querySelector<HTMLElement>('.wb-changes__body')
    expect(splitter?.getAttribute('role')).toBe('separator')
    expect(splitter?.getAttribute('aria-orientation')).toBe('horizontal')
    expect(body?.style.getPropertyValue('--wb-changes-list-height')).toBe('auto')

    splitter?.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(Number.parseInt(
      body?.style.getPropertyValue('--wb-changes-list-height') ?? '',
      10,
    )).toBeGreaterThan(0)

    // Double-click restores the split that fits the content.
    splitter?.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
    await nextTick()
    expect(body?.style.getPropertyValue('--wb-changes-list-height')).toBe('auto')
    mounted.unmount()
  })

  it('moves between files with the arrow keys', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', changeType: 'modified', staged: false, unstaged: true }),
          entry({ path: 'src/b.ts', changeType: 'modified', staged: false, unstaged: true }),
        ],
        totalCount: 2,
      })),
    }))
    await settle()

    const buttons = [...mounted.element.querySelectorAll<HTMLButtonElement>('.wb-changes__entry')]
    buttons[0].focus()
    buttons[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(buttons[1])

    buttons[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(buttons[0])
    mounted.unmount()
  })

  it('reports a binary file without rendering a text diff', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({ text: 'Binary files a and b differ', binary: true })),
    }))
    await settle()

    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    expect(mounted.element.textContent).toContain('This file is binary; no text diff is available.')
    expect(mounted.element.querySelector('.wb-changes__code')).toBeNull()
    mounted.unmount()
  })

  it('notes a truncated change list', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ totalCount: 12, truncated: true })),
    }))
    await settle()

    expect(mounted.element.textContent).toContain('Showing 1 of 12 changed files.')
    mounted.unmount()
  })

  it('notes truncation without hiding the patch', async () => {
    const mounted = mountPanel(reader({
      readDiff: vi.fn(async () => diff({ truncated: true })),
    }))
    await settle()

    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    expect(mounted.element.textContent)
      .toContain('This diff was truncated to keep the panel responsive.')
    expect(mounted.element.querySelector('.wb-changes__code')).not.toBeNull()
    mounted.unmount()
  })

  it('offers one icon action per row, labelled but not spelled out', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', staged: false, unstaged: true }),
          entry({ path: 'src/b.ts', changeType: 'added', staged: true, unstaged: false }),
        ],
      })),
    }))
    await settle()

    const unstagedRow = rowAction(mounted.element, 'src/a.ts')
    const stagedRow = rowAction(mounted.element, 'src/b.ts')

    // The action is the inverse of the group the row sits in.
    expect(unstagedRow.dataset.indexAction).toBe('stage')
    expect(stagedRow.dataset.indexAction).toBe('unstage')
    // Icon only, so the row stays one line: the name lives in the accessible
    // label rather than in the row's text.
    expect(unstagedRow.textContent?.trim()).toBe('')
    expect(unstagedRow.getAttribute('aria-label')).toBe('Stage')
    expect(stagedRow.getAttribute('aria-label')).toBe('Unstage')
    expect(unstagedRow.querySelector('svg')).not.toBeNull()
    mounted.unmount()
  })

  it('gives the row actions their own column beside the counts', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', addedLines: 5, removedLines: 2 })],
      })),
    }))
    await settle()

    const row = mounted.element.querySelector('.wb-changes__row')
    // Counts and state chip sit in one cluster, and the actions have a column of
    // their own beside it: covering the counts and swapping them both moved
    // something the reader was looking at.
    const status = row?.querySelector('.wb-changes__row-status')
    const actions = row?.querySelector('.wb-changes__row-actions')
    expect(status?.querySelector('.wb-changes__stats')).not.toBeNull()
    expect(actions).not.toBeNull()
    expect(status?.contains(actions ?? null)).toBe(false)
    expect(status?.querySelector('[data-testid="changes-index-action"]')).toBeNull()
    mounted.unmount()
  })

  it('uses the app button vocabulary for toolbar and in-list actions', async () => {
    const mounted = mountPanel(reader())
    await settle()

    // Toolbar controls are the shared .btn--icon; actions inside a list row or
    // section header are the shared in-list geometry. A third hand-rolled
    // button is what drifted from the rest of the app.
    const toolbar = [
      mounted.element.querySelector('[data-testid="changes-wrap-lines"]'),
      mounted.element.querySelector('[data-testid="changes-refresh"]'),
      mounted.element.querySelector('[data-testid="changes-undo-commit"]'),
      mounted.element.querySelector('[data-testid="changes-push"]'),
    ]
    for (const control of toolbar) {
      expect(control?.classList.contains('btn')).toBe(true)
      expect(control?.classList.contains('btn--icon')).toBe(true)
      expect(control?.classList.contains('wb-changes__list-action')).toBe(false)
      expect(control?.querySelector('svg')?.getAttribute('width')).toBe('12')
    }

    // Not a `.btn`: the app's in-list action (base.css `.sidebar-project-action`)
    // is a standalone 20px square, and inheriting `.btn`'s padding and height is
    // what stretched it into a thin rectangle.
    const inList = [
      mounted.element.querySelector('[data-testid="changes-group-index-action"]'),
      mounted.element.querySelector('[data-testid="changes-index-action"]'),
    ]
    for (const control of inList) {
      expect(control?.classList.contains('btn')).toBe(false)
      expect(control?.classList.contains('wb-changes__list-action')).toBe(true)
    }
    mounted.unmount()
  })

  it('stages the whole group from its header', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', staged: false, unstaged: true }),
          entry({ path: 'src/b.ts', staged: false, unstaged: true }),
        ],
        totalCount: 2,
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    const groupAction = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-group-index-action"]',
    )
    expect(groupAction?.dataset.indexAction).toBe('stage')
    expect(groupAction?.getAttribute('aria-label')).toBe('Stage all')

    groupAction?.click()
    await settle()

    // One request for the set, not one per row.
    expect(port.stagePaths).toHaveBeenCalledTimes(1)
    expect(port.stagePaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts', 'src/b.ts'],
      staged: true,
    })
    expect(port.readChanges).toHaveBeenCalledTimes(2)
    mounted.unmount()
  })

  it('unstages the whole staged group from its header', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false }),
          entry({ path: 'src/b.ts', changeType: 'added', staged: true, unstaged: false }),
        ],
        totalCount: 2,
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    const groupAction = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-group-index-action"]',
    )
    expect(groupAction?.dataset.indexAction).toBe('unstage')
    groupAction?.click()
    await settle()

    expect(port.stagePaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts', 'src/b.ts'],
      staged: false,
    })
    mounted.unmount()
  })

  it('stages a row without moving the selection, then re-reads the list', async () => {
    const port = reader({
      readChanges: vi.fn()
        // The write is acknowledged with an index-only result, so the panel must
        // re-read the list rather than trust the write response.
        .mockResolvedValueOnce(changes({
          entries: [entry({ path: 'src/a.ts', staged: false, unstaged: true })],
        }))
        .mockResolvedValueOnce(changes({
          entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
        })),
    })
    const mounted = mountPanel(port)
    await settle()

    rowAction(mounted.element, 'src/a.ts').click()
    await settle()

    expect(port.stagePaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts'],
      staged: true,
    })
    expect(port.readChanges).toHaveBeenCalledTimes(2)
    // Acting on a row must not open its diff: the row moved groups, so the
    // action flipped to the inverse, and the reader stayed where it was.
    expect(rowAction(mounted.element, 'src/a.ts').dataset.indexAction).toBe('unstage')
    expect(port.readDiff).not.toHaveBeenCalled()
    mounted.unmount()
  })

  it('keeps the diff open on the same file when its row is staged', async () => {
    const port = reader({
      readChanges: vi.fn()
        .mockResolvedValueOnce(changes({
          entries: [entry({ path: 'src/a.ts', staged: false, unstaged: true })],
        }))
        .mockResolvedValueOnce(changes({
          entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
        })),
    })
    const mounted = mountPanel(port)
    await settle()
    clickEntry(mounted.element, 'src/a.ts')
    await settle()

    rowAction(mounted.element, 'src/a.ts').click()
    await settle()

    // The selection key (path + half) is expected to move with the row, so the
    // panel re-selects by path and the diff follows the half that is now shown.
    expect(mounted.element.querySelector('.wb-changes__diff-head')?.textContent)
      .toContain('src/a.ts')
    expect(port.readDiff).toHaveBeenLastCalledWith({
      workspaceId: 'workspace-1',
      path: 'src/a.ts',
      staged: true,
    })
    mounted.unmount()
  })

  it('unstages a staged-only row', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    rowAction(mounted.element, 'src/a.ts').click()
    await settle()

    expect(port.stagePaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts'],
      staged: false,
    })
    mounted.unmount()
  })

  it('reports why the index did not move instead of failing silently', async () => {
    const port = reader({
      stagePaths: vi.fn(async () => {
        throw new Error("Git rejected the operation: pathspec 'src/a.ts' did not match")
      }),
    })
    const mounted = mountPanel(port)
    await settle()

    rowAction(mounted.element, 'src/a.ts').click()
    await settle()

    const alert = mounted.element.querySelector('[data-testid="changes-index-error"]')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.textContent).toContain('did not match')
    // A failed write must not pretend the list refreshed.
    expect(port.readChanges).toHaveBeenCalledTimes(1)
    mounted.unmount()
  })

  it('names the file in the diff header, with no separate pager row', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'modified' })],
      })),
    }))
    await settle()

    // The list is the navigation: it shows every changed file and the arrow
    // keys walk it, so the header only has to say which file is open.
    const head = mounted.element.querySelector('.wb-changes__diff-head')
    expect(head?.textContent).toContain('Select a file to review its diff.')
    expect(mounted.element.querySelector('.wb-changes__nav')).toBeNull()
    mounted.unmount()
  })

  it('renders no diff header when the working tree is clean', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ totalCount: 0, entries: [] })),
    }))
    await settle()

    expect(mounted.element.querySelector('.wb-changes__diff-head')).toBeNull()
    expect(mounted.element.textContent).toContain('No changes in this workspace.')
    mounted.unmount()
  })

  it('applies one wrap preference to the file rows, the header, and the patch', async () => {
    const longPath = `src/${'nested-directory/'.repeat(6)}file.ts`
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: longPath, changeType: 'modified' })],
      })),
      readDiff: vi.fn(async () => diff({ path: longPath })),
    }))
    await settle()
    clickEntry(mounted.element, longPath)
    await settle()

    const root = () => mounted.element.querySelector('.wb-changes')
    // The header and the rows used to truncate a path the body wraps. One
    // preference now governs all three, so the full path stays readable
    // whichever column shows it.
    expect(root()?.classList.contains('is-wrapped')).toBe(true)
    expect(mounted.element.querySelector('.wb-changes__diff-head')?.textContent)
      .toContain(longPath)
    expect(mounted.element.querySelector('.wb-changes__path')?.textContent)
      .toContain(longPath)

    const toggle = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-wrap-lines"]',
    )
    toggle?.click()
    await nextTick()

    expect(root()?.classList.contains('is-wrapped')).toBe(false)
    mounted.unmount()
  })

  it('folds a section away so a long list cannot hide the others', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', staged: false, unstaged: true }),
          entry({ path: 'src/new.ts', changeType: 'untracked' }),
        ],
        totalCount: 2,
      })),
    }))
    await settle()

    const body = (key: string) => mounted.element.querySelector<HTMLElement>(
      `#wb-changes-group-${key}`,
    )
    const toggle = (key: string) => mounted.element.querySelector<HTMLButtonElement>(
      `[data-testid="changes-group-toggle"][data-group="${key}"]`,
    )

    expect(toggle('unstaged')?.getAttribute('aria-expanded')).toBe('true')
    expect(body('unstaged')?.style.display).not.toBe('none')

    toggle('unstaged')?.click()
    await nextTick()

    expect(toggle('unstaged')?.getAttribute('aria-expanded')).toBe('false')
    expect(body('unstaged')?.style.display).toBe('none')
    // The other section is untouched, which is the point of folding one away.
    expect(body('untracked')?.style.display).not.toBe('none')
    mounted.unmount()
  })

  it('commits the staged work with the typed message', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    const commitButton = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-commit"]',
    )
    const message = mounted.element.querySelector<HTMLInputElement>(
      '[data-testid="changes-commit-message"]',
    )
    // Nothing is written until there is a message and something staged.
    expect(commitButton?.disabled).toBe(true)

    if (message) {
      message.value = '  tighten the thing  '
      message.dispatchEvent(new Event('input'))
    }
    await nextTick()
    expect(commitButton?.disabled).toBe(false)

    commitButton?.click()
    await settle()

    expect(port.commitIndex).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      message: 'tighten the thing',
    })
    expect(message?.value).toBe('')
    expect(mounted.element.querySelector('[data-testid="changes-notice"]')?.textContent)
      .toContain('Committed aaaaaaa tighten the thing')
    mounted.unmount()
  })

  it('fills the commit field with the drafted message', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
      })),
      draftCommitMessage: vi.fn(async () => ({
        subject: 'Add the retry budget',
        body: 'Cap the attempts so a stalled host fails fast.',
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    const draft = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-draft-message"]',
    )
    expect(draft?.disabled).toBe(false)
    draft?.click()
    await settle()

    expect(port.draftCommitMessage).toHaveBeenCalledWith({ workspaceId: 'workspace-1' })
    const message = mounted.element.querySelector<HTMLTextAreaElement>(
      '[data-testid="changes-commit-message"]',
    )
    // A draft is a proposal the operator can edit, and it is committable as-is
    // because the field keeps the subject and the body apart.
    expect(message?.value).toBe(
      'Add the retry budget\n\nCap the attempts so a stalled host fails fast.',
    )
    expect(mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-commit"]',
    )?.disabled).toBe(false)
    mounted.unmount()
  })

  it('keeps the draft action unavailable while nothing is staged', async () => {
    const mounted = mountPanel(reader())
    await settle()

    const draft = mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-draft-message"]',
    )
    expect(draft?.disabled).toBe(true)
    draft?.click()
    await settle()

    expect(mounted.element.querySelector('[data-testid="changes-index-error"]')).toBeNull()
    mounted.unmount()
  })

  it('reports a failed draft in the existing error slot', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
      })),
      draftCommitMessage: vi.fn(async () => {
        throw new Error('No model and credentials are available for commit message generation.')
      }),
    })
    const mounted = mountPanel(port)
    await settle()

    mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-draft-message"]',
    )?.click()
    await settle()

    const alert = mounted.element.querySelector('[data-testid="changes-index-error"]')
    expect(alert?.getAttribute('role')).toBe('alert')
    expect(alert?.textContent).toContain('No model and credentials')
    // A failed draft leaves the field alone rather than filling it with a gap.
    expect(mounted.element.querySelector<HTMLTextAreaElement>(
      '[data-testid="changes-commit-message"]',
    )?.value).toBe('')
    mounted.unmount()
  })

  it('drops a draft whose index moved while the model was writing', async () => {
    // Drafting writes nothing, so the commit action stays available while the
    // call is in flight. The answer can therefore arrive for an index a commit
    // has already replaced, and filling the field with it would offer the
    // operator a message about work that is already committed.
    let releaseDraft: (draft: { subject: string; body: string }) => void = () => {}
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', changeType: 'added', staged: true, unstaged: false })],
      })),
      draftCommitMessage: vi.fn(() => new Promise<{ subject: string; body: string }>(
        resolve => { releaseDraft = resolve },
      )),
    })
    const mounted = mountPanel(port)
    await settle()

    const message = mounted.element.querySelector<HTMLTextAreaElement>(
      '[data-testid="changes-commit-message"]',
    )
    if (message) {
      message.value = 'the message I typed'
      message.dispatchEvent(new Event('input'))
    }
    await nextTick()

    mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-draft-message"]',
    )?.click()
    await settle()

    mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-commit"]',
    )?.click()
    await settle()
    expect(port.commitIndex).toHaveBeenCalled()

    releaseDraft({ subject: 'a message about the previous index', body: '' })
    await settle()

    expect(message?.value).toBe('')
    mounted.unmount()
  })

  it('keeps commit unavailable while nothing is staged', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/a.ts', staged: false, unstaged: true })],
      })),
    }))
    await settle()

    const message = mounted.element.querySelector<HTMLInputElement>(
      '[data-testid="changes-commit-message"]',
    )
    expect(message?.disabled).toBe(true)
    if (message) {
      message.value = 'message with nothing staged'
      message.dispatchEvent(new Event('input'))
    }
    await nextTick()
    expect(mounted.element.querySelector('[data-testid="changes-commit"]')?.hasAttribute('disabled'))
      .toBe(true)
    mounted.unmount()
  })

  it('pushes only when the branch has an upstream', async () => {
    const withoutUpstream = mountPanel(reader())
    await settle()
    expect(withoutUpstream.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-push"]',
    )?.disabled).toBe(true)
    withoutUpstream.unmount()

    const port = reader({
      readChanges: vi.fn(async () => changes({ upstream: 'origin/main' })),
    })
    const mounted = mountPanel(port)
    await settle()

    const push = mounted.element.querySelector<HTMLButtonElement>('[data-testid="changes-push"]')
    expect(push?.disabled).toBe(false)
    push?.click()
    await settle()

    expect(port.pushBranch).toHaveBeenCalledWith({ workspaceId: 'workspace-1' })
    expect(mounted.element.querySelector('[data-testid="changes-notice"]')?.textContent)
      .toContain('Pushed to origin/main')
    mounted.unmount()
  })

  it('asks before discarding, and does nothing when the answer is no', async () => {
    const port = reader()
    const mounted = mountPanel(port)
    await settle()

    confirmMock.mockResolvedValueOnce(false)
    mounted.element.querySelector<HTMLButtonElement>('[data-testid="changes-discard-action"]')?.click()
    await settle()

    expect(confirmMock).toHaveBeenCalled()
    expect(port.discardPaths).not.toHaveBeenCalled()
    mounted.unmount()

    const confirming = reader()
    const second = mountPanel(confirming)
    await settle()
    second.element.querySelector<HTMLButtonElement>('[data-testid="changes-discard-action"]')?.click()
    await settle()

    expect(confirming.discardPaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts'],
    })
    expect(second.element.querySelector('[data-testid="changes-notice"]')?.textContent)
      .toContain('Discarded changes to 1 file(s)')
    second.unmount()
  })

  it('never offers to discard a file Git does not track', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', staged: false, unstaged: true }),
          entry({ path: 'src/new.ts', changeType: 'untracked' }),
        ],
        totalCount: 2,
      })),
    }))
    await settle()

    const rowFor = (path: string) => [...mounted.element.querySelectorAll('.wb-changes__row')]
      .find(row => row.textContent?.includes(path))
    expect(rowFor('src/a.ts')?.querySelector('[data-testid="changes-discard-action"]'))
      .not.toBeNull()
    expect(rowFor('src/new.ts')?.querySelector('[data-testid="changes-discard-action"]'))
      .toBeNull()
    mounted.unmount()
  })

  it('offers undo only while the tip is not published', async () => {
    // Never pushed: the tip is local, so undoing it is safe.
    const local = mountPanel(reader())
    await settle()
    expect(local.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-undo-commit"]',
    )?.disabled).toBe(false)
    local.unmount()

    // Ahead of the upstream: the tip is not published yet either.
    const ahead = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ upstream: 'origin/main', ahead: 2 })),
    }))
    await settle()
    expect(ahead.element.querySelector<HTMLButtonElement>('[data-testid="changes-undo-commit"]')
      ?.disabled).toBe(false)
    ahead.unmount()

    // In sync with the upstream: undoing would rewrite published history.
    const published = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ upstream: 'origin/main', ahead: 0 })),
    }))
    await settle()
    const button = published.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-undo-commit"]',
    )
    expect(button?.disabled).toBe(true)
    expect(button?.getAttribute('title')).toContain('origin/main')
    published.unmount()
  })

  it('undos the tip commit and says which one moved', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({ ahead: 1, upstream: 'origin/main' })),
    })
    const mounted = mountPanel(port)
    await settle()

    mounted.element.querySelector<HTMLButtonElement>('[data-testid="changes-undo-commit"]')?.click()
    await settle()

    expect(port.undoCommit).toHaveBeenCalledWith({ workspaceId: 'workspace-1' })
    expect(mounted.element.querySelector('[data-testid="changes-notice"]')?.textContent)
      .toContain('Undid bbbbbbb the last one')
    mounted.unmount()
  })

  it('keeps push next to the branch rather than in the commit row', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({ upstream: 'origin/main' })),
    }))
    await settle()

    // The branch-level action belongs to the branch row, with the counts it
    // acts on; the commit row is only about the message.
    expect(mounted.element.querySelector('.wb-changes__bar [data-testid="changes-push"]'))
      .not.toBeNull()
    expect(mounted.element.querySelector('.wb-changes__commit [data-testid="changes-push"]'))
      .toBeNull()
    mounted.unmount()
  })

  it('asks before discarding a whole section', async () => {
    const port = reader({
      readChanges: vi.fn(async () => changes({
        entries: [
          entry({ path: 'src/a.ts', staged: false, unstaged: true }),
          entry({ path: 'src/b.ts', staged: false, unstaged: true }),
          entry({ path: 'src/new.ts', changeType: 'untracked' }),
        ],
        totalCount: 3,
      })),
    })
    const mounted = mountPanel(port)
    await settle()

    mounted.element.querySelector<HTMLButtonElement>(
      '[data-testid="changes-group-discard-action"]',
    )?.click()
    await settle()

    expect(confirmMock).toHaveBeenCalled()
    // Untracked rows are excluded: discarding one would delete the file.
    expect(port.discardPaths).toHaveBeenCalledWith({
      workspaceId: 'workspace-1',
      paths: ['src/a.ts', 'src/b.ts'],
    })
    mounted.unmount()
  })

  it('offers no section discard where every row is untracked', async () => {
    const mounted = mountPanel(reader({
      readChanges: vi.fn(async () => changes({
        entries: [entry({ path: 'src/new.ts', changeType: 'untracked' })],
      })),
    }))
    await settle()

    expect(mounted.element.querySelector('[data-testid="changes-group-discard-action"]'))
      .toBeNull()
    mounted.unmount()
  })

  it('stays honest when no reader is provided', async () => {
    const mounted = mountPanel(null)
    await settle()

    expect(mounted.element.textContent).toContain('Workspace changes are unavailable.')
    mounted.unmount()
  })
})
