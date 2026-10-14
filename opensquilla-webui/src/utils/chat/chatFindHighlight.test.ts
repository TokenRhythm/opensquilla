// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import TextPart from '@/components/chat/parts/TextPart.vue'
import i18n from '@/i18n'
import type { VisualizeReceipt } from '@/visualize/types'
import { CHAT_FIND_RANGE_LIMIT, collectChatFindRanges, createChatFindHighlighter } from './chatFindHighlight'

afterEach(() => {
  document.body.innerHTML = ''
  document.getElementById('opensquilla-chat-find-style')?.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function message(html: string) {
  const root = document.createElement('div')
  root.innerHTML = html
  document.body.append(root)
  return root
}

describe('chat find highlights', () => {
  it('searches both text segments around an actual canvas and excludes its host feedback', async () => {
    const root = message('')
    root.className = 'assistant-answer'
    const receipt = { callId: 'call_find', documentId: 'doc_find', revisionId: 'rev_find', artifactId: 'artifact_find', name: 'alpha.html', mime: 'text/html', sha256: '0'.repeat(64), size: 10, marker: 'visualize:doc_find:rev_find' } as VisualizeReceipt
    const app = createApp(TextPart, { sessionKey: 'session-find', visualizeReceipts: [receipt], part: {
      type: 'text', key: 'find', rawText: '', html: '<p>alpha before</p><p><code>visualize:doc_find:rev_find</code></p><p>alpha after</p>',
    } })
    app.use(i18n); app.mount(root)
    try {
      await nextTick(); await nextTick()
      expect(root.querySelector('[data-visualize-canvas]')).not.toBeNull()
      const canvas = root.querySelector('[data-chat-find-ignore]')!
      expect(canvas).not.toBeNull()
      const feedback = document.createElement('p'); feedback.textContent = 'alpha host feedback'; canvas.append(feedback)
      expect(collectChatFindRanges(root, 'alpha').map(range => range.startContainer.parentElement?.textContent)).toEqual(['alpha before', 'alpha after'])
      expect(root.querySelectorAll('[data-chat-find-body]')).toHaveLength(1)
    } finally { app.unmount() }
  })

  it('searches marked message bodies without reasoning, tools or attachment labels', () => {
    const root = message('<div>alpha tool</div><details open><summary>alpha</summary>alpha reasoning</details><div data-chat-find-body><p>alpha正文</p></div><div>alpha附件</div>')
    expect(collectChatFindRanges(root, 'alpha').map(range => range.startContainer.parentElement?.textContent)).toEqual(['alpha正文'])
  })

  it('prefers canonical answer body over intermediate timeline text', () => {
    const root = message('<div data-chat-find-body>alpha intermediate</div><div class="assistant-answer"><div data-chat-find-body>alpha answer</div></div>')
    expect(collectChatFindRanges(root, 'alpha').map(range => range.startContainer.parentElement?.textContent)).toEqual(['alpha answer'])
  })

  it('does not highlight a body inside a collapsed activity or UI-only message without a body', () => {
    const root = message('<div aria-hidden="true"><div data-chat-find-body>alpha hidden activity</div></div><div data-chat-find-body>alpha visible</div>')
    root.className = 'msg-ai'
    expect(collectChatFindRanges(root, 'alpha').map(range => range.startContainer.parentElement?.textContent)).toEqual(['alpha visible'])
    root.innerHTML = '<div>alpha tool status</div><span>alpha attachment</span>'
    expect(collectChatFindRanges(root, 'alpha')).toEqual([])
  })

  it('matches literal punctuation across Markdown inline nodes without changing framework DOM', () => {
    const root = message('<p>找 <strong>A.[</strong><em>B]</em>，a.[b]。</p>')
    const original = root.innerHTML
    const ranges = collectChatFindRanges(root, 'a.[b]')
    expect(ranges.map(range => range.toString())).toEqual(['A.[B]', 'a.[b]'])
    expect(root.innerHTML).toBe(original)
    expect(root.querySelector('mark')).toBeNull()
  })

  it('keeps UTF-16 offsets correct around emoji and case-insensitive matches', () => {
    const root = message('<p>😀STAR ⭐ star İ X</p>')
    expect(collectChatFindRanges(root, 'star').map(range => range.toString())).toEqual(['STAR', 'star'])
    expect(collectChatFindRanges(root, 'X')[0]?.toString()).toBe('X')
    expect(collectChatFindRanges(root, '😀STAR')[0]?.toString()).toBe('😀STAR')
  })

  it('does not invent matches spanning paragraphs or include controls and hidden content', () => {
    const root = message('<p>ab</p><p>cd</p><button>abcd</button><span hidden>abcd</span><span aria-hidden="true">abcd</span><div contenteditable="true">abcd</div><span data-chat-find-ignore>abcd</span><details><summary>title</summary>abcd</details><p>abcd</p>')
    expect(collectChatFindRanges(root, 'abcd').map(range => range.toString())).toEqual(['abcd'])
    expect(collectChatFindRanges(root, '   ')).toEqual([])
  })

  it('returns a message target for an explicit outline fallback when CSS Highlight is absent', () => {
    vi.stubGlobal('Highlight', undefined)
    const root = message('<p>alpha beta alpha</p>')
    const original = root.outerHTML
    const highlighter = createChatFindHighlighter()
    const result = highlighter.update(root, 'alpha', 1)
    expect(result.supported).toBe(false)
    expect(result.activeRange?.toString()).toBe('alpha')
    expect(result.fallbackTarget).toBe(root)
    expect(root.outerHTML).toBe(original)
    expect(highlighter.update(root, 'missing').fallbackTarget).toBeNull()
  })

  it('rebuilds ranges after a streaming update and prevents stale view cleanup from clearing a new view', () => {
    const registry = new Map<string, unknown>()
    class HighlightFixture {
      priority = 0
      constructor(public readonly ranges: Range[] = []) {}
    }
    const Highlight = vi.fn(function (...ranges: Range[]) { return new HighlightFixture(ranges) })
    vi.stubGlobal('CSS', { highlights: registry })
    vi.stubGlobal('Highlight', Highlight)
    const first = createChatFindHighlighter()
    const second = createChatFindHighlighter()
    const root = message('<p>first hit</p>')
    const firstNode = root.querySelector('p')!.firstChild
    expect(first.update(root, 'hit').ranges).toHaveLength(1)
    root.querySelector('p')!.append(' and hit')
    const updated = first.update(root, 'hit', 1)
    expect(updated.supported).toBe(true)
    expect(updated.ranges).toHaveLength(2)
    expect(root.querySelector('p')!.firstChild).toBe(firstNode)
    const another = message('<p>new hit</p>')
    second.update(another, 'hit')
    const current = registry.get('opensquilla-chat-find-active')
    first.clear()
    expect(registry.get('opensquilla-chat-find-active')).toBe(current)
    second.clear()
    expect(registry.size).toBe(0)
  })

  it('caps dense highlights and retains an active occurrence beyond the cap', async () => {
    const root = message('<div data-chat-find-body></div>')
    root.firstChild!.textContent = 'x '.repeat(5000)
    const createRange = vi.spyOn(document, 'createRange')
    const highlighter = createChatFindHighlighter()
    const result = await highlighter.updateAsync(root, 'x', 4999, { sourceText: 'x '.repeat(5000) })
    expect(result?.totalMatches).toBe(5000)
    expect(result?.ranges).toHaveLength(CHAT_FIND_RANGE_LIMIT)
    expect(result?.activeRange?.startOffset).toBe(9998)
    expect(result?.activeRange?.toString()).toBe('x')
    expect(createRange).toHaveBeenCalledTimes(CHAT_FIND_RANGE_LIMIT)
    highlighter.clear()
  })

  it('maps over a thousand inline text nodes and matches spanning batch boundaries', async () => {
    const root = message('<div data-chat-find-body></div>')
    const body = root.firstChild!
    for (let i = 0; i < 1500; i++) {
      const span = document.createElement('span')
      span.textContent = 'alpha.... '
      body.appendChild(span)
    }
    const highlighter = createChatFindHighlighter()
    const result = await highlighter.updateAsync(root, 'alpha', 1499)
    expect(result?.totalMatches).toBe(1500)
    expect(result?.activeRange?.startContainer).toBe(body.lastChild!.firstChild)
    expect(result?.ranges).toHaveLength(CHAT_FIND_RANGE_LIMIT)
    highlighter.clear()
    body.textContent = '.'.repeat(32767) + '😀STAR' + 'a'.repeat(32769)
    expect(collectChatFindRanges(root, '😀STAR')[0]?.toString()).toBe('😀STAR')
    const paired = await highlighter.updateAsync(root, 'aa', 16383)
    expect(paired?.totalMatches).toBe(16384)
    expect(paired?.activeRange?.toString()).toBe('aa')
    highlighter.clear()
  })

  it('reuses unchanged body/query projections and ignores unrelated activity updates', async () => {
    const root = message('<span class="activity">1 s</span><div data-chat-find-body>alpha alpha</div>')
    const invalidated = vi.fn(), highlighter = createChatFindHighlighter(invalidated)
    await highlighter.updateAsync(root, 'alpha', 0, { sourceText: 'alpha alpha' })
    const createRange = vi.spyOn(document, 'createRange')
    root.querySelector('.activity')!.textContent = '2 s'
    const next = await highlighter.updateAsync(root, 'alpha', 1, { sourceText: 'alpha alpha' })
    expect(next?.activeRange?.startOffset).toBe(6)
    expect(createRange).not.toHaveBeenCalled()
    expect(invalidated).not.toHaveBeenCalled()
    root.querySelector('[data-chat-find-body]')!.append(' alpha')
    const streamed = await highlighter.updateAsync(root, 'alpha', 2, { sourceText: 'alpha alpha alpha' })
    expect(streamed?.totalMatches).toBe(3)
    expect(streamed?.activeRange?.toString()).toBe('alpha')
    expect(invalidated).toHaveBeenCalledOnce()
    highlighter.clear()
  })

  it('invalidates when a body ancestor becomes hidden and excludes closed detail bodies', async () => {
    const root = message('<section><div data-chat-find-body>alpha</div></section><details><summary>title</summary><div data-chat-find-body>alpha hidden</div></details>')
    const highlighter = createChatFindHighlighter()
    expect((await highlighter.updateAsync(root, 'alpha'))?.totalMatches).toBe(1)
    root.querySelector('section')!.setAttribute('aria-hidden', 'true')
    expect((await highlighter.updateAsync(root, 'alpha'))?.totalMatches).toBe(0)
    highlighter.clear()
  })

  it('retains source-only fallback without collecting a raw match array', async () => {
    const root = message('<div data-chat-find-body><a href="https://alpha.example">alpha</a> alpha</div>')
    const highlighter = createChatFindHighlighter()
    const result = await highlighter.updateAsync(root, 'alpha', 1, { sourceText: '[alpha](https://alpha.example) alpha' })
    expect(result?.totalMatches).toBe(2)
    expect(result?.activeRange).toBeNull()
    expect(result?.ranges).toHaveLength(2)
    highlighter.clear()
  })

  it('yields during large work and cancels before publishing stale highlights', async () => {
    const root = message('<div data-chat-find-body></div>')
    root.firstChild!.textContent = 'x '.repeat(50000)
    let clock = 0
    vi.spyOn(performance, 'now').mockImplementation(() => clock += 4)
    const controller = new AbortController(), highlighter = createChatFindHighlighter()
    const pending = highlighter.updateAsync(root, 'x', 49999, { signal: controller.signal })
    // A timer gets a turn before the result, so composer/stop input can run too.
    const input = new Promise<void>(resolve => setTimeout(() => { controller.abort(); resolve() }, 0))
    await input
    expect(await pending).toBeNull()
    highlighter.clear()
  })
})
