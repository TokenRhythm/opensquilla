import { describe, expect, it } from 'vitest'

import appSource from './App.vue?raw'

/**
 * The Workbench dock can be collapsed and, before this control existed, a
 * collapsed panel was only recoverable by opening a new item. These assertions
 * keep a visible toggle (and its shortcut) wired to the same store state the
 * host renders from.
 */
describe('App workbench dock toggle contract', () => {
  it('renders a toggle bound to the workbench host it controls', () => {
    const start = appSource.indexOf('data-testid="workbench-toggle"')
    expect(start).toBeGreaterThan(-1)
    const button = appSource.slice(
      appSource.lastIndexOf('<button', start),
      appSource.indexOf('</button>', start),
    )

    expect(button).toContain('aria-controls="workbench-panel"')
    expect(button).toContain(':aria-expanded="workbenchStore.expanded"')
    expect(button).toContain(':aria-keyshortcuts="workbenchToggleAriaShortcut"')
    expect(button).toContain('@click="toggleWorkbench()"')
    // The icon reflects the state instead of relying on the user's memory.
    expect(button).toContain("workbenchStore.expanded ? 'panel-right-close' : 'panel-right-open'")
  })

  it('keeps the toggle available while the dock exists, not only while a panel is open', () => {
    const computedStart = appSource.indexOf('const workbenchToggleVisible')
    const computedEnd = appSource.indexOf('const workbenchToggleTitle')
    expect(computedEnd).toBeGreaterThan(computedStart)
    const gating = appSource.slice(computedStart, computedEnd)

    expect(gating).toContain('appStore.features.artifactWorkbench === true')
    // Gating on open items is what made the control vanish after a reload.
    expect(gating).not.toContain('items.length')
  })

  it('opens the review surface when the dock is empty, and otherwise toggles it', () => {
    const predicateStart = appSource.indexOf('function openReviewInEmptyDock()')
    const toggleStart = appSource.indexOf('function toggleWorkbench()')
    expect(predicateStart).toBeGreaterThan(-1)
    expect(toggleStart).toBeGreaterThan(predicateStart)
    const predicate = appSource.slice(
      predicateStart,
      appSource.indexOf('\n}', predicateStart),
    )
    const toggle = appSource.slice(toggleStart, appSource.indexOf('\n}', toggleStart))

    // An empty dock opens the surface it exists for here rather than a blank
    // area, and an empty dock remains the fallback when there is no project.
    expect(predicate).toContain('workbenchStore.items.length > 0')
    expect(predicate).toContain('reviewableProject.value')
    expect(predicate).toContain('requestWorkspaceChangesOpen(project)')
    expect(toggle).toContain('openReviewInEmptyDock()')
    expect(toggle).toContain('workbenchStore.setExpanded(!workbenchStore.expanded)')
  })

  it('reviews a project that arrives after the dock was already opened', () => {
    // The common order is the opposite of the toggle's: the dock is opened
    // first, then the task is pointed at a repository. With the rule living
    // only inside the toggle, that order left an open dock claiming there was
    // nothing to review while the project sat selected in the composer.
    const start = appSource.indexOf('watch(reviewableProject,')
    expect(start).toBeGreaterThan(-1)
    const body = appSource.slice(start, appSource.indexOf('})', start))

    expect(body).toContain('workbenchStore.expanded')
    expect(body).toContain('openReviewInEmptyDock()')
  })

  it('reviews only the project the current task is on', () => {
    const start = appSource.indexOf('const reviewableProject = computed')
    const end = appSource.indexOf('function toggleWorkbench()', start)
    const body = appSource.slice(start, end)

    // The selected project, and nothing else: falling back to the single
    // registered project opened a review nobody had asked for.
    expect(body).toContain('activeProjectDraftId.value')
    expect(body).toContain('return null')
    expect(body).not.toContain('projects.length === 1')
    expect(body).not.toContain('projectWorkspaces.workspaces.value')
  })

  it('binds the toggle-workbench shortcut to the same function', () => {
    // Bound twice on purpose: once for the button's aria-keyshortcuts, once for
    // the keydown handler, so the last occurrence is the handler branch.
    const start = appSource.lastIndexOf("shortcutsStore.effectiveBinding('toggle-workbench')")
    expect(start).toBeGreaterThan(-1)
    const branch = appSource.slice(start, start + 400)

    expect(branch).toContain('bindingMatches(e, toggleWorkbenchBinding, isMac)')
    expect(branch).toContain('toggleWorkbench()')
  })
})
