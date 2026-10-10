// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, ref, type App } from 'vue'
import { HISTORY_CONTENT_READER_KEY } from '@/modules/historyContent'
import { ContentRangeCache } from '@/utils/chat/contentRangeCache'
import type { HistoryMessageContent } from '@/utils/chat/historyMessageContent'
import { useChatMessageContent } from './useChatMessageContent'

const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

function fixture(fetcher: typeof fetch) {
  const session = ref('session-a')
  const factory = vi.fn(() => new ContentRangeCache({ fetcher, chunkBytes: 4096 }))
  let api!: ReturnType<typeof useChatMessageContent>
  const app = createApp({ setup() { api = useChatMessageContent(session); return () => h('div') } })
  app.provide(HISTORY_CONTENT_READER_KEY, factory)
  app.mount(document.body.appendChild(document.createElement('div')))
  apps.push(app)
  const message: HistoryMessageContent = {
    role: 'user', text: 'preview', messageId: 'user-a', previewComplete: false,
    contentRef: { version: 1, sessionKey: session.value, sessionId: 'physical-a', messageId: 'user-a',
      source: 'compacted', view: 'display', revision: 'revision-a' },
  }
  return { ...api, message, factory, session }
}

describe('message action content reader', () => {
  it('uses the provided transport with the full scoped display identity', async () => {
    const body = '0123456789'.repeat(2048) + 'FULL_TAIL'
    const fetcher = vi.fn<typeof fetch>(async () => new Response(body))
    const { readMessageText, message, factory } = fixture(fetcher)
    const signal = new AbortController().signal
    expect(await readMessageText(message, signal)).toBe(body)
    expect(factory).toHaveBeenCalledOnce()
    const [url, init] = fetcher.mock.calls[0]!
    const query = new URL(String(url), 'http://localhost').searchParams
    expect(Object.fromEntries(query)).toEqual({ sessionKey: 'session-a', sessionId: 'physical-a', messageId: 'user-a',
      source: 'compacted', revision: 'revision-a', view: 'display', export: '1' })
    expect(init).toMatchObject({ signal, cache: 'no-store' })
  })

  it('reads every bounded raw range and preserves a multibyte tail', async () => {
    const body = '0123456789'.repeat(2048) + '🙂FULL_TAIL'
    const bytes = new TextEncoder().encode(body)
    const fetcher = vi.fn<typeof fetch>(async url => {
      const query = new URL(String(url), 'http://localhost').searchParams
      const offset = Number(query.get('offset'))
      const limit = Number(query.get('limit'))
      return new Response(bytes.slice(offset, offset + limit), { status: 206 })
    })
    const { readMessageText, message } = fixture(fetcher)
    message.contentRef = { ...message.contentRef!, view: 'raw', byteLength: bytes.length }
    expect(await readMessageText(message, new AbortController().signal)).toBe(body)
    expect(fetcher).toHaveBeenCalledTimes(Math.ceil(bytes.length / 4096))
    expect(fetcher.mock.calls.every(([, init]) => Boolean((init?.headers as Record<string, string>).Range))).toBe(true)
  })

  it('rejects a truncated raw stream instead of returning its preview', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response('partial', { status: 206 }))
      .mockImplementation(async () => new Response('', { status: 206 }))
    const { readMessageText, message } = fixture(fetcher)
    message.contentRef = { ...message.contentRef!, view: 'raw', byteLength: 20000 }
    await expect(readMessageText(message, new AbortController().signal)).rejects.toThrow('empty range')
  })

  it.each(['revision', 'session', 'message', 'raw-assistant'] as const)('rejects an unsafe %s reference before reading', async kind => {
    const fetcher = vi.fn<typeof fetch>()
    const { readMessageText, message, session } = fixture(fetcher)
    if (kind === 'revision') message.contentRef!.revision = undefined
    if (kind === 'session') session.value = 'other'
    if (kind === 'message') message.contentRef!.messageId = 'other'
    if (kind === 'raw-assistant') { message.role = 'assistant'; message.contentRef!.view = 'raw' }
    await expect(readMessageText(message, new AbortController().signal)).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('applies semantic codepoint slices after reading the complete display body', async () => {
    const { readMessageText, message } = fixture(async () => new Response('A😀完整🙂Z'))
    message.contentSlice = { startCodepoint: 1, endCodepoint: 5 }
    expect(await readMessageText(message, new AbortController().signal)).toBe('😀完整🙂')
    message.contentSlice.endCodepoint = 100
    await expect(readMessageText(message, new AbortController().signal)).rejects.toThrow('Incomplete')
  })

  it('rejects a result after cancellation even when a transport ignores its signal', async () => {
    let resolve!: (response: Response) => void
    const { readMessageText, message } = fixture(() => new Promise(done => { resolve = done }))
    const controller = new AbortController()
    const pending = readMessageText(message, controller.signal)
    controller.abort()
    resolve(new Response('late full body'))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
})
