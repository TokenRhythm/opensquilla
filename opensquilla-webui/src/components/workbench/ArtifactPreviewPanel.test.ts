// @vitest-environment happy-dom

import { createApp, h, nextTick, reactive } from 'vue'
import type { Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'
import en from '@/locales/en.json'
import type { ArtifactPayload } from '@/types/rpc'
import { ARTIFACT_PREVIEW_ESCAPE_MESSAGE } from '@/utils/workbench/artifactPreview'

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
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ArtifactPreviewPanel', () => {
  it('runs offline web HTML scripts in an opaque sandbox', async () => {
    const observed: { blob?: Blob } = {}
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockImplementation(blob => {
      observed.blob = blob as Blob
      return 'about:blank#artifact-preview'
    })
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<html><body><script>document.body.textContent = "ready"</script></body></html>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))

    const mounted = mountPanel({ artifact: artifact() })
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<html><body><form><input></form></body></html>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))

    const mounted = mountPanel({
      artifact: artifact(),
      previewMode: 'full',
      previewNetworkAllowed: false,
      previewSandboxProfile: 'opaque-offline',
    })
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
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      'plain text',
      { status: 200, headers: { 'Content-Type': 'text/plain' } },
    )))

    const mounted = mountPanel({
      artifact: artifact({ name: 'notes.txt', mime: 'text/plain' }),
      showHeader: false,
    })
    await settlePreview()

    expect(mounted.element.querySelector('.artifact-preview__toolbar')).toBeNull()
    expect(mounted.element.querySelector('.artifact-preview__text')?.textContent).toBe('plain text')
    mounted.unmount()
  })

  it('opens PDFs fitted to the panel width without disabling frame interaction', async () => {
    const onWorkbenchEvent = vi.fn()
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank?pdf-preview')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      new Uint8Array([0x25, 0x50, 0x44, 0x46]),
      { status: 200, headers: { 'Content-Type': 'application/pdf' } },
    )))

    const mounted = mountPanel({
      artifact: artifact({ name: 'report.pdf', mime: 'application/pdf' }),
      onWorkbenchEvent,
    })
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

  it('bridges Escape from an opaque HTML frame back to the Workbench', async () => {
    const onWorkbenchEvent = vi.fn()
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('about:blank')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<!doctype html><p>Preview</p>',
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))

    const mounted = mountPanel({
      artifact: artifact(),
      onWorkbenchEvent,
    })
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
    let resolveFetch!: (response: Response) => void
    const onNativeHtmlReady = vi.fn()
    const onWorkbenchEvent = vi.fn()
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })))

    const mounted = mountPanel({
      artifact: artifact(),
      nativeHtml: true,
      onNativeHtmlReady,
      onWorkbenchEvent,
    })
    await nextTick()

    expect(mounted.element.querySelector('[data-workbench-native-surface-slot]')).toBeNull()
    expect(mounted.element.querySelector('[role="status"]')).not.toBeNull()

    resolveFetch(new Response('<!doctype html><p>Native preview</p>', {
      status: 200,
      headers: { 'Content-Type': 'text/html' },
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
    const fetchMock = vi.fn((url: string | URL | Request) => Promise.resolve(new Response(
      `<h1>${String(url)}</h1>`,
      { status: 200, headers: { 'Content-Type': 'text/html' } },
    )))
    const onNativeHtmlReady = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const mounted = mountPanel({
      artifact: artifact(),
      nativeHtml: true,
      onNativeHtmlReady,
      suspended: true,
    })
    await settlePreview()
    expect(fetchMock).not.toHaveBeenCalled()

    mounted.update({ suspended: false })
    await settlePreview()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(onNativeHtmlReady).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: true })
    await settlePreview()
    mounted.update({ suspended: false })
    await settlePreview()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: true })
    await settlePreview()
    mounted.update({
      artifact: artifact({
        id: 'artifact-2',
        download_url: '/api/v1/artifacts/artifact-2',
      }),
    })
    await settlePreview()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    mounted.update({ suspended: false })
    await settlePreview()
    expect(fetchMock).toHaveBeenCalledTimes(2)
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
      '.artifact-preview__actions button',
    )]
    expect(actions).toHaveLength(2)
    actions[0]?.click()
    actions[1]?.click()

    expect(onExternalOpen).toHaveBeenCalledWith(item)
    expect(onDownload).toHaveBeenCalledWith(item)
    mounted.unmount()
  })

  it('explains an artifact integrity failure instead of showing a generic error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 'INTEGRITY_ERROR',
      error: 'checksum mismatch',
    }), {
      status: 409,
      headers: { 'Content-Type': 'application/json' },
    })))

    const mounted = mountPanel({ artifact: artifact() })
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
      vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined)))

      const mounted = mountPanel({
        artifact: artifact(),
        nativeHtml: true,
        nativeSurfaceState,
      })
      await settlePreview()

      expect(mounted.element.textContent).toContain(expectedTitle)
      expect(mounted.element.querySelector('[role="alert"]')).not.toBeNull()
      mounted.unmount()
    },
  )
})
