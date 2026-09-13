// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import ResourceActionsMenu from './ResourceActionsMenu.vue'

const mocks = vi.hoisted(() => ({
  platform: { id: 'web', files: {} as Record<string, unknown>, gateway: {
    getStatus: vi.fn(), getConnection: vi.fn(),
  } }, copy: vi.fn(), download: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/platform', () => ({ usePlatform: () => mocks.platform }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: mocks.copy, downloadBlob: mocks.download }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: mocks.toast }) }))

const source = { source: 'workspace-preview', documentId: 'doc_one', name: 'index.html',
  mime: 'text/html', previewPagePath: 'editorial.html' }
const info = { documentId: 'doc_one', pagePath: 'editorial.html', sourcePath: '/task/site/editorial.html',
  workspace: '/task', name: 'editorial.html', mime: 'text/html', size: 19 }
const apps: App[] = []
async function settle() { for (let i = 0; i < 12; i++) await nextTick() }
async function mount(artifact = source) {
  const content = { workingFileMetadata: vi.fn().mockResolvedValue(info),
    fetchWorkingFile: vi.fn().mockResolvedValue(new Blob(['<h1>current</h1>'], { type: 'text/html' })),
    fetchArtifact: vi.fn().mockResolvedValue({ ok: true, blob: new Blob(['old version'], { type: 'text/plain' }) }),
  }
  const access = reactive({ subscriptionEpoch: 1 })
  const props = reactive({ artifact, sessionKey: 'agent:main:webchat:one', trigger: true })
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
})

describe('shared resource actions', () => {
  it('keeps the specific page and session for preview, current copy, path and download', async () => {
    const f = await mount()
    await f.open()
    item('chat.open')!.click()
    expect(f.onOpen).toHaveBeenCalledWith(source)
    await f.open()
    item('resourceActions.copyContents')!.click()
    await settle()
    expect(mocks.copy).toHaveBeenCalledWith('<h1>current</h1>')
    expect(f.content.fetchWorkingFile).toHaveBeenCalledWith(expect.objectContaining({
      documentId: 'doc_one', pagePath: 'editorial.html', sessionKey: f.props.sessionKey,
    }))
    await f.open()
    item('resourceActions.copyGatewayPath')!.click()
    await settle()
    expect(mocks.copy).toHaveBeenCalledWith(info.sourcePath)
    await f.open()
    item('chat.download')!.click()
    await settle()
    expect(mocks.download).toHaveBeenCalledWith(expect.any(Blob), 'editorial.html')
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it('never substitutes current document bytes for a historical delivery', async () => {
    const artifact = { ...source, source: 'publish_artifact', id: 'old-artifact', revisionId: 'old-revision' }
    const f = await mount(artifact)
    await f.open()
    expect(item('resourceActions.copyGatewayPath')).toBeUndefined()
    item('resourceActions.copyContents')!.click()
    await settle()
    expect(mocks.copy).toHaveBeenCalledWith('old version')
    expect(f.content.fetchArtifact).toHaveBeenCalledWith(artifact, expect.objectContaining({ sessionKey: f.props.sessionKey }))
    expect(f.content.workingFileMetadata).not.toHaveBeenCalled()
    expect(f.content.fetchWorkingFile).not.toHaveBeenCalled()
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
    expect(item('chat.open')).toBeDefined()
    expect(item('chat.download')).toBeUndefined()
    expect(item('resourceActions.copyContents')).toBeUndefined()
    expect(f.content.fetchArtifact).not.toHaveBeenCalled()
  })

  it('fences a delayed copy when connection or session changes', async () => {
    const f = await mount()
    let resolve!: (blob: Blob) => void
    f.content.fetchWorkingFile.mockImplementation(() => new Promise(done => { resolve = done }))
    await f.open()
    item('resourceActions.copyContents')!.click()
    await nextTick()
    f.access.subscriptionEpoch++
    await nextTick()
    resolve(new Blob(['stale'], { type: 'text/html' }))
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
    const f = await mount()
    f.content.fetchWorkingFile.mockResolvedValue(blob)
    await f.open()
    item('resourceActions.copyContents')!.click()
    await settle()
    expect(mocks.copy).not.toHaveBeenCalled()
    expect(mocks.toast).toHaveBeenCalledWith(i18n.global.t('resourceActions.textOnly'), { tone: 'danger' })
    await f.open()
    item('chat.download')!.click()
    await settle()
    expect(mocks.download).toHaveBeenCalledWith(blob, 'editorial.html')
  })

  it('supports keyboard navigation, restores focus and occludes native preview only while open', async () => {
    const f = await mount()
    f.trigger.focus()
    f.trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    await settle()
    expect(useNativeSurfaceOcclusionState().value).toBe(true)
    const menu = document.querySelector('[role="menu"]')!
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
    expect(document.activeElement).toBe(item('resourceActions.copyContents'))
    menu.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await nextTick()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(f.trigger)
    expect(useNativeSurfaceOcclusionState().value).toBe(false)
  })
})
