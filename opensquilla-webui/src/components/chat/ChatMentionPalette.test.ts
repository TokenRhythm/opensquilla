// @vitest-environment happy-dom
import { createApp, h, nextTick, type App } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import i18n from '@/i18n'
import type { MentionCandidate } from '@/composables/chat/useChatMentions'
import ChatMentionPalette from './ChatMentionPalette.vue'

const t = i18n.global.t

// The palette only depends on `kind` plus a label/description, so the fixtures are
// cast once instead of mirroring unrelated mention-source metadata.
const directory = { kind: 'workspace_directory', label: 'src', description: '3 files' } as unknown as MentionCandidate
const file = { kind: 'workspace_file', label: 'src/math.py', description: 'Workspace file' } as unknown as MentionCandidate
const secondFile = { kind: 'workspace_file', label: 'README.md', description: 'Workspace file' } as unknown as MentionCandidate
const session = { kind: 'session', label: 'Refactor the parser', description: 'Yesterday' } as unknown as MentionCandidate

const apps: App[] = []
function mount(props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(ChatMentionPalette, { items: [], activeIndex: 0, loading: false, error: '', ...props }),
  })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  return host
}
function key(element: Element, value: string, options: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true, ...options })
  element.dispatchEvent(event)
  return event
}
afterEach(() => { apps.splice(0).forEach(app => app.unmount()); document.body.innerHTML = '' })

describe('chat mention palette', () => {
  it('groups workspace candidates before chats and renders one row per candidate', async () => {
    const host = mount({ items: [directory, file, session, secondFile] })
    await nextTick()

    const groups = [...host.querySelectorAll('[role="group"]')]
    expect(groups).toHaveLength(2)
    expect(groups.map(group => group.getAttribute('aria-label'))).toEqual([
      t('chat.mention.groups.workspace'),
      t('chat.mention.groups.sessions'),
    ])
    expect(groups[0]!.querySelector('.chat-slash-group')?.textContent).toBe(t('chat.mention.groups.workspace'))
    expect(groups[1]!.querySelector('.chat-slash-group')?.textContent).toBe(t('chat.mention.groups.sessions'))
    expect(groups[0]!.querySelectorAll('[role="option"]')).toHaveLength(3)
    expect(groups[1]!.querySelectorAll('[role="option"]')).toHaveLength(1)
    expect(host.querySelectorAll('[role="option"]')).toHaveLength(4)
    expect(host.querySelector('[role="listbox"]')?.getAttribute('aria-label')).toBe('mentions')
    expect(host.textContent).toContain(t('chat.mention.hint'))
  })

  it('renders only the chats group when there are no workspace candidates', async () => {
    const host = mount({ items: [session] })
    await nextTick()

    const groups = [...host.querySelectorAll('[role="group"]')]
    expect(groups).toHaveLength(1)
    expect(groups[0]!.getAttribute('aria-label')).toBe(t('chat.mention.groups.sessions'))
    expect(host.querySelector('[role="option"]')?.textContent).toContain('Refactor the parser')
  })

  it('shows the empty and loading placeholders instead of an empty list', async () => {
    const empty = mount({ items: [] })
    await nextTick()
    expect(empty.textContent).toContain(t('chat.skillPalette.empty'))
    expect(empty.querySelectorAll('[role="option"]')).toHaveLength(0)
    expect(empty.querySelectorAll('[role="group"]')).toHaveLength(0)

    const loading = mount({ items: [], loading: true })
    await nextTick()
    expect(loading.textContent).toContain(t('chat.skillPalette.loading'))
    expect(loading.textContent).not.toContain(t('chat.skillPalette.empty'))
  })

  it('emits the candidate object when a row is chosen', async () => {
    const choose = vi.fn()
    const host = mount({ items: [directory, file, session], activeIndex: 1, onChoose: choose })
    await nextTick()

    const rows = [...host.querySelectorAll<HTMLButtonElement>('[role="option"]')]
    expect(rows).toHaveLength(3)
    expect(rows[1]!.getAttribute('aria-selected')).toBe('true')
    rows[1]!.click()
    expect(choose).toHaveBeenCalledOnce()
    expect(choose).toHaveBeenCalledWith(file)
    rows[0]!.click()
    expect(choose).toHaveBeenLastCalledWith(directory)
  })

  it('closes on Escape and stays quiet while an IME composition is active', async () => {
    const close = vi.fn()
    const choose = vi.fn()
    const host = mount({ items: [file], onClose: close, onChoose: choose })
    await nextTick()

    const listbox = host.querySelector('[role="listbox"]')!
    expect(key(listbox, 'Escape', { isComposing: true }).defaultPrevented).toBe(false)
    expect(close).not.toHaveBeenCalled()

    const escape = key(listbox, 'Escape')
    expect(escape.defaultPrevented).toBe(true)
    expect(close).toHaveBeenCalledOnce()
    expect(choose).not.toHaveBeenCalled()
  })

  it('surfaces a load error instead of the empty placeholder', async () => {
    const error = t('chat.mention.unavailable')
    const host = mount({ items: [], error })
    await nextTick()

    expect(host.textContent).toContain(error)
    expect(host.textContent).not.toContain(t('chat.skillPalette.empty'))
    expect(host.querySelector('[role="status"]')?.textContent).toBe(error)
  })
})
