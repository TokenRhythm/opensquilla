// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, reactive } from 'vue'
import SetupProviderMenu, { type ProviderMenuItem } from './SetupProviderMenu.vue'

const items: ProviderMenuItem[] = [
  { id: 'edit', label: 'Edit', icon: 'edit' },
  { id: 'verify', label: 'Verify saved configuration', icon: 'check', disabled: true, hint: 'Add a model' },
  { id: 'delete', label: 'Delete', icon: 'trash', danger: true, separatorBefore: true },
]
async function settle() { await nextTick(); await nextTick(); await nextTick() }
async function mountMenu() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const state = reactive({ open: false, disabled: false })
  const action = vi.fn()
  // Render through a parent so reactive changes are propagated as real props.
  const host = createApp({ render: () => h(SetupProviderMenu, {
    label: 'DeepSeek more actions', items, ...state,
    'onUpdate:open': (open: boolean) => { state.open = open }, onAction: action,
  }) })
  host.mount(el)
  await settle()
  return { app: host, el, state, action, trigger: el.querySelector<HTMLButtonElement>('button')! }
}
function key(target: Element, value: string) {
  return target.dispatchEvent(new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }))
}
beforeEach(() => { document.body.innerHTML = '' })
afterEach(() => { vi.restoreAllMocks() })

describe('provider action menu', () => {
  it('portals outside the list, skips disabled items and returns focus without bubbling Escape', async () => {
    const { app, el, trigger, action } = await mountMenu()
    const escape = vi.fn()
    document.addEventListener('keydown', escape)
    trigger.focus()
    key(trigger, 'ArrowDown')
    await settle()
    const menu = document.querySelector<HTMLElement>('[role="menu"]')!
    expect(el.contains(menu)).toBe(false)
    const actions = menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')
    expect(document.activeElement).toBe(actions[0])
    key(menu, 'ArrowDown')
    expect(document.activeElement).toBe(actions[2])
    key(menu, 'Home')
    expect(document.activeElement).toBe(actions[0])
    key(menu, 'End')
    expect(document.activeElement).toBe(actions[2])
    key(menu, 'ArrowDown')
    expect(document.activeElement).toBe(actions[0])
    key(menu, 'ArrowUp')
    expect(document.activeElement).toBe(actions[2])
    escape.mockClear()
    key(menu, 'Escape')
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(escape).not.toHaveBeenCalled()
    expect(action).not.toHaveBeenCalled()
    document.removeEventListener('keydown', escape)
    app.unmount()
  })

  it('allows Tab to continue from the invoker and closes on outside click', async () => {
    const { app, trigger } = await mountMenu()
    key(trigger, 'ArrowUp')
    await settle()
    let menu = document.querySelector<HTMLElement>('[role="menu"]')!
    expect(document.activeElement?.textContent).toBe('Delete')
    expect(key(menu, 'Tab')).toBe(true)
    await settle()
    expect(document.activeElement).toBe(trigger)
    expect(document.querySelector('[role="menu"]')).toBeNull()
    trigger.click()
    await settle()
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    app.unmount()
  })

  it('focuses the stable trigger before emitting an action and blocks a busy portal', async () => {
    const { app, state, trigger, action } = await mountMenu()
    action.mockImplementation(() => expect(document.activeElement).toBe(trigger))
    trigger.click()
    await settle()
    document.querySelector<HTMLButtonElement>('[role="menuitem"]')!.click()
    expect(action).toHaveBeenCalledWith('edit')
    await settle()
    trigger.click()
    await settle()
    state.disabled = true
    await settle()
    expect(trigger.disabled).toBe(true)
    expect(document.querySelector('[role="menu"]')).toBeNull()
    trigger.click()
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(action).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('aligns right and opens upward at the viewport bottom, then closes on outside scrolling', async () => {
    const bounds = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      return (this.classList.contains('setup-provider-more')
        ? { top: 700, bottom: 732, left: 800, right: 832, width: 32, height: 32 }
        : { height: 140, width: 240, top: 0, bottom: 140, left: 0, right: 240 }) as DOMRect
    })
    const { app, trigger } = await mountMenu()
    trigger.click()
    await settle()
    const menu = document.querySelector<HTMLElement>('[role="menu"]')!
    expect(menu.style.left).toBe('592px')
    expect(menu.style.top).toBe('556px')
    menu.dispatchEvent(new Event('scroll'))
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeTruthy()
    document.dispatchEvent(new Event('scroll'))
    await settle()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    app.unmount()
    bounds.mockRestore()
  })

  it('keeps every action icon in a fixed slot for stable labels and checkmarks', async () => {
    const { app, trigger } = await mountMenu()
    trigger.click()
    await settle()

    const menu = document.querySelector<HTMLElement>('[role="menu"]')!
    const items = Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]'))
    expect(items).toHaveLength(3)
    expect(items.every(item => item.querySelector('.setup-provider-menu__icon'))).toBe(true)
    expect(items.map(item => item.querySelector('.setup-provider-menu__icon')?.className))
      .toEqual([
        expect.stringContaining('setup-provider-menu__icon'),
        expect.stringContaining('setup-provider-menu__icon'),
        expect.stringContaining('setup-provider-menu__icon'),
      ])
    expect(menu.querySelector('.setup-provider-menu__separator')).toBeTruthy()
    app.unmount()
  })
})
