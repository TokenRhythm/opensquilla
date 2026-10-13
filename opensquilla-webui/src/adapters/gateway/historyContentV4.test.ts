// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createHistoryContentReader } from './historyContentV4'
import { createPrivateHttpTransport } from './privateHttpTransport'

const ref = { version: 1 as const, sessionKey: 'key', sessionId: 'sid', messageId: 'mid',
  view: 'display' as const, source: 'active' as const, revision: 'legacy-v1:active:1:2:0:12' }

afterEach(() => { sessionStorage.clear(); vi.restoreAllMocks() })

describe('authenticated history content', () => {
  it('uses the current shared token for raw, display and details without changing the reference', async () => {
    sessionStorage.setItem('opensquilla.wsToken', 'first-synthetic-token')
    const fetcher = vi.fn(async (url: RequestInfo | URL, options?: RequestInit) => {
      const headers = new Headers(options?.headers)
      const expected = String(url).includes('view=details') ? 'second-synthetic-token' : 'first-synthetic-token'
      expect(headers.get('Authorization')).toBe(`Bearer ${expected}`)
      expect(options?.cache).toBe('no-store')
      expect(options?.credentials).toBe('same-origin')
      expect(options?.redirect).toBe('error')
      expect(String(url)).toContain('source=active&revision=legacy-v1%3Aactive%3A1%3A2%3A0%3A12')
      return new Response(String(url).includes('view=details')
        ? JSON.stringify({ message_id: 'mid', role: 'assistant', reasoning_content: '思考🙂', tool_calls: [] })
        : '正文🙂')
    })
    const reader = createHistoryContentReader(createPrivateHttpTransport({
      baseUrl: 'http://localhost/control/', fetch: fetcher,
    }))()
    expect([...await reader.read(ref, { limit: 12 })]).toEqual([...new TextEncoder().encode('正文🙂')])
    expect(new Headers(fetcher.mock.calls[0]?.[1]?.headers).get('Range')).toBe('bytes=0-11')
    await expect(reader.readDisplay(ref)).resolves.toBe('正文🙂')
    sessionStorage.setItem('opensquilla.wsToken', 'second-synthetic-token')
    await expect(reader.readDetails(ref)).resolves.toMatchObject({ reasoning: '思考🙂' })
  })

  it('keeps cancellation connected while the semantic response is streaming', async () => {
    let cancelled = false
    const fetcher = vi.fn(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode('prefix')) },
      cancel() { cancelled = true },
    })))
    const reader = createHistoryContentReader(createPrivateHttpTransport({
      baseUrl: 'http://localhost/', fetch: fetcher,
    }))()
    const controller = new AbortController()
    const pending = reader.readDisplay(ref, { signal: controller.signal })
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce())
    controller.abort()
    await expect(pending).rejects.toMatchObject({ kind: 'aborted' })
    expect(cancelled).toBe(true)
  })
})
