// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, ref } from 'vue'
import i18n from '@/i18n'
import type { ChatStreamTimelineItem } from '@/types/chat'
import type { NodeStep } from '@/types/runTrace'
import { copyTextWithFallback } from '@/utils/browser'
import RunTrace from './RunTrace.vue'

vi.mock('@/utils/browser', () => ({
  copyTextWithFallback: vi.fn().mockResolvedValue(undefined),
}))

async function mountRunTrace(
  initialItems: ChatStreamTimelineItem[],
  options: {
    isToolItemOpen?: (renderKey: string) => boolean
    onShowResult?: (content: string, title: string, context?: unknown) => void
  } = {},
) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const items = ref(initialItems)
  const Host = defineComponent({
    setup() {
      return () => h(RunTrace, {
        items: items.value,
        isToolGroupOpen: () => false,
        isToolItemOpen: options.isToolItemOpen ?? (() => false),
        onShowResult: options.onShowResult,
      })
    },
  })
  const app = createApp(Host)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el, items }
}

async function mountFlatRunTrace(steps: NodeStep[]) {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const Host = defineComponent({
    setup() {
      return () => h(RunTrace, {
        steps,
        isToolGroupOpen: () => false,
        isToolItemOpen: () => false,
      })
    },
  })
  const app = createApp(Host)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { app, el }
}

beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
  vi.mocked(copyTextWithFallback).mockClear()
})

describe('RunTrace code block copy control', () => {
  it('uses original tool identity for result counts in flat history traces', async () => {
    const { app, el } = await mountFlatRunTrace([
      {
        id: 'fetch-year',
        title: 'Read web page',
        toolName: 'web_fetch',
        operationKey: 'web.read',
        state: 'output-available',
        output: JSON.stringify({ text: 'AI industry 2026 results and outlook' }),
      },
      {
        id: 'search-count',
        title: 'Search web',
        toolName: 'web_search',
        operationKey: 'web.search',
        state: 'output-available',
        output: JSON.stringify({ results: [{ title: 'One' }, { title: 'Two' }] }),
      },
    ])

    const rows = el.querySelectorAll<HTMLElement>('.tool-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]?.querySelector('.tool-row__status')).toBeNull()
    expect(rows[0]?.textContent).not.toContain('2026 results')
    expect(rows[1]?.querySelector('.tool-row__status')?.textContent).toBe('2 results')

    app.unmount()
  })

  it('decorates code blocks that appear during same-key text updates', async () => {
    const { app, el, items } = await mountRunTrace([
      { type: 'text', key: 'streaming-text', html: '<p>partial result</p>' },
    ])

    expect(el.querySelector('.code-copy-btn')).toBeNull()

    items.value = [
      {
        type: 'text',
        key: 'streaming-text',
        html: '<p>done</p><pre><code>console.log("late")</code></pre>',
      },
    ]
    await nextTick()
    await nextTick()

    const button = el.querySelector<HTMLButtonElement>('.code-copy-btn')
    expect(el.querySelector('.msg-ai-text pre code')?.textContent).toBe('console.log("late")')
    expect(button).not.toBeNull()

    button?.click()
    await Promise.resolve()

    expect(copyTextWithFallback).toHaveBeenCalledWith('console.log("late")')
    app.unmount()
  })
  it('compacts long tool input sections into a short summary', async () => {
    const prompt = Array.from({ length: 12 }, (_, index) => `line ${index + 1}: detailed image prompt`).join('\n')
    const inputRaw = JSON.stringify({
      filename: 'octopus-3d-clay.png',
      prompt,
      provider: 'openrouter',
    }, null, 2)

    const { app, el } = await mountRunTrace([
      {
        type: 'tool-group',
        key: 'image-generate-group',
        group: {
          groupId: 'image-generate-group',
          operationKey: 'image_generate',
          label: 'image_generate',
          iconName: 'gear',
          secondary: '',
          isRunning: false,
          isError: false,
          status: 'success',
          calls: [
            {
              toolId: 'tool-1',
              renderKey: 'tool-1',
              name: 'image_generate',
              displayName: 'image_generate',
              inputRaw,
              inputPreview: inputRaw.slice(0, 200),
              isRunning: false,
              status: 'success',
              isError: false,
              result: '{"status":"ok"}',
              resultPreview: '{"status":"ok"}',
              isOpen: false,
            },
          ],
        },
      },
    ])

    const inputSection = el.querySelector<HTMLElement>('.tool-row-section')
    expect(inputSection?.querySelector('.tool-row-section__compact')).not.toBeNull()
    expect(inputSection?.querySelector('pre')).toBeNull()
    expect(inputSection?.textContent).toContain('JSON')
    expect(inputSection?.textContent).toContain('octopus-3d-clay.png')
    expect(inputSection?.textContent).toContain('view full')

    app.unmount()
  })
  it('uses shell-specific summaries for compacted command input and stdout output', async () => {
    const command = [
      "python - <<'PY'",
      'import json',
      'payload = {',
      '  "filename": "octopus-3d-clay.png",',
      '  "provider": "openrouter",',
      '  "prompt": "\\n".join([',
      '    f"line {i}: detailed image prompt with many style details and lighting constraints"',
      '    for i in range(1, 28)',
      '  ]),',
      '}',
      'print(json.dumps(payload, indent=2))',
      '# keep this fixture long enough to exercise the compact shell summary',
      '# line 01: extra command context that would otherwise crowd the run trace',
      '# line 02: extra command context that would otherwise crowd the run trace',
      '# line 03: extra command context that would otherwise crowd the run trace',
      '# line 04: extra command context that would otherwise crowd the run trace',
      'PY',
    ].join('\n')
    const inputRaw = JSON.stringify({ command }, null, 2)
    const result = [
      'exit_code=0',
      '{',
      '  "filename": "octopus-3d-clay.png",',
      '  "artifact": {',
      '    "path": "/tmp/octopus-3d-clay.png",',
      '    "mime": "image/png"',
      '  }',
      '}',
    ].join('\n')

    const { app, el } = await mountRunTrace([
      {
        type: 'tool-group',
        key: 'shell-group',
        group: {
          groupId: 'shell-group',
          operationKey: 'shell',
          label: '运行命令',
          iconName: 'gear',
          secondary: '',
          isRunning: false,
          isError: false,
          status: 'success',
          calls: [
            {
              toolId: 'tool-1',
              renderKey: 'tool-1',
              name: 'shell',
              displayName: '运行命令',
              inputRaw,
              inputPreview: inputRaw.slice(0, 200),
              isRunning: false,
              status: 'success',
              isError: false,
              result,
              resultPreview: result.slice(0, 200),
              isOpen: false,
            },
          ],
        },
      },
    ])

    const sections = el.querySelectorAll<HTMLElement>('.tool-row-section')
    expect(sections[0]?.querySelector('.tool-row-section__compact')).not.toBeNull()
    expect(sections[0]?.textContent).toContain('shell command')
    expect(sections[0]?.textContent).toContain("command: python - <<'PY'")

    expect(sections[1]?.querySelector('.tool-row-section__compact')).not.toBeNull()
    expect(sections[1]?.textContent).toContain('shell result')
    expect(sections[1]?.textContent).toContain('exit_code=0')
    expect(sections[1]?.textContent).toContain('output: filename: octopus-3d-clay.png')
    expect(sections[1]?.textContent).toContain('artifact: { path: /tmp/octopus-3d-clay.png, mime: image/png }')
    expect(sections[1]?.textContent).not.toContain('[object Object]')

    app.unmount()
  })

  it('passes read_file context along with a full result', async () => {
    const inputRaw = JSON.stringify({ path: '/workspace/HEARTBEAT.yml' })
    const result = 'priority_bands:\n  high: 1.0\n'.repeat(30)
    const onShowResult = vi.fn()
    const { app, el } = await mountRunTrace([
      {
        type: 'tool-group',
        key: 'read-file-group',
        group: {
          groupId: 'read-file-group',
          operationKey: 'read_file',
          label: 'read_file',
          iconName: 'fileText',
          secondary: '',
          isRunning: false,
          isError: false,
          status: 'success',
          calls: [
            {
              toolId: 'tool-1',
              renderKey: 'tool-1',
              name: 'read_file',
              displayName: 'read_file',
              inputRaw,
              inputPreview: inputRaw,
              isRunning: false,
              status: 'success',
              isError: false,
              result,
              resultPreview: result.slice(0, 200),
              isOpen: true,
            },
          ],
        },
      },
    ], {
      isToolItemOpen: () => true,
      onShowResult,
    })

    const viewFull = el.querySelector<HTMLButtonElement>('.step-view-btn')
    expect(viewFull).not.toBeNull()
    viewFull?.click()

    expect(onShowResult).toHaveBeenCalledWith(result, 'read_file · result', {
      toolName: 'read_file',
      inputRaw,
      section: 'result',
    })
    app.unmount()
  })
})
