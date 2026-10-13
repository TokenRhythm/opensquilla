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
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'

const mounts = vi.hoisted(() => [] as Array<{ options: VisualizeRuntimeOptions; dispose: ReturnType<typeof vi.fn> }>)
vi.mock('@/visualize/runtime', () => ({ mountVisualize: vi.fn(async (container: HTMLElement, options: VisualizeRuntimeOptions) => {
  const dispose = vi.fn(() => container.replaceChildren())
  const node = document.createElement('div'); node.textContent = 'Rendered canvas'; container.append(node)
  mounts.push({ options, dispose }); return { dispose, updateTheme: vi.fn() }
}) }))
const html = '<html><body><button>Canvas</button></body></html>'
const bytes = new TextEncoder().encode(html)
const payload = { previewStatus: 'inline-ready', presentation: 'inline', documentId: 'doc_test', revisionId: 'rev_test', artifactId: 'artifact_test', name: 'visual.html', mime: 'text/html', sha256: bytesToHex(sha256(bytes)), size: bytes.length, marker: 'visualize:doc_test:rev_test' }
const toolCall = { toolId: 'tool_test', name: 'open_workspace_preview', displayName: 'open_workspace_preview', status: 'success', isRunning: false, isError: false, result: JSON.stringify(payload), inputPreview: '', resultPreview: '', isOpen: false } as ChatToolCall
const receipt = visualizeReceipts([toolCall])[0]!
const apps: App[] = []
const fetched = vi.fn(async () => new Blob([html], { type: 'text/html' }))
const drafted = vi.fn(() => true)
const gateway = reactive({ isAvailable: true, deliveryIdentity: 'user_test', subscriptionEpoch: 1 })
const message = (): ChatRenderedMessage => ({ role: 'assistant', displayRole: 'assistant', roleLabel: 'Assistant', text: 'Before\n\n`' + payload.marker + '`\n\nAfter', timeStr: '', showHeader: false, toolCalls: [toolCall] })
async function settle() { for (let i = 0; i < 12; i++) { await nextTick(); await new Promise(resolve => setTimeout(resolve, 0)) } }
async function mount(render: () => ReturnType<typeof h>) {
  const host = document.createElement('div'); document.body.append(host)
  const app = createApp({ render }); apps.push(app); app.use(i18n)
  app.provide(ARTIFACT_WORKBENCH_KEY, { content: { fetchVisualize: fetched } } as unknown as ArtifactWorkbench)
  app.provide(GATEWAY_ACCESS_KEY, gateway as GatewayAccess)
  app.provide(VISUALIZE_CONVERSATION_KEY, { enabled: key => key === 'session_test', draft: drafted })
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
})
