// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, shallowRef, type App } from 'vue'
import { createPinia } from 'pinia'
import i18n from '@/i18n'
import { WORKSPACE_REFERENCES_KEY, WorkspaceReferenceError, type WorkspaceReferences } from '@/modules/workspaceReferences'
import type { WorkspaceFileReferenceV1 } from '@/types/references'
import { useWorkbenchStore } from '@/workbench/store'
import WorkspaceReferenceCard from './WorkspaceReferenceCard.vue'
import { GATEWAY_ACCESS_KEY, type GatewayAccess } from '@/modules/gatewayAccess'

const reference: WorkspaceFileReferenceV1 = {
  version: 1, kind: 'workspace_file', id: 'src/math.py', label: 'src/math.py:2-4',
  scope: {}, locator: { relativePath: 'src/math.py', startLine: 2, endLine: 4 },
  state: { available: true, revision: 'file_1234567890abcdef' }, capabilities: { open: true, copy: true },
}
const apps: App[] = []
async function mount(read: WorkspaceReferences['read']) {
  const el = document.createElement('div')
  document.body.append(el)
  const pinia = createPinia()
  const currentReference = shallowRef(structuredClone(reference))
  const gateway = reactive({ endpoint: 'ws://gateway.example/ws', epoch: 1, available: true, owner: true })
  const app = createApp({
    setup: () => () => h(WorkspaceReferenceCard, { sessionKey: 'task', reference: currentReference.value }),
  })
  apps.push(app)
  app.use(pinia).use(i18n).provide(WORKSPACE_REFERENCES_KEY, { read })
  app.provide(GATEWAY_ACCESS_KEY, {
    get isAvailable() { return gateway.available },
    get isLocalOwner() { return gateway.owner },
    get subscriptionEpoch() { return gateway.epoch },
    loadConnectionEndpoint: () => gateway.endpoint,
  } as GatewayAccess)
  app.mount(el)
  const store = useWorkbenchStore(pinia)
  await nextTick()
  return { el, store, gateway, currentReference }
}
const settle = async () => { await new Promise(resolve => setTimeout(resolve, 0)); await nextTick() }
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

describe('WorkspaceReferenceCard', () => {
  it('validates the file before opening a session-scoped Workbench descriptor without source contents', async () => {
    const resolved = { ...reference, scope: { sessionKey: 'task', workspaceId: 'workspace' } }
    const read = vi.fn().mockResolvedValue({ reference: resolved, content: 'private code' })
    const { el, store } = await mount(read)
    el.querySelector<HTMLButtonElement>('.workspace-reference__open')!.click()
    await settle()
    expect(read).toHaveBeenCalledWith('task', reference, expect.any(AbortSignal))
    expect(store.activeItem?.kind).toBe('file')
    expect(store.activeItem?.scope).toEqual({ type: 'session', id: 'task' })
    expect(JSON.stringify(store.items)).not.toContain('private code')
  })
  it.each(['STALE_REFERENCE', 'WORKSPACE_MISMATCH', 'OWNER_REQUIRED', 'FILE_NOT_FOUND'])('shows %s and never opens the file', async code => {
    const { el, store } = await mount(vi.fn().mockRejectedValue(new WorkspaceReferenceError(code)))
    el.querySelector<HTMLButtonElement>('.workspace-reference__open')!.click()
    await settle()
    expect(store.items).toEqual([])
    expect(el.querySelector('[role="status"]')?.textContent).toBeTruthy()
  })
  it('keeps a pending click alive when streaming recreates an equivalent reference', async () => {
    let finish!: (value: unknown) => void
    const read = vi.fn<WorkspaceReferences['read']>(() => new Promise(resolve => { finish = resolve as typeof finish }))
    const ui = await mount(read)
    ui.el.querySelector<HTMLButtonElement>('.workspace-reference__open')!.click()
    const signal = read.mock.calls[0]![2]!
    for (let token = 0; token < 5; token += 1) {
      ui.currentReference.value = structuredClone(ui.currentReference.value)
      await nextTick()
    }
    expect(signal.aborted).toBe(false)
    expect(read).toHaveBeenCalledOnce()
    finish({ reference })
    await settle()
    expect(ui.store.activeItem?.kind).toBe('file')
  })

  it('blocks a Gateway switch both during and after opening a reference', async () => {
    let finish!: (value: unknown) => void
    const read = vi.fn<WorkspaceReferences['read']>(() => new Promise(resolve => { finish = resolve as typeof finish }))
    const ui = await mount(read)
    const button = ui.el.querySelector<HTMLButtonElement>('.workspace-reference__open')!
    button.click()
    const signal = read.mock.calls[0]![2]!
    // Changing the endpoint alone must block reuse, even before epoch updates.
    ui.gateway.endpoint = 'ws://other.example/ws'
    finish({ reference })
    await settle()
    expect(signal.aborted).toBe(true)
    expect(ui.store.items).toEqual([])
    expect(button.disabled).toBe(true)
    button.click()
    expect(read).toHaveBeenCalledOnce()
  })

  it.each(['unavailable', 'non-owner', 'open-disabled', 'resource-unavailable'])('blocks %s before calling the API', async reason => {
    const read = vi.fn()
    const ui = await mount(read)
    if (reason === 'unavailable') ui.gateway.available = false
    else if (reason === 'non-owner') ui.gateway.owner = false
    else if (reason === 'open-disabled') ui.currentReference.value = { ...reference, capabilities: { open: false } }
    else ui.currentReference.value = { ...reference, state: { available: false } }
    await nextTick()
    const button = ui.el.querySelector<HTMLButtonElement>('.workspace-reference__open')!
    expect(button.disabled).toBe(true)
    button.click()
    expect(read).not.toHaveBeenCalled()
    expect(ui.store.items).toEqual([])
  })

  it.each(['epoch', 'ownership', 'availability'])('ignores a late result after Gateway %s changes', async change => {
    let finish!: (value: unknown) => void
    const read = vi.fn<WorkspaceReferences['read']>(() => new Promise(resolve => { finish = resolve as typeof finish }))
    const ui = await mount(read)
    ui.el.querySelector<HTMLButtonElement>('.workspace-reference__open')!.click()
    if (change === 'epoch') ui.gateway.epoch += 1
    else if (change === 'ownership') ui.gateway.owner = false
    else ui.gateway.available = false
    finish({ reference })
    await settle()
    expect(read.mock.calls[0]![2]!.aborted).toBe(true)
    expect(ui.store.items).toEqual([])
  })

  it('closes the action menu on Escape and returns focus to its summary', async () => {
    const { el } = await mount(vi.fn())
    const menu = el.querySelector<HTMLDetailsElement>('details')!
    const summary = menu.querySelector('summary')!
    menu.open = true
    const item = menu.querySelector('button')!
    item.focus()
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    item.dispatchEvent(event)
    await nextTick()
    expect(menu.open).toBe(false)
    expect(event.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(summary)
  })

  it('ignores a successful late result after unmount', async () => {
    let finish!: (value: unknown) => void
    const { el, store } = await mount(vi.fn<WorkspaceReferences['read']>(() => new Promise(resolve => { finish = resolve as typeof finish })))
    el.querySelector<HTMLButtonElement>('.workspace-reference__open')!.click()
    apps.pop()!.unmount()
    finish({ reference })
    await settle()
    expect(store.items).toEqual([])
  })
})
