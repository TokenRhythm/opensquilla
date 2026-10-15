// @vitest-environment happy-dom
import { createApp, h, nextTick, type Component } from 'vue'
import { createI18n } from 'vue-i18n'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatComposerAddMenu from './ChatComposerAddMenu.vue'
import ChatSlashPalette from './ChatSlashPalette.vue'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'
import en from '@/locales/en.json'

const apps: ReturnType<typeof createApp>[] = []
const candidate: ChatSlashCommand = {
  kind: 'skill', name: 'visualize', cmd: '/visualize', label: 'Visualize', desc: 'Create interactive diagrams', aliases: ['画布'],
  skill: { name: 'visualize', instanceId: 'bundled:visualize', digest: 'sha256:one', generation: 1,
    description: 'Create interactive diagrams', aliases: ['画布'], kind: 'skill', source: 'bundled', disabled: false, manualOnly: false, ready: true },
}
function mount(component: Component, props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({ render: () => h(component, props) })
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en }, missingWarn: false, fallbackWarn: false }))
  app.mount(host)
  apps.push(app)
  return host
}
function addMenu(props: Record<string, unknown> = {}) {
  return mount(ChatComposerAddMenu, { goalModeActive: false, goalModeAvailable: true, goalModeBusy: false, goalModeExisting: false,
    planModeActive: false, planModeAvailable: true, planModeBusy: false, skills: [candidate], ...props })
}
function key(element: Element, value: string, options: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...options })
  element.dispatchEvent(event)
  return event
}
async function search(host: HTMLElement, query: string) {
  const input = host.querySelector<HTMLInputElement>('input')!
  input.value = query
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  return input
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

describe('shared composer capability menus', () => {
  it('finds a skill by a Chinese alias and returns the stable catalog object', async () => {
    const choose = vi.fn(); const close = vi.fn()
    const host = addMenu({ onChooseSkill: choose, onClose: close })
    const input = await search(host, '画布')
    expect(host.querySelectorAll('[role="menuitem"]')).toHaveLength(1)
    key(input, 'Enter')
    expect(choose).toHaveBeenCalledWith(candidate)
    expect(close).toHaveBeenCalledOnce()
  })
  it('does not choose or dismiss while a Chinese IME composition is active', async () => {
    const choose = vi.fn(); const close = vi.fn()
    const host = addMenu({ onChooseSkill: choose, onClose: close })
    const input = await search(host, '画布')
    input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: 'hua' }))
    expect(key(input, 'Enter').defaultPrevented).toBe(false)
    key(input, 'Escape')
    expect(choose).not.toHaveBeenCalled()
    expect(close).not.toHaveBeenCalled()
    input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '画布' }))
    key(input, 'Enter', { isComposing: true })
    expect(choose).not.toHaveBeenCalled()
    key(input, 'Enter')
    expect(choose).toHaveBeenCalledOnce()
  })
  it('moves actual focus with arrows, skips disabled actions, and preserves native Tab', async () => {
    const close = vi.fn()
    const host = addMenu({ attachmentsDisabled: true, onClose: close })
    await nextTick()
    const input = host.querySelector<HTMLInputElement>('input')!
    input.focus()
    key(input, 'ArrowDown')
    expect(document.activeElement?.getAttribute('data-skill-name')).toBe('visualize')
    key(document.activeElement!, 'ArrowDown')
    expect(document.activeElement?.textContent).toContain('Plan mode')
    expect(key(document.activeElement!, 'Tab').defaultPrevented).toBe(false)
    key(document.activeElement!, 'Escape')
    expect(close).toHaveBeenCalledOnce()
  })
  it('retains unready skill management and lets a failed catalog load retry locally', async () => {
    const unavailable = { ...candidate, skill: { ...candidate.skill!, ready: false, reasonCode: 'tools_unavailable' } }
    const choose = vi.fn(); const retry = vi.fn()
    const host = addMenu({ skills: [unavailable], skillsError: 'Catalog unavailable', onChooseSkill: choose, onRetrySkills: retry })
    await nextTick()
    const row = host.querySelector<HTMLButtonElement>('[data-skill-name="visualize"]')!
    expect(row.disabled).toBe(false)
    expect(row.textContent).toContain('Required tools unavailable')
    row.click()
    expect(choose).toHaveBeenCalledWith(unavailable)
    host.querySelector<HTMLButtonElement>('.composer-add-menu__retry')!.click()
    expect(retry).toHaveBeenCalledOnce()
  })
  it('shows the same selected capability in plus and slash without hiding its description', async () => {
    const selectedSkills = [candidate.skill!]
    const add = addMenu({ selectedSkills })
    const slash = mount(ChatSlashPalette, { items: [candidate], activeIndex: 0, loading: false, error: '', selectedSkills })
    await nextTick()
    for (const host of [add, slash]) {
      const row = host.querySelector('[data-skill-name="visualize"]')!
      expect(row.classList.contains('is-selected')).toBe(true)
      expect(row.querySelector('.composer-capability__description')?.textContent).toBe(candidate.desc)
    }
    expect(slash.querySelector('.chat-slash-item--active')?.getAttribute('aria-selected')).toBe('true')
  })
  it('deduplicates Browser Use against the skill catalog while retaining the legacy entry without a candidate', async () => {
    const browser = { ...candidate, name: 'browser-use', cmd: '/browser-use', label: 'Browser Use',
      skill: { ...candidate.skill!, name: 'browser-use', instanceId: 'bundled:browser-use' } }
    const choose = vi.fn()
    const host = addMenu({ skills: [browser], browserUseAvailable: true, onChooseSkill: choose })
    await nextTick()
    const rows = [...host.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].filter(row => row.textContent?.includes('Browser Use'))
    expect(rows).toHaveLength(1)
    rows[0]!.click()
    expect(choose).toHaveBeenCalledWith(browser)
    const fallback = addMenu({ skills: [], browserUseAvailable: true })
    await nextTick()
    expect(fallback.querySelector('[data-capability-key="browser"]')).not.toBeNull()
  })
  it('uses distinct semantic icons consistently in both menus', async () => {
    const pdf = { ...candidate, name: 'pdf-toolkit', cmd: '/pdf-toolkit', label: 'PDF tools',
      skill: { ...candidate.skill!, name: 'pdf-toolkit', instanceId: 'bundled:pdf-toolkit' } }
    const add = addMenu({ skills: [candidate, pdf] })
    const slash = mount(ChatSlashPalette, { items: [candidate, pdf], activeIndex: 0, loading: false, error: '' })
    await nextTick()
    const icon = (host: HTMLElement, name: string) => host.querySelector(`[data-skill-name="${name}"] .composer-capability__icon`)?.innerHTML
    expect(icon(add, 'visualize')).not.toBe(icon(add, 'pdf-toolkit'))
    expect(icon(add, 'visualize')).toBe(icon(slash, 'visualize'))
    expect(icon(add, 'pdf-toolkit')).toBe(icon(slash, 'pdf-toolkit'))
  })
})
