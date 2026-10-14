// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY, HtmlBundleExportError } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import type { ArtifactPayload } from '@/types/artifacts'
import ResourceActionsMenu from './ResourceActionsMenu.vue'

const mocks = vi.hoisted(() => ({
  platform: { id: 'web', files: {} as Record<string, unknown>, gateway: {
    getStatus: vi.fn(), getConnection: vi.fn(),
  } }, copy: vi.fn(), download: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/platform', () => ({ usePlatform: () => mocks.platform }))
vi.mock('@/utils/browser', async importOriginal => ({
  ...await importOriginal<typeof import('@/utils/browser')>(),
  copyTextWithFallback: mocks.copy, downloadBlob: mocks.download,
}))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: mocks.toast }) }))

const source = { source: 'workspace-preview', documentId: 'doc_one', name: 'index.html',
  mime: 'text/html', previewPagePath: 'editorial.html' }
const info = { documentId: 'doc_one', pagePath: 'editorial.html', sourcePath: '/task/site/editorial.html',
  workspace: '/task', name: 'editorial.html', mime: 'text/html', size: 19 }
const markdown = { source: 'publish_artifact', id: 'notes', name: 'notes.md', mime: 'text/markdown' }
const apps: App[] = []
async function settle() { for (let i = 0; i < 12; i++) await nextTick() }
async function mount(artifact: ArtifactPayload = source, previewable = true, requireSameOrigin = false, compact = false) {
  const content = { workingFileMetadata: vi.fn().mockResolvedValue(info),
    fetchWorkingFile: vi.fn().mockResolvedValue(new Blob(['<h1>current</h1>'], { type: 'text/html' })),
    fetchArtifact: vi.fn().mockResolvedValue({ ok: true, blob: new Blob(['old version'], { type: 'text/plain' }) }),
    exportHtmlBundle: vi.fn().mockResolvedValue({ blob: new Blob(['zip bytes'], { type: 'application/zip' }), filename: 'webpage.zip' }),
  }
  const access = reactive({ subscriptionEpoch: 1 })
  const props = reactive({ artifact, sessionKey: 'agent:main:webchat:one', trigger: true, previewable, requireSameOrigin, compact, downloadAction: undefined as string | undefined, extraActions: compact ? [{ id: 'close', label: 'Close tab', separator: true }] : [] })
  const onOpen = vi.fn(), onAction = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ResourceActionsMenu, { ...props, onOpen, onAction }) })
  app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, { content } as never)
  app.provide(GATEWAY_ACCESS_KEY, access as never)
  apps.push(app)
  app.mount(host)
  await nextTick()
  const trigger = host.querySelector('button')!
  const open = async () => { trigger.click(); await settle() }
  return { content, access, props, onOpen, onAction, trigger, open }
}
function item(key: string) {
  const label = i18n.global.t(key)
  return [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(el => el.textContent?.trim() === label)
}
beforeEach(() => {
  vi.clearAllMocks()
  mocks.platform.id = 'web'
  mocks.platform.files = {}
  mocks.platform.gateway.getStatus.mockResolvedValue({ owned: true, status: 'ready' })
  mocks.platform.gateway.getConnection.mockResolvedValue({ status: 'ready', instanceId: 'owned-one' })
})
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('shared resource actions', () => {
  it('retains a compact panel download when its optional working source is unavailable', async () => {
    const f = await mount(source, false, false, true)
    f.props.downloadAction = 'panel:download'
    f.content.workingFileMetadata.mockRejectedValue(new Error('source unavailable'))
    await f.open()
    expect(item('chat.download')).toBeDefined()
    expect(document.querySelector('[role="menu"]')?.textContent).not.toContain(i18n.global.t('resourceActions.unavailable'))
    item('chat.download')!.click()
    await settle()
    expect(f.onAction).toHaveBeenCalledExactlyOnceWith('panel:download')
    expect(f.content.fetchWorkingFile).not.toHaveBeenCalled()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
    expect(mocks.toast).not.toHaveBeenCalled()
  })
  it('exports the selected document revision to a local desktop ZIP without invoking an opener', async () => {
    mocks.platform.id = 'desktop'
    const saveArtifact = vi.fn().mockResolvedValue({ status: 'saved' })
    const openArtifact = vi.fn()
    mocks.platform.files = { saveArtifact, openArtifact }
    const f = await mount({ id: 'art-old', documentId: 'doc_one', revisionId: 'rev_two', name: 'index.html', mime: 'text/html',
      download_url: '/api/v1/artifact-documents/doc_one' })
    await f.open()
    item('resourceActions.downloadZip')!.click()
    await vi.waitFor(() => expect(saveArtifact).toHaveBeenCalledOnce())
    expect(f.content.exportHtmlBundle).toHaveBeenCalledWith({ target: { type: 'document', documentId: 'doc_one', revisionId: 'rev_two' },
      sessionKey: f.props.sessionKey, signal: expect.any(AbortSignal) })
    expect(saveArtifact).toHaveBeenCalledWith(expect.objectContaining({ name: 'webpage.zip', mime: 'application/zip' }))
    expect(openArtifact).not.toHaveBeenCalled()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it('downloads a published version on Web without touching current source files', async () => {
    const f = await mount({ id: 'art-published', name: 'index.html', mime: 'text/html' })
    await f.open()
    expect(item('resourceActions.downloadZip')!.title).toBe(i18n.global.t('resourceActions.zipSnapshot'))
    item('resourceActions.downloadZip')!.click()
    await settle()
    expect(f.content.exportHtmlBundle).toHaveBeenCalledWith(expect.objectContaining({ target: { type: 'artifact', artifactId: 'art-published' } }))
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), 'webpage.zip')
    expect(f.content.fetchWorkingFile).not.toHaveBeenCalled()
  })

  it('exports current workspace contents explicitly and explains remote permission denial without fallback', async () => {
    const f = await mount()
    f.content.exportHtmlBundle.mockRejectedValue(new HtmlBundleExportError('source-forbidden'))
    await f.open()
    expect(item('resourceActions.downloadZip')!.title).toBe(i18n.global.t('resourceActions.zipWorking'))
    item('resourceActions.downloadZip')!.click()
    await settle()
    expect(f.content.exportHtmlBundle).toHaveBeenCalledWith(expect.objectContaining({ target: { type: 'working', documentId: 'doc_one', pagePath: 'editorial.html' } }))
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('resourceActions.zipSourceForbidden'), { tone: 'danger' })
    expect(mocks.download).not.toHaveBeenCalled()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it.each(['unavailable', 'incomplete', 'too-large'] as const)('does not save a fallback HTML file after ZIP failure %s', async code => {
    const f = await mount({ id: 'art-published', name: 'index.html', mime: 'text/html' })
    f.content.exportHtmlBundle.mockRejectedValue(new HtmlBundleExportError(code))
    await f.open()
    item('resourceActions.downloadZip')!.click()
    await settle()
    expect(mocks.download).not.toHaveBeenCalled()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledOnce()
  })

  it.each(['session', 'epoch', 'revision', 'unmount'])('discards a completed ZIP when %s changes while downloading', async change => {
    const f = await mount({ id: 'art-published', documentId: 'doc_one', revisionId: 'rev_one', name: 'index.html', mime: 'text/html' })
    let finish!: (value: { blob: Blob; filename: string }) => void
    f.content.exportHtmlBundle.mockImplementation(() => new Promise(resolve => { finish = resolve }))
    await f.open()
    item('resourceActions.downloadZip')!.click()
    await nextTick()
    const request = f.content.exportHtmlBundle.mock.calls[0]![0]
    if (change === 'session') f.props.sessionKey = 'another-session'
    if (change === 'epoch') f.access.subscriptionEpoch++
    if (change === 'revision') f.props.artifact.revisionId = 'rev_two'
    if (change === 'unmount') apps.pop()!.unmount()
    await nextTick()
    expect(request.signal.aborted).toBe(true)
    finish({ blob: new Blob(['stale']), filename: 'stale.zip' })
    await settle()
    expect(mocks.download).not.toHaveBeenCalled()
    expect(mocks.toast).not.toHaveBeenCalled()
  })

  it.each([
    { name: 'page.html', download_url: 'https://external.invalid/page.html' },
    { documentId: 'doc_one', name: 'page.html' },
    { id: 'art-image', name: 'image.png', mime: 'image/png' },
  ])('hides ZIP for resources without an eligible immutable/source HTML identity: %j', async artifact => {
    const f = await mount(artifact)
    await f.open()
    expect(item('resourceActions.downloadZip')).toBeUndefined()
    expect(f.content.exportHtmlBundle).not.toHaveBeenCalled()
  })

  it('carries the preview trust policy into Save As and keeps native open absent on Web', async () => {
    const artifact = { ...markdown, revisionId: 'revision-2', sha256: '2'.repeat(64),
      download_url: '/api/v1/artifacts/notes', revision_download_url: '/api/v1/artifacts/notes' }
    const f = await mount(artifact, false, true)
    await f.open()
    expect(item('resourceActions.openCopy')).toBeUndefined()
    item('chat.download')!.click()
    await settle()
    expect(f.content.fetchArtifact).toHaveBeenCalledWith(artifact, expect.objectContaining({
      sessionKey: f.props.sessionKey, requireSameOrigin: true,
    }))
    expect(mocks.download).toHaveBeenCalledOnce()
  })

  it('copies original SVG source through the shared async clipboard path', async () => {
    const text = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 8">\n<!-- unchanged -->\n</svg>'
    const copied: Blob[] = []
    class ClipboardItemMock {
      constructor(readonly items: Record<string, Promise<Blob>>) {}
    }
    vi.stubGlobal('ClipboardItem', ClipboardItemMock)
    const write = vi.fn(async (items: ClipboardItems) => {
      copied.push(await (items[0] as unknown as ClipboardItemMock).items['text/plain']!)
    })
    vi.spyOn(navigator.clipboard, 'write').mockImplementation(write)
    const f = await mount({ id: 'vector', name: 'vector.svg', mime: 'image/svg+xml' })
    f.content.fetchArtifact.mockResolvedValue({ ok: true, blob: new Blob([text], { type: 'image/svg+xml' }) })
    await f.open()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    item('imageClipboard.copySource')!.click()
    // Clipboard write starts during the click, before fetching the bytes.
    expect(write).toHaveBeenCalledOnce()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
    await vi.waitFor(() => expect(copied).toHaveLength(1))
    expect(await copied[0]!.text()).toBe(text)
    expect(copied[0]!.type).toBe('text/plain')
    expect(f.content.fetchArtifact).toHaveBeenCalledWith(expect.objectContaining({ id: 'vector' }),
      expect.objectContaining({ sessionKey: f.props.sessionKey, maxBytes: 1024 * 1024 }))
    expect(mocks.download).not.toHaveBeenCalled()
  })

  it('keeps the specific HTML page and session for preview and current download without copy options', async () => {
    const f = await mount()
    await f.open()
    item('resourceActions.preview')!.click()
    expect(f.onOpen).toHaveBeenCalledWith(source)
    await f.open()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    expect(item('resourceActions.copyGatewayPath')).toBeUndefined()
    expect(item('resourceActions.copyPath')).toBeUndefined()
    item('chat.download')!.click()
    await settle()
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), 'editorial.html')
    expect(await mocks.download.mock.calls[0]![0].text()).toBe('<h1>current</h1>')
    expect(f.content.fetchWorkingFile).toHaveBeenCalledWith(expect.objectContaining({
      documentId: 'doc_one', pagePath: 'editorial.html', sessionKey: f.props.sessionKey,
    }))
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it('saves historical HTML delivery bytes without substituting current source or showing copy options', async () => {
    const artifact = { ...source, source: 'publish_artifact', id: 'old-artifact', revisionId: 'old-revision' }
    const f = await mount(artifact)
    await f.open()
    expect(item('resourceActions.copyGatewayPath')).toBeUndefined()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    item('chat.download')!.click()
    await settle()
    expect(await mocks.download.mock.calls[0]![0].text()).toBe('old version')
    expect(f.content.fetchArtifact).toHaveBeenCalledWith(artifact, expect.objectContaining({ sessionKey: f.props.sessionKey }))
    expect(f.content.workingFileMetadata).not.toHaveBeenCalled()
    expect(f.content.fetchWorkingFile).not.toHaveBeenCalled()
  })

  it.each([
    markdown,
    { ...markdown, name: 'example.ts', mime: 'text/plain' },
  ])('retains copying for text files: $name', async artifact => {
    const f = await mount(artifact)
    await f.open()
    item('resourceActions.copyContents')!.click()
    await settle()
    expect(mocks.copy).toHaveBeenCalledWith('old version')
    expect(f.content.fetchWorkingFile).not.toHaveBeenCalled()
  })

  it.each([
    { name: 'page.HTML' },
    { name: 'page.htm', mime: 'application/octet-stream' },
    { name: 'page.xhtml', mime: 'application/xhtml+xml' },
    { name: 'page', mime: 'text/html; charset=utf-8' },
  ])('also simplifies HTML files detected by type or filename: $name', async artifact => {
    const f = await mount(artifact)
    await f.open()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    expect(item('resourceActions.preview')).toBeDefined()
    expect(item('chat.download')).toBeDefined()
  })

  it.each([
    ['Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', 'resourceActions.revealFinder'],
    ['Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'resourceActions.revealExplorer'],
    ['Mozilla/5.0 (X11; Linux x86_64)', 'resourceActions.reveal'],
  ])('shows local HTML actions with the native label for %s', async (userAgent, revealKey) => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(userAgent)
    mocks.platform.id = 'desktop'
    const action = vi.fn().mockResolvedValue(undefined)
    mocks.platform.files = { sourceFileAction: action, saveArtifact: vi.fn() }
    const f = await mount()
    await f.open()
    expect([...document.querySelectorAll('[role="menuitem"]')].map(el => el.textContent?.trim())).toEqual([
      'resourceActions.preview', 'resourceActions.openSource', revealKey, 'resourceActions.downloadZip', 'resourceActions.saveAs',
    ].map(key => i18n.global.t(key)))
    item(revealKey)!.click()
    await settle()
    expect(action).toHaveBeenCalledExactlyOnceWith({ gatewayInstanceId: 'owned-one',
      sessionKey: f.props.sessionKey, documentId: 'doc_one', pagePath: 'editorial.html', action: 'reveal' })
  })

  it('provides native real-source actions only for owned ready connection; passes identities not paths', async () => {
    mocks.platform.id = 'desktop'
    const action = vi.fn().mockResolvedValue(undefined)
    mocks.platform.files = { sourceFileAction: action, saveArtifact: vi.fn().mockResolvedValue({ status: 'cancelled' }) }
    const f = await mount()
    await f.open()
    item('resourceActions.openSource')!.click()
    await settle()
    expect(action).toHaveBeenCalledExactlyOnceWith({ gatewayInstanceId: 'owned-one', sessionKey: f.props.sessionKey,
      documentId: 'doc_one', pagePath: 'editorial.html', action: 'open' })
    await f.open()
    item('resourceActions.saveAs')!.click()
    await settle()
    expect(mocks.platform.files.saveArtifact).toHaveBeenCalledWith(expect.objectContaining({ name: 'editorial.html', mime: 'text/html' }))
    expect(mocks.toast).not.toHaveBeenCalledWith(i18n.global.t('resourceActions.saved'), expect.anything())
    mocks.platform.gateway.getStatus.mockResolvedValue({ owned: false, status: 'ready' })
    await f.open()
    expect(item('resourceActions.openSource')).toBeUndefined()
    expect(item('resourceActions.reveal')).toBeUndefined()
    expect(item('resourceActions.saveAs')).toBeDefined()
  })

  it('missing capability hides source operations without snapshot fallback', async () => {
    const f = await mount()
    f.content.workingFileMetadata.mockResolvedValue(null)
    await f.open()
    expect(item('resourceActions.preview')).toBeDefined()
    expect(item('chat.download')).toBeUndefined()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it('fences a delayed copy when connection or session changes', async () => {
    const f = await mount(markdown)
    let resolve!: (result: { ok: boolean; blob: Blob }) => void
    f.content.fetchArtifact.mockImplementation(() => new Promise(done => { resolve = done }))
    await f.open()
    item('resourceActions.copyContents')!.click()
    await nextTick()
    f.access.subscriptionEpoch++
    await nextTick()
    resolve({ ok: true, blob: new Blob(['stale'], { type: 'text/markdown' }) })
    await settle()
    expect(mocks.copy).not.toHaveBeenCalled()
    expect(mocks.toast).not.toHaveBeenCalled()
    await f.open()
    f.props.sessionKey = 'agent:main:webchat:two'
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  it.each([
    new Blob([new Uint8Array([255, 254])], { type: 'text/html' }),
    new Blob(['binary'], { type: 'application/pdf' }),
    new Blob(['x'.repeat(1024 * 1024 + 1)], { type: 'text/html' }),
  ])('rejects invalid, binary and oversized clipboard content but permits raw saving', async blob => {
    const f = await mount(markdown)
    f.content.fetchArtifact.mockResolvedValue({ ok: true, blob })
    await f.open()
    item('resourceActions.copyContents')!.click()
    await settle()
    expect(mocks.copy).not.toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('resourceActions.textOnly'), { tone: 'danger' })
    await f.open()
    item('chat.download')!.click()
    await settle()
    expect(mocks.download).toHaveBeenCalledWith(blob, 'notes.md')
  })

  it('supports keyboard navigation, restores focus and occludes native preview only while open', async () => {
    const f = await mount()
    f.trigger.focus()
    f.trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    await settle()
    expect(useNativeSurfaceOcclusionState().value).toBe(true)
    const menu = document.querySelector('[role="menu"]')!
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(item('chat.download'))
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(f.trigger)
    expect(useNativeSurfaceOcclusionState().value).toBe(false)
  })

  it('offers ZIP without waiting for metadata and preserves its focus as other actions load', async () => {
    const f = await mount(source, false)
    let resolve!: (metadata: typeof info) => void
    f.content.workingFileMetadata.mockImplementation(() => new Promise(done => { resolve = done }))
    f.trigger.focus()
    f.trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    await settle()
    const menu = document.querySelector('[role="menu"]')!
    expect(document.activeElement).toBe(item('resourceActions.downloadZip'))
    expect(item('resourceActions.preview')).toBeUndefined()
    resolve(info)
    await settle()
    expect(document.activeElement).toBe(item('resourceActions.downloadZip'))
    expect(document.querySelectorAll('[role="menuitem"]')).toHaveLength(2)
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(f.trigger)
    expect(f.onOpen).not.toHaveBeenCalled()
  })
})


describe('compact workbench tab actions', () => {
  it('keeps only download and tab actions on Web, and downloads the exact targeted file', async () => {
    const f = await mount(markdown, false, false, true)
    await f.open()
    expect([...document.querySelectorAll('[role="menuitem"]')].map(el => el.textContent?.trim())).toEqual([i18n.global.t('chat.download'), 'Close tab'])
    item('chat.download')!.click(); await settle()
    expect(f.content.fetchArtifact).toHaveBeenCalledWith(markdown, expect.objectContaining({ sessionKey: f.props.sessionKey }))
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), 'notes.md')
    expect(f.content.workingFileMetadata).not.toHaveBeenCalled()
  })
  it.each([true, false])('reveals original files only with an owned ready local Gateway (owned=%s)', async owned => {
    mocks.platform.id = 'desktop'
    const sourceFileAction = vi.fn().mockResolvedValue({ ok: true })
    mocks.platform.files = { sourceFileAction }
    mocks.platform.gateway.getStatus.mockResolvedValue({ owned, status: 'ready' })
    const f = await mount(source, false, false, true); await f.open()
    const reveal = [...document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(el => /Finder|Explorer|file manager/i.test(el.textContent || ''))
    expect(Boolean(reveal)).toBe(owned)
    expect(item('resourceActions.openSource')).toBeUndefined()
    expect(item('resourceActions.downloadZip')).toBeUndefined()
    if (owned) {
      reveal!.click(); await settle()
      expect(sourceFileAction).toHaveBeenCalledWith(expect.objectContaining({ documentId: 'doc_one', pagePath: 'editorial.html', action: 'reveal', gatewayInstanceId: 'owned-one' }))
    }
  })
  it('opens collection/browser actions without borrowing a file from another tab', async () => {
    const f = await mount(markdown, false, false, true)
    f.props.artifact = undefined as unknown as ArtifactPayload
    await nextTick(); await f.open()
    expect(document.querySelectorAll('[role="menuitem"]')).toHaveLength(1)
    expect(document.querySelector('[role="menuitem"]')?.textContent).toBe('Close tab')
    ;(document.querySelector('[role="menuitem"]') as HTMLButtonElement).click(); await settle()
    expect(f.onAction).toHaveBeenCalledWith('close')
    expect(f.content.fetchArtifact).not.toHaveBeenCalled(); expect(f.content.workingFileMetadata).not.toHaveBeenCalled()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })
})
