// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createOfficeSelectionInteraction, type OfficeHitTarget } from './officeSelectionInteraction'
import type { OfficeSelection } from '@/types/officeSelection'

const hit: OfficeHitTarget = { kind: 'element', rect: { left: 30, top: 40, width: 100, height: 40 },
  selection: { format: 'pptx', kind: 'element', slideIndex: 0, shapeId: '4', text: '标题', truncated: false } }
let frames: Map<number, FrameRequestCallback>, frameId: number
beforeEach(() => {
  frames = new Map(); frameId = 0
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => { frames.set(++frameId, callback); return frameId })
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(id => { frames.delete(id) })
})
afterEach(() => { vi.restoreAllMocks(); document.body.replaceChildren() })
async function flushFrame() {
  const callbacks = [...frames.values()]; frames.clear()
  callbacks.forEach(callback => callback(0))
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
}
function harness(hitTest = vi.fn(async () => hit as OfficeHitTarget | null)) {
  const container = document.createElement('div'); document.body.append(container)
  vi.spyOn(container, 'getBoundingClientRect').mockReturnValue(new DOMRect(10, 20, 400, 300))
  const onSelection = vi.fn(), onSelectionCommit = vi.fn(), clearNativeSelection = vi.fn()
  const readNativeSelection = vi.fn<() => OfficeSelection | null>(() => null)
  const api = createOfficeSelectionInteraction({ container, hitTest, onSelection, onSelectionCommit, readNativeSelection, clearNativeSelection,
    labels: { text: '选择文字', element: '选择对象', range: '选择单元格' } })
  const move = (x = 50) => container.dispatchEvent(new PointerEvent('pointermove', { clientX: x, clientY: 50, bubbles: true }))
  const down = () => container.dispatchEvent(new PointerEvent('pointerdown', { clientX: 50, clientY: 50, button: 0, pointerId: 1, bubbles: true }))
  const up = () => window.dispatchEvent(new PointerEvent('pointerup', { clientX: 50, clientY: 50, button: 0, pointerId: 1, bubbles: true }))
  const click = () => {
    down(); up()
    container.dispatchEvent(new MouseEvent('click', { clientX: 50, clientY: 50, button: 0, bubbles: true }))
  }
  return { container, api, hitTest, onSelection, onSelectionCommit, readNativeSelection, clearNativeSelection, move, click, down, up,
    hover: () => container.querySelector<HTMLElement>('[data-office-hover]')!,
    selected: () => container.querySelector<HTMLElement>('[data-office-selected]')! }
}

describe('Office annotation interaction', () => {
  it('does no annotation work while idle and batches hover moves to one public hit test', async () => {
    const h = harness()
    h.move(); h.click(); h.api.nativeSelection(hit.selection!)
    await flushFrame()
    expect(h.hitTest).not.toHaveBeenCalled(); expect(h.onSelection).not.toHaveBeenCalled()
    h.api.setSelectionMode(true)
    h.move(45); h.move(55); h.move(65)
    expect(h.hitTest).not.toHaveBeenCalled()
    await flushFrame()
    expect(h.hitTest).toHaveBeenCalledExactlyOnceWith(65, 50)
    expect(h.hover().style.display).toBe('block')
    expect(h.hover().style.left).toBe('20px')
    expect(h.hover().textContent).toBe('选择对象')
    h.api.destroy()
  })

  it('keeps semantic selection across relevant scrolls and ignores unrelated scrolls', async () => {
    const h = harness(); h.api.setSelectionMode(true); h.click()
    await flushFrame()
    expect(h.selected().style.display).toBe('block')
    expect(h.onSelection).toHaveBeenLastCalledWith(hit.selection)
    const unrelated = document.createElement('div'); document.body.append(unrelated)
    unrelated.dispatchEvent(new Event('scroll'))
    expect(h.selected().style.display).toBe('block')
    h.container.dispatchEvent(new Event('scroll'))
    expect(h.api.enabled).toBe(true)
    expect(h.selected().style.display).toBe('none')
    expect(h.onSelection).toHaveBeenLastCalledWith(hit.selection)
    expect(h.clearNativeSelection).toHaveBeenCalledTimes(1)
    h.api.setSelectionMode(false)
    expect(h.clearNativeSelection).toHaveBeenCalled()
    h.api.nativeSelection(hit.selection!)
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    h.api.destroy()
  })

  it('drops an asynchronous hover result after exit and destruction', async () => {
    let resolve!: (value: OfficeHitTarget | null) => void
    const pending = new Promise<OfficeHitTarget | null>(done => { resolve = done })
    const h = harness(vi.fn(() => pending)); h.api.setSelectionMode(true); h.move()
    await flushFrame()
    h.api.setSelectionMode(false); h.api.destroy(); resolve(hit)
    await flushFrame()
    expect(h.container.querySelector('[data-office-annotation-layer]')).toBeNull()
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    expect(frames.size).toBe(0)
  })

  it('serializes hover hit tests and only displays the latest pointer target', async () => {
    let resolve!: (value: OfficeHitTarget | null) => void
    const hitTest = vi.fn<() => Promise<OfficeHitTarget | null>>()
      .mockImplementationOnce(() => new Promise(done => { resolve = done }))
      .mockResolvedValue(null)
    const h = harness(hitTest); h.api.setSelectionMode(true); h.move(40); await flushFrame()
    h.move(300); await flushFrame()
    expect(hitTest).toHaveBeenCalledTimes(1)
    resolve(hit); await Promise.resolve(); await Promise.resolve()
    expect(h.hover().style.display).toBe('none')
    await flushFrame()
    expect(hitTest).toHaveBeenCalledTimes(2)
    expect(h.hover().style.display).toBe('none')
    h.api.destroy()
  })

  it('keeps a pending click selection when the pointer moves or leaves during hit testing', async () => {
    let resolve!: (value: OfficeHitTarget | null) => void
    const hitTest = vi.fn<() => Promise<OfficeHitTarget | null>>()
      .mockImplementationOnce(() => new Promise(done => { resolve = done }))
      .mockResolvedValue(null)
    const h = harness(hitTest); h.api.setSelectionMode(true); h.click()
    h.move(300); await flushFrame()
    h.container.dispatchEvent(new PointerEvent('pointerleave'))
    resolve(hit); await flushFrame()
    expect(h.selected().style.display).toBe('block')
    expect(h.onSelection).toHaveBeenLastCalledWith(hit.selection)
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(hit.selection)
    h.api.destroy()
  })

  it.each(['clear', 'exit', 'new-pointerdown'] as const)('still cancels a pending click after %s', async action => {
    let resolve!: (value: OfficeHitTarget | null) => void
    const h = harness(vi.fn(() => new Promise<OfficeHitTarget | null>(done => { resolve = done })))
    h.api.setSelectionMode(true); h.click()
    if (action === 'clear') h.api.clearSelection()
    else if (action === 'exit') h.api.setSelectionMode(false)
    else h.container.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 200, clientY: 100 }))
    resolve(hit); await flushFrame()
    expect(h.selected().style.display).toBe('none')
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    h.api.destroy()
  })

  it('keeps reading text selectable and clears only a selection belonging to this preview', () => {
    const h = harness(), other = document.createElement('p')
    other.textContent = 'outside'; document.body.append(other)
    const text = document.createTextNode('inside'); h.container.append(text)
    const selection = document.getSelection()!, outsideRange = document.createRange()
    outsideRange.selectNodeContents(other); selection.addRange(outsideRange)
    h.api.setSelectionMode(true)
    expect(selection.toString()).toBe('outside')
    selection.removeAllRanges()
    const insideRange = document.createRange(); insideRange.selectNodeContents(text); selection.addRange(insideRange)
    h.api.setSelectionMode(false)
    expect(selection.toString()).toBe('')
    const reading = document.createRange(); reading.selectNodeContents(text); selection.addRange(reading)
    h.api.nativeSelection({ format: 'docx', kind: 'text', text: 'inside', truncated: false })
    expect(selection.toString()).toBe('inside')
    expect(h.onSelection).toHaveBeenLastCalledWith(null)
    h.api.destroy()
  })
})

describe('Office completed selection gestures', () => {
  const first: OfficeSelection = { format: 'docx', kind: 'text', text: '中', truncated: false }
  const final: OfficeSelection = { ...first, text: '中文最终选区' }
  const textHit: OfficeHitTarget = { ...hit, kind: 'text', selection: undefined }

  it('commits an object immediately once and permits the same object in a new gesture', async () => {
    const h = harness(); h.api.setSelectionMode(true); h.click()
    await Promise.resolve(); await Promise.resolve()
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(hit.selection)
    const selectionCalls = h.onSelection.mock.invocationCallOrder
    expect(selectionCalls[selectionCalls.length - 1]).toBeLessThan(h.onSelectionCommit.mock.invocationCallOrder[0]!)
    await flushFrame(); await flushFrame()
    expect(h.readNativeSelection).not.toHaveBeenCalled()
    h.api.nativeSelection(hit.selection!); h.api.nativeSelection(hit.selection!)
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(1)
    h.click(); await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(2)
    h.api.destroy()
  })

  it('waits for pointerup and the final public selection, including a release outside the preview', async () => {
    const h = harness(vi.fn(async () => textHit)); h.api.setSelectionMode(true); h.down()
    h.api.nativeSelection(first); h.api.nativeSelection(first)
    h.readNativeSelection.mockReturnValue(first)
    await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    h.up(); h.up()
    await flushFrame()
    expect(h.readNativeSelection).not.toHaveBeenCalled()
    h.readNativeSelection.mockReturnValue(final)
    h.api.nativeSelection(final)
    await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(final)
    h.api.nativeSelection(final); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(1)
    h.api.destroy()
  })

  it.each(['clear', 'exit', 'destroy', 'new-pointerdown', 'pointercancel'] as const)(
    'does not commit a pending native selection after %s', async action => {
      const h = harness(vi.fn(async () => textHit)); h.api.setSelectionMode(true)
      h.readNativeSelection.mockReturnValue(final); h.down(); h.up(); await flushFrame()
      if (action === 'clear') h.api.clearSelection()
      else if (action === 'exit') h.api.setSelectionMode(false)
      else if (action === 'destroy') h.api.destroy()
      else if (action === 'new-pointerdown') h.down()
      else window.dispatchEvent(new PointerEvent('pointercancel', { pointerId: 1 }))
      await flushFrame(); await flushFrame()
      expect(h.onSelectionCommit).not.toHaveBeenCalled()
      h.api.destroy()
    },
  )

  it('does not commit a preserved range from whitespace, buttons, or native scrollbars', async () => {
    const h = harness(vi.fn(async () => null)); h.api.setSelectionMode(true)
    h.readNativeSelection.mockReturnValue({ format: 'xlsx', kind: 'range', sheetIndex: 0, range: 'A1:B2', truncated: false })
    h.click(); await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    h.hitTest.mockResolvedValue({ ...textHit, kind: 'range' })
    const button = document.createElement('button'); h.container.append(button)
    button.dispatchEvent(new PointerEvent('pointerdown', { button: 0, pointerId: 1, bubbles: true }))
    h.up(); await flushFrame(); await flushFrame()
    const scroll = document.createElement('div'); h.container.append(scroll)
    vi.spyOn(scroll, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 100))
    Object.defineProperties(scroll, { clientWidth: { value: 80 }, offsetWidth: { value: 100 } })
    scroll.dispatchEvent(new PointerEvent('pointerdown', { button: 0, pointerId: 1, clientX: 90, bubbles: true }))
    h.up(); await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    expect(h.readNativeSelection).not.toHaveBeenCalled()
    h.api.destroy()
  })

  it('allows native range completion after drag autoscroll without duplicating click completion', async () => {
    const range: OfficeSelection = { format: 'xlsx', kind: 'range', sheetIndex: 0, range: 'A1:B2', truncated: false }
    const h = harness(vi.fn(async () => ({ ...textHit, kind: 'range' as const })))
    h.api.setSelectionMode(true); h.readNativeSelection.mockReturnValue(range); h.down()
    h.container.dispatchEvent(new Event('scroll')); h.api.invalidateFeedback(); h.up()
    h.container.dispatchEvent(new MouseEvent('click', { clientX: 50, clientY: 50, button: 0, bubbles: true }))
    await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(range)
    h.api.destroy()
  })

  it('commits a completed keyboard selection once and ignores repeats, Shift release, and unrelated keys', async () => {
    const h = harness(); h.api.setSelectionMode(true); h.readNativeSelection.mockReturnValue(final)
    const key = (type: string, key: string, extra: KeyboardEventInit = {}) => h.container.dispatchEvent(new KeyboardEvent(type, { key, bubbles: true, ...extra }))
    key('keydown', 'ArrowRight', { shiftKey: true }); key('keydown', 'ArrowRight', { shiftKey: true, repeat: true })
    h.api.nativeSelection(first); await flushFrame()
    expect(h.onSelectionCommit).not.toHaveBeenCalled()
    key('keyup', 'ArrowRight', { shiftKey: true }); key('keyup', 'ArrowRight', { shiftKey: true }); key('keyup', 'Shift')
    await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledExactlyOnceWith(final)
    key('keydown', 'x'); key('keyup', 'x'); await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(1)
    key('keydown', 'a', { ctrlKey: true }); key('keyup', 'a')
    await flushFrame(); await flushFrame()
    expect(h.onSelectionCommit).toHaveBeenCalledTimes(2)
    h.api.destroy()
  })
})
