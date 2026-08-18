import { describe, expect, it } from 'vitest'

import appWorkbenchSource from './AppWorkbench.vue?raw'

describe('AppWorkbench annotation mode status', () => {
  it('renders a visible, live status only for the active annotation toolbar action', () => {
    expect(appWorkbenchSource).toContain(
      'v-if="isActiveAnnotationToolbarItem(toolbarItem)"',
    )
    expect(appWorkbenchSource).toContain(
      'data-testid="workbench-annotation-mode-status"',
    )
    expect(appWorkbenchSource).toContain('role="status"')
    expect(appWorkbenchSource).toContain('aria-live="polite"')
    expect(appWorkbenchSource).toContain(
      "t('workbench.artifactAnnotation.selectElement')",
    )
    expect(appWorkbenchSource).toContain(
      "t('workbench.artifactAnnotation.selectElementShort')",
    )

    const predicateStart = appWorkbenchSource.indexOf(
      'function isActiveAnnotationToolbarItem',
    )
    const predicateEnd = appWorkbenchSource.indexOf('\n}', predicateStart)
    const predicate = appWorkbenchSource.slice(predicateStart, predicateEnd)
    expect(predicate).toContain("toolbarItem.kind === 'action'")
    expect(predicate).toContain("toolbarItem.id === 'toggle-annotation-mode'")
    expect(predicate).toContain('toolbarItem.pressed === true')
  })

  it('associates the active toggle with the visible guidance', () => {
    expect(appWorkbenchSource).toContain(':aria-describedby="isActiveAnnotationToolbarItem(toolbarItem)')
    expect(appWorkbenchSource).toContain("? 'workbench-annotation-mode-status'")
  })

  it('refreshes mounted document metadata after state events and reconnects', () => {
    expect(appWorkbenchSource).toContain(
      'async function refreshArtifactDocumentItem',
    )
    expect(appWorkbenchSource).toContain(
      'payload: { ...current.payload }',
    )
    expect(appWorkbenchSource).toContain(
      'void refreshArtifactDocumentItem(item)',
    )
    expect(appWorkbenchSource).toContain(
      'const previousRevisionId = artifactDocuments.snapshot',
    )
    expect(appWorkbenchSource).toContain(
      'runtimeManager.handleComponentEvent(updated',
    )
    expect(appWorkbenchSource).toContain("type: 'artifact-head-changed'")
    expect(appWorkbenchSource).toContain(
      'refreshOpenArtifactDocuments(sessionKey)',
    )
  })

  it('surfaces a localized readonly reason instead of a generic open failure', () => {
    const start = appWorkbenchSource.indexOf('async function openWorkbenchResource(')
    const end = appWorkbenchSource.indexOf('\nasync function importWorkbenchResourceForSession', start)
    const source = appWorkbenchSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('current?.reasonCode || workbenchResourceActionReasonCode(')
    expect(source).toContain('workbenchResourceUnavailableReasonKey(')
    expect(source).not.toContain("throw new Error(t('workbench.resources.actionFailed'))")
  })

  it('opens current and silently materialized HTML documents on Preview', () => {
    const openStart = appWorkbenchSource.indexOf('async function openWorkbenchResource(')
    const importStart = appWorkbenchSource.indexOf(
      '\nasync function importWorkbenchResourceForSession',
      openStart,
    )
    const publishStart = appWorkbenchSource.indexOf(
      '\nasync function publishWorkbenchResource',
      importStart,
    )
    const openSource = appWorkbenchSource.slice(openStart, importStart)
    const importSource = appWorkbenchSource.slice(importStart, publishStart)

    expect(openSource).toContain(
      'openResourceArtifact(current.resource, artifact, sessionKey)',
    )
    expect(openSource).not.toContain("undefined, 'source'")
    expect(importSource).toContain('}, artifact, sessionKey)')
    expect(importSource).not.toContain("undefined, 'source'")
  })

  it('contains expected resource refresh aborts at every fire-and-forget call site', () => {
    const guardedLoads = appWorkbenchSource.match(
      /void workbenchResources\.load\([^;]+?\.catch\(\(\) => undefined\)/gs,
    ) || []
    expect(guardedLoads).toHaveLength(4)
  })
})
