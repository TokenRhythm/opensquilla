// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadRetainedAttachment } from './attachmentDownloadV4'
import type { DisplayAttachment } from '@/types/chat'
const mocks = vi.hoisted(() => ({ platform: { id: 'web', files: {} as Record<string, unknown>, gateway: {} as Record<string, unknown> }, save: vi.fn() }))
vi.mock('@/platform', () => ({ getPlatform: () => mocks.platform }))
vi.mock('@/utils/streamDownload', () => ({ saveByteStream: mocks.save }))
const attachment: DisplayAttachment = { kind: 'file', renderKey: 'a', displayId: 'a', name: 'folder/test.txt', mime: 'text/plain', sha256_ref: 'a'.repeat(64), size: 100 }
afterEach(() => { vi.resetAllMocks(); mocks.platform.id = 'web'; mocks.platform.files = {}; mocks.platform.gateway = {} })
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
  it.each(['saved', 'cancelled'] as const)('returns native %s for an owned Gateway without HTTP fallback', async status => {
    mocks.platform.id = 'desktop'
    const save = vi.fn().mockResolvedValue({ status })
    mocks.platform.files = { saveAttachment: save }
    mocks.platform.gateway = { getConnection: async () => ({ status: 'ready', instanceId: 'gateway-a', authToken: 'owned-instance-token' }) }
    const http = { requestBinary: vi.fn() }
    expect(await downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).toBe(status)
    expect(save).toHaveBeenCalledWith({ gatewayInstanceId: 'gateway-a', sessionKey: 'session-a', sha256: 'a'.repeat(64), name: 'test.txt' })
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it.each([null, undefined, ''])('streams external Gateway downloads with authToken=%s through authenticated HTTP', async authToken => {
    mocks.platform.id = 'desktop'
    const nativeSave = vi.fn().mockRejectedValue(new Error('Native save only supports owned Gateway'))
    mocks.platform.files = { saveAttachment: nativeSave }
    mocks.platform.gateway = { getConnection: async () => ({ status: 'ready', instanceId: 'external-a', authToken }) }
    const stream = new ReadableStream<Uint8Array>()
    const http = { requestBinary: vi.fn().mockResolvedValue({ stream: () => stream }) }
    const controller = new AbortController()
    mocks.save.mockImplementation(async (_name, source) => { expect(await source()).toBe(stream); return 'saved' })
    expect(await downloadRetainedAttachment(http, { ...attachment,
      fileSnapshotRef: { messageId: 'message-1', toolUseId: 'write-1' } },
    { sessionKey: 'session-a', signal: controller.signal })).toBe('saved')
    expect(nativeSave).not.toHaveBeenCalled()
    expect(http.requestBinary).toHaveBeenCalledExactlyOnceWith(
      `/api/v1/attachments/${'a'.repeat(64)}?messageId=message-1&toolUseId=write-1`,
      { sessionKey: 'session-a', signal: controller.signal, timeoutMs: 0 })
    expect(mocks.save).toHaveBeenCalledWith('test.txt', expect.any(Function),
      expect.objectContaining({ expectedBytes: 100, signal: controller.signal }))
  })
  it('propagates a native save failure without retrying through HTTP', async () => {
    mocks.platform.id = 'desktop'
    const failure = new Error('Attachment save failed')
    mocks.platform.files = { saveAttachment: vi.fn().mockRejectedValue(failure) }
    mocks.platform.gateway = { getConnection: async () => ({ status: 'ready', instanceId: 'gateway-a', authToken: 'owned-instance-token' }) }
    const http = { requestBinary: vi.fn() }
    await expect(downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).rejects.toBe(failure)
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it.each(['owned-instance-token', null])('honors cancellation while resolving connection with token=%s', async authToken => {
    mocks.platform.id = 'desktop'
    const controller = new AbortController()
    const nativeSave = vi.fn()
    mocks.platform.files = { saveAttachment: nativeSave }
    mocks.platform.gateway = { getConnection: async () => {
      controller.abort()
      return { status: 'ready', instanceId: 'gateway-a', authToken }
    } }
    const http = { requestBinary: vi.fn() }
    await expect(downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a', signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
    expect(nativeSave).not.toHaveBeenCalled()
    expect(http.requestBinary).not.toHaveBeenCalled()
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it.each([
    { status: 'starting', instanceId: null, authToken: null },
    { status: 'ready', instanceId: null, authToken: 'owned-instance-token' },
  ])('rejects unavailable or unbound desktop connections: %j', async connection => {
    mocks.platform.id = 'desktop'
    const nativeSave = vi.fn()
    mocks.platform.files = { saveAttachment: nativeSave }
    mocks.platform.gateway = { getConnection: async () => connection }
    const http = { requestBinary: vi.fn() }
    await expect(downloadRetainedAttachment(http, attachment, { sessionKey: 'session-a' })).rejects.toThrow('Gateway unavailable')
    expect(nativeSave).not.toHaveBeenCalled()
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
