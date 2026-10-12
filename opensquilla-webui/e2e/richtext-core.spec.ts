import { expect, test } from '@playwright/test'

test.skip(!process.env.OPENSQUILLA_DIAGRAM_ENGINE_DEV, 'Requires the Vite development server')

test.beforeEach(async ({ page }) => {
  await page.route('**/__richtext-core-fixture', route => route.fulfill({ contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>' }))
  await page.goto('/__richtext-core-fixture')
})

test('math keeps root/overbrace paths and accessible MathML while supporting numeric and parenthesized expressions', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const root = document.createElement('div')
    root.innerHTML = renderMarkdownCore(String.raw`$2^n$ and \(f(x)\), $\sqrt{x}$, $\overbrace{a+b}^{n}$` + '\n\n$$\\frac{a}{b}$$')
    document.body.append(root)
    const style = document.createElement('link')
    style.rel = 'stylesheet'; style.href = '/node_modules/katex/dist/katex.min.css'; document.head.append(style)
    return { math: root.querySelectorAll('math').length, display: root.querySelectorAll('.katex-display').length, svg: root.querySelectorAll('svg path').length, annotations: Array.from(root.querySelectorAll('annotation')).map(n => n.textContent), hidden: Array.from(root.querySelectorAll('.katex-html')).every(n => n.getAttribute('aria-hidden') === 'true'), paths: Array.from(root.querySelectorAll('svg')).map(n => n.getAttribute('viewBox')) }
  })
  expect(result.math).toBe(5)
  expect(result.display).toBe(1)
  expect(result.svg).toBeGreaterThanOrEqual(2)
  expect(result.annotations).toEqual(['2^n', 'f(x)', '\\sqrt{x}', '\\overbrace{a+b}^{n}', '\\frac{a}{b}'])
  expect(result.hidden).toBe(true)
  expect(result.paths.every(Boolean)).toBe(true)
})

test('all Markdown code boundaries preserve formula source exactly, including diagrams and escaped dollars', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const blocks = ['~~~text\n$x^2$\n~~~', '````text\n```\n$x^2$\n```\n````', '    $x^2$', '`$x^2$`', '``a `$x^2$` b``', '> ~~~text\n> $x^2$\n> ~~~', '```mermaid\nflowchart LR\nA["$$x^2$$"] --> B\n```']
    return blocks.map(source => {
      const root = document.createElement('div'); root.innerHTML = renderMarkdownCore(source)
      return { code: root.querySelector('code')?.textContent, formulas: root.querySelectorAll('.katex').length }
    }).concat([(() => { const root = document.createElement('div'); root.innerHTML = renderMarkdownCore(String.raw`\$2 and \$3; $5 and $10.`); return { code: root.textContent, formulas: root.querySelectorAll('.katex').length } })()])
  })
  expect(result.map(item => item.formulas)).toEqual(Array(8).fill(0))
  expect(result.slice(0, 7).map(item => item.code)).toEqual(['$x^2$', '```\n$x^2$\n```', '$x^2$', '$x^2$', 'a `$x^2$` b', '$x^2$', 'flowchart LR\nA["$$x^2$$"] --> B'])
  expect(result.at(-1)?.code).toContain('$2 and $3; $5 and $10.')
})

test('explicit defer mode skips math/diagrams, isolated parser never modifies the global marked instance', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const markedPath = '/node_modules/.vite/deps/marked.js'
    const { marked } = await import(markedPath)
    const before = marked.parse('~one~')
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const html = renderMarkdownCore('$2^n$\n\n```mermaid\nflowchart LR\nA --> B\n```', { math: 'defer', diagrams: 'defer' })
    return { html, before, after: marked.parse('~one~'), strict: renderMarkdownCore('~one~ ~~two~~') }
  })
  expect(result.html).toContain('$2^n$')
  expect(result.html).not.toContain('data-diagram-language')
  expect(result.before).toBe(result.after)
  expect(result.strict).toContain('~one~ <del>two</del>')
})

test('safe math output cannot widen raw Markdown SVG/style/script privileges', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const root = document.createElement('div')
    root.innerHTML = renderMarkdownCore(String.raw`$\sqrt{x}$ <span class="katex" style="position:fixed;inset:0">raw</span><svg onload="alert(1)"><path d="M0 0"/></svg><script>alert(1)</script> $\href{javascript:alert(1)}{bad}$ $\includegraphics{https://example.com/a.png}$`)
    return { scripts: root.querySelectorAll('script, img, a[href], [onload]').length, raw: Array.from(root.querySelectorAll('span')).find(n => n.textContent === 'raw')?.outerHTML, svgCount: root.querySelectorAll('svg').length, math: root.querySelectorAll('math').length }
  })
  expect(result.scripts).toBe(0)
  expect(result.raw).toBe('<span>raw</span>')
  expect(result.svgCount).toBe(1)
  expect(result.math).toBe(3)
})

test('ordered starts/table alignment and full footnote bodies survive sanitization', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const root = document.createElement('div')
    root.innerHTML = renderMarkdownCore('3. Third\n4. Fourth\n\n| Left | Center | Right |\n| :--- | :---: | ---: |\n| a | b | c |\n\nText[^a] then again[^a], unknown[^missing].\n\n[^a]: First **strong** paragraph with $2^n$.\n\n    Second paragraph.\n\n[^unused]: Keep unused note text.\n\n[^a]: Keep duplicate text.')
    return { start: root.querySelector('ol')?.getAttribute('start'), align: Array.from(root.querySelectorAll('th')).map(n => n.getAttribute('align')), refs: Array.from(root.querySelectorAll('[data-markdown-footnote-ref]')).map(n => n.textContent), notes: root.querySelectorAll('[data-markdown-footnote]').length, note: root.querySelector('[data-markdown-footnote]')?.textContent, text: root.textContent, strong: root.querySelector('[data-markdown-footnote] strong')?.textContent, ids: root.querySelectorAll('[id]').length, math: root.querySelector('[data-markdown-footnote] math') !== null }
  })
  expect(result.start).toBe('3')
  expect(result.align).toEqual(['left', 'center', 'right'])
  expect(result.refs).toEqual(['1', '1'])
  expect(result.notes).toBe(1)
  expect(result.note).toContain('Second paragraph.')
  expect(result.strong).toBe('strong')
  expect(result.math).toBe(true)
  expect(result.text).toContain('[^missing]')
  expect(result.text).toContain('Keep unused note text.')
  expect(result.text).toContain('Keep duplicate text.')
  expect(result.ids).toBe(0)
})

test('footnote keyboard/click navigation stays within the mounted message and cleanup detaches handlers', async ({ page }) => {
  await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'; const footnotesPath = '/src/utils/markdown/footnotes.ts'
    const { renderMarkdownCore } = await import(path); const { bindMarkdownFootnotes } = await import(footnotesPath)
    for (let index = 0; index < 2; index++) {
      const root = document.createElement('div'); root.className = `message-${index}`; root.innerHTML = renderMarkdownCore('Text[^a].\n\n[^a]: Note.')
      document.body.append(root); const cleanup = bindMarkdownFootnotes(root)
      if (!index) { cleanup(); root.querySelector('[data-markdown-footnote-ref]')?.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
    }
  })
  const second = page.locator('.message-1')
  await second.locator('[data-markdown-footnote-ref]').focus()
  await page.keyboard.press('Enter')
  await expect(second.locator('[data-markdown-footnote]')).toBeFocused()
  await second.locator('[data-markdown-footnote-back]').click()
  await expect(second.locator('[data-markdown-footnote-ref]')).toBeFocused()
  expect(new URL(page.url()).hash).toBe('')
})

test('image placeholders keep private file references inert while HTTPS keeps main image support', async ({ page }) => {
  const external: string[] = []; await page.route('https://external.invalid/**', route => { external.push(route.request().url()); return route.abort() })
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'; const { renderMarkdownCore } = await import(path)
    const root = document.createElement('div'); root.innerHTML = renderMarkdownCore('![safe alt](images/demo.png)\n\n![external](https://external.invalid/pixel)', { images: 'placeholder' }); document.body.append(root)
    return { references: Array.from(root.querySelectorAll('[data-markdown-image]')).map(n => [n.getAttribute('data-markdown-image'), n.getAttribute('data-markdown-image-alt')]), images: root.querySelectorAll('img').length }
  })
  expect(result.references).toEqual([['images/demo.png', 'safe alt']])
  expect(result.images).toBe(1)
  await expect.poll(() => external).toEqual(['https://external.invalid/pixel'])
})

test('oversized or numerous formulas retain source without unbounded KaTeX work', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'; const { renderMarkdownCore } = await import(path)
    const large = `$${'x'.repeat(20_000)}$`
    const root = document.createElement('div'); const start = performance.now()
    root.innerHTML = renderMarkdownCore(`${large}\n\n${Array(300).fill('$2^n$').join(' ')}`)
    return { count: root.querySelectorAll('math').length, large: root.textContent?.includes(large), remaining: root.textContent?.includes('$2^n$'), elapsed: performance.now() - start }
  })
  expect(result.count).toBeLessThanOrEqual(256)
  expect(result.large).toBe(true)
  expect(result.remaining).toBe(true)
  expect(result.elapsed).toBeLessThan(5000)
})

test('document highlight budgets keep every code block and reset for the next document', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const path = '/src/utils/markdown/renderCore.ts'
    const { renderMarkdownCore } = await import(path)
    const fence = (source: string) => '```js\n' + source + '\n```'
    const inspect = (sources: string[]) => {
      const root = document.createElement('div')
      root.innerHTML = renderMarkdownCore(sources.map(fence).join('\n\n'))
      const blocks = Array.from(root.querySelectorAll('pre > code'))
      return {
        count: blocks.length,
        highlighted: blocks.filter(code => code.querySelector('.hljs-keyword')).length,
        complete: blocks.every((code, index) => code.textContent === sources[index]),
      }
    }
    return {
      large: inspect(Array.from({ length: 8 }, (_, index) => `const value${index} = 1;\n// ${'x'.repeat(28_000)}`)),
      small: inspect(Array.from({ length: 50 }, (_, index) => `const value${index} = ${index}`)),
      next: inspect(['const next = 1']),
    }
  })
  expect(result.large).toEqual({ count: 8, highlighted: 3, complete: true })
  expect(result.small).toEqual({ count: 50, highlighted: 32, complete: true })
  expect(result.next).toEqual({ count: 1, highlighted: 1, complete: true })
})
