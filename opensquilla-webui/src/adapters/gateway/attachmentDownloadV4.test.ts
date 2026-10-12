// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadRetainedAttachment } from './attachmentDownloadV4'
import type { DisplayAttachment } from '@/types/chat'
const mocks = vi.hoisted(() => ({ platform: { id: 'web', files: {} as Record<string, unknown>, gateway: {} as Record<string, unknown> }, save: vi.fn() }))
vi.mock('@/platform', () => ({ getPlatform: () => mocks.platform }))
vi.mock('@/utils/streamDownload', () => ({ saveByteStream: mocks.save }))
const attachment: DisplayAttachment = { kind: 'file', renderKey: 'a', displayId: 'a', name: 'folder/test.txt', mime: 'text/plain', sha256_ref: 'a'.repeat(64), size: 100 }
afterEach(() => { vi.clearAllMocks(); mocks.platform.id = 'web'; mocks.platform.files = {}; mocks.platform.gateway = {} })
describe('retained material download', () => {
  it('keeps the exact historical locator for a web download with unknown original UTF-8 size', async () => {
    const stream = new ReadableStream<Uint8Array>()
    const http = { requestBinary: vi.fn().mockResolvedValue({ stream: () => stream }) }
    mocks.save.mockImplementation(async (_name, source) => { expect(await source()).toBe(stream); return 'saved' })
    await downloadRetainedAttachment(http, { ...attachment, size: undefined,
      fileSnapshotRef: { messageId: 'message-1', toolUseId: 'write-1' } }, { sessionKey: 'session-a' })
    expect(http.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'a'.repeat(64)}?messageId=message-1&toolUseId=write-1`,
      expect.objectContaining({ sessionKey: 'session-a' }))
    expect(mocks.save).toHaveBeenCalledWith('test.txt', expect.any(Function), expect.objectContaining({ expectedBytes: undefined }))
  })
  it('streams through authenticated transport with no session or token in URL', async () => {
    const stream = new ReadableStream<Uint8Array>()
    const http = { requestBinary: vi.fn().mockResolvedValue({ stream: () => stream }) }
    mocks.save.mockImplementation(async (_name, source) => { expect(await source()).toBe(stream); return 'saved' })
    expect(await downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).toBe('saved')
    expect(http.requestBinary).toHaveBeenCalledWith(`/api/v1/attachments/${'a'.repeat(64)}`, { sessionKey: 'session-a', signal: undefined, timeoutMs: 0 })
    expect(mocks.save).toHaveBeenCalledWith('test.txt', expect.any(Function), expect.objectContaining({ expectedBytes: 100 }))
  })
  it('passes only identifiers to native streaming saving, never renderer file bytes', async () => {
    mocks.platform.id = 'desktop'
    const save = vi.fn().mockResolvedValue({ status: 'saved' })
    mocks.platform.files = { saveAttachment: save }
    mocks.platform.gateway = { getConnection: async () => ({ status: 'ready', instanceId: 'gateway-a' }) }
    const http = { requestBinary: vi.fn() }
    expect(await downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).toBe('saved')
    expect(save).toHaveBeenCalledWith({ gatewayInstanceId: 'gateway-a', sessionKey: 'session-a', sha256: 'a'.repeat(64), name: 'test.txt' })
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it('refuses arbitrary download URLs and missing session ownership', async () => {
    const http = { requestBinary: vi.fn() }
    await expect(downloadRetainedAttachment(http, { ...attachment, download_url: 'https://example.com/file' }, { sessionKey: 'a' })).rejects.toThrow('Invalid')
    await expect(downloadRetainedAttachment(http, attachment)).rejects.toThrow('Invalid')
    expect(http.requestBinary).not.toHaveBeenCalled()
  })
})
