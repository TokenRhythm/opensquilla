// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, h, nextTick, type App } from 'vue'
import { createI18n } from 'vue-i18n'
import SidebarConversations from './SidebarConversations.vue'
import type { SidebarSectionRow } from '@/composables/useSessions'

const mountedApps: App<Element>[] = []

async function mountSidebar() {
  const rows: SidebarSectionRow[] = ['first', 'second', 'third'].map((key, index) => ({
    key,
    title: `Task ${key}`,
    rowKind: 'session',
    sessionKind: 'chat',
    effectiveAgentId: 'main',
    agentName: 'Main',
    depth: 0,
    runStatus: 'idle',
    runLabel: 'Idle',
    taskAttention: 'none',
    updatedAt: 3 - index,
    hasContractGaps: false,
  }))
  const events = { select: vi.fn(), reorder: vi.fn() }
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(() => h(SidebarConversations, {
    sections: [{ family: 'chats', label: 'Tasks', rows }],
    error: false,
    loading: false,
    currentKey: '',
    contractDebugEnabled: false,
    searchHint: 'Ctrl+K',
    onSelect: events.select,
    onReorder: events.reorder,
  }))
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    missingWarn: false,
    fallbackWarn: false,
    messages: { en: {} },
  }))
  app.mount(host)
  mountedApps.push(app)
  await nextTick()
  const elements = rows.map((row, index) => {
    const element = host.querySelector<HTMLElement>(`[data-session-key="${row.key}"]`)!
    vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(new DOMRect(20, 100 + index * 40, 240, 40))
    return element
  })
  const source = elements[0]!
  const target = elements[2]!
  vi.spyOn(document, 'elementFromPoint').mockReturnValue(target)
  return { host, events, source, target }
}

function pointer(target: EventTarget, type: string, options: PointerEventInit = {}) {
  const event = new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    pointerId: 1,
    pointerType: 'mouse',
    button: 0,
    clientX: 80,
    clientY: type === 'pointerdown' ? 120 : 210,
    ...options,
  })
  target.dispatchEvent(event)
  return event
}

afterEach(() => {
  mountedApps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  localStorage.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SidebarConversations drag interaction', () => {
  it('keeps small pointer movements as a normal task selection', async () => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    const move = pointer(document, 'pointermove', { clientY: 123 })
    pointer(document, 'pointerup', { clientY: 123 })
    source.querySelector<HTMLButtonElement>('.sidebar-history-item')!.click()
    await nextTick()

    expect(move.defaultPrevented).toBe(false)
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
    expect(events.reorder).not.toHaveBeenCalled()
    expect(events.select).toHaveBeenCalledWith('first')
  })

  it('lifts a preview under the pointer and suppresses the click following a drop', async () => {
    const { host, source, target, events } = await mountSidebar()
    source.dispatchEvent(new MouseEvent('mouseenter'))
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    await nextTick()

    const preview = document.querySelector<HTMLElement>('.sidebar-session-drag-preview')!
    expect(preview.textContent).toContain('Task first')
    expect(preview.style.transform).toBe('translate3d(96px, 226px, 0)')
    expect(preview.style.width).toBe('220px')
    expect(source.classList.contains('is-dragging')).toBe(true)
    expect(target.classList.contains('is-drop-after')).toBe(true)
    expect(document.querySelector('.sidebar-session-preview')).toBeNull()

    pointer(document, 'pointermove', { clientX: 90, clientY: 215 })
    await nextTick()
    expect(preview.style.transform).toBe('translate3d(106px, 231px, 0)')
    pointer(document, 'pointerup')
    source.querySelector<HTMLButtonElement>('.sidebar-history-item')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    await nextTick()

    expect(events.reorder).toHaveBeenCalledExactlyOnceWith({
      draggedKey: 'first', targetKey: 'third', position: 'after',
    })
    expect(events.select).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
    expect(host.querySelector('.is-reordering, .is-dragging, .is-drop-after')).toBeNull()
  })

  it.each(['keyboard', 'touch'])('allows %s activation after a drag click lands on the wrapper', async (input) => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    pointer(document, 'pointerup')
    source.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))
    await nextTick()
    expect(events.select).not.toHaveBeenCalled()

    if (input === 'touch') {
      pointer(source, 'pointerdown', { pointerType: 'touch' })
      pointer(document, 'pointerup', { pointerType: 'touch' })
    }
    source.querySelector<HTMLButtonElement>('.sidebar-history-item')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true, detail: input === 'keyboard' ? 0 : 1 }))
    expect(events.select).toHaveBeenCalledExactlyOnceWith('first')
  })

  it('keeps the preview inside the viewport near the bottom right corner', async () => {
    const { source } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove', {
      clientX: window.innerWidth - 4,
      clientY: window.innerHeight - 4,
    })
    await nextTick()
    const preview = document.querySelector<HTMLElement>('.sidebar-session-drag-preview')!
    expect(preview.style.transform).toBe(
      `translate3d(${window.innerWidth - 232}px, ${window.innerHeight - 60}px, 0)`,
    )
  })

  it('moves the preview without measuring untouched list rows again', async () => {
    const { source } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    await nextTick()
    vi.mocked(source.getBoundingClientRect).mockClear()

    pointer(document, 'pointermove', { clientX: 90, clientY: 215 })
    await nextTick()
    const preview = document.querySelector<HTMLElement>('.sidebar-session-drag-preview')!
    expect(preview.style.transform).toBe('translate3d(106px, 231px, 0)')
    expect(source.getBoundingClientRect).not.toHaveBeenCalled()
  })

  it('updates the insertion marker when the list scrolls under a stationary drag', async () => {
    const { host, source, target, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    await nextTick()
    expect(target.classList.contains('is-drop-after')).toBe(true)

    const nextTarget = host.querySelector<HTMLElement>('[data-session-key="second"]')!
    vi.mocked(document.elementFromPoint).mockReturnValue(nextTarget)
    const list = host.querySelector<HTMLElement>('.sidebar-history-list')!
    list.scrollTop = 40
    list.dispatchEvent(new Event('scroll'))
    await nextTick()
    expect(nextTarget.classList.contains('is-drop-after')).toBe(true)
    expect(target.classList.contains('is-drop-after')).toBe(false)

    pointer(document, 'pointerup')
    expect(events.reorder).toHaveBeenCalledExactlyOnceWith({
      draggedKey: 'first', targetKey: 'second', position: 'after',
    })
  })

  it('hit-tests the release position instead of trusting the previous move', async () => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    vi.mocked(document.elementFromPoint).mockReturnValue(null)
    pointer(document, 'pointerup', { clientX: 800, clientY: 400 })
    await nextTick()

    expect(document.elementFromPoint).toHaveBeenLastCalledWith(800, 400)
    expect(events.reorder).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
  })

  it('ignores a different pointer without interrupting the active drag', async () => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove', { pointerId: 2 })
    await nextTick()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()

    pointer(document, 'pointermove')
    pointer(document, 'pointerup', { pointerId: 2 })
    pointer(document, 'pointercancel', { pointerId: 2 })
    await nextTick()
    expect(document.querySelector('.sidebar-session-drag-preview')).not.toBeNull()
    expect(events.reorder).not.toHaveBeenCalled()

    pointer(document, 'pointercancel')
    await nextTick()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
  })

  it.each(['Escape', 'blur'])('cancels with %s and allows the next ordinary click', async (reason) => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    if (reason === 'Escape') document.dispatchEvent(new KeyboardEvent('keydown', { key: reason }))
    else window.dispatchEvent(new Event('blur'))
    pointer(document, 'pointerup')
    await nextTick()

    expect(events.reorder).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
    pointer(source, 'pointerdown')
    pointer(document, 'pointerup')
    source.querySelector<HTMLButtonElement>('.sidebar-history-item')!.click()
    expect(events.select).toHaveBeenCalledWith('first')
  })

  it('leaves touch swipes to native scrolling', async () => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown', { pointerType: 'touch' })
    const move = pointer(document, 'pointermove', { pointerType: 'touch' })
    pointer(document, 'pointerup', { pointerType: 'touch' })
    await nextTick()

    expect(move.defaultPrevented).toBe(false)
    expect(events.reorder).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
  })

  it('cancels when the captured source disappears during a list update', async () => {
    const { source, events } = await mountSidebar()
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove')
    await nextTick()
    expect(document.querySelector('.sidebar-session-drag-preview')).not.toBeNull()

    source.remove()
    pointer(document, 'lostpointercapture')
    await nextTick()
    pointer(document, 'pointerup')

    expect(events.reorder).not.toHaveBeenCalled()
    expect(document.querySelector('.sidebar-session-drag-preview')).toBeNull()
  })

  it('scrolls a long list at its edge and stops the animation frame on cancellation', async () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback: FrameRequestCallback) => frames.push(callback)))
    const cancelFrame = vi.fn()
    vi.stubGlobal('cancelAnimationFrame', cancelFrame)
    const { host, source } = await mountSidebar()
    const list = host.querySelector<HTMLElement>('.sidebar-history-list')!
    vi.spyOn(list, 'getBoundingClientRect').mockReturnValue(new DOMRect(20, 100, 240, 200))
    pointer(source, 'pointerdown')
    pointer(document, 'pointermove', { clientY: 295 })
    frames[0]!(16)
    expect(list.scrollTop).toBeGreaterThan(0)
    expect(frames).toHaveLength(2)

    pointer(document, 'pointercancel')
    expect(cancelFrame).toHaveBeenCalledWith(2)
    const stoppedAt = list.scrollTop
    frames[1]!(32)
    expect(list.scrollTop).toBe(stoppedAt)
    expect(frames).toHaveLength(2)
  })
})
