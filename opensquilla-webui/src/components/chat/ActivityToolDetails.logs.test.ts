// @vitest-environment happy-dom
import { createApp, h, nextTick, reactive } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SESSION_INSPECTION_KEY, SessionInspectionLogNotReadyError, type SessionInspection } from '@/modules/sessionInspection'
import type { ChatToolCallRenderItem } from '@/types/chat'
import en from '@/locales/en.json'
import { HISTORY_DETAILS_READY } from '@/modules/historyDetails'
const mocks = vi.hoisted(() => ({ copy: vi.fn().mockResolvedValue(undefined) }))
vi.mock('@/utils/browser', () => ({ copyTextWithFallback: mocks.copy }))
import ActivityToolDetails from './ActivityToolDetails.vue'

const apps: ReturnType<typeof createApp>[] = []
function mount(readExecutionLog: SessionInspection['readExecutionLog'], operationKey = 'command.run', executionLogHandle = 'log1', overrides: Partial<ChatToolCallRenderItem> = {}, historyReady?: () => boolean | Promise<boolean>) {
  const call = reactive<ChatToolCallRenderItem>({ toolId: 'exec1', renderKey: 'message1:exec1:0', name: 'exec', displayName: 'Run command',
    inputRaw: '{"command":"npm test"}', inputPreview: '', result: 'saved preview', resultPreview: '', resultTruncated: true,
    executionLogHandle, isRunning: false, status: 'success', isError: false, isOpen: true, ...overrides })
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ render: () => h(ActivityToolDetails, { call, operationKey, label: 'Run command', sessionKey: 'session1' }) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en } }))
  app.provide(SESSION_INSPECTION_KEY, { readExecutionLog } as SessionInspection)
  if (historyReady) app.provide(HISTORY_DETAILS_READY, historyReady)
  app.mount(host)
  apps.push(app)
  return host
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; vi.clearAllMocks(); vi.useRealTimers() })

describe('inline persisted execution output', () => {
  it.each([
    ['complete', { result: 'Python 3.12.10\n', resultTruncated: false }, en.chat.copy],
    ['unknown old record', { result: 'Python 3.12.10\n', resultTruncated: undefined }, en.chat.copy],
    ['preview-only old record', { result: '', resultPreview: 'Python 3.12.10\n', resultTruncated: undefined }, en.shared.runTrace.copyPreview],
  ])('keeps %s output and copying independent of an expired optional log', async (_label, overrides, copyLabel) => {
    const read = vi.fn().mockRejectedValue(new Error('NOT_FOUND'))
    const host = mount(read, 'command.run', 'expired-log', overrides)
    await nextTick()
    expect(read).not.toHaveBeenCalled()
    expect(host.querySelector('.activity-tool-details__log-status')).toBeNull()
    expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('Python 3.12.10\n')
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await vi.waitFor(() => expect(mocks.copy).toHaveBeenCalledWith('$ npm test\n\nPython 3.12.10\n'))
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(copyLabel))
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"] polyline')?.getAttribute('points')).toBe('20 6 9 17 4 12')
    expect(host.querySelector('.activity-code-details__sr')?.textContent).toBe('')
    expect(read).not.toHaveBeenCalled()
  })

  it('reads the first page on expansion, advances near the bottom, and copies the redacted full log', async () => {
    const tail = 'OPENAI_API_KEY=secret\nlast'
    const total = 6 + tail.length
    const read = vi.fn().mockResolvedValueOnce({ handle: 'log1', content: 'first\n', offset: 0, nextOffset: 6, chars: total, complete: true })
      .mockResolvedValueOnce({ handle: 'log1', content: tail, offset: 6, nextOffset: null, chars: total, complete: true })
    const host = mount(read)
    await vi.waitFor(() => expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('first\n'))
    expect(read).toHaveBeenCalledTimes(1)
    const viewport = host.querySelector<HTMLElement>('.activity-code-details__viewport')!
    Object.defineProperties(viewport, { clientHeight: { value: 200 }, scrollHeight: { value: 250 }, scrollTop: { value: 50 } })
    viewport.dispatchEvent(new Event('scroll'))
    await vi.waitFor(() => expect(host.querySelector('.activity-code-details__output')?.textContent).toContain('last'))
    expect(read).toHaveBeenCalledTimes(2)
    const body = host.querySelector('.activity-code-details__output')?.textContent
    expect(body).toBe('first\nOPENAI_API_KEY=[redacted]\nlast')
    expect(body).not.toContain('saved preview')
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await vi.waitFor(() => expect(mocks.copy).toHaveBeenCalledWith('$ npm test\n\nfirst\nOPENAI_API_KEY=[redacted]\nlast'))
    expect(host.querySelector('.activity-code-details__note')).toBeNull()
  })

  it('retains the preview on a real log failure and retries inline', async () => {
    const read = vi.fn().mockRejectedValueOnce(new SessionInspectionLogNotReadyError())
      .mockResolvedValueOnce({ handle: 'log1', content: 'complete output', offset: 0, nextOffset: null, chars: 15, complete: true })
    const host = mount(read)
    await vi.waitFor(() => expect(host.querySelector('.activity-tool-details__log-status')).not.toBeNull())
    expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('saved preview')
    host.querySelector<HTMLButtonElement>('.activity-tool-details__log-status button')?.click()
    await vi.waitFor(() => expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('complete output'))
    expect(host.querySelector('.activity-tool-details__log-status')).toBeNull()
  })

  it('retains server-side incompleteness instead of claiming the stored log is complete', async () => {
    const host = mount(vi.fn().mockResolvedValue({ handle: 'log1', content: 'retained tail', offset: 0, nextOffset: null, chars: 13, complete: false }))
    await vi.waitFor(() => expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('retained tail'))
    expect(host.querySelector('.activity-code-details__note')?.textContent).toBe(en.shared.runTrace.codePartial)
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.shared.runTrace.copyPreview)
  })

  it('labels a generic retained log fragment as copying a preview', async () => {
    const host = mount(vi.fn().mockResolvedValue({ handle: 'log1', content: 'retained tail', offset: 0, nextOffset: null, chars: 13, complete: false }), 'code.python')
    await vi.waitFor(() => expect(host.textContent).toContain('retained tail'))
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.shared.runTrace.copyPreview)
  })

  it('waits for the owning history row before copying and refuses a failed history restore', async () => {
    let finish!: (ready: boolean) => void
    const ready = vi.fn(() => new Promise<boolean>(resolve => { finish = resolve }))
    const host = mount(vi.fn(), 'command.run', '', { resultTruncated: false }, ready)
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')!.click()
    expect(ready).toHaveBeenCalledTimes(1)
    expect(mocks.copy).not.toHaveBeenCalled()
    finish(false)
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.chat.toast.copyFailed))
    expect(mocks.copy).not.toHaveBeenCalled()
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')!.click()
    finish(true)
    await vi.waitFor(() => expect(mocks.copy).toHaveBeenCalledWith('$ npm test\n\nsaved preview'))
  })

  it('never reports copy success when loading the remaining saved log fails', async () => {
    const read = vi.fn().mockResolvedValueOnce({ handle: 'log1', content: 'first', offset: 0, nextOffset: 5, chars: 10, complete: true })
      .mockRejectedValueOnce(new Error('page unavailable'))
    const host = mount(read)
    await vi.waitFor(() => expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('first'))
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await vi.waitFor(() => expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')?.getAttribute('aria-label')).toBe(en.chat.toast.copyFailed))
    expect(mocks.copy).not.toHaveBeenCalled()
    expect(host.querySelector('.activity-code-details__output')?.textContent).toBe('first')
  })

  it('does not create a feedback timer after a pending copy resolves on an unmounted detail', async () => {
    vi.useFakeTimers()
    let completeCopy!: () => void
    mocks.copy.mockImplementationOnce(() => new Promise<void>(resolve => { completeCopy = resolve }))
    const host = mount(vi.fn().mockReturnValue(new Promise(() => {})), 'command.run', '')
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await Promise.resolve()
    apps.pop()!.unmount()
    completeCopy()
    await Promise.resolve()
    await nextTick()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('keeps generic input/result and appends the persisted log in the same result section', async () => {
    const host = mount(vi.fn().mockResolvedValue({ handle: 'log1', content: 'full saved output', offset: 0, nextOffset: null, chars: 17, complete: true }), 'code.python')
    await vi.waitFor(() => expect(host.textContent).toContain('full saved output'))
    expect(host.querySelectorAll('.activity-tool-details__section')).toHaveLength(2)
    expect(host.textContent).toContain('saved preview')
    expect(host.textContent).toContain('npm test')
    host.querySelector<HTMLButtonElement>('[data-testid="activity-tool-detail-copy"]')?.click()
    await vi.waitFor(() => expect(mocks.copy).toHaveBeenCalledWith('INPUT\n{"command":"npm test"}\n\nRESULT\nsaved preview\n\nfull saved output'))
  })
})
