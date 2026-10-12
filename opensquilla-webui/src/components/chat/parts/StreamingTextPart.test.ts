// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, ref, type Ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createI18n } from 'vue-i18n'
import StreamingTextPart from './StreamingTextPart.vue'
import { useChatTextRendering, type RenderMarkdownOptions } from '@/composables/chat/useChatTextRendering'
import hljs from 'highlight.js/lib/common'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

// The component pulls decoration labels (copy buttons, mermaid chrome) from
// vue-i18n; the host app must install it before mount.
function mountI18n(app: ReturnType<typeof createApp>): void {
  app.use(createI18n({ legacy: false, missingWarn: false, messages: {} }))
}

function mountStream(raw: Ref<string>, renderMarkdown: (text: string, options?: RenderMarkdownOptions) => string): HTMLElement {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(defineComponent(() => () => h(StreamingTextPart, { rawText: raw.value, renderMarkdown })))
  mountI18n(app)
  app.mount(host)
  apps.push(app)
  return host
}

describe('StreamingTextPart', () => {
  it.each([
    ['shorter backtick fence', '````text\nbody\n```\n\nmore\n', '````\n\n'],
    ['different fence character', '```text\nbody\n~~~\n\nmore\n', '```\n\n'],
    ['fence text after marker', '```text\nbody\n```still source\n\nmore\n', '```\n\n'],
    ['nested backtick example in tilde fence', '~~~~text\n```js\n\nsource\n```\n\n', '~~~~\n\n'],
  ])('waits for a real close around %s', async (_name, open, close) => {
    const raw = ref(open)
    const renderMarkdown = vi.fn((_text: string) => '<p>committed</p>')
    mountStream(raw, renderMarkdown)
    await nextTick()
    expect(renderMarkdown).not.toHaveBeenCalled()
    raw.value += close
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(renderMarkdown.mock.calls[0]?.[0]).toBe(open + close)
  })

  it('keeps bracket display math intact across blank lines', async () => {
    const open = '\\[\na + b\n\nc + d\n'
    const raw = ref(open)
    const renderMarkdown = vi.fn((_text: string) => '<p>formula</p>')
    mountStream(raw, renderMarkdown)
    await nextTick()
    expect(renderMarkdown).not.toHaveBeenCalled()
    raw.value += '\\]\n\n'
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(renderMarkdown.mock.calls[0]?.[0]).toBe(open + '\\]\n\n')
  })

  it('retains the blank-line commit boundary after a fenced list item', async () => {
    const firstItem = '- Example:\n  ```text\n  source\n  ```\n'
    const raw = ref(firstItem)
    const renderMarkdown = vi.fn((_text: string) => '<p>list</p>')
    mountStream(raw, renderMarkdown)
    await nextTick()
    expect(renderMarkdown).not.toHaveBeenCalled()
    raw.value += '- Next item\n\n'
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(renderMarkdown.mock.calls[0]?.[0]).toBe(firstItem + '- Next item\n\n')
  })

  it('restores enhancement when canonical source is replaced with the same block count', async () => {
    const raw = ref('alpha\n\n')
    const host = mountStream(raw, text => `<pre><code>${text.trim()}</code></pre>`)
    await nextTick()
    const previous = host.querySelector('.streaming-rich-block')
    expect(host.querySelectorAll('.code-copy-btn')).toHaveLength(1)
    raw.value = 'bravo\n\n'
    await nextTick()
    expect(host.querySelector('.streaming-rich-block')).not.toBe(previous)
    expect(host.querySelectorAll('.code-copy-btn')).toHaveLength(1)
    expect(host.querySelector('code')?.textContent).toBe('bravo')
  })

  it('shares heavy-work limits across commits and replenishes them on canonical reset', async () => {
    const highlight = vi.spyOn(hljs, 'highlight').mockImplementation(() => { throw new Error('plain fallback') })
    const raw = ref('')
    const { renderMarkdown } = useChatTextRendering()
    const host = mountStream(raw, renderMarkdown)
    for (let index = 0; index < 40; index++) {
      raw.value += `\x60\x60\x60js\nconst value${index} = ${index}\n\x60\x60\x60\n\n`
      await nextTick()
    }
    expect(highlight).toHaveBeenCalledTimes(32)
    expect(host.querySelectorAll('code')).toHaveLength(40)
    expect(host.querySelector('code:last-of-type')?.textContent).toBeTruthy()
    const codes = host.querySelectorAll('code')
    expect(codes[codes.length - 1]?.textContent).toBe('const value39 = 39')
    raw.value = '\x60\x60\x60js\nconst next = 1\n\x60\x60\x60\n\n'
    await nextTick()
    expect(highlight).toHaveBeenCalledTimes(33)
    expect(host.querySelectorAll('code')).toHaveLength(1)
  })

  it('enhances each new committed subtree once without rescanning historical blocks', async () => {
    const raw = ref('')
    const host = mountStream(raw, text => `<pre><code>${text.trim()}</code></pre>`)
    const queries = vi.spyOn(HTMLElement.prototype, 'querySelectorAll')
    for (let index = 0; index < 30; index++) {
      raw.value += `block ${index}\n\n`
      await nextTick()
    }
    const passesByBlock = new Map<HTMLElement, number>()
    queries.mock.calls.forEach(([selector], index) => {
      const element = queries.mock.contexts[index] as HTMLElement
      if (selector !== 'pre' || !element.classList.contains('streaming-rich-block')) return
      passesByBlock.set(element, (passesByBlock.get(element) ?? 0) + 1)
    })
    // Copy, viewport admission and engine detection each inspect only the new
    // subtree. Every historical block retains its original fixed pass count.
    expect(passesByBlock.size).toBe(30)
    expect([...passesByBlock.values()]).toEqual(Array(30).fill(3))
    expect(host.querySelectorAll('.code-copy-btn')).toHaveLength(30)
  })

  it('keeps the growing tail as text and renders each closed block once', async () => {
    const raw = ref('')
    const renderMarkdown = vi.fn((text: string) => `<p>${text.trim()}</p>`)
    const root = defineComponent(() => () => h(StreamingTextPart, {
      rawText: raw.value,
      renderMarkdown,
    }))
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(root)
    mountI18n(app)
    app.mount(host)
    apps.push(app)

    raw.value = 'A growing paragraph'
    await nextTick()
    expect(renderMarkdown).not.toHaveBeenCalled()
    expect(host.textContent).toContain('A growing paragraph')

    raw.value += '\n\n'
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    // A committed block renders exactly once, fully rich: highlighting and
    // math are paid per block at commit time, not deferred to settle.
    expect(renderMarkdown).toHaveBeenLastCalledWith('A growing paragraph\n\n', {
      highlight: true,
      cache: 'none',
      math: 'full',
      budget: expect.any(Object),
    })

    raw.value += 'Second block still open'
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(host.textContent).toContain('Second block still open')
  })

  it('bounds an unclosed tail by freezing plain text chunks', async () => {
    const raw = ref('x'.repeat(24 * 1024))
    const renderMarkdown = vi.fn((text: string) => text)
    const root = defineComponent(() => () => h(StreamingTextPart, {
      rawText: raw.value,
      renderMarkdown,
    }))
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(root)
    mountI18n(app)
    app.mount(host)
    apps.push(app)
    await nextTick()

    expect(renderMarkdown).not.toHaveBeenCalled()
    expect(host.querySelectorAll('.streaming-plain-block').length).toBeGreaterThan(0)
    expect(host.textContent).toHaveLength(24 * 1024)
  })

  it('never freezes a plain block between an emoji surrogate pair', async () => {
    const source = `${'x'.repeat((8 * 1024) - 1)}😀${'y'.repeat(9 * 1024)}`
    const raw = ref(source)
    const root = defineComponent(() => () => h(StreamingTextPart, {
      rawText: raw.value,
      renderMarkdown: (text: string) => text,
    }))
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(root)
    mountI18n(app)
    app.mount(host)
    apps.push(app)
    await nextTick()

    const firstFrozen = host.querySelector('.streaming-plain-block')?.textContent ?? ''
    const lastCodeUnit = firstFrozen.charCodeAt(firstFrozen.length - 1)
    expect(lastCodeUnit < 0xD800 || lastCodeUnit > 0xDBFF).toBe(true)
    expect(host.textContent).toBe(source)
  })

  it('preserves source order when a frozen open block later closes', async () => {
    const prefix = 'x'.repeat(24 * 1024)
    const raw = ref(prefix)
    const renderMarkdown = vi.fn((text: string) => `<strong>${text}</strong>`)
    const root = defineComponent(() => () => h(StreamingTextPart, {
      rawText: raw.value,
      renderMarkdown,
    }))
    const host = document.createElement('div')
    document.body.appendChild(host)
    const app = createApp(root)
    mountI18n(app)
    app.mount(host)
    apps.push(app)
    await nextTick()

    raw.value += 'tail\n\n'
    await nextTick()

    const committed = [...host.querySelectorAll(
      '.streaming-plain-block, .streaming-rich-block',
    )]
    expect(committed[0]?.classList.contains('streaming-plain-block')).toBe(true)
    expect(committed[committed.length - 1]?.classList.contains('streaming-rich-block')).toBe(true)
    expect(host.textContent).toBe(`${prefix}tail\n\n`)
  })
})
