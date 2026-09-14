// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
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
async function mount(artifact: ArtifactPayload = source, previewable = true) {
  const content = { workingFileMetadata: vi.fn().mockResolvedValue(info),
    fetchWorkingFile: vi.fn().mockResolvedValue(new Blob(['<h1>current</h1>'], { type: 'text/html' })),
    fetchArtifact: vi.fn().mockResolvedValue({ ok: true, blob: new Blob(['old version'], { type: 'text/plain' }) }),
  }
  const access = reactive({ subscriptionEpoch: 1 })
  const props = reactive({ artifact, sessionKey: 'agent:main:webchat:one', trigger: true, previewable })
  const onOpen = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(ResourceActionsMenu, { ...props, onOpen }) })
  app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, { content } as never)
  app.provide(GATEWAY_ACCESS_KEY, access as never)
  apps.push(app)
  app.mount(host)
  await nextTick()
  const trigger = host.querySelector('button')!
  const open = async () => { trigger.click(); await settle() }
  return { content, access, props, onOpen, trigger, open }
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
})

describe('shared resource actions', () => {
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
  ])('shows exactly four local HTML actions with the native label for %s', async (userAgent, revealKey) => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(userAgent)
    mocks.platform.id = 'desktop'
    const action = vi.fn().mockResolvedValue(undefined)
    mocks.platform.files = { sourceFileAction: action, saveArtifact: vi.fn() }
    const f = await mount()
    await f.open()
    expect([...document.querySelectorAll('[role="menuitem"]')].map(el => el.textContent?.trim())).toEqual([
      'resourceActions.preview', 'resourceActions.openSource', revealKey, 'resourceActions.saveAs',
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

  it('omits in-app preview in the toolbar and focuses an action after metadata loads', async () => {
    const f = await mount(source, false)
    let resolve!: (metadata: typeof info) => void
    f.content.workingFileMetadata.mockImplementation(() => new Promise(done => { resolve = done }))
    f.trigger.focus()
    f.trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    await settle()
    const menu = document.querySelector('[role="menu"]')!
    expect(document.activeElement).toBe(menu)
    expect(item('resourceActions.preview')).toBeUndefined()
    resolve(info)
    await settle()
    expect(document.activeElement).toBe(item('chat.download'))
    expect(document.querySelectorAll('[role="menuitem"]')).toHaveLength(1)
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.activeElement).toBe(f.trigger)
    expect(f.onOpen).not.toHaveBeenCalled()
  })
})
