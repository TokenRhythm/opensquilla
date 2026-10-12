// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import type { ChatToolCallRenderItem } from '@/types/chat'
import en from '@/locales/en.json'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactContentAccess, type ArtifactWorkbench } from '@/modules/artifactWorkbench'

const mocks = vi.hoisted(() => ({
  features: { artifactWorkbench: true },
  open: vi.fn(), download: vi.fn().mockResolvedValue(undefined), copy: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/stores/app', () => ({ useAppStore: () => ({ features: mocks.features }) }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: mocks.copy, downloadText: mocks.download }))
vi.mock('@/workbench/toolHistoryFileItems', async importOriginal => ({
  ...await importOriginal<typeof import('@/workbench/toolHistoryFileItems')>(),
  openToolHistoryFile: mocks.open,
}))
import ActivityToolDetails from './ActivityToolDetails.vue'

const apps: ReturnType<typeof createApp>[] = []
function call(overrides: Partial<ChatToolCallRenderItem> = {}): ChatToolCallRenderItem {
  return { toolId: 'actual-id', renderKey: 'message-2:actual-id:0', name: 'write_file', displayName: 'Write file',
    inputRaw: '{"path":"a.ts","content":"const x = 1"}', inputPreview: '', result: 'Written 11 bytes',
    resultPreview: '', status: 'success', isRunning: false, isError: false, isOpen: true, ...overrides }
}
function mount(toolCall: ChatToolCallRenderItem, sessionKey: string | undefined = 'session-a', operationKey = 'file.write', pinia = true,
  readAttachmentPreview?: ArtifactContentAccess['readAttachmentPreview']) {
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(ActivityToolDetails, { call: toolCall, sessionKey, operationKey, label: 'Write file' }) })
  if (pinia) app.use(createPinia())
  if (readAttachmentPreview) app.provide(ARTIFACT_WORKBENCH_KEY, { content: { readAttachmentPreview, downloadAttachment: mocks.download } } as unknown as ArtifactWorkbench)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.mount(host)
  apps.push(app)
  return host
}
beforeEach(() => { mocks.features.artifactWorkbench = true; vi.clearAllMocks(); mocks.copy.mockResolvedValue(undefined) })
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); setActivePinia(undefined); document.body.innerHTML = '' })

describe('historical file action boundary', () => {
  it('ignores duplicate download clicks and late failures after the displayed record changes', async () => {
    const snapshot = { kind: 'write' as const, path: 'large.ts', content: '', sha256_ref: 'a'.repeat(64), bytes: 8_000_000 }
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>()
      .mockResolvedValue({ content: 'bounded preview', truncated: true, totalBytes: snapshot.bytes })
    let reject!: (cause: Error) => void
    mocks.download.mockReturnValueOnce(new Promise((_resolve, rejectDownload) => { reject = rejectDownload }))
    const tool = reactive(call({ fileSnapshot: snapshot }))
    const host = mount(tool, 'session-a', 'file.write', true, read)
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).not.toBeNull())
    const button = host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!
    button.click()
    button.click()
    expect(mocks.download).toHaveBeenCalledTimes(1)
    tool.toolId = 'next-call'
    tool.renderKey = 'message:next-call'
    await nextTick()
    reject(new Error('late failure'))
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()
    expect(host.textContent).not.toContain(en.shared.runTrace.downloadContentFailed)
  })
  it('downloads the full retained attachment for a truncated preview without copying its prefix', async () => {
    const snapshot = { kind: 'write' as const, path: 'large.ts', content: '', sha256_ref: 'a'.repeat(64), bytes: 8_000_000 }
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>()
      .mockResolvedValue({ content: 'bounded preview', truncated: true, totalBytes: snapshot.bytes })
    const host = mount(call({ fileSnapshot: snapshot }), 'session-a', 'file.write', true, read)
    await vi.waitFor(() => expect(host.textContent).toContain('bounded preview'))
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')).toBeNull()
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    await vi.waitFor(() => expect(mocks.download).toHaveBeenCalledWith(expect.objectContaining({ sha256_ref: snapshot.sha256_ref }), { sessionKey: 'session-a' }))
    expect(mocks.copy).not.toHaveBeenCalled()
    host.querySelector<HTMLButtonElement>('[data-testid="activity-code-open"]')!.click()
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ truncated: true, content: 'bounded preview' }),
      expect.objectContaining({ fileSnapshot: snapshot }))
  })

  it('downloads complete already-received inline text while rendering only the bounded prefix', async () => {
    const content = `${'a retained line\n'.repeat(3000)}LAST LINE`
    const host = mount(call({ fileSnapshot: { kind: 'write', path: 'large.ts', content } }))
    expect(host.textContent).not.toContain('LAST LINE')
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    await vi.waitFor(() => expect(mocks.download).toHaveBeenCalledWith('large.ts', 'text/plain;charset=utf-8', content))
    expect(mocks.copy).not.toHaveBeenCalled()
  })

  it('reports a download failure without changing the successful file operation', async () => {
    const snapshot = { kind: 'write' as const, path: 'large.ts', content: '', sha256_ref: 'a'.repeat(64), bytes: 8_000_000 }
    const read = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>()
      .mockResolvedValue({ content: 'bounded preview', truncated: true, totalBytes: snapshot.bytes })
    mocks.download.mockRejectedValueOnce(new Error('download failed'))
    const host = mount(call({ fileSnapshot: snapshot }), 'session-a', 'file.write', true, read)
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-download"]')).not.toBeNull())
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-download"]')!.click()
    await vi.waitFor(() => expect(host.textContent).toContain(en.shared.runTrace.downloadContentFailed))
    expect(host.textContent).toContain('bounded preview')
  })
  it('shows loading instead of a partial body, then copies and opens the complete retained attachment', async () => {
    let resolve!: (value: Awaited<ReturnType<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>>) => void
    const readAttachmentPreview = vi.fn<NonNullable<ArtifactContentAccess['readAttachmentPreview']>>().mockReturnValue(new Promise(done => { resolve = done }))
    const body = `${'a complete line\n'.repeat(500)}LAST LINE`
    const blob = new Blob([body])
    const host = mount(call({ fileSnapshot: { kind: 'write', path: 'a.ts', content: 'MISLEADING PREFIX',
      sha256_ref: 'a'.repeat(64), bytes: blob.size } }), 'session-a', 'file.write', true, readAttachmentPreview)
    expect(host.textContent).not.toContain('MISLEADING PREFIX')
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')).toBeNull()
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    resolve({ content: body, truncated: false, totalBytes: blob.size })
    await vi.waitFor(() => expect(host.textContent).toContain('LAST LINE'))
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')!.click()
    await vi.waitFor(() => expect(mocks.copy).toHaveBeenCalledWith(body))
    host.querySelector<HTMLButtonElement>('[data-testid="activity-code-open"]')!.click()
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ content: body }),
      expect.objectContaining({ sessionKey: 'session-a', toolId: 'actual-id' }))
  })
  it('opens only the recorded file with real session and tool identity', () => {
    const host = mount(call())
    host.querySelector<HTMLButtonElement>('[data-testid="activity-code-open"]')?.click()
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ kind: 'write', path: 'a.ts', content: 'const x = 1' }),
      { sessionKey: 'session-a', toolId: 'actual-id', recordKey: 'message-2:actual-id:0', title: 'Write file' })
  })

  it('copies and opens the complete durable snapshot when model arguments are omitted', async () => {
    const content = `${'const retained = 1;\n'.repeat(500)}// final line`
    const host = mount(call({ inputRaw: JSON.stringify({ path: 'a.ts',
      content: '[historical_tool_argument_omitted]\nhead: partial' }),
      fileSnapshot: { kind: 'write', path: 'a.ts', content } }))
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await nextTick()
    expect(mocks.copy).toHaveBeenCalledWith(content)
    host.querySelector<HTMLButtonElement>('[data-testid="activity-code-open"]')?.click()
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({ kind: 'write', path: 'a.ts', content }),
      expect.objectContaining({ sessionKey: 'session-a', toolId: 'actual-id' }))
    expect(host.querySelector('.activity-code-details__viewport')).not.toBeNull()
  })

  it.each([
    ['missing real tool id', { toolId: '' }],
    ['running invocation', { isRunning: true }],
    ['unknown completion', { status: '' }],
    ['failed invocation', { status: 'error', isError: true }],
  ])('keeps %s from opening a completed-file view', (_label, overrides) => {
    const host = mount(call(overrides as Partial<ChatToolCallRenderItem>))
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
  })

  it('preserves inline content without an enabled workbench or mounted store', () => {
    mocks.features.artifactWorkbench = false
    const disabled = mount(call())
    expect(disabled.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    expect(disabled.textContent).toContain('const x = 1')
    setActivePinia(undefined)
    const standalone = mount(call(), 'session-a', 'file.write', false)
    expect(standalone.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    expect(standalone.textContent).toContain('const x = 1')
  })

  it('does not substitute a rendered key for missing session or real tool id', () => {
    const host = mount(call(), '')
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
  })

  it('never routes command logs to the file workbench', () => {
    const host = mount(call({ name: 'exec', inputRaw: '{"command":"echo hi"}', result: 'hi' }), 'session-a', 'command.run')
    expect(host.querySelector('[data-testid="activity-code-open"]')).toBeNull()
    expect(host.textContent).toContain('echo hi')
  })

  it('surfaces copy failure without dropping the retained content', async () => {
    mocks.copy.mockRejectedValueOnce(new Error('clipboard denied'))
    const host = mount(call())
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await Promise.resolve()
    await nextTick()
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.chat.toast.copyFailed)
    expect(host.textContent).toContain('const x = 1')
  })
})
