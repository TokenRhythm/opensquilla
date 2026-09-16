// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import AssistantMessage from './AssistantMessage.vue'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import type { WorkbenchResource } from '@/types/workbenchResources'

const apps: App[] = []
const result = {
  documentId: 'doc_weather', resourceId: 'document:doc_weather',
  entrypoint: '/private/task/site/beijing-weather.html', previewStatus: 'ready',
  workspace: '/private/task',
  open: { resourceId: 'document:doc_weather' },
}
function call(overrides: Partial<ChatToolCall> = {}): ChatToolCall {
  return {
    toolId: 'preview-1', name: 'open_workspace_preview', displayName: 'open_workspace_preview',
    inputPreview: '', isRunning: false, status: 'success', isError: false,
    result: JSON.stringify(result), resultPreview: '', isOpen: false, ...overrides,
  }
}
async function mount(calls: ChatToolCall[], extras: Partial<ChatRenderedMessage> = {},
  resolveWorkspacePreviewResource?: (key: string, documentId: string) => Promise<WorkbenchResource | null>) {
  const onOpenArtifact = vi.fn()
  const el = document.createElement('div')
  document.body.appendChild(el)
  const message = reactive<ChatRenderedMessage>({
    role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant',
    text: 'Your page is ready.', timeStr: '', showHeader: false, toolCalls: calls, ...extras,
  })
  const host = reactive({ sessionKey: 'agent:main:webchat:weather', resolveWorkspacePreviewResource })
  const app = createApp({ render: () => h(AssistantMessage, {
    message,
    index: 0, shareMode: false, shareSelected: false, shareMessageId: 'assistant-0',
    ...host, workbenchEnabled: true,
    renderMarkdown: useChatTextRendering().renderMarkdown, fmtTok: String, toolCallGroups: () => [],
    isToolGroupOpen: () => false, isToolItemOpen: () => false,
    toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '',
    copyMessage: async () => true, onOpenArtifact,
  }) })
  apps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  return { el, onOpenArtifact, message, host }
}
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('workspace preview message action', () => {
  it('uses an unframed answer line when restored output has no inline file reference', async () => {
    const { el, onOpenArtifact } = await mount([call()])
    const button = el.querySelector<HTMLButtonElement>('.workspace-preview-fallback .workspace-file-link')!
    expect(button.textContent).toContain('beijing-weather.html')
    expect(button.closest('.assistant-answer')).not.toBeNull()
    expect(button.closest('.msg-ai-ending')).toBeNull()
    expect(el.querySelector('.workspace-preview-link')).toBeNull()
    expect(el.textContent).not.toContain('/private/task')
    expect(onOpenArtifact).not.toHaveBeenCalled()
    button.click()
    await nextTick()
    expect(onOpenArtifact).toHaveBeenCalledWith({
      source: 'workspace-preview', documentId: 'doc_weather', name: 'beijing-weather.html',
      mime: 'text/html', session_key: 'agent:main:webchat:weather',
    })
    expect(el.querySelector('a[download]')).toBeNull()
  })

  it('merges repeated opens of the same Document into one action', async () => {
    const { el } = await mount([call(), call({ toolId: 'preview-2' })])
    expect(el.querySelectorAll('.workspace-file-link')).toHaveLength(1)
  })

  it.each([
    { name: 'read_file' }, { isRunning: true }, { status: 'error' as const, isError: true },
    { result: 'not JSON', resultPreview: JSON.stringify(result) },
    { result: JSON.stringify({ ...result, resourceId: 'document:doc_other' }) },
    { result: JSON.stringify({ ...result, open: { resourceId: 'document:doc_other' } }) },
    { result: JSON.stringify({ ...result, is_error: true }) },
    { result: JSON.stringify({ ...result, previewStatus: 'unavailable' }) },
    { result: JSON.stringify({ ...result, entrypoint: 'bad\nname.html' }) },
  ])('does not authorize an action from invalid output %j', async overrides => {
    const { el } = await mount([call(overrides)], { text: '`beijing-weather.html`' })
    expect(el.querySelector('.workspace-file-link')).toBeNull()
  })

  it('does not turn model prose paths into open actions', async () => {
    const { el } = await mount([], { text: 'Opened /private/task/beijing-weather.html on the right.' })
    expect(el.querySelector('.workspace-file-link')).toBeNull()
  })

  it.each(['beijing-weather.html', 'site/beijing-weather.html', './site/beijing-weather.html',
    '/private/task/site/beijing-weather.html'])('opens complete inline reference %s without duplicate fallback', async path => {
    const text = `Your page is ready: \`${path}\`.`
    const { el, onOpenArtifact, message } = await mount([call()], { text })
    const link = el.querySelector<HTMLButtonElement>('.msg-ai-text .workspace-file-link')!
    expect(link.textContent).toBe(path)
    expect(link.getAttribute('role')).toBe('link')
    expect(link.type).toBe('button')
    expect(link.getAttribute('aria-label')).toContain('beijing-weather.html')
    expect(link.getAttribute('href')).toBeNull()
    expect(el.querySelector('.workspace-preview-fallback')).toBeNull()
    expect(el.querySelector('.msg-ai-ending .workspace-file-link')).toBeNull()
    expect(onOpenArtifact).not.toHaveBeenCalled()
    link.focus()
    expect(document.activeElement).toBe(link)
    link.click()
    await nextTick()
    expect(onOpenArtifact).toHaveBeenCalledWith(expect.objectContaining({
      documentId: 'doc_weather', source: 'workspace-preview',
    }))
    expect(message.text).toBe(text)
    expect(message.artifacts).toBeUndefined()
  })

  it.each([
    'beijing-weather.html',
    '`open beijing-weather.html now`',
    '```html\nbeijing-weather.html\n```',
    '[`beijing-weather.html`](https://example.com/weather)',
    '`other/beijing-weather.html`',
  ])('does not upgrade prose, code samples, existing links or partial paths: %s', async text => {
    const { el } = await mount([call()], { text })
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
  })

  it('preserves ordinary web links and code-copy chrome', async () => {
    const { el } = await mount([call()], {
      text: '[Website](https://example.com)\n\n```html\nbeijing-weather.html\n```',
    })
    expect(el.querySelector('a[href="https://example.com"]')).not.toBeNull()
    expect(el.querySelector('pre .code-copy-btn')).not.toBeNull()
    expect(el.querySelector('pre .workspace-file-link')).toBeNull()
  })

  it('does not guess ambiguous basenames and labels remaining previews with relative paths', async () => {
    const other = { ...result, documentId: 'doc_other', resourceId: 'document:doc_other',
      entrypoint: '/private/task/other/beijing-weather.html', open: { resourceId: 'document:doc_other' } }
    const { el, onOpenArtifact, message } = await mount([
      call(), call({ toolId: 'preview-other', result: JSON.stringify(other) }),
    ], { text: '`beijing-weather.html` and `site/beijing-weather.html`' })
    expect(el.querySelectorAll('.msg-ai-text .workspace-file-link')).toHaveLength(1)
    const fallback = el.querySelector<HTMLButtonElement>('.workspace-preview-fallback .workspace-file-link')!
    expect(fallback.textContent).toBe('other/beijing-weather.html')
    fallback.click()
    expect(onOpenArtifact).toHaveBeenCalledWith(expect.objectContaining({ documentId: 'doc_other' }))
    message.text = '`beijing-weather.html`'
    await nextTick()
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(2)
  })

  it('reconciles streaming body changes and late tool results without stale links or duplicate handlers', async () => {
    const { el, onOpenArtifact, message } = await mount([], { text: '`beijing-weather.html`' })
    expect(el.querySelector('.workspace-file-link')).toBeNull()
    message.toolCalls = [call()]
    await nextTick()
    expect(el.querySelectorAll('.workspace-file-link')).toHaveLength(1)
    expect(el.querySelector('.workspace-preview-fallback')).toBeNull()
    message.toolCalls = [call(), call({ toolId: 'preview-again' })]
    await nextTick()
    message.text = 'Updated: `beijing-weather.html`.'
    await nextTick()
    el.querySelector<HTMLButtonElement>('.workspace-file-link')!.click()
    expect(onOpenArtifact).toHaveBeenCalledOnce()
    expect(el.querySelectorAll('.workspace-file-link')).toHaveLength(1)
    message.text = 'Updated.'
    await nextTick()
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
    message.text = '`beijing-weather.html`'
    await nextTick()
    message.toolCalls = []
    await nextTick()
    expect(el.querySelector('.workspace-file-link')).toBeNull()
    expect(el.querySelector('.msg-ai-text code')?.textContent).toBe('beijing-weather.html')
  })

  it('provides a light link even when no final answer exists', async () => {
    const { el } = await mount([call()], { text: '' })
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
    expect(el.querySelector('.msg-ai-ending .workspace-file-link')).toBeNull()
  })

  it('keeps all repeated references clickable but shows no extra preview row', async () => {
    const { el } = await mount([call()], { text: 'Open `beijing-weather.html`.\n\nEdit `site/beijing-weather.html`.' })
    expect(el.querySelectorAll('.msg-ai-text .workspace-file-link')).toHaveLength(2)
    expect(el.querySelector('.workspace-preview-fallback')).toBeNull()
  })

  it('hydrates four links from an old one-entry tool result without rewriting history or adding Documents', async () => {
    const existing = call({ result: JSON.stringify({ ...result, bundleMode: 'directory', bundleRoot: 'site' }) })
    const resource = { resource: { type: 'document', documentId: 'doc_weather' },
      capabilities: { preview: true },
      previewPages: ['beijing-weather.html', 'culture.html', 'food.html', 'places.html'],
    } as WorkbenchResource
    const resolve = vi.fn().mockResolvedValue(resource)
    const text = '首页 `site/beijing-weather.html`，文化 `site/culture.html`，美食 `site/food.html`，景点 `site/places.html`。'
    const { el, onOpenArtifact, message } = await mount([existing], { text }, resolve)
    await vi.waitFor(() => expect(el.querySelectorAll('.msg-ai-text .workspace-file-link')).toHaveLength(4))
    expect(el.querySelector('.workspace-preview-fallback')).toBeNull()
    expect(resolve).toHaveBeenCalledOnce()
    expect(resolve).toHaveBeenCalledWith('agent:main:webchat:weather', 'doc_weather')
    const links = el.querySelectorAll<HTMLButtonElement>('.msg-ai-text .workspace-file-link')
    links[1]!.click()
    expect(onOpenArtifact).toHaveBeenCalledWith(expect.objectContaining({
      documentId: 'doc_weather', previewPagePath: 'culture.html', source: 'workspace-preview',
    }))
    expect(message.text).toBe(text)
    expect(message.toolCalls?.[0]?.result).toBe(existing.result)
    expect(message.artifacts).toBeUndefined()
    message.text = '页面已完成。'
    await nextTick()
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
    expect(el.textContent).not.toContain('culture.html')
  })

  it('does not decorate unknown subpages when resource hydration fails', async () => {
    const existing = call({ result: JSON.stringify({ ...result, bundleMode: 'directory', bundleRoot: 'site' }) })
    const resolve = vi.fn().mockRejectedValue(new Error('ACCESS_DENIED'))
    const { el } = await mount([existing], { text: '`site/unknown.html`' }, resolve)
    await vi.waitFor(() => expect(resolve).toHaveBeenCalledOnce())
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
  })

  it('refreshes the page inventory for a new successful tool identity, not body streaming or replay', async () => {
    const existing = call({ result: JSON.stringify({ ...result, bundleMode: 'directory', bundleRoot: 'site' }) })
    const resource = { resource: { type: 'document', documentId: 'doc_weather' },
      capabilities: { preview: true }, previewPages: ['beijing-weather.html'],
    } as WorkbenchResource
    const resolve = vi.fn().mockResolvedValue(resource)
    const { el, message } = await mount([existing], { text: '`site/culture.html`' }, resolve)
    await vi.waitFor(() => expect(resolve).toHaveBeenCalledOnce())
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
    message.text = '文化页：`site/culture.html`。'
    message.toolCalls = [{ ...existing }]
    await nextTick()
    expect(resolve).toHaveBeenCalledOnce()

    resolve.mockResolvedValue({ ...resource, previewPages: ['beijing-weather.html', 'culture.html'] })
    const reopened = call({ toolId: 'preview-2', result: existing.result })
    message.toolCalls = [existing, reopened]
    await vi.waitFor(() => expect(el.querySelectorAll('.msg-ai-text .workspace-file-link')).toHaveLength(1))
    expect(resolve).toHaveBeenCalledTimes(2)
    expect(el.querySelectorAll('.workspace-preview-fallback .workspace-file-link')).toHaveLength(1)
    message.toolCalls = [existing, reopened, { ...reopened }]
    await nextTick()
    expect(resolve).toHaveBeenCalledTimes(2)
  })

  it('discards pending site metadata after a session switch, even when switching back', async () => {
    const existing = call({ result: JSON.stringify({ ...result, bundleMode: 'directory', bundleRoot: 'site' }) })
    let finish!: (value: WorkbenchResource) => void
    const pending = new Promise<WorkbenchResource>(done => { finish = done })
    const resolve = vi.fn().mockReturnValueOnce(pending).mockResolvedValue(null)
    const { el, host } = await mount([existing], { text: '`site/culture.html`' }, resolve)
    host.sessionKey = 'agent:main:webchat:other'
    await nextTick()
    host.sessionKey = 'agent:main:webchat:weather'
    await nextTick()
    finish({ resource: { type: 'document', documentId: 'doc_weather' },
      capabilities: { preview: true }, previewPages: ['culture.html'],
    } as WorkbenchResource)
    await nextTick()
    await nextTick()
    expect(el.querySelector('.msg-ai-text .workspace-file-link')).toBeNull()
  })
})
