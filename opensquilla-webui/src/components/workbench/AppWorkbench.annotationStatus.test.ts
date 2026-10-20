import { describe, expect, it } from 'vitest'

import appWorkbenchSource from './AppWorkbench.vue?raw'

describe('AppWorkbench annotation mode status', () => {
  it('omits reopening and refresh callbacks from the active preview file menu', () => {
    const menu = appWorkbenchSource.match(/<ResourceActionsMenu\b[^>]*\/>/)?.[0]

    expect(menu).toBeDefined()
    expect(menu).toContain(':previewable="false"')
    expect(menu).not.toContain('@open=')
  })

  it('keeps the original annotation target and includes its optional subpage when focusing', () => {
    const start = appWorkbenchSource.indexOf('async function onPromptAnnotationFocus(')
    const end = appWorkbenchSource.indexOf('\nasync function onPromptAnnotationReuse(', start)
    const source = appWorkbenchSource.slice(start, end)
    expect(source).toContain('targetRef: draft.targetRef')
    expect(source).toContain('...(draft.pagePath ? { pagePath: draft.pagePath } : {})')
    expect(source).not.toContain('getWorkbenchBrowserTarget')
  })
  it('renders live guidance only for the active annotation toolbar action', () => {
    expect(appWorkbenchSource).toContain(
      'v-if="isActiveAnnotationToolbarItem(toolbarItem)"',
    )
    expect(appWorkbenchSource).toContain(
      'data-testid="workbench-annotation-mode-status"',
    )
    expect(appWorkbenchSource).toContain('role="status"')
    expect(appWorkbenchSource).toContain('aria-live="polite"')
    expect(appWorkbenchSource).toContain(
      "t('workbench.annotationUnified.selectTarget')",
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

  it('shows the annotation action label with no beta badge and preserves its accessible state', () => {
    expect(appWorkbenchSource).toContain(
      "v-if=\"['toggle-annotation-mode', 'prepare-annotation', 'annotate'].includes(toolbarItem.id)\"",
    )
    expect(appWorkbenchSource).toContain('class="app-workbench__action-label"')
    expect(appWorkbenchSource).toMatch(
      /app-workbench__action-label[\s\S]*aria-hidden="true"[\s\S]*>\s*\{\{ toolbarItem.label \}\}\s*<\/span>/,
    )
    expect(appWorkbenchSource).not.toContain('app-workbench__action-beta')
    expect(appWorkbenchSource).not.toContain('β')
    expect(appWorkbenchSource).toContain(':aria-label="toolbarItem.label"')
    expect(appWorkbenchSource).toContain(':aria-pressed="toolbarItem.pressed"')
    expect(appWorkbenchSource).toContain(':disabled="toolbarItem.disabled"')
    expect(appWorkbenchSource).toMatch(
      /\.app-workbench__action--annotation\s*\{\s*width: auto;\s*flex: 0 0 auto;/,
    )
  })

  it('uses the workbench container width and preserves fixed-size toolbar actions', () => {
    expect(appWorkbenchSource).toContain('@container (max-width: 520px)')
    expect(appWorkbenchSource).toMatch(
      /@container \(max-width: 520px\)[\s\S]*\.app-workbench__annotation-mode-status\s*\{\s*display: none;/,
    )
    expect(appWorkbenchSource).toContain('flex: 0 0 30px')
    expect(appWorkbenchSource).not.toContain('@media (max-width: 600px)')
    expect(appWorkbenchSource).not.toContain('app-workbench__annotation-mode-status-short')
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

  it("routes document version invalidations through resource and preview refresh", () => {
    const start = appWorkbenchSource.indexOf('function onArtifactState(')
    const end = appWorkbenchSource.indexOf('\nfunction promptAnnotationItem(', start)
    const source = appWorkbenchSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('workbenchResources.load(activeSessionKey, true)')
    expect(source).toContain('refreshResourceCollectionItem(activeSessionKey)')
    expect(source).toContain('void refreshArtifactDocumentItem(item)')
    // Version actions share one content-free
    // invalidation path; filtering by action here would leave Preview stale.
    expect(source).not.toContain('event.action')
    expect(appWorkbenchSource).toContain(
      'artifactWorkbench.subscribeDocumentChanges(onArtifactState)',
    )
  })

  it("matches sent page annotations across provisional and canonical session keys", () => {
    const start = appWorkbenchSource.indexOf("async function onPageAnnotationsSent")
    const end = appWorkbenchSource.indexOf('\nasync function beforeCloseItem', start)
    const source = appWorkbenchSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain("pageAnnotationSendQueue.enqueue(detail)")
    expect(source).toContain("schedulePageAnnotationSendFlush()")
    expect(appWorkbenchSource).toContain(
      'const stopPromptAnnotationLifecycle = store.onLifecycle',
    )
    expect(appWorkbenchSource).toContain(
      "const artifactItems = store.items.filter(item => item.kind === 'artifact-preview')",
    )
    expect(appWorkbenchSource).toContain(
      'if (!runtimeManager.hasRuntime(item.id)) return false',
    )
    expect(appWorkbenchSource).toContain(
      "!Object.prototype.hasOwnProperty.call(state, 'annotationMode')",
    )
    expect(appWorkbenchSource).not.toContain(
      'for (let attempt = 0; attempt < 3; attempt += 1)',
    )
  })

  it('surfaces a localized readonly reason instead of a generic open failure', () => {
    const start = appWorkbenchSource.indexOf('async function openWorkbenchResource(')
    const end = appWorkbenchSource.indexOf('\nasync function importWorkbenchResourceForSession', start)
    const source = appWorkbenchSource.slice(start, end)

    expect(start).toBeGreaterThan(-1)
    expect(source).toContain('current?.reasonCode || workbenchResourceActionReasonCode(')
    expect(source).toContain("artifactProductClientError('RESOURCE_UNSUPPORTED'")
    expect(source).not.toContain("throw new Error(t('workbench.resources.actionFailed'))")
  })

  it('downloads non-previewable files directly while preserving HTML readonly reasons', () => {
    const start = appWorkbenchSource.indexOf('async function openWorkbenchResource(')
    const end = appWorkbenchSource.indexOf('\nasync function importWorkbenchResourceForSession', start)
    const source = appWorkbenchSource.slice(start, end)

    expect(source).toContain("previewKind !== 'html' && readonlyResource.capabilities.download")
    expect(source).toContain('await downloadWorkbenchResource(readonlyResource, item)')
    expect(source.indexOf("previewKind !== 'html'"))
      .toBeLessThan(source.indexOf("artifactProductClientError('RESOURCE_UNSUPPORTED'"))
  })

  it('opens current and silently materialized HTML documents on Preview in the owning workbench', () => {
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
      "openResourceArtifact(current.resource, artifact, sessionKey, undefined, 'preview', item)",
    )
    expect(openSource).not.toContain("undefined, 'source'")
    expect(importSource).toContain("}, artifact, sessionKey, undefined, 'preview', ownerItem)")
    expect(importSource).not.toContain("undefined, 'source'")
  })

  it('creates a fresh section request for every explicit artifact open path', () => {
    const helperStart = appWorkbenchSource.indexOf('function artifactPreviewItemForExplicitOpen(')
    const helperEnd = appWorkbenchSource.indexOf('\nfunction confirmWorkbenchPermission', helperStart)
    expect(appWorkbenchSource.slice(helperStart, helperEnd)).toContain('requestInitialSectionForWorkbenchItem(')
    // Scope is applied after the fresh section request is allocated. Verify
    // each entry path still uses that helper, without requiring nested calls
    // that would drop the child resource's parent-owned visible scope.
    const resourceStart = appWorkbenchSource.indexOf('function openResourceArtifact(')
    const resourceEnd = appWorkbenchSource.indexOf('\nasync function openWorkbenchResource(', resourceStart)
    const resourceOpen = appWorkbenchSource.slice(resourceStart, resourceEnd)
    expect(resourceOpen).toContain('const previewItem = artifactPreviewItemForExplicitOpen(')
    expect(resourceOpen).toContain('store.openItem(ownerItem ? { ...previewItem, scope: ownerItem.scope } : previewItem)')
    const navigationStart = appWorkbenchSource.indexOf('async function selectNavigationArtifact(')
    const navigationEnd = appWorkbenchSource.indexOf('\nfunction panelHeader(', navigationStart)
    const navigationOpen = appWorkbenchSource.slice(navigationStart, navigationEnd)
    expect(navigationOpen).toContain('const previewItem = artifactPreviewItemForExplicitOpen(')
    expect(navigationOpen).toContain('store.openItem({ ...previewItem, scope: item.scope })')
    expect(appWorkbenchSource).not.toContain('let artifactSectionRequestId')
  })

  it('contains expected resource refresh aborts at every fire-and-forget call site', () => {
    const loadCalls = appWorkbenchSource.match(
      /void (?:artifactWorkbench\.ready\(\)\.then\(\(\) => )?workbenchResources\.load\(/g,
    ) || []
    const guardedLoads = appWorkbenchSource.match(
      /void (?:artifactWorkbench\.ready\(\)\.then\(\(\) => )?workbenchResources\.load\([^;]+?\.catch\(\(\) => undefined\)/gs,
    ) || []
    expect(loadCalls).toHaveLength(3)
    expect(guardedLoads).toHaveLength(loadCalls.length)
  })

})
