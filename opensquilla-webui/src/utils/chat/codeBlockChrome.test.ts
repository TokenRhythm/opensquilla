// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createCodeBlockChrome, decorateCodeBlocks } from './codeBlockChrome'
import { copyTextWithFallback } from '@/utils/browser'

vi.mock('@/utils/browser', () => ({ copyTextWithFallback: vi.fn() }))

const labels = { copy: 'Copy', copyFailed: 'Copy failed' }
const owners: ReturnType<typeof createCodeBlockChrome>[] = []

beforeEach(() => { vi.mocked(copyTextWithFallback).mockReset().mockResolvedValue(undefined) })

afterEach(() => {
  owners.splice(0).forEach(owner => owner.dispose())
  document.body.innerHTML = ''
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('decorateCodeBlocks', () => {
  it('adds the code-block chrome and one copy button per pre', () => {
    const root = document.createElement('div')
    root.innerHTML = '<pre><code class="hljs language-ts">const a = 1</code></pre>'
    decorateCodeBlocks(root, labels)
    const pre = root.querySelector('pre')
    expect(pre?.classList.contains('code-block')).toBe(true)
    expect(root.querySelectorAll('.code-copy-btn')).toHaveLength(1)
    const button = root.querySelector('.code-copy-btn') as HTMLButtonElement
    expect(button.getAttribute('aria-label')).toBe('Copy')
  })

  it('is idempotent across re-runs', () => {
    const root = document.createElement('div')
    root.innerHTML = '<pre><code>hello</code></pre>'
    decorateCodeBlocks(root, labels)
    decorateCodeBlocks(root, labels)
    expect(root.querySelectorAll('.code-copy-btn')).toHaveLength(1)
  })

  it('copies the code text on click', async () => {
    const root = document.createElement('div')
    root.innerHTML = '<pre><code class="hljs language-py">print(1)</code></pre>'
    decorateCodeBlocks(root, labels)
    const button = root.querySelector('.code-copy-btn') as HTMLButtonElement
    button.click()
    await vi.waitFor(() => {
      expect(copyTextWithFallback).toHaveBeenCalledWith('print(1)')
      expect(button.getAttribute('aria-label')).toBe('Copy')
    })
  })
})

function owned(html: string, selector?: string) {
  const root = document.createElement('div')
  root.innerHTML = html
  document.body.append(root)
  const owner = createCodeBlockChrome()
  owners.push(owner)
  owner.decorate(root, labels, selector)
  return { root, owner, button: root.querySelector<HTMLButtonElement>('.code-copy-btn')! }
}

describe('owned code-block copy lifecycle', () => {
  it('copies only the direct source code, including hidden diagram source', async () => {
    const { root, button } = owned('<pre><span class="code-lang">dot</span><code hidden>digraph { A -&gt; B }</code><div><code>rendered label</code></div><small>Source retained</small></pre>')
    button.click()
    await Promise.resolve()
    expect(copyTextWithFallback).toHaveBeenCalledExactlyOnceWith('digraph { A -> B }')
    expect(root.querySelector('code')?.hidden).toBe(true)
    expect(button.dataset.shareControl).toBe('true')
  })

  it('clears failed feedback on retry and briefly shows a check with the original action name', async () => {
    vi.useFakeTimers()
    const { button } = owned('<pre><code>source</code></pre>')
    vi.mocked(copyTextWithFallback).mockRejectedValueOnce(new Error('denied'))
    button.click()
    await Promise.resolve()
    expect(button.classList.contains('is-error')).toBe(true)
    vi.advanceTimersByTime(1500)
    button.click()
    await Promise.resolve()
    expect(button.classList.contains('is-error')).toBe(false)
    expect(button.classList.contains('is-copied')).toBe(true)
    expect(button.querySelector('polyline')).not.toBeNull()
    expect(button.getAttribute('aria-label')).toBe('Copy')
    expect(button.textContent).toBe('')
    expect(vi.getTimerCount()).toBe(1)
    vi.advanceTimersByTime(200)
    expect(button.title).toBe('Copy')
    expect(button.classList.contains('is-copied')).toBe(true)
    vi.advanceTimersByTime(1400)
    expect(button.title).toBe('Copy')
    expect(button.classList.contains('is-copied')).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('updates locale in place while retaining keyboard focus and current feedback', async () => {
    const { owner, root, button } = owned('<pre><code>source</code></pre>')
    button.focus()
    button.click()
    await Promise.resolve()
    owner.updateLabels({ copy: '复制', copyFailed: '复制失败' })
    expect(root.querySelectorAll('button')).toHaveLength(1)
    expect(document.activeElement).toBe(button)
    expect(button.title).toBe('复制')
    expect(button.getAttribute('aria-label')).toBe('复制')
    expect(button.classList.contains('is-copied')).toBe(true)
  })

  it('ignores an older copy completion after a newer attempt', async () => {
    const { button } = owned('<pre><code>source</code></pre>')
    let rejectFirst!: (error: Error) => void
    vi.mocked(copyTextWithFallback).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    button.click()
    button.click()
    await Promise.resolve()
    rejectFirst(new Error('late failure'))
    await Promise.resolve()
    expect(button.title).toBe('Copy')
    expect(button.classList.contains('is-error')).toBe(false)
  })

  it('disposes pending callbacks, listeners and timers across reset and unmount', async () => {
    vi.useFakeTimers()
    const { root, owner, button } = owned('<pre><code>source</code></pre>')
    let resolveFirst!: () => void
    vi.mocked(copyTextWithFallback).mockImplementationOnce(() => new Promise<void>(resolve => { resolveFirst = resolve }))
    button.click()
    owner.reset()
    resolveFirst()
    await Promise.resolve()
    expect(button.classList.contains('is-copied')).toBe(false)
    button.click()
    expect(copyTextWithFallback).toHaveBeenCalledOnce()
    owner.decorate(root, labels)
    vi.mocked(copyTextWithFallback).mockRejectedValueOnce(new Error('denied'))
    root.querySelector<HTMLButtonElement>('button')!.click()
    await Promise.resolve()
    expect(vi.getTimerCount()).toBe(1)
    owner.dispose()
    expect(vi.getTimerCount()).toBe(0)
    expect(root.querySelector('button')).toBeNull()
    owner.decorate(root, labels)
    expect(root.querySelector('button')).toBeNull()
  })

  it('restricts controls to Markdown bodies without changing tool output or owned controls', () => {
    const { root } = owned('<div class="msg-ai-text"><pre><code>answer</code></pre><pre><code></code></pre><pre><code>owned</code><button class="code-copy-btn">existing</button></pre></div><pre class="tool-output"><code>tool</code></pre>', '.msg-ai-text pre')
    expect(root.querySelectorAll('.code-copy-btn')).toHaveLength(2)
    expect(root.querySelector('.tool-output button')).toBeNull()
  })
})
