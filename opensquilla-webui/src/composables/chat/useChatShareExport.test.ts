// @vitest-environment happy-dom

import { createApp, h, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ActivityDisclosure from '@/components/chat/ActivityDisclosure.vue'
import ActivityCodeDetails from '@/components/chat/ActivityCodeDetails.vue'
import ActivityToolDetails from '@/components/chat/ActivityToolDetails.vue'
import activityCodeSource from '@/components/chat/ActivityCodeDetails.vue?raw'
import activityToolSource from '@/components/chat/ActivityToolDetails.vue?raw'
import i18n from '@/i18n'
import zhHans from '@/locales/zh-Hans.json'
import type { ActivityToolCodeView } from '@/utils/chat/activityToolDetails'
import type { ChatToolCallRenderItem } from '@/types/chat'
import { useChatTextRendering } from './useChatTextRendering'
import { buildShareDom, staticAssetUrl, useChatShareExport } from './useChatShareExport'

const diagramExportMocks = vi.hoisted(() => ({
  render: vi.fn(),
  capture: vi.fn(),
}))
vi.mock('@/utils/markdown/mermaidRenderer', () => ({
  renderMarkdownDiagrams: diagramExportMocks.render,
  restoreClonedDiagramSources: vi.fn(),
}))
vi.mock('html-to-image', () => ({ toCanvas: diagramExportMocks.capture }))

describe('share export diagram preparation', () => {
  const captureStop = new Error('stop after verifying capture input')

  beforeEach(() => {
    diagramExportMocks.render.mockReset().mockResolvedValue(undefined)
    diagramExportMocks.capture.mockReset().mockRejectedValue(captureStop)
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      queueMicrotask(() => callback(0))
      return 1
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    delete document.documentElement.dataset.theme
  })

  function createExport() {
    const thread = document.createElement('div')
    thread.innerHTML = '<article data-share-message-id="diagram"><div class="msg-ai-text">'
      + '<pre data-diagram-language="mermaid"><code>graph TD; A--&gt;B</code>'
      + '<button data-share-control>Copy</button></pre></div></article>'
    document.body.appendChild(thread)
    return {
      thread,
      ...useChatShareExport({
        threadRef: ref(thread),
        title: () => 'Diagrams',
        aiGeneratedLabel: () => 'AI generated',
      }),
    }
  }

  it('waits for inline diagrams in the export theme and disposes the clone owner after capture', async () => {
    const { thread, buildShareImage } = createExport()
    const original = thread.innerHTML
    let finishRender!: () => void
    const dispose = vi.fn()
    diagramExportMocks.render.mockImplementation((stage: HTMLElement, _labels: unknown, options: { onRendered: (value: unknown) => void }) => {
      expect(stage.isConnected).toBe(true)
      expect(stage.dataset.theme).toBe('light')
      expect(stage.querySelector('[data-share-control]')).toBeNull()
      return new Promise<void>((resolve) => {
        finishRender = () => {
          const diagram = document.createElement('div')
          diagram.className = 'mermaid-diagram'
          diagram.innerHTML = '<div class="mermaid-svg-wrap"><svg viewBox="0 0 10 10"><text>Done</text></svg></div><div class="mermaid-toolbar"><button>Copy</button></div>'
          stage.querySelector('pre')!.replaceWith(diagram)
          options.onRendered({ element: diagram, dispose })
          resolve()
        }
      })
    })
    document.documentElement.dataset.theme = 'dark'
    const result = buildShareImage(new Set(['diagram']), { theme: 'light' })
    const failure = expect(result).rejects.toBe(captureStop)
    await vi.waitFor(() => expect(diagramExportMocks.render).toHaveBeenCalledOnce())
    expect(diagramExportMocks.render.mock.calls[0]?.[1]).toMatchObject({
      loadFailed: i18n.global.t('chat.mermaid.loadFailed'),
      tooLarge: i18n.global.t('chat.mermaid.tooLarge'),
      copyCode: i18n.global.t('chat.mermaid.copyCode'),
    })
    expect(diagramExportMocks.render.mock.calls[0]?.[2]).toMatchObject({
      themeId: 'light',
      signal: expect.any(AbortSignal),
      onRendered: expect.any(Function),
    })
    expect(diagramExportMocks.capture).not.toHaveBeenCalled()

    finishRender()
    await failure

    expect(diagramExportMocks.capture).toHaveBeenCalledOnce()
    const captured = diagramExportMocks.capture.mock.calls[0]?.[0] as HTMLElement
    expect(captured.style.left).toBe('0px')
    expect(captured.style.top).toBe('0px')
    expect(captured.querySelector('.mermaid-svg-wrap > svg')).not.toBeNull()
    expect(captured.querySelector('.mermaid-toolbar')).toBeNull()
    expect(dispose).toHaveBeenCalledOnce()
    expect(diagramExportMocks.render.mock.calls[0]?.[2].signal.aborted).toBe(true)
    expect(captured.isConnected).toBe(false)
    expect(document.getElementById('opensquilla-share-export-stage')).toBeNull()
    expect(thread.innerHTML).toBe(original)
  })

  it('cleans up the mounted stage when diagram preparation fails', async () => {
    const { buildShareImage } = createExport()
    const failure = new Error('renderer initialization failed')
    diagramExportMocks.render.mockRejectedValue(failure)

    await expect(buildShareImage(new Set(['diagram']))).rejects.toBe(failure)

    expect(diagramExportMocks.capture).not.toHaveBeenCalled()
    expect(document.getElementById('opensquilla-share-export-stage')).toBeNull()
  })

  it('preserves diagram source for re-rendering while stripping code-copy controls', () => {
    const { thread } = createExport()
    const source = thread.querySelector<HTMLElement>('article')!
    const code = source.querySelector('code')!
    code.hidden = true
    source.querySelector('pre')!.dataset.diagramState = 'ready'
    const legacyCopy = document.createElement('button')
    legacyCopy.className = 'code-copy-btn'
    legacyCopy.textContent = 'Copy source'
    source.querySelector('pre')!.appendChild(legacyCopy)

    const stage = buildShareDom([source], 'light')

    expect(stage.querySelector('pre')?.dataset.diagramLanguage).toBe('mermaid')
    expect(stage.querySelector('code')?.textContent).toBe('graph TD; A-->B')
    expect(stage.querySelector('code')?.hidden).toBe(true)
    expect(stage.querySelector('button')).toBeNull()
    expect(source.querySelectorAll('button')).toHaveLength(2)
  })
})

describe('buildShareDom protocol-shaped documentation', () => {
  it('clones the complete rendered message into the share image stage', () => {
    const text = [
      'Document `<tool_calls>` inline.',
      '```xml',
      '<tool_calls><invoke name="demo"></invoke></tool_calls>',
      '```',
      'Keep `<｜DSML｜tool_calls>` too.',
      '<details><summary>View areas around line 10</summary>Visible note.</details>',
      'Final suffix.',
    ].join('\n')
    const source = document.createElement('article')
    source.className = 'msg msg-ai'
    source.dataset.shareMessageId = 'assistant-literal'
    const body = document.createElement('div')
    body.className = 'msg-body'
    body.innerHTML = useChatTextRendering().renderMarkdown(text)
    source.appendChild(body)

    const stage = buildShareDom([source])
    const sharedText = stage.querySelector('.msg-body')?.textContent || ''

    expect(sharedText).toContain('<tool_calls>')
    expect(sharedText).toContain('<｜DSML｜tool_calls>')
    expect(sharedText).toContain('Visible note.')
    expect(sharedText).toContain('Final suffix.')
  })

  it('records the localized AI provenance label for the rendered PNG footer', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-provenance'

    const stage = buildShareDom(
      [source],
      'light',
      'Content generated by AI, for reference only.',
    )
    const metrics = JSON.parse(stage.dataset.shareTemplateMetrics || '{}')

    expect(metrics.disclaimer).toBe('Content generated by AI, for reference only.')
  })

  it('removes tool timeline controls from the static share image', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-tools'
    const toolbar = document.createElement('div')
    toolbar.className = 'tool-timeline__toolbar'
    toolbar.setAttribute('data-share-control', '')
    toolbar.innerHTML = '<span>3 calls</span><button type="button">Collapse all</button>'
    const toolResult = document.createElement('div')
    toolResult.className = 'tool-row-body'
    toolResult.textContent = 'tool output'
    source.append(toolbar, toolResult)

    const stage = buildShareDom([source])

    expect(stage.querySelector('.tool-timeline__toolbar')).toBeNull()
    expect(stage.querySelector('.tool-row-body')?.textContent).toBe('tool output')
  })

  it('removes turn usage details from the static share image', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-usage'
    source.innerHTML = [
      '<div data-share-activity data-share-expanded="true">',
      '<button data-share-activity-label data-share-control>Completed · 2s</button>',
      '<div data-share-activity-body>',
      '<div class="turn-usage-details">model-a · 321 input tokens</div>',
      '<div data-turn-usage-details>reasoning 45 · $0.0012</div>',
      '<div class="msg-ai-meta"><button class="msg-meta__more">Usage details</button></div>',
      '<div class="tool-row-body">Kept activity output</div>',
      '</div>',
      '</div>',
      '<div class="msg-meta__more">Legacy usage trigger</div>',
      '<div class="msg-meta__cost">$0.0012</div>',
      '<div class="msg-ai-text">Canonical answer</div>',
    ].join('')

    const stage = buildShareDom([source])

    expect(stage.querySelector('.turn-usage-details')).toBeNull()
    expect(stage.querySelector('[data-turn-usage-details]')).toBeNull()
    expect(stage.querySelector('.msg-ai-meta')).toBeNull()
    expect(stage.querySelector('.msg-meta__more')).toBeNull()
    expect(stage.querySelector('.msg-meta__cost')).toBeNull()
    expect(stage.textContent).toContain('Kept activity output')
    expect(stage.textContent).toContain('Canonical answer')
    expect(stage.textContent).not.toContain('model-a')
    expect(stage.textContent).not.toContain('321 input tokens')
    expect(stage.textContent).not.toContain('reasoning 45')
    expect(stage.textContent).not.toContain('$0.0012')
    expect(stage.textContent).not.toContain('Legacy usage trigger')
  })

  it('omits collapsed execution activity while keeping the canonical answer', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-activity'
    const activity = document.createElement('div')
    activity.dataset.shareActivity = ''
    activity.dataset.shareExpanded = 'false'
    // The stable data contract must win even if migration leaves legacy state
    // on a non-details activity root.
    activity.setAttribute('open', '')
    activity.innerHTML = [
      '<button data-share-activity-label>Activity · 2 steps</button>',
      '<div data-share-activity-body>',
      '<div>Private path: /Users/example/project</div>',
      '<div>Tool error: secret diagnostic</div>',
      '</div>',
    ].join('')
    const answer = document.createElement('div')
    answer.className = 'msg-ai-text'
    answer.textContent = 'Canonical answer'
    source.append(activity, answer)

    const stage = buildShareDom([source])

    expect(stage.querySelector('[data-share-activity]')).toBeNull()
    expect(stage.textContent).toContain('Canonical answer')
    expect(stage.textContent).not.toContain('/Users/example/project')
    expect(stage.textContent).not.toContain('secret diagnostic')
  })

  it('preserves explicitly expanded activity as a structured static block', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-expanded-activity'
    const activity = document.createElement('div')
    activity.dataset.shareActivity = ''
    activity.dataset.shareExpanded = 'true'
    activity.innerHTML = [
      '<button data-share-activity-label data-share-control>Activity · 2 steps</button>',
      '<div data-share-activity-body style="animation: pulse 1s infinite">',
      '<section class="thinking-block">Checked the evidence.</section>',
      '<div class="step-card"><div class="tool-row-body">',
      '<span class="tool-row__activity-arrow" data-share-control>›</span>',
      '<div class="activity-tool-details__summary">',
      '<span>games/index.html</span><span aria-hidden="true">·</span><span>30 lines</span>',
      '<button class="activity-tool-details__hit-target" data-share-control aria-label="View details"></button>',
      '</div>',
      '</div></div>',
      '<button class="activity-control" data-share-control>Expand result</button>',
      '</div>',
    ].join('')
    const answer = document.createElement('div')
    answer.className = 'msg-ai-text'
    answer.textContent = 'Canonical answer'
    source.append(activity, answer)

    const stage = buildShareDom([source])

    expect(stage.querySelector('[data-share-activity]')).toBeNull()
    expect(stage.querySelector('.chat-share-export-activity')).not.toBeNull()
    expect(stage.querySelector('.chat-share-export-activity__label')?.textContent)
      .toBe('Activity · 2 steps')
    expect(stage.querySelector('.thinking-block')?.textContent).toBe('Checked the evidence.')
    expect(stage.querySelector('.tool-row-body')?.textContent)
      .toBe('games/index.html·30 lines')
    expect(stage.querySelector('.tool-row__activity-arrow')).toBeNull()
    expect(stage.querySelector('.activity-tool-details__summary')?.textContent)
      .toBe('games/index.html·30 lines')
    expect(stage.querySelector('.activity-tool-details__hit-target')).toBeNull()
    expect(stage.textContent).not.toContain('View details')
    expect(stage.querySelector('.activity-control')).toBeNull()
    expect(stage.querySelector('style')?.textContent).toContain('animation: none !important')
    expect(stage.textContent?.match(/Canonical answer/g)).toHaveLength(1)
  })

  it('uncaps the reasoning scroll container so long reasoning is not clipped', () => {
    // ReasoningPart caps .thinking-block__body at max-height 16rem with a
    // scrollbar. The exported PNG has no scrollbar, so without a stage-scoped
    // reset a long trace inside an expanded activity fold would be silently
    // cut off with no indication anything is missing.
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-long-reasoning'
    source.innerHTML = [
      '<div data-share-activity data-share-expanded="true">',
      '<div data-share-activity-body>',
      '<section class="thinking-block">',
      '<div class="thinking-block__body">A very long reasoning trace.</div>',
      '</section>',
      '</div>',
      '</div>',
    ].join('')

    const stage = buildShareDom([source])
    const css = stage.querySelector('style')?.textContent || ''
    const bodyRule = css.match(/\.thinking-block__body\s*\{[^}]*\}/)?.[0] || ''

    expect(stage.querySelector('.thinking-block__body')?.textContent)
      .toBe('A very long reasoning trace.')
    expect(bodyRule).toContain('max-height: none !important;')
    expect(bodyRule).toContain('overflow-y: visible !important;')
  })

  it('keeps legacy details activity compatible during the data-contract migration', () => {
    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-legacy-activity'
    source.innerHTML = [
      '<details class="assistant-activity">',
      '<summary class="assistant-activity__summary">Collapsed legacy activity</summary>',
      '<div class="assistant-activity__body">Hidden legacy output</div>',
      '</details>',
      '<details class="assistant-activity" open>',
      '<summary class="assistant-activity__summary">Expanded legacy activity</summary>',
      '<div class="assistant-activity__body">Visible legacy output</div>',
      '</details>',
      '<div class="msg-ai-text">Canonical answer</div>',
    ].join('')

    const stage = buildShareDom([source])

    expect(stage.querySelector('.assistant-activity')).toBeNull()
    expect(stage.querySelectorAll('.chat-share-export-activity')).toHaveLength(1)
    expect(stage.textContent).not.toContain('Hidden legacy output')
    expect(stage.textContent).toContain('Visible legacy output')
    expect(stage.textContent?.match(/Canonical answer/g)).toHaveLength(1)
  })
})

describe('share export expanded tool content', () => {
  const apps: ReturnType<typeof createApp>[] = []

  afterEach(() => {
    while (apps.length) apps.pop()?.unmount()
    document.body.innerHTML = ''
  })

  function mountTool(render: () => ReturnType<typeof h>) {
    const host = document.createElement('article')
    host.dataset.shareMessageId = 'expanded-tool'
    document.body.appendChild(host)
    // Use the real renderer and its live stylesheet so the export must undo
    // the actual capped/scrolling treatment, not a synthetic export fixture.
    const style = document.createElement('style')
    style.textContent = [activityCodeSource, activityToolSource]
      .map(source => source.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1] || '')
      .join('\n')
    document.body.appendChild(style)
    const app = createApp({ render })
    app.use(i18n)
    app.mount(host)
    apps.push(app)
    return host
  }

  const longLine = `long-line-${'abcdef'.repeat(150)}-line-end`
  const retainedText = ['first-loaded-line', longLine,
    ...Array.from({ length: 80 }, (_, index) => `loaded line ${index}`), 'last-loaded-line'].join('\n')

  it.each<ActivityToolCodeView>([
    { kind: 'command', command: `echo ${longLine}`, output: retainedText, exitCode: 1, partial: true },
    { kind: 'write', path: 'generated.html', language: 'html', content: retainedText },
    { kind: 'edit', path: 'generated.svg', content: `--- before\n+++ after\n-${longLine}\n+${retainedText}` },
  ])('exports every loaded $kind line without cropping or changing the live viewport', (view) => {
    const host = mountTool(() => h(ActivityCodeDetails, { view }))
    const original = host.querySelector<HTMLElement>('.activity-code-details__viewport')!
    original.scrollTop = 260
    original.scrollLeft = 170
    const originalHtml = host.innerHTML
    expect(getComputedStyle(original).overflow).toBe('auto')

    const stage = buildShareDom([host])
    document.body.appendChild(stage)
    const shared = stage.querySelector<HTMLElement>('.activity-code-details__viewport')!
    const viewportStyle = getComputedStyle(shared)
    expect(shared.textContent).toBe(original.textContent)
    expect(shared.textContent).toContain('first-loaded-line')
    expect(shared.textContent).toContain('last-loaded-line')
    expect(shared.textContent).toContain(longLine)
    expect(viewportStyle.maxHeight).toBe('none')
    expect(viewportStyle.overflow).toBe('visible')
    expect(shared.scrollTop).toBe(0)
    expect(shared.scrollLeft).toBe(0)
    for (const code of shared.querySelectorAll<HTMLElement>('pre, code')) {
      const style = getComputedStyle(code)
      expect(style.whiteSpace).toBe('pre-wrap')
      expect(style.overflowWrap).toBe('anywhere')
      expect(style.width).toBe('100%')
    }
    // Retained-log incompleteness remains honest; exporting never fetches the
    // missing pages or pretends the loaded rows are the complete result.
    if (view.partial) expect(shared.querySelector('.activity-code-details__note')?.textContent)
      .toBe(original.querySelector('.activity-code-details__note')?.textContent)
    expect(stage.querySelector('[data-share-control]')).toBeNull()
    expect(host.innerHTML).toBe(originalHtml)
    expect(original.scrollTop).toBe(260)
    expect(original.scrollLeft).toBe(170)
    expect(getComputedStyle(original).overflow).toBe('auto')
  })

  it('uncaps both layers of a generic tool result and keeps its last loaded row', () => {
    const call: ChatToolCallRenderItem = {
      toolId: 'generic', renderKey: 'generic', name: 'custom_tool', displayName: 'Custom tool',
      inputRaw: '', inputPreview: '', result: retainedText, resultPreview: retainedText.slice(0, 200),
      isRunning: false, status: 'success', isError: false, isOpen: true,
    }
    const host = mountTool(() => h(ActivityToolDetails, { call, operationKey: 'tool.custom', label: 'Custom tool' }))
    const original = host.querySelector<HTMLElement>('.activity-tool-details__window')!
    original.scrollTop = 500
    const stage = buildShareDom([host])
    document.body.appendChild(stage)

    for (const selector of ['.activity-tool-details--bounded', '.activity-tool-details__window']) {
      const element = stage.querySelector<HTMLElement>(selector)!
      expect(getComputedStyle(element).maxHeight).toBe('none')
      expect(getComputedStyle(element).overflow).toBe('visible')
      expect(element.scrollTop).toBe(0)
    }
    expect(stage.querySelector('.activity-tool-details__preview')?.textContent).toBe(retainedText)
    expect(stage.querySelector('[data-share-control]')).toBeNull()
    expect(original.scrollTop).toBe(500)
    expect(host.querySelector('[data-testid="activity-tool-detail-copy"]')).not.toBeNull()
  })
})

describe('share export label localization', () => {
  afterEach(() => {
    i18n.global.locale.value = 'en'
  })

  it('localizes the activity and thinking fallback labels baked into the image', () => {
    // The exported PNG is static: any fallback label the composable writes is
    // burned in, so it must follow the UI locale rather than hardcode English.
    i18n.global.setLocaleMessage('zh-Hans', zhHans)
    i18n.global.locale.value = 'zh-Hans'

    const source = document.createElement('article')
    source.dataset.shareMessageId = 'assistant-localized'
    source.innerHTML = [
      '<div data-share-activity data-share-expanded="true">',
      '<div data-share-activity-body><div>step output</div></div>',
      '</div>',
      '<details class="thinking-fold" open>',
      '<summary class="thinking-fold__summary"></summary>',
      '<div class="thinking-fold__body">Weighed the options.</div>',
      '</details>',
    ].join('')

    const stage = buildShareDom([source])

    expect(stage.querySelector('.chat-share-export-activity__label')?.textContent).toBe('活动')
    expect(stage.querySelector('.chat-share-export-thinking__label')?.textContent).toBe('思考中')
  })
})

describe('share export static asset URLs', () => {
  afterEach(() => {
    document.getElementById('opensquilla-data')?.remove()
  })

  it('keeps Desktop root assets on the current origin', () => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/'
    document.body.appendChild(data)

    const assetUrl = staticAssetUrl('img/QRcode.png')
    expect(assetUrl).toBe('/static/img/QRcode.png')
    expect(new URL(assetUrl, 'opensquilla-app://desktop/chat/new').href)
      .toBe('opensquilla-app://desktop/static/img/QRcode.png')
  })

  it('preserves a trailing-slash gateway base path', () => {
    const data = document.createElement('div')
    data.id = 'opensquilla-data'
    data.dataset.basePath = '/control/'
    document.body.appendChild(data)

    expect(staticAssetUrl('/img/opensquilla-mark.png'))
      .toBe('/control/static/img/opensquilla-mark.png')
  })
})

// Pins the renderer/export boundary against the REAL ActivityDisclosure
// markup: the data-share-* hooks are a contract, and restyling the fold must
// not silently drop expanded activity from (or leak collapsed activity into)
// the share image.
describe('ActivityDisclosure share hook contract', () => {
  const mountedApps: ReturnType<typeof createApp>[] = []

  function mountDisclosure(props: InstanceType<typeof ActivityDisclosure>['$props']) {
    const host = document.createElement('article')
    host.dataset.shareMessageId = 'assistant-disclosure'
    document.body.appendChild(host)
    const app = createApp({
      render: () => h(ActivityDisclosure, props, {
        default: () => h('div', { class: 'activity-step' }, 'step output'),
      }),
    })
    mountedApps.push(app)
    app.use(i18n)
    app.mount(host)
    return host
  }

  afterEach(() => {
    while (mountedApps.length) mountedApps.pop()?.unmount()
    document.body.innerHTML = ''
  })

  it('exports the expanded settled fold through the data-share hooks', () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 2,
      failureCount: 0,
      summaryLabel: 'Ran 2 tools',
      defaultOpen: true,
    })
    const activity = host.querySelector<HTMLElement>('[data-share-activity]')

    expect(activity).not.toBeNull()
    expect(activity?.dataset.shareExpanded).toBe('true')
    const label = activity?.querySelector<HTMLElement>('[data-share-activity-label]')
    expect(label?.textContent).toContain('Ran 2 tools')
    // The interactive summary chrome must keep opting out of the static image.
    expect(label?.hasAttribute('data-share-control')).toBe(true)
    expect(activity?.querySelector('[data-share-activity-body]')).not.toBeNull()

    const stage = buildShareDom([host])

    expect(stage.querySelector('[data-share-activity]')).toBeNull()
    expect(stage.querySelector('.chat-share-export-activity__label')?.textContent)
      .toContain('Ran 2 tools')
    expect(stage.querySelector('.chat-share-export-activity__body')?.textContent)
      .toContain('step output')
  })

  it('drops the collapsed settled fold from the share image', () => {
    const host = mountDisclosure({
      lifecycle: 'settled',
      stepCount: 2,
      failureCount: 0,
      summaryLabel: 'Ran 2 tools',
    })

    expect(
      host.querySelector<HTMLElement>('[data-share-activity]')?.dataset.shareExpanded,
    ).toBe('false')

    const stage = buildShareDom([host])

    expect(stage.querySelector('.chat-share-export-activity')).toBeNull()
    expect(stage.textContent).not.toContain('step output')
  })

  it('drops live activity from the share image while it is collapsed', () => {
    const host = mountDisclosure({
      lifecycle: 'working',
      stepCount: 3,
      failureCount: 0,
      phaseLabel: 'Running commands',
    })
    const activity = host.querySelector<HTMLElement>('[data-share-activity]')

    expect(activity?.dataset.shareExpanded).toBe('false')
    expect(activity?.querySelector('[data-share-activity-label]')).not.toBeNull()
    expect(activity?.querySelector('[data-share-activity-body]')).not.toBeNull()

    const stage = buildShareDom([host])

    expect(stage.querySelector('.chat-share-export-activity')).toBeNull()
    expect(stage.textContent).not.toContain('step output')
  })
})
