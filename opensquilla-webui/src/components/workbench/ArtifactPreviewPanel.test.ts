// @vitest-environment happy-dom

import { createApp, h, nextTick, reactive } from 'vue'
import { readFileSync } from 'node:fs'
import type { Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { HttpTransportError } from '@/adapters/gateway/privateHttpTransport'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'
import en from '@/locales/en.json'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import {
  httpBinaryResponse,
  httpTransportTestDouble,
  type TestHttpBinaryResponse,
  type TestHttpTransport,
} from '@/testing/httpTransport.test-helper'
import type { ArtifactPayload } from '@/types/artifacts'
import { ARTIFACT_PREVIEW_ESCAPE_MESSAGE } from '@/utils/workbench/artifactPreview'
import type { Window as TestWindow } from 'happy-dom'
import { createOfficePreviewSession } from '@/workbench/officePreviewAdapter'
import type { OfficePreviewSession } from '@/workbench/officePreviewAdapter'
import { OFFICE_PREVIEW_MIMES } from '@/utils/workbench/artifactPreview'
import { ARTIFACT_DOCUMENT_RPC_METHODS, createRpcArtifactDocumentProvider } from '@/adapters/gateway/artifactDocumentsV4'
import officeRevisionWire from '@/testing/fixtures/officeRevisionPreview/wire.json'

vi.mock('@/workbench/officePreviewAdapter', () => ({ createOfficePreviewSession: vi.fn() }))

function artifact(overrides: Partial<ArtifactPayload> = {}): ArtifactPayload {
  return {
    id: 'artifact-1',
    name: 'page.html',
    mime: 'text/html',
    download_url: '/api/v1/artifacts/artifact-1',
    ...overrides,
  }
}

async function settlePreview() {
  for (let index = 0; index < 6; index += 1) {
    await Promise.resolve()
    await nextTick()
  }
}

function mountPanel(
  props: Record<string, unknown>,
  http: TestHttpTransport = httpTransportTestDouble(),
): { element: HTMLElement; unmount: () => void; update: (patch: Record<string, unknown>) => void } {
  const element = document.createElement('div')
  document.body.append(element)
  const state = reactive({ ...props })
  const app = createApp({
    render: () => h(ArtifactPreviewPanel as Component, state),
  })
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en },
  }))
  app.provide(ARTIFACT_WORKBENCH_KEY, {
    previews: createV4ArtifactPreviews(http, { baseOrigin: () => 'http://localhost' }),
  } as ArtifactWorkbench)
  app.mount(element)
  return {
    element,
    unmount: () => {
      app.unmount()
      element.remove()
    },
    update: patch => Object.assign(state, patch),
  }
}

afterEach(() => {
  (window as unknown as TestWindow).happyDOM.settings.disableIframePageLoading = false
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ArtifactPreviewPanel', () => {
  it('automatically previews pinned Office revisions while the mutable download head advances', async () => {
    // Recorded from the synthetic live Gateway trial: only the first slide's
    // title changed, but both the SHA-256 and byte length changed with it.
    const updatedDocument = officeRevisionWire.documentResponse.document
    const revisions = officeRevisionWire.revisionsResponse.revisions
    const initialRevision = revisions.find(revision => revision.generation === 1)!
    const updatedRevision = revisions.find(revision => revision.generation === 2)!
    const initialBytes = new Uint8Array(readFileSync('src/testing/fixtures/officeRevisionPreview/initial.pptx')).buffer
    const updatedBytes = new Uint8Array(readFileSync('src/testing/fixtures/officeRevisionPreview/updated.pptx')).buffer
    let selectedRevision = initialRevision
    const call = vi.fn(async (method: string) => {
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.capabilities) return { formats: { pptx: { preview: true } } }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet) return { document: {
        ...updatedDocument, headRevisionId: selectedRevision.id, generation: selectedRevision.generation, head: selectedRevision,
      } }
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList) return officeRevisionWire.revisionsResponse
      if (method === ARTIFACT_DOCUMENT_RPC_METHODS.changesList) return { changeSets: [] }
      throw new Error(`Unexpected RPC ${method}`)
    })
    const provider = createRpcArtifactDocumentProvider({ call: call as Parameters<typeof createRpcArtifactDocumentProvider>[0]['call'], hasRpcMethod: () => true })
    const source = { documentId: updatedDocument.id, name: updatedDocument.name, mime: updatedRevision.mime }
    const initialWorkspace = await provider.loadWorkspace(source, 'session-office-preview')
    const requestBinary = vi.fn(async (url: string) => httpBinaryResponse(
      url === initialRevision.downloadUrl ? initialBytes : updatedBytes,
      { contentType: updatedRevision.mime },
    ))
    vi.mocked(createOfficePreviewSession).mockResolvedValue({
      goTo: vi.fn(), getPageSize: vi.fn(() => null), getNotes: vi.fn(() => null), renderThumbnail: vi.fn(), fitPage: vi.fn(),
      destroy: vi.fn(), previous: vi.fn(), next: vi.fn(), resize: vi.fn(), fitWidth: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(), setSelectionMode: vi.fn(), clearSelection: vi.fn(),
    })
    const onStateChange = vi.fn()
    const mounted = mountPanel({ artifact: initialWorkspace.headArtifact, sessionKey: 'session-office-preview', onStateChange }, httpTransportTestDouble({ requestBinary }))
    await vi.waitFor(() => expect(onStateChange).toHaveBeenLastCalledWith('ready'))
    expect(requestBinary).toHaveBeenLastCalledWith(initialRevision.downloadUrl, expect.objectContaining({ sessionKey: 'session-office-preview' }))
    expect(initialWorkspace.headArtifact.download_url).toBe(updatedDocument.latestDownloadUrl)

    selectedRevision = updatedRevision
    const updatedWorkspace = await provider.loadWorkspace(source, 'session-office-preview')
    mounted.update({ artifact: updatedWorkspace.headArtifact })
    await vi.waitFor(() => {
      expect(requestBinary).toHaveBeenLastCalledWith(updatedRevision.downloadUrl, expect.objectContaining({ sessionKey: 'session-office-preview' }))
      expect(onStateChange).toHaveBeenLastCalledWith('ready')
    })
    expect(updatedWorkspace.headArtifact.download_url).toBe(updatedDocument.latestDownloadUrl)
    expect(requestBinary.mock.calls.map(([url]) => url)).toEqual([initialRevision.downloadUrl, updatedRevision.downloadUrl])
    expect(mounted.element.querySelector('[role="alert"]')).toBeNull()
    mounted.unmount()
  })

  it('reports Office rendering state after byte download and retains render failure with retry', async () => {
    let resolve!: (session: OfficePreviewSession) => void
    vi.mocked(createOfficePreviewSession).mockImplementationOnce(() => new Promise(next => { resolve = next }))
    const onStateChange = vi.fn()
    const mounted = mountPanel({ artifact: artifact({ name: 'sample.docx', mime: OFFICE_PREVIEW_MIMES.docx }), onStateChange },
      httpTransportTestDouble({ requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse('office-bytes', {
        contentType: OFFICE_PREVIEW_MIMES.docx,
      })) }))
    await settlePreview()
    expect(onStateChange).toHaveBeenLastCalledWith('loading')
    expect(onStateChange).not.toHaveBeenCalledWith('ready')
    const destroy = vi.fn()
    resolve({ destroy, previous: vi.fn(), next: vi.fn(), resize: vi.fn(), fitWidth: vi.fn(), zoomIn: vi.fn(), zoomOut: vi.fn(), setSelectionMode: vi.fn(), clearSelection: vi.fn(),
      goTo: vi.fn(), getPageSize: vi.fn(() => null), getNotes: vi.fn(() => null), renderThumbnail: vi.fn(), fitPage: vi.fn() })
    await settlePreview()
    expect(onStateChange).toHaveBeenLastCalledWith('ready')
    vi.mocked(createOfficePreviewSession).mockRejectedValueOnce(new Error('invalid archive'))
    mounted.update({ artifact: artifact({ name: 'sample.docx', mime: OFFICE_PREVIEW_MIMES.docx, revisionId: 'next' }) })
    await settlePreview()
    expect(onStateChange).toHaveBeenLastCalledWith('error')
    expect(mounted.element.querySelector('[role="alert"]')).toBeTruthy()
    expect(destroy).toHaveBeenCalledOnce()
    mounted.unmount()
  })

  it('invalidates file actions after an opaque iframe navigates without changing preview permissions', async () => {
    // Load events are injected deterministically; do not make real DNS requests.
    (window as unknown as TestWindow).happyDOM.settings.disableIframePageLoading = true
    const onWorkbenchEvent = vi.fn()
    const mounted = mountPanel({ artifact: artifact(), previewLaunchUrl: 'http://preview.localhost/minimal.html', onWorkbenchEvent })
    await settlePreview()
    const frame = mounted.element.querySelector('iframe')!
    frame.dispatchEvent(new Event('load'))
    expect(onWorkbenchEvent).not.toHaveBeenCalledWith({ type: 'preview-page-unknown' })
    frame.dispatchEvent(new Event('load'))
    expect(onWorkbenchEvent).toHaveBeenCalledWith({ type: 'preview-page-unknown' })
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin')
    mounted.unmount()
  })
  it('runs offline web HTML scripts in an opaque sandbox', async () => {
    const observed: { blob?: Blob } = {}
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
      observed.blob = blob as Blob
      return 'about:blank#artifact-preview'
    })
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse(
      '<html><body><script>document.body.textContent = "ready"</script></body></html>',
        { contentType: 'text/html' },
      )),
    })

    const mounted = mountPanel({ artifact: artifact() }, http)
    await settlePreview()

    const frame = mounted.element.querySelector<HTMLIFrameElement>('.artifact-preview__frame--html')
    expect(frame).not.toBeNull()
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-forms')
    expect(frame?.getAttribute('allow')).toBe('')
    expect(frame?.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(frame?.getAttribute('tabindex')).toBe('0')
    expect(createObjectUrl).toHaveBeenCalledOnce()
    expect(await observed.blob?.text()).toContain("connect-src 'none'")

    mounted.unmount()
    expect(revokeObjectUrl).toHaveBeenCalledWith('about:blank#artifact-preview')
  })

  it('keeps a prepared opaque preview isolated even when the saved mode is full', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#prepared-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse(
      '<html><body><form><input></form></body></html>',
        { contentType: 'text/html' },
      )),
    })

    const mounted = mountPanel({
      artifact: artifact(),
      previewMode: 'full',
      previewNetworkAllowed: false,
      previewSandboxProfile: 'opaque-offline',
    }, http)
    await settlePreview()

    const frame = mounted.element.querySelector<HTMLIFrameElement>(
      '.artifact-preview__frame--html',
    )
    expect(frame?.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-same-origin')
    expect(frame?.getAttribute('sandbox')).not.toContain('allow-forms')
    expect(frame?.getAttribute('allow')).toBe('')
    mounted.unmount()
  })

  it('can omit its header when embedded in the workbench chrome', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse('plain text', {
        contentType: 'text/plain',
      })),
    })

    const mounted = mountPanel({
      artifact: artifact({ name: 'notes.txt', mime: 'text/plain' }),
      showHeader: false,
    }, http)
    await settlePreview()

    expect(mounted.element.querySelector('.artifact-preview__toolbar')).toBeNull()
    expect(mounted.element.querySelector('.artifact-preview__text')?.textContent).toBe('plain text')
    mounted.unmount()
  })

  it('keeps refreshing on its own toolbar button and omits preview from the file menu', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank#toolbar-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const requestBinary = vi.fn().mockResolvedValue(httpBinaryResponse(
      '<html><body>Preview</body></html>',
      { contentType: 'text/html' },
    ))
    const mounted = mountPanel({ artifact: artifact() }, httpTransportTestDouble({ requestBinary }))
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledOnce()

    mounted.element.querySelector<HTMLButtonElement>('.resource-actions-trigger')!.click()
    await settlePreview()
    const menu = document.querySelector<HTMLElement>('[role="menu"]')!
    expect(menu).not.toBeNull()
    const actions = [...menu.querySelectorAll('[role="menuitem"]')]
      .map(action => action.textContent?.trim())
    expect(actions).not.toContain(en.resourceActions.preview)
    expect(actions).not.toContain(en.workbench.artifactPreview.refresh)
    expect(requestBinary).toHaveBeenCalledOnce()

    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    mounted.element.querySelector<HTMLButtonElement>(
      `[aria-label="${en.workbench.artifactPreview.refresh}"]`,
    )!.click()
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledTimes(2)
    mounted.unmount()
  })

  it('opens PDFs fitted to the panel width without disabling frame interaction', async () => {
    const onWorkbenchEvent = vi.fn()
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank?pdf-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse(
        new Uint8Array([0x25, 0x50, 0x44, 0x46]),
        { contentType: 'application/pdf' },
      )),
    })

    const mounted = mountPanel({
      artifact: artifact({ name: 'report.pdf', mime: 'application/pdf' }),
      onWorkbenchEvent,
    }, http)
    await settlePreview()

    const frame = mounted.element.querySelector<HTMLIFrameElement>(
      '.artifact-preview__frame--pdf',
    )!
    expect(frame.getAttribute('src')).toBe(
      'about:blank?pdf-preview#zoom=page-width&view=FitH',
    )
    expect(frame.getAttribute('aria-hidden')).toBeNull()
    expect(frame.getAttribute('tabindex')).toBe('0')
    expect(frame.classList.contains('is-mobile-inert')).toBe(false)

    const frameExit = mounted.element.querySelector<HTMLButtonElement>(
      '.artifact-preview__frame-exit',
    )!
    expect(frame.compareDocumentPosition(frameExit) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    expect(frameExit.textContent?.trim()).toBe('Collapse workbench')
    frameExit.focus()
    expect(document.activeElement).toBe(frameExit)
    frameExit.click()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({ type: 'request-collapse' })
    mounted.unmount()
  })

  it('shows a useful fallback when the browser disables inline PDF viewing', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(window.navigator, 'pdfViewerEnabled')
    Object.defineProperty(window.navigator, 'pdfViewerEnabled', {
      configurable: true,
      value: false,
    })
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse(
        new Uint8Array([0x25, 0x50, 0x44, 0x46]),
        { contentType: 'application/pdf' },
      )),
    })

    try {
      const mounted = mountPanel({
        artifact: artifact({ name: 'report.pdf', mime: 'application/pdf' }),
      }, http)
      await settlePreview()

      expect(mounted.element.querySelector('.artifact-preview__frame--pdf')).toBeNull()
      expect(mounted.element.querySelector('[role="alert"]')?.textContent)
        .toContain(en.workbench.artifactPreview.pdfViewerUnavailable)
      mounted.unmount()
    } finally {
      if (descriptor) Object.defineProperty(window.navigator, 'pdfViewerEnabled', descriptor)
      else Reflect.deleteProperty(window.navigator, 'pdfViewerEnabled')
    }
  })

  it('bridges Escape from an opaque HTML frame back to the Workbench', async () => {
    const onWorkbenchEvent = vi.fn()
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockResolvedValue(httpBinaryResponse(
      '<!doctype html><p>Preview</p>',
        { contentType: 'text/html' },
      )),
    })

    const mounted = mountPanel({
      artifact: artifact(),
      onWorkbenchEvent,
    }, http)
    await settlePreview()
    const frame = mounted.element.querySelector<HTMLIFrameElement>(
      '.artifact-preview__frame--html',
    )!

    window.dispatchEvent(new MessageEvent('message', {
      data: ARTIFACT_PREVIEW_ESCAPE_MESSAGE,
      source: frame.contentWindow,
    }))

    expect(onWorkbenchEvent).toHaveBeenCalledWith({ type: 'request-collapse' })
    mounted.unmount()
  })

  it('replaces native HTML loading state with the native surface slot', async () => {
    let resolveRequest!: (response: TestHttpBinaryResponse) => void
    const onNativeHtmlReady = vi.fn()
    const onWorkbenchEvent = vi.fn()
    const http = httpTransportTestDouble({
      requestBinary: vi.fn(() => new Promise<TestHttpBinaryResponse>((resolve) => {
        resolveRequest = resolve
      })),
    })

    const mounted = mountPanel({
      artifact: artifact(),
      nativeHtml: true,
      onNativeHtmlReady,
      onWorkbenchEvent,
    }, http)
    await nextTick()

    expect(mounted.element.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    expect(mounted.element.querySelector('[role="status"]')).not.toBeNull()

    resolveRequest(httpBinaryResponse('<!doctype html><p>Native preview</p>', {
      contentType: 'text/html',
    }))
    await settlePreview()

    expect(mounted.element.querySelector('[data-workbench-native-surface-slot]'))
      .toBeInstanceOf(HTMLElement)
    expect(mounted.element.querySelector('.artifact-preview__frame--html')).toBeNull()
    expect(onNativeHtmlReady).toHaveBeenCalledOnce()
    expect(onWorkbenchEvent).toHaveBeenCalledWith({
      type: 'native-html-ready',
      payload: expect.any(Object),
    })
    mounted.unmount()
  })

  it('reloads changed native HTML instead of resuming stale bytes after Source was active', async () => {
    const requestBinary = vi.fn((url: string) => Promise.resolve(httpBinaryResponse(
      `<h1>${url}</h1>`,
      { contentType: 'text/html' },
    )))
    const http = httpTransportTestDouble({ requestBinary })
    const onNativeHtmlReady = vi.fn()

    const mounted = mountPanel({
      artifact: artifact(),
      nativeHtml: true,
      onNativeHtmlReady,
      suspended: true,
    }, http)
    await settlePreview()
    expect(requestBinary).not.toHaveBeenCalled()

    mounted.update({ suspended: false })
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledTimes(1)
    expect(onNativeHtmlReady).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: true })
    await settlePreview()
    mounted.update({ suspended: false })
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: true })
    await settlePreview()
    mounted.update({
      artifact: artifact({
        id: 'artifact-2',
        download_url: '/api/v1/artifacts/artifact-2',
      }),
    })
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: false })
    await settlePreview()
    expect(requestBinary).toHaveBeenCalledTimes(2)
    expect(onNativeHtmlReady).toHaveBeenCalledTimes(2)
    expect(onNativeHtmlReady.mock.calls[1]?.[0].artifact.id).toBe('artifact-2')
    mounted.unmount()
  })

  it('emits external-open and download intents without performing them', async () => {
    const onExternalOpen = vi.fn()
    const onDownload = vi.fn()
    const item = artifact({
      name: 'archive.zip',
      mime: 'application/zip',
    })
    const mounted = mountPanel({
      artifact: item,
      onDownload,
      onExternalOpen,
    })
    await settlePreview()

    const actions = [...mounted.element.querySelectorAll<HTMLButtonElement>(
      '.artifact-preview__actions button:not(.resource-actions-trigger)',
    )]
    expect(actions).toHaveLength(2)
    actions[0]?.click()
    actions[1]?.click()

    expect(onExternalOpen).toHaveBeenCalledWith(item)
    expect(onDownload).toHaveBeenCalledWith(item)
    mounted.unmount()
  })

  it('pins header actions to the shown revision while preserving its metadata', async () => {
    const onExternalOpen = vi.fn()
    const onDownload = vi.fn()
    const onWorkbenchEvent = vi.fn()
    const item = artifact({ id: 'artifact-2', revisionId: 'revision-2', name: 'archive-v2.zip',
      mime: 'application/zip', sha256: '2'.repeat(64), size: 200,
      download_url: '/api/v1/artifact-documents/document-1', revision_download_url: '/api/v1/artifacts/artifact-2' })
    const mounted = mountPanel({ artifact: item, onDownload, onExternalOpen, onWorkbenchEvent })
    await settlePreview()
    const actions = [...mounted.element.querySelectorAll<HTMLButtonElement>('.artifact-preview__actions button:not(.resource-actions-trigger)')]
    actions[0]?.click()
    actions[1]?.click()
    const pinned = { ...item, download_url: '/api/v1/artifacts/artifact-2' }
    expect(onExternalOpen).toHaveBeenCalledWith(pinned)
    expect(onDownload).toHaveBeenCalledWith(pinned)
    expect(onWorkbenchEvent).toHaveBeenCalledWith({ type: 'artifact-download', payload: pinned })
    expect(item.download_url).toBe('/api/v1/artifact-documents/document-1')
    mounted.unmount()
  })

  it('explains an artifact integrity failure instead of showing a generic error', async () => {
    const http = httpTransportTestDouble({
      requestBinary: vi.fn().mockRejectedValue(new HttpTransportError(
        'http-status',
        'checksum mismatch',
        409,
        { code: 'INTEGRITY_ERROR', error: 'checksum mismatch' },
      )),
    })

    const mounted = mountPanel({ artifact: artifact() }, http)
    await settlePreview()

    expect(mounted.element.textContent).toContain('Artifact integrity check failed')
    expect(mounted.element.textContent).toContain('no longer matches its recorded checksum')
    mounted.unmount()
  })

  it.each([
    ['error', 'Preview failed'],
    ['crashed', 'The preview stopped'],
  ] as const)(
    'renders the native %s state in the DOM recovery surface',
    async (nativeSurfaceState, expectedTitle) => {
      const http = httpTransportTestDouble({
        requestBinary: vi.fn(() => new Promise<TestHttpBinaryResponse>(() => undefined)),
      })

      const mounted = mountPanel({
        artifact: artifact(),
        nativeHtml: true,
        nativeSurfaceState,
      }, http)
      await settlePreview()

      expect(mounted.element.textContent).toContain(expectedTitle)
      expect(mounted.element.querySelector('[role="alert"]')).not.toBeNull()
      mounted.unmount()
    },
  )
})
