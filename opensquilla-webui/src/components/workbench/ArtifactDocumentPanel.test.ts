// @vitest-environment happy-dom

import { createApp, h, nextTick, ref } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createPinia } from 'pinia'

import en from '@/locales/en.json'
import type { ArtifactDocumentWorkspaceSnapshot } from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import { createLegacyArtifactWorkspace } from '@/workbench/artifactDocumentProvider'
import {
  artifactPayloadFromWorkbenchResource,
  resourceFromPreparedPreview,
} from '@/workbench/workbenchResourceItems'
import { normalizeWorkbenchResource } from '@/workbench/workbenchResourceProvider'
import ArtifactDocumentPanel from './ArtifactDocumentPanel.vue'
import artifactDocumentPanelSource from './ArtifactDocumentPanel.vue?raw'

const officeArtifact: ArtifactPayload = {
  id: 'artifact-office',
  name: 'quarterly-plan.pptx',
  mime: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  download_url: '/api/v1/artifacts/artifact-office',
}

function snapshot(artifact = officeArtifact): ArtifactDocumentWorkspaceSnapshot {
  return {
    key: 'fixture',
    loading: false,
    loaded: true,
    stale: false,
    error: null,
    workspace: createLegacyArtifactWorkspace(artifact, 'session-a'),
  }
}

function mountPanel(props: Record<string, unknown>) {
  const element = document.createElement('div')
  document.body.append(element)
  const app = createApp(ArtifactDocumentPanel, props)
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en },
  }))
  app.use(createPinia())
  app.mount(element)
  return {
    element,
    unmount() {
      app.unmount()
      element.remove()
    },
  }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('ArtifactDocumentPanel', () => {
  it('routes a successful source save through the canonical head-change event', () => {
    expect(artifactDocumentPanelSource).toContain('@source-saved="onSourceSaved"')
    expect(artifactDocumentPanelSource).toContain("type: 'artifact-head-changed'")
    expect(artifactDocumentPanelSource).toContain('payload: { revisionId }')
  })

  it('keeps immutable artifacts read-only without exposing an editable-copy action', async () => {
    const onWorkbenchEvent = vi.fn()
    const mounted = mountPanel({
      artifact: {
        id: 'deliverable-html',
        name: 'published.html',
        mime: 'text/html',
        download_url: '/api/v1/artifacts/deliverable-html',
      },
      documentFeatures: false,
      onWorkbenchEvent,
      suspended: true,
    })
    await nextTick()

    expect([...mounted.element.querySelectorAll('[role="tab"]')]
      .map(tab => tab.textContent?.trim())).toEqual(['Preview'])
    expect(mounted.element.querySelector(
      '[data-artifact-action="create-editable-copy"]',
    )).toBeNull()
    expect(onWorkbenchEvent).not.toHaveBeenCalledWith(expect.objectContaining({
      type: 'artifact-create-editable-copy',
    }))
    mounted.unmount()
  })

  it('opens a mutable document on Source when requested by resource navigation', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      documentId: 'document-html',
      revisionId: 'revision-head',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const mounted = mountPanel({
      artifact: htmlArtifact,
      documentSnapshot: {
        key: 'fixture-source',
        loading: false,
        loaded: true,
        stale: false,
        error: null,
        workspace: {
          ...legacy,
          source: 'document-api',
          document: {
            ...legacy.document,
            documentId: 'document-html',
            headRevisionId: 'revision-head',
            capabilities: {
              ...legacy.document.capabilities,
              edit: true,
              source: true,
            },
          },
        },
      } satisfies ArtifactDocumentWorkspaceSnapshot,
      initialSection: 'source',
      sessionKey: 'session-a',
      suspended: true,
    })
    await nextTick()

    expect(mounted.element.querySelector('[role="tab"][aria-selected="true"]')?.textContent)
      .toContain('Source')
    mounted.unmount()
  })

  it('reapplies Preview when an already-open document is opened again', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      documentId: 'document-html',
      revisionId: 'revision-head',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const initialSectionRequestId = ref(1)
    const element = document.createElement('div')
    document.body.append(element)
    const app = createApp({
      setup() {
        return () => h(ArtifactDocumentPanel, {
          artifact: htmlArtifact,
          documentSnapshot: {
            key: 'fixture-reopen',
            loading: false,
            loaded: true,
            stale: false,
            error: null,
            workspace: {
              ...legacy,
              source: 'document-api',
              document: {
                ...legacy.document,
                documentId: 'document-html',
                headRevisionId: 'revision-head',
                capabilities: {
                  ...legacy.document.capabilities,
                  edit: true,
                  source: true,
                },
              },
            },
          } satisfies ArtifactDocumentWorkspaceSnapshot,
          initialSection: 'preview',
          initialSectionRequestId: initialSectionRequestId.value,
          sessionKey: 'session-a',
          suspended: true,
        })
      },
    })
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
    app.use(createPinia())
    app.mount(element)
    await nextTick()

    const sourceTab = [...element.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
      .find(tab => tab.textContent?.includes('Source'))
    sourceTab?.click()
    await nextTick()
    expect(element.querySelector('[role="tab"][aria-selected="true"]')?.textContent)
      .toContain('Source')

    initialSectionRequestId.value = 2
    await nextTick()
    expect(element.querySelector('[role="tab"][aria-selected="true"]')?.textContent)
      .toContain('Preview')

    app.unmount()
    element.remove()
  })

  it('keeps publishing out of the V1 document surface', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      documentId: 'document-html',
      revisionId: 'revision-head',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const documentSnapshot: ArtifactDocumentWorkspaceSnapshot = {
      key: 'fixture-publishable',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: {
        ...legacy,
        source: 'document-api',
        document: {
          ...legacy.document,
          documentId: 'document-html',
          headRevisionId: 'revision-head',
        },
      },
    }
    const onWorkbenchEvent = vi.fn()
    const mounted = mountPanel({
      artifact: htmlArtifact,
      documentSnapshot,
      sessionKey: 'session-a',
      onWorkbenchEvent,
    })
    await nextTick()

    expect(mounted.element.querySelector('[data-artifact-action="publish-head"]')).toBeNull()
    expect(mounted.element.textContent).not.toContain('Publish current version')
    expect(onWorkbenchEvent).not.toHaveBeenCalledWith(expect.objectContaining({
      type: 'artifact-document-publish',
    }))
    mounted.unmount()
  })

  it('explains the desktop-only element editing boundary in Web HTML preview', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      documentId: 'document-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const documentSnapshot: ArtifactDocumentWorkspaceSnapshot = {
      key: 'fixture-prompt-annotations',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: {
        ...legacy,
        source: 'document-api',
        document: {
          ...legacy.document,
          documentId: 'document-html',
          capabilities: {
            ...legacy.document.capabilities,
            preview: true,
            selectionContext: false,
            manualEdit: true,
            agentEdit: true,
            source: true,
            promptAnnotations: false,
          },
        },
      },
    }

    const web = mountPanel({
      artifact: htmlArtifact,
      documentSnapshot,
      nativeHtml: false,
      suspended: true,
    })
    await nextTick()
    expect(web.element.querySelector('[data-testid="artifact-document-desktop-editing-hint"]')
      ?.textContent).toContain('desktop app')
    web.unmount()

    const desktop = mountPanel({
      artifact: htmlArtifact,
      documentSnapshot,
      nativeHtml: true,
      suspended: true,
    })
    await nextTick()
    expect(desktop.element.querySelector(
      '[data-testid="artifact-document-desktop-editing-hint"]',
    )).toBeNull()
    desktop.unmount()

    const downloadOnly = mountPanel({
      artifact: officeArtifact,
      documentSnapshot: snapshot(),
      nativeHtml: false,
      suspended: true,
    })
    await nextTick()
    expect(downloadOnly.element.querySelector(
      '[data-testid="artifact-document-desktop-editing-hint"]',
    )).toBeNull()
    downloadOnly.unmount()
  })

  it('does not mount the source editor while the document is only being previewed', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      documentId: 'document-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const documentSnapshot: ArtifactDocumentWorkspaceSnapshot = {
      key: 'fixture-source-capable',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: {
        ...legacy,
        source: 'document-api',
        document: {
          ...legacy.document,
          documentId: 'document-html',
          capabilities: {
            ...legacy.document.capabilities,
            edit: true,
            source: true,
          },
        },
      },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<h1>Preview only</h1>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#document-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)

    const mounted = mountPanel({ artifact: htmlArtifact, documentSnapshot })
    await nextTick()

    expect([...mounted.element.querySelectorAll('[role="tab"]')]
      .map(tab => tab.textContent?.trim())).toContain('Source')
    expect(mounted.element.querySelector('.artifact-html-studio')).toBeNull()
    expect(mounted.element.querySelector('[data-document-section="preview"]')).not.toBeNull()
    mounted.unmount()
  })

  it('keeps a published wire resource on rev1 after its source Document reaches rev2', async () => {
    const deliverable = normalizeWorkbenchResource({
      resource: { type: 'deliverable', artifactId: 'deliverable-rev-1' },
      name: 'published.html',
      mime: 'text/html',
      sha256: '1'.repeat(64),
      downloadUrl: '/api/v1/artifacts/deliverable-rev-1',
      capabilities: { preview: true, download: true, edit: true, publish: false },
      relations: {
        documentId: 'document-a',
        headRevisionId: 'revision-2',
        headArtifactId: 'artifact-internal-2',
        publishedRevisionId: 'revision-1',
      },
    })!
    const preview = {
      resource: deliverable,
      preview: {
        protocolVersion: 1,
        mode: 'isolated' as const,
        resource: deliverable.resource,
        launchUrl: 'http://localhost/api/v1/workbench/previews/deliverable-rev-1',
        sandboxProfile: 'opaque-offline' as const,
        network: false as const,
        adapter: null,
      },
    }
    const immutableArtifact = artifactPayloadFromWorkbenchResource(
      resourceFromPreparedPreview(preview),
    )
    const mutableHead = createLegacyArtifactWorkspace({
      id: 'artifact-internal-2',
      documentId: 'document-a',
      revisionId: 'revision-2',
      name: 'published.html',
      mime: 'text/html',
      sha256: '2'.repeat(64),
      download_url: '/api/v1/artifact-documents/document-a',
    }, 'session-a')
    const documentSnapshot: ArtifactDocumentWorkspaceSnapshot = {
      key: 'document-a',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: {
        ...mutableHead,
        source: 'document-api',
        document: {
          ...mutableHead.document,
          documentId: 'document-a',
          headRevisionId: 'revision-2',
          capabilities: {
            ...mutableHead.document.capabilities,
            edit: true,
            source: true,
            revisions: true,
            changeSets: true,
          },
        },
      },
    }
    const onWorkbenchEvent = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<h1>Published revision 1</h1>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#deliverable-rev-1')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const mounted = mountPanel({
      artifact: immutableArtifact,
      documentFeatures: false,
      documentSnapshot,
      previewNetworkAllowed: false,
      previewSandboxProfile: 'opaque-offline',
      sessionKey: 'session-a',
      showHeader: true,
      suspended: true,
      onWorkbenchEvent,
    })
    await nextTick()

    expect(immutableArtifact).toMatchObject({
      id: 'deliverable-rev-1',
      sha256: '1'.repeat(64),
      workbenchResourceType: 'deliverable',
    })
    expect(immutableArtifact).not.toHaveProperty('documentId')
    expect([...mounted.element.querySelectorAll('[role="tab"]')]
      .map(tab => tab.textContent?.replace(/\s+/g, ' ').trim())).toEqual(['Preview'])
    expect(mounted.element.querySelector('[data-artifact-action="publish-head"]')).toBeNull()
    expect(mounted.element.querySelector('[data-document-section="versions"]')).toBeNull()
    expect(mounted.element.querySelector('[data-document-section="changes"]')).toBeNull()
    expect(mounted.element.querySelector('.artifact-html-studio')).toBeNull()
    expect(immutableArtifact.download_url).toContain('/deliverable-rev-1')
    expect(preview.preview.launchUrl).toContain('/deliverable-rev-1')

    mounted.element.querySelector<HTMLButtonElement>(
      '.artifact-preview__actions button[title*="Download"]',
    )?.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'artifact-download',
      payload: expect.objectContaining({
        id: 'deliverable-rev-1',
        sha256: '1'.repeat(64),
      }),
    })
    mounted.unmount()
  })

  it('keeps annotation input available in the trusted Vue fallback', async () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
    )
    const onWorkbenchEvent = vi.fn()
    const mounted = mountPanel({
      artifact: officeArtifact,
      documentSnapshot: snapshot(),
      annotationFallback: {
        annotationId: 'annotation-1',
        body: 'Initial draft',
        reason: 'overlay-crashed',
        screenshotUrl: 'blob:frozen-preview',
      },
      onWorkbenchEvent,
    })
    await nextTick()

    const dialog = mounted.element.querySelector('[role="dialog"]')
    const input = dialog?.querySelector<HTMLTextAreaElement>('textarea')
    expect(dialog?.textContent).toContain('Continue annotation')
    expect(dialog?.querySelector<HTMLImageElement>('img')?.src).toContain('blob:frozen-preview')
    expect(input?.value).toBe('Initial draft')
    expect(dialog?.textContent).toContain('Shift + Enter for a new line')
    if (input) {
      input.value = 'Updated draft'
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
    await nextTick()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'artifact-annotation-fallback-update',
      payload: { annotationId: 'annotation-1', body: 'Updated draft' },
    })
    const shiftEnter = new KeyboardEvent('keydown', {
      key: 'Enter',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    })
    input?.dispatchEvent(shiftEnter)
    expect(shiftEnter.defaultPrevented).toBe(false)
    expect(onWorkbenchEvent).not.toHaveBeenCalledWith(expect.objectContaining({
      type: 'artifact-annotation-fallback-submit',
    }))

    const enter = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    })
    input?.dispatchEvent(enter)
    expect(enter.defaultPrevented).toBe(true)
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'artifact-annotation-fallback-submit',
      payload: { annotationId: 'annotation-1', body: 'Updated draft' },
    })
    mounted.unmount()
  })

  it('uses the compact macOS newline chord in the annotation fallback hint', async () => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)',
    )
    const mounted = mountPanel({
      artifact: officeArtifact,
      documentSnapshot: snapshot(),
      annotationFallback: {
        annotationId: 'annotation-mac',
        body: '',
        reason: 'overlay-crashed',
        screenshotUrl: '',
      },
    })
    await nextTick()

    expect(mounted.element.querySelector(
      '.artifact-document__annotation-shortcut-hint',
    )?.textContent).toContain('⇧ Return for a new line')
    mounted.unmount()
  })

  it('labels Office artifacts download-only without exposing fake editing actions', async () => {
    const onWorkbenchEvent = vi.fn()
    const mounted = mountPanel({
      artifact: officeArtifact,
      documentSnapshot: snapshot(),
      onWorkbenchEvent,
    })
    await nextTick()

    expect(mounted.element.querySelector('.artifact-document')?.getAttribute(
      'data-download-only',
    )).toBe('true')
    expect(mounted.element.textContent).toContain(
      'Office preview and editing are not enabled',
    )
    expect(mounted.element.textContent).toContain('Download latest version')
    expect(mounted.element.querySelector('.artifact-preview')).toBeNull()
    expect(mounted.element.querySelector('button[aria-label*="edit" i]')).toBeNull()

    mounted.element.querySelector<HTMLButtonElement>(
      '.artifact-document__download-only button',
    )?.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'artifact-download',
      payload: officeArtifact,
    })
    mounted.unmount()
  })

  it('renders testable preview, version, and change tabs', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const workspace = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const documentSnapshot: ArtifactDocumentWorkspaceSnapshot = {
      key: 'fixture-html',
      loading: false,
      loaded: true,
      stale: false,
      error: null,
      workspace: {
        ...workspace,
        changeSets: [{
          changeSetId: 'change-1',
          documentId: workspace.document.documentId,
          baseRevisionId: workspace.document.headRevisionId,
          turnId: 'turn-1',
          summary: 'stale native editor trusted editor document_apply sha256 deadbeef',
          status: 'applied',
          operations: [{ op: 'replace' }],
          candidateArtifact: null,
          validation: null,
          stateRevision: 1,
          createdByKind: 'agent',
          createdById: 'main',
          appliedRevisionId: workspace.document.headRevisionId,
          createdAt: null,
          updatedAt: null,
          schemaVersion: 1,
        }],
      },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<h1>Preview</h1>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#document-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const mounted = mountPanel({ artifact: htmlArtifact, documentSnapshot })
    await nextTick()

    const tabs = [...mounted.element.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    expect(tabs.map(tab => tab.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Preview',
      'Versions1',
      'Changes1',
    ])

    tabs[1]?.click()
    await nextTick()
    const versions = mounted.element.querySelector('[data-document-section="versions"]')
    expect(versions?.textContent).toContain('Original version')
    expect(versions?.textContent).toContain('Version 1')
    expect(versions?.textContent).not.toMatch(/system|actor|initial/i)

    tabs[2]?.click()
    await nextTick()
    const changes = mounted.element.querySelector('[data-document-section="changes"]')
    expect(changes?.textContent).toContain('Updated by OpenSquilla')
    expect(changes?.textContent).toContain('Page updated')
    expect(changes?.textContent).not.toMatch(
      /stale|native editor|trusted editor|operations|change-1|document_apply|sha256|deadbeef/i,
    )

    mounted.unmount()
  })

  it('supports scoped history and revert actions', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const head = legacy.revisions[0]!
    const oldRevision = {
      ...head,
      revisionId: 'revision-old',
      generation: 0,
      artifactId: 'artifact-old',
      artifactSha256: 'old-sha',
      downloadUrl: '/api/v1/artifact-documents/document-a?revisionId=revision-old',
    }
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        capabilities: {
          ...legacy.document.capabilities,
          revisions: true,
          changeSets: true,
          comments: true,
        },
      },
      revisions: [head, oldRevision],
      changeSets: [{
        changeSetId: 'change-applied',
        documentId: legacy.document.documentId,
        baseRevisionId: oldRevision.revisionId,
        turnId: 'turn-1',
        summary: 'Replace the heading',
        status: 'applied' as const,
        operations: [{ op: 'replace' }],
        candidateArtifact: null,
        validation: { ok: true },
        stateRevision: 1,
        createdByKind: 'agent' as const,
        createdById: 'main',
        appliedRevisionId: legacy.document.headRevisionId,
        createdAt: null,
        updatedAt: null,
        schemaVersion: 1,
      }],
    }
    const actions = {
      restoreRevision: vi.fn().mockResolvedValue(workspace),
      revertChangeSet: vi.fn().mockResolvedValue(workspace),
    }
    const onWorkbenchEvent = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<h1>Preview</h1>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#document-review')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const mounted = mountPanel({
      artifact: htmlArtifact,
      documentActions: actions,
      documentSnapshot: {
        key: 'fixture-review',
        loading: false,
        loaded: true,
        stale: false,
        error: null,
        workspace,
      },
      sessionKey: 'session-a',
      onWorkbenchEvent,
    })
    await nextTick()

    const tabs = [...mounted.element.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    tabs.find(tab => tab.textContent?.includes('Versions'))?.click()
    await nextTick()
    const versionPanel = mounted.element.querySelector('[data-document-section="versions"]')!
    const oldVersion = [...versionPanel.querySelectorAll('li')]
      .find(item => item.textContent?.includes('Version 0'))!
    oldVersion.querySelector<HTMLButtonElement>('[data-artifact-action="download-revision"]')
      ?.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'artifact-download',
      payload: expect.objectContaining({
        id: 'artifact-old',
        download_url: oldRevision.downloadUrl,
      }),
    })
    oldVersion.querySelector<HTMLButtonElement>('[data-artifact-action="restore-revision"]')
      ?.click()
    await vi.waitFor(() => expect(actions.restoreRevision).toHaveBeenCalledWith(
      htmlArtifact,
      'session-a',
      'revision-old',
    ))

    tabs.find(tab => tab.textContent?.includes('Changes'))?.click()
    await nextTick()
    expect(mounted.element.querySelector(
      '[data-document-section="changes"]',
    )?.textContent).toContain('Replace the heading')
    mounted.element.querySelector<HTMLButtonElement>(
      '[data-artifact-action="revert-change-set"]',
    )?.click()
    await vi.waitFor(() => expect(actions.revertChangeSet).toHaveBeenCalledWith(
      htmlArtifact,
      'session-a',
      'change-applied',
    ))

    mounted.unmount()
  })

  it('fails closed and shows a localized error when a review mutation is rejected', async () => {
    const htmlArtifact: ArtifactPayload = {
      id: 'artifact-html',
      name: 'page.html',
      mime: 'text/html',
      download_url: '/api/v1/artifacts/artifact-html',
    }
    const legacy = createLegacyArtifactWorkspace(htmlArtifact, 'session-a')
    const head = legacy.revisions[0]!
    const oldRevision = {
      ...head,
      revisionId: 'revision-old',
      generation: 0,
    }
    const workspace = {
      ...legacy,
      source: 'document-api' as const,
      document: {
        ...legacy.document,
        capabilities: { ...legacy.document.capabilities, revisions: true },
      },
      revisions: [head, oldRevision],
    }
    const actions = {
      restoreRevision: vi.fn().mockRejectedValue(new Error('conflict details')),
      revertChangeSet: vi.fn(),
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<h1>Preview</h1>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#error')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const mounted = mountPanel({
      artifact: htmlArtifact,
      documentActions: actions,
      documentSnapshot: {
        key: 'fixture-error',
        loading: false,
        loaded: true,
        stale: false,
        error: null,
        workspace,
      },
      sessionKey: 'session-a',
    })
    await nextTick()
    const tabs = [...mounted.element.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    tabs.find(tab => tab.textContent?.includes('Versions'))?.click()
    await nextTick()
    mounted.element.querySelector<HTMLButtonElement>(
      '[data-artifact-action="restore-revision"]',
    )?.click()
    await vi.waitFor(() => {
      expect(mounted.element.querySelector('[role="alert"]')?.textContent)
        .toContain('could not be restored')
    })
    expect(mounted.element.textContent).not.toContain('conflict details')
    mounted.unmount()
  })
})
