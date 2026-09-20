// @vitest-environment happy-dom
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import i18n, { loadLocaleMessages } from '@/i18n'
import type { TraceSpan } from '@/types/traceView'
import TraceStepDetails from './TraceStepDetails.vue'

type DetailTab = 'overview' | 'input' | 'preview' | 'raw'
const cleanup: Array<() => void> = []
const initialLocale = i18n.global.locale.value

beforeAll(async () => {
  await loadLocaleMessages('zh-Hans')
  i18n.global.locale.value = 'zh-Hans'
})
afterEach(() => { cleanup.splice(0).forEach(dispose => dispose()) })
afterAll(() => { i18n.global.locale.value = initialLocale })

function span(overrides: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: 'tool:synthetic-call', kind: 'tool_response', phase: 'tool_execution',
    title: 'Tool', status: 'success', ...overrides,
  }
}

function mountStep(row: TraceSpan, input: unknown, output: unknown) {
  const state = reactive({ row, input, output })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ setup: () => () => h(TraceStepDetails, state) })
  app.use(i18n)
  app.mount(host)
  cleanup.push(() => { app.unmount(); host.remove() })
  return { host, state }
}

function tabButton(host: HTMLElement, tab: DetailTab): HTMLButtonElement {
  const button = host.querySelector<HTMLButtonElement>(`[data-detail-tab="${tab}"]`)
  expect(button).not.toBeNull()
  return button!
}

async function selectTab(host: HTMLElement, tab: DetailTab) {
  tabButton(host, tab).click()
  await nextTick()
  expect(tabButton(host, tab).getAttribute('aria-selected')).toBe('true')
}

function contentBlock(host: HTMLElement, id: string): HTMLElement {
  const block = host.querySelector<HTMLElement>(`[data-block-id="${id}"]`)
  expect(block, `content block ${id}`).not.toBeNull()
  return block!
}

function rawValue(host: HTMLElement, side: 'input' | 'output'): unknown {
  return JSON.parse(contentBlock(host, `raw-${side}`).querySelector('pre code')!.textContent!)
}

function expectNoActiveMarkup(host: HTMLElement) {
  expect(host.querySelector('script, iframe, img, video, audio, source, link, object, embed, style, svg')).toBeNull()
  expect(host.querySelector('[src], [srcdoc], [srcset], [onload], [onerror], [onclick], [style]')).toBeNull()
  for (const link of host.querySelectorAll('a')) {
    expect(link.getAttribute('href') || '').not.toMatch(/^javascript:/i)
  }
}

describe('TraceStepDetails', () => {
  it('shows complete file code in preview, separates parameters, and keeps the full original record', async () => {
    const content = Array.from({ length: 120 }, (_, index) => `const item${index} = "value ${index}";`).join('\n')
      + '\nexport const endMarker = "complete-file-tail";\n'
    const input = { arguments: { path: '/workspace/example.ts', content, create_parents: false } }
    const output = { result: { path: '/workspace/example.ts', written: true }, is_error: false }
    const { host } = mountStep(span({ toolName: 'write_file' }), input, output)

    await selectTab(host, 'preview')
    const codeBlock = contentBlock(host, 'input.arguments.content')
    expect(codeBlock.dataset.format).toBe('code')
    expect(codeBlock.querySelector('pre code')?.textContent).toBe(content)
    expect(codeBlock.querySelector('pre code')?.textContent).not.toContain('\\n')

    await selectTab(host, 'input')
    expect(contentBlock(host, 'input.arguments.path').textContent).toContain('/workspace/example.ts')
    const optionalParameter = contentBlock(host, 'input.arguments.create_parents')
    const parameterHeading = optionalParameter.querySelector<HTMLButtonElement>('.trace-content-block__heading')!
    if (parameterHeading.getAttribute('aria-expanded') === 'false') {
      parameterHeading.click()
      await nextTick()
    }
    expect(optionalParameter.textContent).toContain('false')
    expect(contentBlock(host, 'input.arguments.content').querySelector('pre code')?.textContent).toBe(content)

    await selectTab(host, 'raw')
    expect(rawValue(host, 'input')).toEqual(input)
    expect(rawValue(host, 'output')).toEqual(output)
  })

  it.each<DetailTab>(['preview', 'raw'])('retains the selected %s tab when a live model call completes', async (tab) => {
    const row = span({ id: 'step:synthetic-model', kind: 'llm_progress', phase: 'model_execution', status: 'running' })
    const input = { messages: [{ role: 'user', content: 'Describe the synthetic example.' }] }
    const { host, state } = mountStep(row, input, { text: 'Partial response', partial: true })
    await selectTab(host, tab)
    expect(host.textContent).toContain('Partial response')
    expect(host.querySelector('.trace-detail-notice')?.textContent).toContain('运行尚未结束')

    state.output = { text: 'Growing response with another sentence', partial: true }
    state.row = { ...row, seq: 4 }
    await nextTick()
    expect(tabButton(host, tab).getAttribute('aria-selected')).toBe('true')
    expect(host.textContent).toContain('Growing response with another sentence')
    expect(host.textContent).not.toContain('Partial response')

    const finalOutput = { text: 'Final response with a complete ending', usage: { output_tokens: 12 } }
    state.output = finalOutput
    state.row = { ...row, kind: 'llm_response', status: 'success', seq: 5, endedAt: 900, durationMs: 890 }
    await nextTick()
    expect(tabButton(host, tab).getAttribute('aria-selected')).toBe('true')
    expect(host.textContent).toContain('Final response with a complete ending')
    expect(host.textContent).not.toContain('Growing response')
    expect(host.querySelector('.trace-detail-notice')).toBeNull()
    if (tab === 'raw') expect(rawValue(host, 'output')).toEqual(finalOutput)
    else expect(contentBlock(host, 'output.text').querySelector('.trace-content-block__markdown')?.textContent).toContain(finalOutput.text)
  })

  it('separates shell errors into output, error, and exit-code blocks', async () => {
    const { host } = mountStep(span({ toolName: 'shell', status: 'error' }), {
      arguments: { command: 'synthetic-check --strict', timeout: 10 },
    }, { result: JSON.stringify({ stdout: 'Checking example\n', stderr: 'Missing synthetic input\n', exit_code: 7 }) })
    expect(host.querySelector('.trace-detail-facts__status--error')?.textContent).toBe('错误')
    await selectTab(host, 'input')
    expect(contentBlock(host, 'input.arguments.command').querySelector('pre code')?.textContent).toBe('synthetic-check --strict')
    await selectTab(host, 'preview')
    expect(contentBlock(host, 'output.result.stdout').textContent).toContain('标准输出')
    expect(contentBlock(host, 'output.result.stdout').querySelector('pre code')?.textContent).toBe('Checking example\n')
    expect(contentBlock(host, 'output.result.stderr').textContent).toContain('标准错误')
    expect(contentBlock(host, 'output.result.stderr').querySelector('pre code')?.textContent).toBe('Missing synthetic input\n')
    expect(contentBlock(host, 'output.result.exit_code').textContent).toContain('退出码')
    expect(contentBlock(host, 'output.result.exit_code').querySelector('.trace-value__text')?.textContent).toBe('7')
  })

  it('shows exact before and after text for an edit without requiring raw JSON', async () => {
    const before = 'const oldValue = "first";\nconst removed = true;\n'
    const after = 'const newValue = "second";\n'
    const input = { arguments: { path: '/workspace/example.ts', old_text: before, new_text: after } }
    const { host } = mountStep(span({ toolName: 'edit_file' }), input, { result: 'Updated' })
    await selectTab(host, 'preview')
    const diff = contentBlock(host, 'input.arguments.changes')
    expect(diff.dataset.format).toBe('diff')
    expect(diff.querySelector('.trace-content-block__before')?.textContent).toContain('处理前')
    expect(diff.querySelector('.trace-content-block__before pre code')?.textContent).toBe(before)
    expect(diff.querySelector('.trace-content-block__after')?.textContent).toContain('处理后')
    expect(diff.querySelector('.trace-content-block__after pre code')?.textContent).toBe(after)
    await selectTab(host, 'raw')
    expect(rawValue(host, 'input')).toEqual(input)
  })

  it('renders authored HTML as inert code and preserves every original tag in raw content', async () => {
    const content = '<h1>Example</h1>\n<script>globalThis.syntheticTraceProbe = true</script>\n'
      + '<img src="https://example.invalid/image.png" onerror="syntheticTraceProbe()">\n'
      + '<iframe srcdoc="&lt;script&gt;syntheticTraceProbe()&lt;/script&gt;"></iframe>\n'
    const input = { arguments: { path: '/workspace/example.html', content } }
    const { host } = mountStep(span({ toolName: 'write_file' }), input, { result: 'Stored' })
    await selectTab(host, 'preview')
    expect(contentBlock(host, 'input.arguments.content').querySelector('pre code')?.textContent).toBe(content)
    expectNoActiveMarkup(host)
    await selectTab(host, 'raw')
    expect(rawValue(host, 'input')).toEqual(input)
    expectNoActiveMarkup(host)
  })

  it('sanitizes model markdown without loading embedded resources and keeps its untouched raw source', async () => {
    const markdown = '# Example heading\n\n**Readable content**\n\n'
      + '<script>globalThis.syntheticTraceProbe = true</script>\n\n'
      + '<iframe srcdoc="&lt;script&gt;syntheticTraceProbe()&lt;/script&gt;"></iframe>\n\n'
      + '![external image](https://example.invalid/image.png)\n\n'
      + '<svg onload="syntheticTraceProbe()"></svg>\n\n'
      + '<a href="javascript:syntheticTraceProbe()" onclick="syntheticTraceProbe()">Unsafe link</a>\n'
    const input = { messages: [{ role: 'user', content: markdown }] }
    const output = { text: markdown }
    const { host } = mountStep(span({ phase: 'model_execution', kind: 'llm_response' }), input, output)
    for (const tab of ['input', 'preview'] as const) {
      await selectTab(host, tab)
      expect(host.querySelector('.trace-content-block__markdown h1')?.textContent).toBe('Example heading')
      expect(host.querySelector('.trace-content-block__markdown strong')?.textContent).toBe('Readable content')
      expectNoActiveMarkup(host)
    }
    await selectTab(host, 'raw')
    expect(rawValue(host, 'input')).toEqual(input)
    expect(rawValue(host, 'output')).toEqual(output)
    expectNoActiveMarkup(host)
  })

  it('moves selection and focus between detail tabs with the keyboard', async () => {
    const { host } = mountStep(span(), { message: 'Example input' }, { result: 'Example output' })
    const overview = tabButton(host, 'overview')
    overview.focus()
    overview.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
    await nextTick()
    expect(document.activeElement).toBe(tabButton(host, 'input'))
    expect(tabButton(host, 'input').getAttribute('aria-selected')).toBe('true')
    expect(host.querySelector('[role="tabpanel"]')?.getAttribute('aria-labelledby')).toBe(tabButton(host, 'input').id)
    tabButton(host, 'input').dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true, cancelable: true }))
    await nextTick()
    expect(document.activeElement).toBe(tabButton(host, 'raw'))
    expect(tabButton(host, 'raw').getAttribute('aria-selected')).toBe('true')
    expect(rawValue(host, 'output')).toEqual({ result: 'Example output' })
  })
})
