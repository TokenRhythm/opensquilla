// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import type { ChatToolCallRenderItem, ToolFileSnapshot } from '@/types/chat'
import en from '@/locales/en.json'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactContentAccess, type ArtifactWorkbench } from '@/modules/artifactWorkbench'

const mocks = vi.hoisted(() => ({
  open: vi.fn(), copy: vi.fn(),
  saveAttachment: vi.fn().mockResolvedValue({ status: 'saved' }),
  connection: vi.fn().mockResolvedValue({ status: 'ready', instanceId: 'gateway-one', authToken: 'owned-instance-token' }),
  requestBinary: vi.fn(),
}))
vi.mock('@/stores/app', () => ({ useAppStore: () => ({ features: { artifactWorkbench: true } }) }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: mocks.copy, downloadText: vi.fn() }))
vi.mock('@/platform', () => ({ getPlatform: () => ({ id: 'desktop', files: { saveAttachment: mocks.saveAttachment },
  gateway: { getConnection: mocks.connection } }) }))
vi.mock('@/workbench/toolHistoryFileItems', async importOriginal => ({
  ...await importOriginal<typeof import('@/workbench/toolHistoryFileItems')>(), openToolHistoryFile: mocks.open,
}))
import { downloadRetainedAttachment } from '@/adapters/gateway/attachmentDownloadV4'
import ActivityToolDetails from './ActivityToolDetails.vue'

const apps: ReturnType<typeof createApp>[] = []
const snapshot: ToolFileSnapshot = { kind: 'write', path: 'games/snake.html', content: '', legacy_ref: {
  message_id: 'saved-assistant-message', tool_use_id: 'saved-write-call', sha256: 'a'.repeat(64), original_chars: 400_000,
} }
function mount(readAttachmentPreview: ArtifactContentAccess['readAttachmentPreview']) {
  const tool: ChatToolCallRenderItem = {
    toolId: 'saved-write-call', renderKey: 'saved-assistant-message:saved-write-call:0', name: 'write_file', displayName: 'Write file',
    inputRaw: JSON.stringify({ path: 'games/snake.html', content: '[historical_tool_argument_omitted]\nhead: partial' }),
    inputPreview: '', result: 'Written successfully', resultPreview: '', status: 'success', isRunning: false,
    isError: false, isOpen: true, fileSnapshot: snapshot,
  }
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(ActivityToolDetails, { call: tool, sessionKey: 'session-a', operationKey: 'file.write', label: 'Write file' }) })
  app.use(createPinia())
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(ARTIFACT_WORKBENCH_KEY, { content: { readAttachmentPreview,
    downloadAttachment: (attachment, request) => downloadRetainedAttachment({ requestBinary: mocks.requestBinary }, attachment, request),
  } } as ArtifactWorkbench)
  app.mount(host)
  apps.push(app)
  return host
}
beforeEach(() => vi.clearAllMocks())
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); setActivePinia(undefined); document.body.innerHTML = ''; vi.unstubAllGlobals() })

describe('legacy file detail recovery actions', () => {
  it.each(['owned', 'external'] as const)('reveals recovered file actions and downloads from an %s Gateway without another preview read', async gateway => {
    mocks.connection.mockResolvedValue({ status: 'ready', instanceId: 'gateway-one',
      authToken: gateway === 'owned' ? 'owned-instance-token' : null })
    const originalBytes = new TextEncoder().encode('<!doctype html>\n<main>Complete original historical file</main>')
    const target = { write: vi.fn().mockResolvedValue(undefined), close: vi.fn().mockResolvedValue(undefined), abort: vi.fn() }
    const picker = vi.fn().mockResolvedValue({ createWritable: async () => target })
    vi.stubGlobal('showSaveFilePicker', picker)
    mocks.requestBinary.mockResolvedValue({ stream: () => new ReadableStream({ start(controller) {
      controller.enqueue(originalBytes)
      controller.close()
    } }) })
    let resolve!: (value: Awaited<ReturnType<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>>) => void
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>()
      .mockReturnValue(new Promise(done => { resolve = done }))
    const host = mount(read)
    expect(read).toHaveBeenCalledWith(expect.objectContaining({
      sha256_ref: snapshot.legacy_ref!.sha256,
      fileSnapshotRef: { messageId: 'saved-assistant-message', toolUseId: 'saved-write-call' },
    }), expect.objectContaining({ sessionKey: 'session-a', signal: expect.any(AbortSignal) }))
    expect(host.querySelector('[data-testid="activity-code-details"]')).toBeNull()
    expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).toBeNull()
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    expect(host.querySelector('.activity-tool-details')?.getAttribute('aria-busy')).toBe('true')
    expect(host.querySelector('.activity-tool-details__log-status')).toBeNull()
    expect(mocks.saveAttachment).not.toHaveBeenCalled()

    resolve({ content: '<!doctype html>\n<main>Recovered historical file</main>', truncated: true, totalBytes: 400_000 })
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).not.toBeNull())
    expect(host.textContent).toContain('Recovered historical file')
    expect(host.querySelector('.activity-tool-details')?.hasAttribute('aria-busy')).toBe(false)
    host.querySelector<HTMLButtonElement>('[data-testid="activity-code-open"]')!.click()
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ kind: 'write', path: 'games/snake.html', truncated: true }),
      expect.objectContaining({ sessionKey: 'session-a', toolId: 'saved-write-call', fileSnapshot: snapshot }))
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    if (gateway === 'owned') {
      await vi.waitFor(() => expect(mocks.saveAttachment).toHaveBeenCalledWith({
        gatewayInstanceId: 'gateway-one', sessionKey: 'session-a', sha256: snapshot.legacy_ref!.sha256, name: 'snake.html',
      }))
      expect(mocks.requestBinary).not.toHaveBeenCalled()
      expect(picker).not.toHaveBeenCalled()
    } else {
      await vi.waitFor(() => expect(target.close).toHaveBeenCalledOnce())
      expect(mocks.requestBinary).toHaveBeenCalledExactlyOnceWith(
        `/api/v1/attachments/${snapshot.legacy_ref!.sha256}?messageId=saved-assistant-message&toolUseId=saved-write-call`,
        expect.objectContaining({ sessionKey: 'session-a', timeoutMs: 0 }))
      expect(target.write).toHaveBeenCalledExactlyOnceWith(originalBytes)
      expect(target.abort).not.toHaveBeenCalled()
      expect(mocks.saveAttachment).not.toHaveBeenCalled()
    }
    expect(read).toHaveBeenCalledTimes(1)
    expect(mocks.copy).not.toHaveBeenCalled()
  })

  it('keeps download and workbench unavailable if historical recovery fails', async () => {
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>().mockRejectedValue(new Error('No verified retained body'))
    const host = mount(read)
    await vi.waitFor(() => expect(host.textContent).toContain(en.workspaceReference.unavailable))
    expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).toBeNull()
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    expect(mocks.saveAttachment).not.toHaveBeenCalled()
    expect(mocks.open).not.toHaveBeenCalled()
  })
})
