// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive, type App } from 'vue'
import i18n from '@/i18n'
import ChatComposerModelRouting from './ChatComposerModelRouting.vue'

const apps: App[] = []
beforeEach(() => {
  i18n.global.locale.value = 'en'
  document.body.innerHTML = ''
})
afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount())
  vi.restoreAllMocks()
})
async function mount(overrides: Record<string, unknown> = {}, openModels = true) {
  const selected = vi.fn(),
    mode = vi.fn(),
    close = vi.fn(),
    settings = vi.fn(),
    refresh = vi.fn()
  const props = reactive({
    modelRoutingMode: 'off',
    busy: false,
    modelSelectionAvailable: true,
    modelSelection: null as { model: string; provider: string } | null,
    availableModels: [
      { id: 'shared-model', name: 'Model Alpha', provider: 'provider-a' },
      { id: 'shared-model', name: 'Model Beta', provider: 'provider-b' },
    ],
    onSelectModel: selected,
    onSetSessionRoutingMode: mode,
    onClose: close,
    onOpenModelSettings: settings,
    onRefreshModels: refresh,
    ...overrides,
  })
  const el = document.createElement('div')
  document.body.append(el)
  const app = createApp({ render: () => h(ChatComposerModelRouting, props as any) })
  apps.push(app)
  app.use(i18n)
  app.mount(el)
  await nextTick()
  if (openModels && document.querySelector('[aria-haspopup="listbox"]')) {
    document.querySelector<HTMLButtonElement>('.routing-mode')!.click()
    await nextTick()
  }
  return { props, selected, mode, close, settings, refresh }
}
const query = <T extends HTMLElement = HTMLElement>(selector: string) =>
  document.body.querySelector<T>(selector)!
async function key(element: HTMLElement, value: string, extra: KeyboardEventInit = {}) {
  element.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, ...extra }))
  await nextTick()
}
async function search(value: string) {
  const input = query<HTMLInputElement>('input')
  input.value = value
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  return input
}

describe('Native cascading model routing menu', () => {
  it.each(['off', 'squilla_router', 'llm_ensemble'])(
    'starts with only the primary menu in %s mode',
    async (modelRoutingMode) => {
      await mount({ modelRoutingMode }, false)
      expect(query('[role="listbox"]')).toBeNull()
      expect(query('.routing-primary')).toBeTruthy()
      expect(document.activeElement?.getAttribute('data-mode')).toBe(modelRoutingMode)
    },
  )
  it('keeps the compact primary target mounted until a tap or click activates it', async () => {
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(390)
    await mount({}, false)
    const single = query<HTMLButtonElement>('.routing-mode')
    single.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'touch' }))
    await nextTick()
    expect(query('[role="listbox"]')).toBeNull()
    single.click()
    await nextTick()
    expect(query('[role="listbox"]')).toBeTruthy()
    query<HTMLButtonElement>('[aria-label="Back to routing modes"]').click()
    await nextTick()
    expect(query('[role="listbox"]')).toBeNull()
  })
  it('shows concise theme-colored benefit tags for each routing mode', async () => {
    await mount({}, false)
    expect([...document.querySelectorAll('.routing-mode__benefit')].map((el) => el.textContent))
      .toEqual(['Token-efficient', 'Capability-first'])
    expect(document.querySelector('.routing-mode__label')?.textContent).toBe('Fixed model')
  })
  it('preserves provider identity when two providers expose the same model id', async () => {
    const { selected } = await mount()
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(3)
    const input = await search('provider-b')
    await key(input, 'ArrowDown')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledExactlyOnceWith({
      model: 'shared-model',
      provider: 'provider-b',
    })
  })

  it('groups multiple providers in the same submenu without losing keyboard order', async () => {
    const { selected } = await mount({ availableModels: [
      { id: 'a1', name: 'Alpha one', provider: 'provider-a' },
      { id: 'b1', name: 'Beta one', provider: 'provider-b' },
      { id: 'a2', name: 'Alpha two', provider: 'provider-a' },
    ] })
    expect([...document.querySelectorAll('.routing-provider-heading')].map(el => el.textContent))
      .toEqual(['provider-a', 'provider-b'])
    expect([...document.querySelectorAll('.routing-model__name')].map(el => el.textContent))
      .toEqual(['Default model', 'Alpha one', 'Alpha two', 'Beta one'])
    const input = query<HTMLInputElement>('input')
    await key(input, 'ArrowDown')
    await key(input, 'ArrowDown')
    await key(input, 'ArrowDown')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'a2', provider: 'provider-a' })
  })
  it('limits each provider to 12 while prioritizing the primary model and retaining API order', async () => {
    const rows = (provider: string) => Array.from({ length: 15 }, (_, index) => ({
      id: `m-${index}`, name: `Model ${index}`, provider,
    }))
    await mount({
      availableModels: [...rows('provider-b'), ...rows('provider-a')],
      defaultModel: { model: 'm-13', provider: 'provider-a' },
    })
    const groups = [...document.querySelectorAll('[role="listbox"] > [role="group"]')]
    expect(groups).toHaveLength(3)
    expect(groups[1]!.textContent).toContain('provider-a')
    expect([...groups[1]!.querySelectorAll('.routing-model__name')].map(el => el.textContent))
      .toEqual(['Model 13', ...Array.from({ length: 11 }, (_, i) => `Model ${i}`)])
    expect([...groups[2]!.querySelectorAll('.routing-model__name')].map(el => el.textContent))
      .toEqual(Array.from({ length: 12 }, (_, i) => `Model ${i}`))
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(25)
    expect(query('.routing-show-all').textContent).toBe('View all models')
  })
  it.each([360, 390, 768, 1366])(
    'keeps the expansion action outside the scrollable model list at %ipx',
    async (width) => {
      vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(width)
      await mount({
        availableModels: Array.from({ length: 15 }, (_, index) => ({
          id: `m-${index}`,
          name: `Model ${index}`,
          provider: 'provider-a',
        })),
      })
      const listArea = query('.routing-models')
      const listbox = query('[role="listbox"]')
      const showAll = query<HTMLButtonElement>('.routing-show-all')
      expect(listArea.contains(listbox)).toBe(true)
      expect(listArea.children).toHaveLength(1)
      expect(listArea.firstElementChild).toBe(listbox)
      expect(listArea.contains(showAll)).toBe(false)
      expect(showAll.parentElement).toBe(listArea.parentElement)
      expect(showAll.tabIndex).toBe(0)
    },
  )
  it.each(['m-14', 'private-model'])('keeps selected %s visible within its provider budget', async (model) => {
    const { selected } = await mount({
      availableModels: Array.from({ length: 15 }, (_, index) => ({
        id: `m-${index}`, name: `Model ${index}`, provider: 'provider-a',
      })),
      modelSelection: { model, provider: 'provider-a' },
    })
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(13)
    expect(query('[aria-selected="true"]').getAttribute('aria-disabled')).toBe('false')
    query<HTMLButtonElement>('[aria-selected="true"]').click()
    expect(selected).toHaveBeenCalledWith({ model, provider: 'provider-a' })
  })
  it('searches beyond the preview limit with provider identity and restores the limit when cleared', async () => {
    const { selected } = await mount({ availableModels: ['provider-a', 'provider-b'].flatMap(provider =>
      Array.from({ length: 15 }, (_, index) => ({ id: `m-${index}`, name: `Model ${index}`, provider })),
    ) })
    const input = await search('m-14')
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(2)
    expect(query('.routing-show-all')).toBeNull()
    await key(input, 'ArrowUp')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'm-14', provider: 'provider-b' })
    await search('')
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(25)
    expect(query('.routing-show-all')).toBeTruthy()
  })
  it('makes expansion keyboard reachable and preserves the highlighted model without selecting', async () => {
    const { selected, mode, close } = await mount({ availableModels: ['provider-a', 'provider-b'].flatMap(provider =>
      Array.from({ length: 15 }, (_, index) => ({ id: `m-${index}`, name: `Model ${index}`, provider })),
    ) })
    const input = query<HTMLInputElement>('input')
    input.focus()
    await key(input, 'ArrowUp') // provider-b m-11, last in the preview
    await key(input, 'Tab')
    const showAll = query<HTMLButtonElement>('.routing-show-all')
    expect(document.activeElement).toBe(showAll)
    expect(showAll.closest('[role="listbox"]')).toBeNull()
    await key(showAll, 'Tab', { shiftKey: true })
    expect(document.activeElement).toBe(input)
    await key(input, 'Tab')
    showAll.click() // Native Enter/Space activation is exercised in the browser.
    await nextTick()
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(31)
    expect(query('.routing-show-all')).toBeNull()
    expect(document.activeElement).toBe(input)
    expect(close).not.toHaveBeenCalled()
    expect(mode).not.toHaveBeenCalled()
    expect(selected).not.toHaveBeenCalled()
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'm-11', provider: 'provider-b' })
  })
  it('does not add provider headings or loading noise to a usable single-provider list', async () => {
    await mount({
      availableModels: [{ id: 'a1', name: 'Alpha', provider: 'provider-a' }],
      modelsLoading: true,
      modelProviderErrors: [{ provider: 'provider-a', kind: 'network', detail: 'offline' }],
    })
    expect(query('.routing-provider-heading')).toBeNull()
    expect(query('.routing-issue')).toBeNull()
    expect(query('[role="option"][aria-disabled="true"]')).toBeNull()
  })
  it('keeps keyboard focus on the same model when discovery inserts an earlier row', async () => {
    const { props, selected } = await mount()
    const input = query<HTMLInputElement>('input')
    await key(input, 'ArrowUp')
    props.availableModels = [
      ...props.availableModels,
      { id: 'new-model', name: 'New Alpha', provider: 'provider-a' },
    ]
    await nextTick()
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-b' })
  })
  it('starts ArrowUp at the last available model when search has no active result', async () => {
    const { selected } = await mount()
    const input = query<HTMLInputElement>('input')
    await key(input, 'ArrowUp')
    await key(input, 'Enter')
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-b' })
  })
  it('does not change routing when merely opening or hovering the model submenu', async () => {
    const { selected, mode } = await mount({ modelRoutingMode: 'squilla_router' }, false)
    const focused = document.activeElement
    query('.routing-mode').dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }))
    await nextTick()
    expect(query('[role="listbox"]')).toBeTruthy()
    expect(document.activeElement).toBe(focused)
    query('.routing-mode').dispatchEvent(new PointerEvent('pointerleave', { pointerType: 'mouse' }))
    await nextTick()
    expect(query('[role="listbox"]')).toBeTruthy()
    expect(selected).not.toHaveBeenCalled()
    expect(mode).not.toHaveBeenCalled()
  })
  it('shows the selected provider-specific model in the primary menu and updates it', async () => {
    const { props } = await mount(
      { modelSelection: { model: 'shared-model', provider: 'provider-b' } },
      false,
    )
    expect(query('.routing-mode__model').textContent).toContain('Model Beta')
    props.modelSelection = { model: 'shared-model', provider: 'provider-a' }
    await nextTick()
    expect(query('.routing-mode__model').textContent).toContain('Model Alpha')
    props.modelSelection = null
    await nextTick()
    expect(query('.routing-mode__model').textContent).toContain('Default model')
    props.modelRoutingMode = 'squilla_router'
    await nextTick()
    expect(query('.routing-mode__model')).toBeNull()
  })
  it('distinguishes the configured default from an explicit pin to the same model', async () => {
    const { props } = await mount({
      defaultModel: { model: 'shared-model', provider: 'provider-b' },
    })
    expect(query('.routing-mode__model-name').textContent).toBe('Model Beta')
    expect(query('.routing-mode__default').textContent).toBe('Default')
    expect(query('[role="option"]').getAttribute('aria-selected')).toBe('true')
    expect(query('[role="option"] .routing-model__provider').textContent).toContain(
      'Model Beta · provider-b',
    )
    props.modelSelection = { model: 'shared-model', provider: 'provider-b' }
    await nextTick()
    expect(query('.routing-mode__model-name').textContent).toBe('Model Beta')
    expect(query('.routing-mode__default')).toBeNull()
    expect(query('[role="option"]').getAttribute('aria-selected')).toBe('false')
    expect(query('[aria-selected="true"]').textContent).toContain('Model Beta')
  })
  it('lets a model selection request the direct-mode handoff from router mode', async () => {
    const { selected } = await mount({
      modelRoutingMode: 'squilla_router',
      modelSelectionDisabledReason: 'routing',
    })
    query<HTMLButtonElement>('.routing-mode').click()
    await nextTick()
    document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1]!.click()
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-a' })
  })
  it('preserves the single model default choice and explicit routing alternatives', async () => {
    const { selected, mode } = await mount()
    query<HTMLButtonElement>('[role="option"]').click()
    expect(selected).toHaveBeenCalledWith(null)
    query<HTMLButtonElement>('.routing-mode:nth-child(2)').click()
    expect(mode).toHaveBeenCalledWith('squilla_router')
    query<HTMLButtonElement>('.routing-mode:nth-child(3)').click()
    expect(mode).toHaveBeenCalledWith('llm_ensemble')
  })
  it('uses Escape to return one level first, then close, and ignores IME confirmation', async () => {
    const { close, selected } = await mount()
    const input = await search('Alpha')
    input.focus()
    await key(input, 'ArrowDown')
    await key(input, 'Enter', { isComposing: true })
    expect(selected).not.toHaveBeenCalled()
    await key(input, 'Escape')
    expect(query('[role="listbox"]')).toBeNull()
    expect(document.activeElement).toBe(query('.routing-mode'))
    expect(close).not.toHaveBeenCalled()
    await key(query('.routing-mode'), 'Escape')
    expect(close).toHaveBeenCalledOnce()
  })
  it('keeps a selected model absent from discovery selectable for backend validation', async () => {
    const { selected, refresh } = await mount({
      modelSelection: { model: 'offline-model', provider: 'provider-c' },
      modelProviderErrors: [{ provider: 'provider-c', kind: 'network', detail: 'offline' }],
    })
    const missing = query<HTMLButtonElement>('[aria-selected="true"]')
    expect(missing.textContent).toContain('offline-model')
    expect(missing.getAttribute('aria-disabled')).toBe('false')
    missing.click()
    expect(selected).toHaveBeenCalledWith({ model: 'offline-model', provider: 'provider-c' })
    expect(query('.routing-issue').textContent).toContain('provider-c')
    query<HTMLButtonElement>('.routing-retry').click()
    expect(refresh).toHaveBeenCalledOnce()
  })
  it('guards busy model and route controls without discarding focused elements', async () => {
    const { props, mode, selected } = await mount()
    const row = query<HTMLButtonElement>('.routing-mode:nth-child(2)')
    row.focus()
    props.busy = true
    await nextTick()
    expect(document.activeElement).toBe(row)
    row.click()
    document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1]!.click()
    expect(mode).not.toHaveBeenCalled()
    expect(selected).not.toHaveBeenCalled()
  })
  it('retains all routing modes and settings for an existing task without exposing a new-task picker', async () => {
    const { mode, settings } = await mount({ modelSelectionAvailable: false, sessionModelName: 'session-bound-model' })
    expect(query('.routing-mode__model-name').textContent).toBe('session-bound-model')
    expect(query('.routing-mode__default')).toBeNull()
    expect(document.querySelectorAll('[role="menuitemradio"]')).toHaveLength(3)
    expect(query('[role="listbox"]')).toBeNull()
    query<HTMLButtonElement>('.routing-mode').click()
    expect(mode).toHaveBeenCalledWith('off')
    query<HTMLButtonElement>('.routing-settings').click()
    expect(settings).toHaveBeenCalledOnce()
  })
  it('keeps model selection available after a conversation has started', async () => {
    const { selected } = await mount({
      isNewTask: false,
      modelSelection: { model: 'shared-model', provider: 'provider-b' },
    })
    expect(query('.new-task-model-menu strong').textContent).toBe('Conversation model')
    expect(query('.routing-model-scope').textContent).toContain('next turn')
    expect(query('[aria-selected="true"]').textContent).toContain('Model Beta')
    await search('Alpha')
    query<HTMLButtonElement>('[role="option"]').click()
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-a' })
  })
  it('keeps a legacy model without provider distinct from the default and provider-specific models', async () => {
    const { selected } = await mount({
      isNewTask: false,
      modelSelection: { model: 'shared-model', provider: null },
    })
    const legacy = query<HTMLButtonElement>('[aria-selected="true"]')
    expect(legacy.textContent).toContain('shared-model')
    expect(legacy.getAttribute('aria-disabled')).toBe('true')
    legacy.click()
    expect(selected).not.toHaveBeenCalled()
    document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1]!.click()
    expect(selected).toHaveBeenCalledWith({ model: 'shared-model', provider: 'provider-a' })
  })
  it('blocks concrete model changes during a response while retaining next-turn routing controls', async () => {
    const { selected, mode } = await mount({ isNewTask: false, modelSelectionDisabledReason: 'busy' })
    expect(query('.routing-model-scope').textContent).toContain('Finish the current response')
    document.querySelectorAll<HTMLButtonElement>('[role="option"]')[1]!.click()
    query<HTMLButtonElement>('[role="option"]').click()
    expect(selected).not.toHaveBeenCalled()
    query<HTMLButtonElement>('[data-mode="squilla_router"]').click()
    expect(mode).toHaveBeenCalledWith('squilla_router')
  })

  it('does not offer a local default escape for a disconnected existing session', async () => {
    const { selected } = await mount({
      isNewTask: false, modelSelectionAvailable: false,
      modelSelection: { model: 'shared-model', provider: 'provider-a' },
    })
    const defaultRow = query<HTMLButtonElement>('[role="option"]')
    expect(defaultRow.getAttribute('aria-disabled')).toBe('true')
    defaultRow.click()
    expect(selected).not.toHaveBeenCalled()
  })

})
