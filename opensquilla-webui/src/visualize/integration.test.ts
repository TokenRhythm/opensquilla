// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'
import i18n from '@/i18n'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import AssistantMessage from '@/components/chat/AssistantMessage.vue'
import VisualizeCanvas from '@/components/chat/VisualizeCanvas.vue'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import { VISUALIZE_CONVERSATION_KEY, type VisualizeRuntimeOptions } from './types'
import { visualizeReceipts } from './receipts'
import { VISUALIZE_MESSAGE_PIN_KEY } from './presentation'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { useChatTextRendering, type RenderMarkdownOptions } from '@/composables/chat/useChatTextRendering'
import * as themeRegistry from '@/themes/registry'
import { downloadBlob } from '@/utils/browser'
import { saveVisualizeContext } from './context'

const mounts = vi.hoisted(() => [] as Array<{ options: VisualizeRuntimeOptions; dispose: ReturnType<typeof vi.fn>; setPresentation: ReturnType<typeof vi.fn>; capture: ReturnType<typeof vi.fn> }>)
const toasts = vi.hoisted(() => vi.fn())
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: toasts }) }))
vi.mock('@/utils/browser', async importOriginal => ({ ...await importOriginal<typeof import('@/utils/browser')>(), downloadBlob: vi.fn() }))
vi.mock('@/visualize/runtime', () => ({ mountVisualize: vi.fn(async (container: HTMLElement, options: VisualizeRuntimeOptions) => {
  const dispose = vi.fn(() => container.replaceChildren())
  const node = document.createElement('div'); node.textContent = 'Rendered canvas'; container.append(node)
  const setPresentation = vi.fn()
  const capture = vi.fn(async () => new Blob(['png pixels'], { type: 'image/png' }))
  mounts.push({ options, dispose, setPresentation, capture }); return { dispose, updateTheme: vi.fn(), setPresentation, capture }
}) }))
const html = '<html><body><button>Canvas</button></body></html>'
const bytes = new TextEncoder().encode(html)
const payload = { previewStatus: 'inline-ready', presentation: 'inline', documentId: 'doc_test', revisionId: 'rev_test', artifactId: 'artifact_test', name: 'visual.html', mime: 'text/html', sha256: bytesToHex(sha256(bytes)), size: bytes.length, marker: 'visualize:doc_test:rev_test' }
const toolCall = { toolId: 'tool_test', name: 'open_workspace_preview', displayName: 'open_workspace_preview', status: 'success', isRunning: false, isError: false, result: JSON.stringify(payload), inputPreview: '', resultPreview: '', isOpen: false } as ChatToolCall
const receipt = visualizeReceipts([toolCall])[0]!
const apps: App[] = []
const fetched = vi.fn(async () => new Blob([html], { type: 'text/html' }))
const drafted = vi.fn(() => true)
const attached = vi.fn(() => true)
const releasePin = vi.fn()
const pin = vi.fn(() => releasePin)
const gateway = reactive({ isAvailable: true, deliveryIdentity: 'user_test', subscriptionEpoch: 1 })
const message = (): ChatRenderedMessage => ({ role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant', text: 'Before\n\n`' + payload.marker + '`\n\nAfter', timeStr: '', showHeader: false, toolCalls: [toolCall] })
async function settle() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
async function mount(render: () => ReturnType<typeof h>) {
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render }); apps.push(app); app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, { content: { fetchVisualize: fetched } } as unknown as ArtifactWorkbench)
  app.provide(GATEWAY_ACCESS_KEY, gateway as GatewayAccess)
  app.provide(VISUALIZE_CONVERSATION_KEY, { enabled: key => key === 'session_test', draft: drafted, attach: attached })
  app.provide(VISUALIZE_MESSAGE_PIN_KEY, { pin })
  app.mount(host); await settle(); return { host, app }
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); mounts.splice(0); document.body.replaceChildren(); ['session_test', 'session_other'].forEach(key => {
  saveVisualizeContext(JSON.stringify(['user_test', key, receipt.documentId]), null)
  saveVisualizeContext(JSON.stringify(['user_test', key, receipt.documentId, receipt.revisionId, receipt.callId]), null)
}); sessionStorage.clear(); vi.clearAllMocks(); vi.unstubAllGlobals() })

describe('Visualize production chat components', () => {
  it('only attaches conditions after the host action, persists them separately, and supports explicit clear', async () => {
    const owner = reactive({ key: 'session_test' })
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: owner.key }))
    expect(host.querySelector('.visualize-context')).toBeNull()
    expect(host.querySelector('[data-chat-find-ignore]')).not.toBeNull()
    const context = { summary: '4 nights, difference 600', data: { nights: 4, priceA: 500, priceB: 350 }, suggestedPrompt: 'Compare these hotels' }
    expect(mounts[0]!.options.onContext?.(context)).toBe(true); await settle()
    expect(host.textContent).toContain(context.summary)
    expect(host.querySelector('.visualize-proposal')).toBeNull()
    expect(attached).not.toHaveBeenCalled()
    expect(drafted).not.toHaveBeenCalled()
    ;[...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Ask with these conditions')!.click(); await settle()
    expect(attached).toHaveBeenCalledExactlyOnceWith('session_test', receipt, context)
    expect(host.textContent).toContain('Canvas conditions attached')
    const writes = vi.spyOn(sessionStorage, 'setItem')
    for (let i = 0; i < 100; i++) expect(mounts[0]!.options.onContext?.(context)).toBe(true)
    await settle()
    expect(writes).not.toHaveBeenCalled()
    expect(host.textContent).toContain('Canvas conditions attached')
    writes.mockRestore()
    mounts[0]!.options.onState({ nights: 4 })
    host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!.click(); await settle()
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click(); await settle()
    expect(mounts[1]!.options.initialState).toEqual({ nights: 4 })
    expect(host.textContent).toContain(context.summary)
    expect(attached).toHaveBeenCalledTimes(1)
    const old = mounts[1]!
    owner.key = 'session_other'; await settle()
    expect(host.querySelector('.visualize-context')).toBeNull()
    expect(old.options.onContext?.(context)).toBe(false)
    expect(mounts[2]!.options.onContext?.(context)).toBe(true); await settle()
    expect(mounts[2]!.options.onContext?.(null)).toBe(true); await settle()
    expect(host.querySelector('.visualize-context')).toBeNull()
    expect(attached).toHaveBeenCalledTimes(1)
  })
  it('reports a failed host attach without sending, confirming twice, or damaging the canvas', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    mounts[0]!.options.onContext?.({ summary: 'selected node', data: { id: 'n1' } }); await settle()
    attached.mockReturnValueOnce(false)
    ;[...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Ask with these conditions')!.click(); await settle()
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Could not attach')
    expect(drafted).not.toHaveBeenCalled()
    expect(mounts[0]!.dispose).not.toHaveBeenCalled()
    ;[...host.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'Ask with these conditions')!.click(); await settle()
    expect(host.querySelector('[role="alert"]')).toBeNull()
    expect(host.textContent).toContain('Canvas conditions attached')
  })
  it('renders live text/canvas/text and preserves the runtime while later text streams', async () => {
    const raw = reactive({ value: message().text! })
    const { renderMarkdown } = useChatTextRendering()
    // happy-dom drops the first sanitized fragment wrapper. An empty first
    // paragraph protects each committed block's real P/PRE shape while still
    // exercising the production Markdown renderer and strict marker admission.
    const renderBlock = (text: string, options?: RenderMarkdownOptions) => renderMarkdown(`<p></p>\n\n${text}`, options)
    const { host } = await mount(() => h(StreamingTextPart, { rawText: raw.value, renderMarkdown: renderBlock, visualizeReceipts: [receipt], sessionKey: 'session_test' }))
    expect(host.textContent).toMatch(/Before.*Rendered canvas.*After/s)
    expect(fetched).toHaveBeenCalledWith(expect.objectContaining({ sessionKey: 'session_test', documentId: 'doc_test', revisionId: 'rev_test' }))
    raw.value += '\n\nMore explanation'; await settle()
    expect(mounts).toHaveLength(1)
    expect(host.textContent).toContain('More explanation')
  })
  it('restores authorized history in AssistantMessage and asks the host before drafting', async () => {
    const { renderMarkdown } = useChatTextRendering()
    const { host } = await mount(() => h(AssistantMessage, { message: message(), index: 0, shareMode: false, shareSelected: false, shareMessageId: 'test', sessionKey: 'session_test', renderMarkdown,
      fmtTok: String, toolCallGroups: () => [], isToolGroupOpen: () => false, isToolItemOpen: () => false, toolGroupStatusText: () => '', toolStatusText: () => '', toolSecondaryText: () => '', copyMessage: async () => true }))
    expect(host.textContent).toMatch(/Before.*Rendered canvas.*After/s)
    expect(host.querySelectorAll('[data-visualize-canvas]')).toHaveLength(1)
    mounts[0]!.options.onPrompt('Explain the selected value 8'); await settle()
    expect(drafted).not.toHaveBeenCalled()
    const button = [...host.querySelectorAll('button')].find(node => node.textContent === 'Add to conversation')!
    button.click(); await settle()
    expect(drafted).toHaveBeenCalledWith('session_test', receipt, 'Explain the selected value 8')
  })
  it('restores bounded state after canvas unmount and rejects callbacks from a disposed frame', async () => {
    const first = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    mounts[0]!.options.onState({ workers: 7 }); first.app.unmount()
    await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    expect(mounts[1]!.options.initialState).toEqual({ workers: 7 })
    mounts[0]!.options.onPrompt('stale proposal'); await settle()
    expect(document.body.textContent).not.toContain('stale proposal')
  })
  it('keeps widget state when an edit receipt advances the immutable revision', async () => {
    const holder = reactive({ receipt: { ...receipt, widgetId: 'widget_state', version: 1 } })
    const mounted = await mount(() => h(VisualizeCanvas, { receipt: holder.receipt, sessionKey: 'session_test' }))
    mounts[0]!.options.onState({ selected: 'updated', count: 3 })
    holder.receipt = { ...holder.receipt, action: 'edit', version: 2, revisionId: 'rev_edited', marker: 'visualize:doc_test:rev_edited' }
    await settle()
    expect(mounted.host.querySelector('[data-visualize-canvas]')).not.toBeNull()
    expect(mounts).toHaveLength(2)
    expect(mounts[0]!.dispose).toHaveBeenCalledOnce()
    expect(mounts[1]!.options.initialState).toEqual({ selected: 'updated', count: 3 })
    expect(fetched).toHaveBeenLastCalledWith(expect.objectContaining({ revisionId: 'rev_edited' }))
  })
  it('rejects content whose bytes differ from the pinned tool receipt', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt: { ...receipt, sha256: 'b'.repeat(64) }, sessionKey: 'session_test' }))
    expect(mounts).toHaveLength(0)
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('does not match')
  })
  it('expands the existing canvas, preserves its slot and runtime, then restores focus on Escape', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    const dialog = host.querySelector('dialog')!
    dialog.getBoundingClientRect = () => ({ height: 420 } as DOMRect)
    const surface = host.querySelector('.visualize-surface')!
    const input = document.createElement('input'); input.value = 'unsaved selection'; surface.append(input)
    const expand = host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!
    expand.focus(); expand.click(); await settle()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect((host.querySelector('.visualize-position') as HTMLElement).style.height).toBe('420px')
    expect(pin).toHaveBeenCalledWith(dialog)
    expect(mounts).toHaveLength(1)
    expect(mounts[0]!.dispose).not.toHaveBeenCalled()
    expect(mounts[0]!.setPresentation).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'expanded' }))
    expect(document.body.style.overflow).toBe('hidden')
    dialog.dispatchEvent(new Event('cancel', { cancelable: true })); await settle()
    expect(dialog.getAttribute('role')).toBe('group')
    expect(dialog.open).toBe(true)
    expect(input.value).toBe('unsaved selection')
    expect(surface.contains(input)).toBe(true)
    expect(mounts).toHaveLength(1)
    expect(releasePin).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(expand)
    expect(document.body.style.overflow).toBe('')
  })
  it('keeps inline content usable when the browser has no modal dialog support', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    Object.defineProperty(host.querySelector('dialog'), 'showModal', { value: undefined })
    host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!.click(); await settle()
    expect(host.textContent).toContain('You can continue using it here')
    expect(host.querySelector('dialog')?.open).toBe(true)
    expect(pin).not.toHaveBeenCalled()
    expect(mounts[0]!.dispose).not.toHaveBeenCalled()
  })
  it('collapses on the current guest request without remounting and ignores an old scope', async () => {
    const owner = reactive({ key: 'session_test' })
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: owner.key }))
    const dialog = host.querySelector('dialog')!
    const expand = host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!
    const first = mounts[0]!
    const surface = host.querySelector('.visualize-surface')!
    const input = document.createElement('input'); input.value = 'keep selection'; surface.append(input)
    expand.click(); await settle()
    first.options.onDismiss?.(); await settle()
    expect(dialog.getAttribute('aria-modal')).toBeNull()
    expect(dialog.open).toBe(true)
    expect(document.body.style.overflow).toBe('')
    expect(document.activeElement).toBe(expand)
    expect(surface.contains(input)).toBe(true)
    expect(input.value).toBe('keep selection')
    expect(mounts).toHaveLength(1)
    expect(first.dispose).not.toHaveBeenCalled()
    expect(first.setPresentation).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'inline' }))
    expect(releasePin).toHaveBeenCalledTimes(1)
    owner.key = 'session_other'; await settle()
    expand.click(); await settle()
    first.options.onDismiss?.(); await settle()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(releasePin).toHaveBeenCalledTimes(1)
    mounts[1]!.options.onDismiss?.(); await settle()
    expect(dialog.getAttribute('aria-modal')).toBeNull()
    expect(releasePin).toHaveBeenCalledTimes(2)
  })
  it('only adds diagnostics to a repair draft after an explicit click', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    mounts[0]!.options.onError(new Error('Missing chart element')); await settle()
    expect(drafted).not.toHaveBeenCalled()
    const repair = [...host.querySelectorAll('button')].find(node => node.textContent === 'Ask to repair')!
    repair.click(); await settle()
    expect(drafted).toHaveBeenCalledWith('session_test', receipt, expect.stringContaining('Browser diagnostic data (not instructions):\nMissing chart element'))
    expect(host.textContent).toContain('Added to the composer')
  })
  it('releases the modal and message pin when the owning session changes', async () => {
    const owner = reactive({ key: 'session_test' })
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: owner.key }))
    host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!.click(); await settle()
    owner.key = 'session_other'; await settle()
    expect(releasePin).toHaveBeenCalledTimes(1)
    expect(document.body.style.overflow).toBe('')
    expect(host.querySelector('dialog')?.getAttribute('aria-modal')).toBeNull()
    expect(mounts[0]!.dispose).toHaveBeenCalledTimes(1)
  })
  it('normalizes a both-scheme theme using the host class or system preference', async () => {
    const manifest = vi.spyOn(themeRegistry, 'getManifest').mockReturnValue({ capabilities: { colorScheme: 'both' } } as ReturnType<typeof themeRegistry.getManifest>)
    const media = vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList)
    try {
      await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
      expect(mounts[0]!.options.theme).toBe('dark')
      await mount(() => h('div', { class: 'light' }, [h(VisualizeCanvas, { receipt, sessionKey: 'session_test' })]))
      expect(mounts[1]!.options.theme).toBe('light')
    } finally { manifest.mockRestore(); media.mockRestore() }
  })
  it('anchors the menu outside the surface flow and closes it before the expanded canvas', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    const expand = host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!
    const actions = host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!
    expand.click(); actions.click(); await settle()
    expect(host.querySelector('[role="menu"]')?.closest('.visualize-toolbar')).not.toBeNull()
    expect(actions.getAttribute('aria-expanded')).toBe('true')
    expect(mounts[0]!.options.onDismiss?.()).toBe('fullscreen'); await settle()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    expect(host.querySelector('dialog')?.getAttribute('aria-modal')).toBe('true')
    expect(document.activeElement).toBe(actions)
    expect(mounts[0]!.options.onDismiss?.()).toBe('inline'); await settle()
    expect(document.activeElement).toBe(expand)
    expect(mounts).toHaveLength(1)
    actions.click(); await settle()
    const surface = host.querySelector('.visualize-surface')!
    surface.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true })); await settle()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    actions.click(); await settle()
    mounts[0]!.options.onPointerDown?.(); await settle()
    expect(host.querySelector('[role="menu"]')).toBeNull()
  })
  it('supports menu keyboard traversal and restores the trigger after Escape', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    const actions = host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!
    actions.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })); await settle()
    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(document.activeElement).toBe(items[0])
    items[0]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })); await settle()
    expect(document.activeElement).toBe(items[1])
    items[1]!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await settle()
    expect(host.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(actions)
    expect(host.querySelector('dialog')?.getAttribute('aria-modal')).toBeNull()
  })
  it('reloads the current expanded canvas with reported state and keeps its presentation', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    host.querySelector<HTMLButtonElement>('[aria-label="Expand"]')!.click(); await settle()
    mounts[0]!.options.onState({ workers: 12, selected: 'b' })
    host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!.click(); await settle()
    host.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click(); await settle()
    expect(mounts).toHaveLength(2)
    expect(mounts[0]!.dispose).toHaveBeenCalledOnce()
    expect(mounts[1]!.options.initialState).toEqual({ workers: 12, selected: 'b' })
    expect(host.querySelector('dialog')?.getAttribute('aria-modal')).toBe('true')
    expect(mounts[1]!.setPresentation).toHaveBeenLastCalledWith(expect.objectContaining({ mode: 'expanded' }))
  })
  it('starts clipboard writing in the click before capturing and does not expand or remount', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    let resolveCapture!: (value: Blob) => void
    mounts[0]!.capture.mockImplementation(() => new Promise<Blob>(resolve => { resolveCapture = resolve }))
    let image!: Promise<Blob>
    vi.stubGlobal('ClipboardItem', class { constructor(data: Record<string, Promise<Blob>>) { image = data['image/png']! } })
    const write = vi.fn(async () => { await image })
    vi.stubGlobal('navigator', { clipboard: { write } })
    host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!.click(); await settle()
    const copy = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Copy as image')!
    copy.click()
    expect(write).toHaveBeenCalledOnce()
    expect(mounts[0]!.capture).not.toHaveBeenCalled()
    await settle()
    expect(host.textContent).toContain('Preparing image')
    expect(toasts).not.toHaveBeenCalled()
    const png = new Blob(['captured pixels'], { type: 'image/png' }); resolveCapture(png); await settle()
    expect(await image).toBe(png)
    expect(toasts).toHaveBeenCalledWith('Image copied', { tone: 'ok' })
    expect(host.querySelector('dialog')?.getAttribute('aria-modal')).toBeNull()
    expect(mounts).toHaveLength(1)
    expect(mounts[0]!.dispose).not.toHaveBeenCalled()
    expect(drafted).not.toHaveBeenCalled()
  })
  it('downloads only a captured PNG and recovers from export errors without changing the canvas', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    const actions = host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!
    const download = async () => {
      actions.click(); await settle()
      ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Download PNG')!.click()
      await settle()
    }
    mounts[0]!.capture.mockRejectedValueOnce(new Error('screenshot failed'))
    await download()
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(toasts).toHaveBeenLastCalledWith('Could not export the canvas. Try again.', { tone: 'danger' })
    expect(host.querySelector('[role="alert"]')).toBeNull()
    await download()
    expect(downloadBlob).toHaveBeenCalledWith(expect.objectContaining({ type: 'image/png' }), 'visual.png')
    expect(toasts).toHaveBeenLastCalledWith('Download started', { tone: 'ok' })
    expect(mounts).toHaveLength(1)
    expect(mounts[0]!.dispose).not.toHaveBeenCalled()
    expect(drafted).not.toHaveBeenCalled()
  })
  it('downloads the pinned HTML revision only after an explicit menu action', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    fetched.mockClear()
    const actions = host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!
    actions.click(); await settle()
    const htmlDownload = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Download HTML')!
    expect(htmlDownload.disabled).toBe(false)
    htmlDownload.click(); await settle()
    expect(fetched).toHaveBeenCalledWith(expect.objectContaining({ sessionKey: 'session_test', documentId: receipt.documentId, revisionId: receipt.revisionId, maxBytes: 1024 * 1024 }))
    expect(downloadBlob).toHaveBeenCalledWith(expect.objectContaining({ type: 'text/html;charset=utf-8' }), 'visual.html')
    expect(host.querySelector('[data-visualize-canvas]')).not.toBeNull()
  })
  it('drops a pending export when its owning session changes', async () => {
    const owner = reactive({ key: 'session_test' })
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: owner.key }))
    let resolveCapture!: (value: Blob) => void
    mounts[0]!.capture.mockImplementation(() => new Promise<Blob>(resolve => { resolveCapture = resolve }))
    host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!.click(); await settle()
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Download PNG')!.click(); await settle()
    owner.key = 'session_other'; await settle()
    resolveCapture(new Blob(['old pixels'], { type: 'image/png' })); await settle()
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(toasts).not.toHaveBeenCalled()
    expect(host.textContent).not.toContain('Preparing image')
  })
  it('offers download after unsupported clipboard copying and disables export on a broken canvas', async () => {
    const { host } = await mount(() => h(VisualizeCanvas, { receipt, sessionKey: 'session_test' }))
    vi.stubGlobal('navigator', { clipboard: undefined })
    const actions = host.querySelector<HTMLButtonElement>('[aria-label="Canvas actions"]')!
    actions.click(); await settle()
    ;[...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Copy as image')!.click(); await settle()
    expect(mounts[0]!.capture).not.toHaveBeenCalled()
    expect(toasts).toHaveBeenLastCalledWith('Image copying is unavailable here. Use Download PNG instead.', { tone: 'danger' })
    actions.click(); await settle()
    const download = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(button => button.textContent === 'Download PNG')!
    expect(download.disabled).toBe(false)
    download.click(); await settle()
    expect(downloadBlob).toHaveBeenCalledOnce()
    mounts[0]!.options.onError(new Error('canvas execution failed')); await settle()
    actions.click(); await settle()
    const items = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
    expect(items.map(button => button.disabled)).toEqual([false, true, true, true])
    items[0]!.click(); await settle()
    expect(mounts).toHaveLength(2)
    expect(host.querySelector('[role="alert"]')).toBeNull()
  })
})
