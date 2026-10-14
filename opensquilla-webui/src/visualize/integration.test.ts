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
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import * as themeRegistry from '@/themes/registry'

const mounts = vi.hoisted(() => [] as Array<{ options: VisualizeRuntimeOptions; dispose: ReturnType<typeof vi.fn>; setPresentation: ReturnType<typeof vi.fn> }>)
vi.mock('@/visualize/runtime', () => ({ mountVisualize: vi.fn(async (container: HTMLElement, options: VisualizeRuntimeOptions) => {
  const dispose = vi.fn(() => container.replaceChildren())
  const node = document.createElement('div'); node.textContent = 'Rendered canvas'; container.append(node)
  const setPresentation = vi.fn()
  mounts.push({ options, dispose, setPresentation }); return { dispose, updateTheme: vi.fn(), setPresentation }
}) }))
const html = '<html><body><button>Canvas</button></body></html>'
const bytes = new TextEncoder().encode(html)
const payload = { previewStatus: 'inline-ready', presentation: 'inline', documentId: 'doc_test', revisionId: 'rev_test', artifactId: 'artifact_test', name: 'visual.html', mime: 'text/html', sha256: bytesToHex(sha256(bytes)), size: bytes.length, marker: 'visualize:doc_test:rev_test' }
const toolCall = { toolId: 'tool_test', name: 'open_workspace_preview', displayName: 'open_workspace_preview', status: 'success', isRunning: false, isError: false, result: JSON.stringify(payload), inputPreview: '', resultPreview: '', isOpen: false } as ChatToolCall
const receipt = visualizeReceipts([toolCall])[0]!
const apps: App[] = []
const fetched = vi.fn(async () => new Blob([html], { type: 'text/html' }))
const drafted = vi.fn(() => true)
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
  app.provide(VISUALIZE_CONVERSATION_KEY, { enabled: key => key === 'session_test', draft: drafted })
  app.provide(VISUALIZE_MESSAGE_PIN_KEY, { pin })
  app.mount(host); await settle(); return { host, app }
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); mounts.splice(0); document.body.replaceChildren(); sessionStorage.clear(); vi.clearAllMocks() })

describe('Visualize production chat components', () => {
  it('renders live text/canvas/text and preserves the runtime while later text streams', async () => {
    const raw = reactive({ value: message().text! })
    const { renderMarkdown } = useChatTextRendering()
    const { host } = await mount(() => h(StreamingTextPart, { rawText: raw.value, renderMarkdown, visualizeReceipts: [receipt], sessionKey: 'session_test' }))
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
    first.options.onCollapse?.(); await settle()
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
    first.options.onCollapse?.(); await settle()
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(releasePin).toHaveBeenCalledTimes(1)
    mounts[1]!.options.onCollapse?.(); await settle()
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
})
