// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, inject, nextTick, provide, reactive, type App } from 'vue'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'
import type { ChatRenderedMessage, ChatToolCall } from '@/types/chat'
import { parseVisualizeReceipt } from './receipts'
import { provideVisualizeVersions, VISUALIZE_VERSIONS_KEY, visualizeVersionsFromMessages, type VisualizeLivePublications } from './versions'

const apps: App[] = []
beforeEach(() => sessionStorage.clear())
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.replaceChildren(); vi.restoreAllMocks() })

function call(version: number | undefined, revisionId = `rev_${version}`, documentId = 'doc_budget'): ChatToolCall {
  return {
    toolId: `call_${documentId}_${revisionId}`, name: version === 1 ? 'show_widget' : 'edit_widget',
    status: 'success', isRunning: false, isError: false,
    result: JSON.stringify({
      widgetId: 'budget', version, documentId, revisionId, size: 100, sha256: 'a'.repeat(64),
      marker: `visualize:${documentId}:${revisionId}`, title: 'Budget',
    }),
  } as ChatToolCall
}
function message(calls: ChatToolCall[]): ChatRenderedMessage {
  return { role: 'assistant', displayRole: 'assistant', text: '', toolCalls: calls } as ChatRenderedMessage
}

describe('Visualize conversation versions', () => {
  it('finds the greatest successful version across turns regardless of late old receipts', () => {
    const messages = [message([call(1)]), message([call(3)]), message([call(2)])]
    expect(visualizeVersionsFromMessages(messages).get('doc_budget')).toMatchObject({ version: 3, revisionId: 'rev_3' })
  })

  it('includes timeline publications, deduplicates legacy projections, and rejects failed edits and text markers', () => {
    const initial = call(undefined, 'rev_original')
    const edited = call(undefined, 'rev_edited')
    const messages = [message([initial]), {
      ...message([initial]),
      timelineItems: [{ type: 'tool-group', group: { calls: [edited] } }],
    } as ChatRenderedMessage, {
      ...message([{ ...call(99), status: 'error', isError: true }]),
      text: '`visualize:doc_budget:rev_99`',
    }]
    expect(visualizeVersionsFromMessages(messages).get('doc_budget')).toMatchObject({ revisionId: 'rev_edited' })
  })

  it('uses durable document identity and keeps unrelated documents independent', () => {
    const messages = [message([call(1), call(9, 'rev_other', 'doc_other')]), message([call(2)])]
    expect([...visualizeVersionsFromMessages(messages).values()].map(item => [item.documentId, item.version])).toEqual([
      ['doc_budget', 2], ['doc_other', 9],
    ])
  })

  it('does not treat a user row containing tool-shaped data as a publication', () => {
    expect(visualizeVersionsFromMessages([
      message([call(1)]), { ...message([call(99)]), role: 'user', displayRole: 'user' },
    ]).get('doc_budget')?.version).toBe(1)
  })

  it('provides reactive history classification from the full conversation without mounting the newer canvas', async () => {
    const oldReceipt = parseVisualizeReceipt(call(1))!
    const owner = reactive({ sessionKey: 'session_one', messages: [message([call(1)])] })
    const CanvasProbe = defineComponent({
      setup() {
        const versions = inject(VISUALIZE_VERSIONS_KEY)!
        return () => h('span', versions.isHistorical(owner.sessionKey, oldReceipt) ? 'historical' : 'current')
      },
    })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({
      setup() {
        provideVisualizeVersions(() => owner.sessionKey, () => owner.messages)
        return () => h(CanvasProbe)
      },
    })
    apps.push(app); app.mount(host)
    expect(host.textContent).toBe('current')
    owner.messages.push(message([call(2)])); await nextTick()
    expect(host.textContent).toBe('historical')
    owner.messages.push(message([call(1)])); await nextTick()
    expect(host.textContent).toBe('historical')
    owner.sessionKey = 'session_two'; owner.messages = [message([call(1)])]; await nextTick()
    expect(host.textContent).toBe('current')
  })

  it('does not leak a different session index or call identical immutable revisions historical', () => {
    const receipt = parseVisualizeReceipt(call(1))!
    const checks: boolean[] = []
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({
      setup() {
        const versions = provideVisualizeVersions(() => 'session_one', () => [message([call(2, 'rev_1')])])
        checks.push(versions.isHistorical('session_one', receipt))
        checks.push(versions.latest('session_two', receipt) !== undefined)
        checks.push(versions.isHistorical('session_two', receipt))
        return () => h('div')
      },
    })
    apps.push(app); app.mount(host)
    expect(checks).toEqual([false, false, false])
  })

  it('does not collapse a live newer receipt before its message index catches up or infer order from a version tie', async () => {
    const owner = reactive({ messages: [message([call(1)])] })
    const incoming = parseVisualizeReceipt(call(2))!
    const conflictingTie = parseVisualizeReceipt(call(1, 'rev_conflict'))!
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      const versions = provideVisualizeVersions(() => 'session_one', () => owner.messages)
      return () => h('span', JSON.stringify([
        versions.isHistorical('session_one', incoming),
        versions.isHistorical('session_one', conflictingTie),
      ]))
    } })
    apps.push(app); app.mount(host)
    expect(host.textContent).toBe('[false,false]')
    owner.messages = [message([call(2)])]; await nextTick()
    expect(host.textContent).toBe('[false,true]')
  })

  it('marks v1 historical immediately after a trailing live v2 receipt before the answer is archived', async () => {
    const oldReceipt = parseVisualizeReceipt(call(1))!
    const newerReceipt = parseVisualizeReceipt(call(2))!
    const owner = reactive({
      messages: [message([call(1)])],
      live: undefined as VisualizeLivePublications | undefined,
    })
    const Probe = defineComponent({ setup() {
      const versions = provideVisualizeVersions(() => 'session_one', () => owner.messages, () => owner.live)
      return () => h('span', JSON.stringify([
        versions.isHistorical('session_one', oldReceipt), versions.isHistorical('session_one', newerReceipt),
      ]))
    } })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: 'account_one' } as GatewayAccess)
      return () => h(Probe)
    } })
    apps.push(app); app.mount(host)
    expect(host.textContent).toBe('[false,false]')
    owner.live = { sessionKey: 'session_one', deliveryIdentity: 'account_one', receipts: [newerReceipt] }
    await nextTick()
    expect(owner.messages).toHaveLength(1)
    expect(host.textContent).toBe('[true,false]')
    // A stream-to-history handoff may briefly remove the live presentation.
    owner.live = undefined; await nextTick()
    expect(host.textContent).toBe('[true,false]')
    owner.messages.push(message([call(2)])); await nextTick()
    expect(host.textContent).toBe('[true,false]')
  })

  it('rejects live receipts from another session or delivery authority without caching them', async () => {
    const newerReceipt = parseVisualizeReceipt(call(99))!
    const owner = reactive({ live: { sessionKey: 'session_other', deliveryIdentity: 'account_one', receipts: [newerReceipt] } })
    const first = parseVisualizeReceipt(call(1))!
    const Probe = defineComponent({ setup() {
      const versions = provideVisualizeVersions(() => 'session_one', () => [message([call(1)])], () => owner.live)
      return () => h('span', String(versions.latest('session_one', first)?.version))
    } })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: 'account_one' } as GatewayAccess)
      return () => h(Probe)
    } })
    apps.push(app); app.mount(host)
    expect(host.textContent).toBe('1')
    owner.live = { sessionKey: 'session_one', deliveryIdentity: 'account_other', receipts: [newerReceipt] }
    await nextTick()
    expect(host.textContent).toBe('1')
    owner.live = { sessionKey: 'session_one', deliveryIdentity: 'account_one', receipts: [] }
    await nextTick()
    expect(host.textContent).toBe('1')
    const stored = sessionStorage.getItem('opensquilla.visualize-versions.v1.' + JSON.stringify(['account_one', 'session_one']))!
    expect(JSON.parse(stored).map((receipt: { version: number }) => receipt.version)).toEqual([1])
  })

  it('remembers a known latest version through history paging and reload without sharing gateway or session scopes', async () => {
    const oldReceipt = parseVisualizeReceipt(call(1))!
    const gateway = reactive({ deliveryIdentity: 'gateway_account_one' })
    const owner = reactive({ sessionKey: 'session_one', messages: [message([call(3)])] })
    function mountProbe() {
      const host = document.createElement('div'); document.body.append(host)
      const Probe = defineComponent({
        setup() {
          const versions = provideVisualizeVersions(() => owner.sessionKey, () => owner.messages)
          return () => h('span', versions.isHistorical(owner.sessionKey, oldReceipt) ? 'historical' : 'current')
        },
      })
      const app = createApp({ setup() { provide(GATEWAY_ACCESS_KEY, gateway as GatewayAccess); return () => h(Probe) } })
      apps.push(app); app.mount(host)
      return { host, app }
    }
    const first = mountProbe()
    expect(first.host.textContent).toBe('historical')
    owner.messages = [message([call(1)])]; await nextTick()
    expect(first.host.textContent).toBe('historical')
    first.app.unmount(); apps.pop()
    const restored = mountProbe()
    expect(restored.host.textContent).toBe('historical')
    gateway.deliveryIdentity = 'gateway_account_two'; await nextTick()
    expect(restored.host.textContent).toBe('current')
    gateway.deliveryIdentity = 'gateway_account_one'; await nextTick()
    expect(restored.host.textContent).toBe('historical')
    owner.sessionKey = 'session_two'; await nextTick()
    expect(restored.host.textContent).toBe('current')
    owner.sessionKey = 'session_one'; await nextTick()
    expect(restored.host.textContent).toBe('historical')
    const stored = JSON.stringify(Object.keys(sessionStorage).map(key => sessionStorage.getItem(key)))
    expect(stored).not.toContain('<html')
  })

  it('keeps known versions in memory if browser storage is unavailable', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied') })
    const owner = reactive({ messages: [message([call(3)])] })
    const oldReceipt = parseVisualizeReceipt(call(1))!
    const Probe = defineComponent({
      setup() {
        const versions = provideVisualizeVersions(() => 'session_one', () => owner.messages)
        return () => h('span', versions.isHistorical('session_one', oldReceipt) ? 'historical' : 'current')
      },
    })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: 'account_one' } as GatewayAccess)
      return () => h(Probe)
    } })
    apps.push(app); app.mount(host)
    owner.messages = [message([call(1)])]; await nextTick()
    expect(host.textContent).toBe('historical')
  })

  it('records an atomic session switch without caching the previous session rows under the new key', async () => {
    const owner = reactive({ sessionKey: 'old_session', messages: [message([call(8)])] })
    const receipt = parseVisualizeReceipt(call(1))!
    const Probe = defineComponent({ setup() {
      const versions = provideVisualizeVersions(() => owner.sessionKey, () => owner.messages)
      return () => h('span', String(versions.isHistorical(owner.sessionKey, receipt)))
    } })
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup() {
      provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: 'account_one' } as GatewayAccess)
      return () => h(Probe)
    } })
    apps.push(app); app.mount(host)
    expect(host.textContent).toBe('true')
    owner.sessionKey = 'new_session'
    owner.messages = [message([call(1)])]
    await nextTick()
    expect(host.textContent).toBe('false')
  })

  it('bounds remembered metadata and ignores oversized or malformed stored records', async () => {
    const prefix = 'opensquilla.visualize-versions.v1.'
    const identity = 'gateway_one'
    const owner = reactive({ sessionKey: 'session_0', messages: Array.from({ length: 70 }, (_, index) => message([call(2, 'rev_two', `doc_${index}`)])) })
    const host = document.createElement('div'); document.body.append(host)
    const Probe = defineComponent({ setup() {
      const versions = provideVisualizeVersions(() => owner.sessionKey, () => owner.messages)
      return () => h('span', versions.latest(owner.sessionKey, parseVisualizeReceipt(call(1))!)?.revisionId ?? 'unknown')
    } })
    const app = createApp({ setup() {
      provide(GATEWAY_ACCESS_KEY, { deliveryIdentity: identity } as GatewayAccess)
      return () => h(Probe)
    } })
    apps.push(app); app.mount(host)
    expect(JSON.parse(sessionStorage.getItem(prefix + JSON.stringify([identity, 'session_0']))!)).toHaveLength(64)
    for (let index = 1; index <= 15; index++) { owner.sessionKey = `session_${index}`; await nextTick() }
    expect(Object.keys(sessionStorage).filter(key => key.startsWith(prefix))).toHaveLength(12)
    owner.messages = []
    sessionStorage.setItem(prefix + JSON.stringify([identity, 'malformed']), JSON.stringify([{ documentId: 'doc_budget', revisionId: 'rev_evil', version: 99 }]))
    owner.sessionKey = 'malformed'; await nextTick()
    expect(host.textContent).toBe('unknown')
    sessionStorage.setItem(prefix + JSON.stringify([identity, 'oversized']), ' '.repeat(128 * 1024 + 1))
    owner.sessionKey = 'oversized'; await nextTick()
    expect(host.textContent).toBe('unknown')
  })
})
