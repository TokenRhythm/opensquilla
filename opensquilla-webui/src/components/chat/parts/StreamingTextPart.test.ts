// @vitest-environment happy-dom

import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import StreamingTextPart from './StreamingTextPart.vue'

const apps: ReturnType<typeof createApp>[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('StreamingTextPart', () => {
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
    app.mount(host)
    apps.push(app)

    raw.value = 'A growing paragraph'
    await nextTick()
    expect(renderMarkdown).not.toHaveBeenCalled()
    expect(host.textContent).toContain('A growing paragraph')

    raw.value += '\n\n'
    await nextTick()
    expect(renderMarkdown).toHaveBeenCalledTimes(1)
    expect(renderMarkdown).toHaveBeenLastCalledWith('A growing paragraph\n\n', {
      highlight: false,
      cache: 'none',
      math: 'defer',
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
