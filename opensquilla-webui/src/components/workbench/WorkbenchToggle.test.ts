// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick, reactive, type App } from 'vue'
import { createPinia } from 'pinia'
import WorkbenchToggle from './WorkbenchToggle.vue'
import { useWorkbenchStore } from '@/workbench/store'
import { createBrowserWorkbenchItem } from '@/workbench/browserItems'

vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast: vi.fn() }) }))
const apps: App[] = []

async function mount() {
  const pinia = createPinia()
  const props = reactive({ enabled: true, sessionId: 'session-a', blocked: false, allowEmpty: false })
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(defineComponent(() => () => h(WorkbenchToggle, props)))
  app.use(pinia)
  apps.push(app)
  app.mount(root)
  await nextTick()
  return { root, props, store: useWorkbenchStore(pinia), button: () => root.querySelector('button')! }
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('Workbench toolbar toggle', () => {
  it('opens and collapses an empty desktop workbench before any page exists', async () => {
    const { store, props, button } = await mount()
    props.allowEmpty = true
    props.sessionId = ''
    await nextTick()
    expect(button().disabled).toBe(false)
    button().click()
    await nextTick()
    expect(store.expanded).toBe(true)
    expect(store.items).toEqual([])
    expect(button().getAttribute('aria-expanded')).toBe('true')
    button().click()
    expect(store.expanded).toBe(false)
  })

  it('uses the common icon button and toggles the existing browser with matching accessible labels', async () => {
    const { root, store, button } = await mount()
    expect(root.querySelector('button')).toBeNull()
    const item = createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'https://example.test/' })!
    store.openItem(item)
    await nextTick()
    expect(button().className).toBe('btn btn--icon btn--ghost')
    expect(button().getAttribute('title')).toBe('workbench.collapse')
    expect(button().getAttribute('aria-label')).toBe('workbench.collapse')
    expect(button().getAttribute('aria-expanded')).toBe('true')
    expect(button().textContent).toBe('')
    button().click()
    await nextTick()
    expect(store.expanded).toBe(false)
    expect(button().getAttribute('aria-label')).toBe('workbench.expand')
    expect(button().getAttribute('aria-pressed')).toBe('false')
    button().click()
    await nextTick()
    expect(store.activeItemId).toBe(item.id)
    expect(store.expanded).toBe(true)
    expect(store.items).toHaveLength(1)
  })

  it('reopens a closed current-session browser without exposing another task', async () => {
    const { store, props, button, root } = await mount()
    const item = createBrowserWorkbenchItem({ scopeId: 'session-a', url: 'https://example.test/' })!
    store.openItem(item)
    store.closeItem(item.id)
    await nextTick()
    expect(button().getAttribute('aria-expanded')).toBe('false')
    props.sessionId = 'session-b'
    await nextTick()
    expect(root.querySelector('button')).toBeNull()
    props.sessionId = 'session-a'
    await nextTick()
    button().click()
    await nextTick()
    expect(store.activeItemId).toBe(item.id)
    expect(store.expanded).toBe(true)
  })

  it('keeps artifact panels usable and respects modal and route availability', async () => {
    const { store, props, button, root } = await mount()
    store.openItem({ id: 'artifact', kind: 'artifact-preview', title: 'Report',
      scope: { type: 'session', id: 'session-a' }, hostKind: 'dom', retention: 'keep-alive', payload: {} })
    await nextTick()
    props.blocked = true
    await nextTick()
    expect(button().disabled).toBe(true)
    button().click()
    expect(store.expanded).toBe(true)
    props.blocked = false
    await nextTick()
    button().click()
    await nextTick()
    button().click()
    expect(store.activeItemId).toBe('artifact')
    expect(store.expanded).toBe(true)
    props.enabled = false
    await nextTick()
    expect(root.querySelector('button')).toBeNull()
  })
})
