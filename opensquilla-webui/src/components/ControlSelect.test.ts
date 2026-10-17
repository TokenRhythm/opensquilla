// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, ref, type App } from 'vue'
import ControlSelect from './ControlSelect.vue'

const apps: App[] = []
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = ''; vi.restoreAllMocks(); vi.unstubAllGlobals() })
async function mount(options: { value?: string | number; disabled?: boolean; fieldset?: boolean; numeric?: boolean } = {}) {
  const value = ref(options.value ?? 'b')
  const disabled = ref(Boolean(options.disabled))
  const fieldsetDisabled = ref(Boolean(options.fieldset))
  const changed = vi.fn()
  const host = document.createElement('div')
  document.body.append(host)
  const app = createApp({ setup: () => () => h('fieldset', { disabled: fieldsetDisabled.value }, [
    h('label', { for: 'picker' }, 'Provider'),
    h(ControlSelect, { id: 'picker', name: 'provider', modelValue: value.value, disabled: disabled.value,
      'onUpdate:modelValue': (next: string | number | boolean) => { value.value = next as string | number }, onChange: changed },
    { default: () => options.numeric ? [h('option', { value: 1 }, 'One'), h('option', { value: 2 }, 'Two')]
      : [h('option', { value: 'a', disabled: true }, 'Unavailable'), h('optgroup', { label: 'Configured' }, [
        h('option', { value: 'b' }, 'Beta'), h('option', { value: 'c' }, 'Charlie'), h('option', { value: 'd' }, 'Delta'),
      ]), h('optgroup', { label: 'Unavailable group', disabled: true }, [h('option', { value: 'e' }, 'Echo')])] }),
    h('button', { id: 'after' }, 'After'),
  ]) })
  app.mount(host); apps.push(app); await nextTick()
  const trigger = host.querySelector<HTMLButtonElement>('button[role="combobox"]')!
  const select = host.querySelector<HTMLSelectElement>('select')!
  async function key(key: string, extra: KeyboardEventInit = {}) {
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra })
    trigger.dispatchEvent(event); await nextTick(); await nextTick(); return event
  }
  async function show() { trigger.focus(); trigger.click(); await nextTick(); await nextTick() }
  const active = () => document.getElementById(trigger.getAttribute('aria-activedescendant') || '')
  return { host, trigger, select, value, disabled, fieldsetDisabled, changed, key, show, active }
}

describe('ControlSelect visible control', () => {
  it('labels the visible control and keeps one tab stop and the native named value', async () => {
    const { host, trigger, select } = await mount()
    expect(trigger.id).toBe('picker')
    expect(trigger.textContent).toContain('Beta')
    expect(select.name).toBe('provider')
    expect(select.value).toBe('b')
    expect(select.getAttribute('aria-hidden')).toBe('true')
    expect(select.tabIndex).toBe(-1)
    expect(host.querySelectorAll('#picker')).toHaveLength(1)
  })

  it('keeps focus on the trigger and skips disabled choices with arrows and Home/End', async () => {
    const { trigger, key, active } = await mount()
    trigger.focus()
    await key('ArrowUp'); expect(active()?.textContent).toContain('Delta')
    expect(document.activeElement).toBe(trigger)
    await key('Home'); expect(active()?.textContent).toContain('Beta')
    await key('ArrowDown'); expect(active()?.textContent).toContain('Charlie')
    await key('End'); expect(active()?.textContent).toContain('Delta')
    await key('ArrowDown'); expect(active()?.textContent).toContain('Delta')
  })

  it('keeps group names and unavailable choices visible without allowing selection', async () => {
    const { host, show, value, changed } = await mount()
    await show()
    expect(host.querySelector('[role="group"][aria-label="Configured"]')).toBeTruthy()
    const disabled = host.querySelector<HTMLElement>('[role="group"][aria-label="Unavailable group"] [role="option"]')!
    expect(disabled.getAttribute('aria-disabled')).toBe('true')
    disabled.click(); await nextTick()
    expect(value.value).toBe('b')
    expect(changed).not.toHaveBeenCalled()
  })

  it('refreshes translated labels and dynamic choices while preserving the saved value', async () => {
    const label = ref('English label')
    const showMore = ref(false)
    const host = document.createElement('div'); document.body.append(host)
    const app = createApp({ setup: () => () => h(ControlSelect, { value: 'saved', 'aria-label': label.value }, {
      default: () => [h('option', { value: 'saved' }, label.value), showMore.value ? h('option', { value: 'other' }, 'Other') : null],
    }) })
    apps.push(app); app.mount(host); await nextTick()
    label.value = '翻译后的名称'; showMore.value = true
    await nextTick(); await nextTick()
    const trigger = host.querySelector<HTMLButtonElement>('[role="combobox"]')!
    expect(trigger.textContent).toContain('翻译后的名称')
    expect(trigger.getAttribute('aria-label')).toBe('翻译后的名称')
    trigger.click(); await nextTick(); await nextTick()
    expect(host.querySelectorAll('[role="option"]')).toHaveLength(2)
    expect(host.querySelector('select')?.value).toBe('saved')
  })

  it('retains the highlighted value across reorder, recovers from removal, and closes an empty list', async () => {
    const choices = ref(['saved', 'second', 'third'])
    const host = document.createElement('div'); document.body.append(host)
    const changed = vi.fn()
    const app = createApp({ setup: () => () => h(ControlSelect, { value: 'saved', onChange: changed }, {
      default: () => choices.value.map(value => h('option', { value, key: value }, value)),
    }) })
    apps.push(app); app.mount(host); await nextTick()
    const trigger = host.querySelector<HTMLButtonElement>('[role="combobox"]')!
    const activeText = () => document.getElementById(trigger.getAttribute('aria-activedescendant') || '')?.textContent
    const settle = async () => { await nextTick(); await nextTick(); await nextTick() }
    trigger.click(); await settle()
    trigger.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true })); await settle()
    expect(activeText()).toContain('third')
    choices.value = ['third', 'saved', 'second']; await settle()
    expect(activeText()).toContain('third')
    choices.value = ['second', 'saved']; await settle()
    expect(activeText()).toContain('saved')
    choices.value = ['second']; await settle()
    expect(activeText()).toContain('second')
    choices.value = []; await settle()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(trigger.hasAttribute('aria-activedescendant')).toBe(false)
    expect(host.querySelector('[role="listbox"]')).toBeNull()
    expect(changed).not.toHaveBeenCalled()
  })

  it.each([' ', 'Enter'])('commits the highlighted choice with %s exactly once', async (confirmation) => {
    const { key, changed, value, trigger } = await mount()
    await key('ArrowDown'); await key('ArrowDown'); await key(confirmation)
    expect(value.value).toBe('c')
    expect(changed).toHaveBeenCalledTimes(1)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
  })

  it('commits pointer choices, preserves numeric values and the native change target', async () => {
    const { show, host, value, changed, select } = await mount({ numeric: true, value: 1 })
    await show()
    host.querySelectorAll<HTMLElement>('[role="option"]')[1]!.click()
    await nextTick()
    expect(value.value).toBe(2)
    expect(changed.mock.calls[0]![0].target).toBe(select)
    expect(select.value).toBe('2')
  })

  it('Escape cancels only this list while Tab permits the surrounding dialog to handle focus', async () => {
    const { show, key, value, trigger } = await mount()
    const documentKey = vi.fn()
    document.addEventListener('keydown', documentKey)
    await show(); await key('ArrowDown')
    documentKey.mockClear()
    const escape = await key('Escape')
    expect(escape.defaultPrevented).toBe(true)
    expect(documentKey).not.toHaveBeenCalled()
    expect(value.value).toBe('b')
    await show()
    const tab = await key('Tab')
    expect(tab.defaultPrevented).toBe(false)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    document.removeEventListener('keydown', documentKey)
  })

  it('supports typeahead without consuming an IME composition confirmation', async () => {
    const { key, active, trigger, value } = await mount()
    await key('c'); expect(active()?.textContent).toContain('Charlie')
    const enter = await key('Enter', { isComposing: true })
    expect(enter.defaultPrevented).toBe(false)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(value.value).toBe('b')
    await key('Enter'); expect(value.value).toBe('c')
  })

  it('honors a disabled fieldset, recovers when enabled, and closes if disabled while open', async () => {
    const { fieldsetDisabled, show, trigger, host } = await mount({ fieldset: true })
    expect(trigger.disabled).toBe(true)
    await show(); expect(host.querySelector('[role="listbox"]')).toBeNull()
    fieldsetDisabled.value = false
    await nextTick(); await nextTick(); await nextTick()
    expect(trigger.disabled).toBe(false)
    await show(); expect(host.querySelector('[role="listbox"]')).not.toBeNull()
    fieldsetDisabled.value = true
    await nextTick(); await nextTick(); await nextTick()
    expect(host.querySelector('[role="listbox"]')).toBeNull()
  })

  it('closes on outside pointer and focus changes without pulling focus back', async () => {
    const { show, host, trigger } = await mount()
    await show()
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await nextTick(); expect(trigger.getAttribute('aria-expanded')).toBe('false')
    await show()
    const after = host.querySelector<HTMLButtonElement>('#after')!
    after.focus(); await nextTick()
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(after)
  })

  it('uses actual visual viewport space, opens above a low anchor, and never forces a minimum height', async () => {
    const { trigger, show, host } = await mount()
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({ left: 270, right: 390, top: 190, bottom: 228, width: 120, height: 38, x: 270, y: 190, toJSON() {} })
    const viewport = new EventTarget() as EventTarget & { width: number; height: number; offsetLeft: number; offsetTop: number }
    Object.assign(viewport, { width: 320, height: 240, offsetLeft: 0, offsetTop: 0 })
    vi.stubGlobal('visualViewport', viewport)
    await show()
    const menu = host.querySelector<HTMLElement>('[role="listbox"]')!
    expect(Number.parseFloat(menu.style.width)).toBe(240)
    expect(Number.parseFloat(menu.style.top)).toBeLessThan(190)
    expect(Number.parseFloat(menu.style.left) + Number.parseFloat(menu.style.width)).toBeLessThanOrEqual(312)
    viewport.height = 80; window.dispatchEvent(new Event('resize')); await nextTick()
    expect(Number.parseFloat(menu.style.maxHeight)).toBeLessThanOrEqual(64)
  })

  it('caps a long list at 320px even when the viewport has more room above the trigger', async () => {
    const { trigger, show, host } = await mount()
    vi.spyOn(trigger, 'getBoundingClientRect').mockReturnValue({ left: 20, right: 220, top: 700, bottom: 738, width: 200, height: 38, x: 20, y: 700, toJSON() {} })
    vi.stubGlobal('visualViewport', Object.assign(new EventTarget(), { width: 900, height: 800, offsetLeft: 0, offsetTop: 0 }))
    await show()
    const menu = host.querySelector<HTMLElement>('[role="listbox"]')!
    vi.spyOn(menu, 'scrollHeight', 'get').mockReturnValue(1000)
    window.dispatchEvent(new Event('resize')); await nextTick()
    expect(menu.style.maxHeight).toBe('320px')
    expect(Number.parseFloat(menu.style.top)).toBe(376)
    expect(Number.parseFloat(menu.style.top) + Number.parseFloat(menu.style.maxHeight)).toBeLessThan(700)
  })
})
