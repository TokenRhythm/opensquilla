import { describe, expect, it, vi } from 'vitest'
import type { Virtualizer } from '@tanstack/vue-virtual'
import { readDistanceFromEnd, remeasureVirtualizer } from './virtualizerLayout'
import { nextTick } from 'vue'

describe('readDistanceFromEnd', () => {
  const container = () => ({ scrollTop: 250.5, scrollHeight: 1000, clientHeight: 600 }) as HTMLElement

  it('delegates to TanStack when its offset belongs to the current native position', () => {
    const element = container()
    const instance = Object.freeze({
      scrollElement: element, scrollOffset: 250.5, getDistanceFromEnd: vi.fn(() => 149.5),
    })
    expect(readDistanceFromEnd(element, instance)).toBe(149.5)
    expect(instance.getDistanceFromEnd).toHaveBeenCalledOnce()
  })

  it.each([0, 400, null])('reads live geometry without mutating a stale or pending core offset (%s)', offset => {
    const element = Object.freeze(container())
    const instance = Object.freeze({
      scrollElement: element, scrollOffset: offset, getDistanceFromEnd: vi.fn(() => 0),
    })
    expect(readDistanceFromEnd(element, instance)).toBe(149.5)
    expect(instance.getDistanceFromEnd).not.toHaveBeenCalled()
  })

  it('ignores another scroll container even if its offset happens to match', () => {
    const element = container()
    const instance = {
      scrollElement: container(), scrollOffset: element.scrollTop, getDistanceFromEnd: vi.fn(() => 0),
    }
    expect(readDistanceFromEnd(element, instance)).toBe(149.5)
    expect(instance.getDistanceFromEnd).not.toHaveBeenCalled()
  })

  it('reads current layout, preserves subpixels, and clamps short content and bottom overscroll', () => {
    const element = container()
    expect(readDistanceFromEnd(element)).toBe(149.5)
    Object.assign(element, { scrollHeight: 1200, clientHeight: 650 })
    expect(readDistanceFromEnd(element)).toBe(299.5)
    element.scrollTop = 551
    expect(readDistanceFromEnd(element)).toBe(0)
    Object.assign(element, { scrollHeight: 400, scrollTop: 0 })
    expect(readDistanceFromEnd(element)).toBe(0)
  })

  it('does not claim the reader is at the bottom before mount or after disposal', () => {
    expect(readDistanceFromEnd(null)).toBe(Infinity)
    expect(readDistanceFromEnd(undefined)).toBe(Infinity)
  })
})

function harness() {
  const release = vi.fn()
  const options = {
    shouldFollowEnd: vi.fn(() => false),
    isCurrent: vi.fn(() => true),
    keepAnchorMounted: vi.fn(() => release),
    getElement: vi.fn(() => ({} as HTMLElement)),
  }
  const instance = {
    scrollOffset: 147.5,
    getVirtualItemForOffset: vi.fn(() => ({ key: 'message-2', start: 120 })),
    measure: vi.fn(),
    getVirtualItems: vi.fn(() => [{ key: 'message-2', start: 280.25 }]),
    scrollToOffset: vi.fn(),
    scrollToEnd: vi.fn(),
    measureElement: vi.fn(),
    scrollElement: new EventTarget(),
    targetWindow: null as { requestAnimationFrame: (callback: FrameRequestCallback) => number } | null,
  }
  return {
    instance,
    virtualizer: instance as unknown as Virtualizer<HTMLElement, HTMLElement>,
    options,
    release,
  }
}

describe('remeasureVirtualizer', () => {
  it('leases a stable key and preserves the fractional offset inside its remeasured row', async () => {
    const { instance, virtualizer, options, release } = harness()
    const transaction = remeasureVirtualizer(virtualizer, options)
    expect(options.keepAnchorMounted).toHaveBeenCalledWith('message-2')
    expect(instance.measure).toHaveBeenCalledOnce()
    expect(instance.scrollToOffset).not.toHaveBeenCalled()
    expect(await transaction).toBe(true)
    expect(instance.scrollToOffset).toHaveBeenCalledWith(307.75, { behavior: 'auto' })
    expect(release).toHaveBeenCalledOnce()
  })

  it('checks current follow intent after the DOM update', async () => {
    const { instance, virtualizer, options, release } = harness()
    const transaction = remeasureVirtualizer(virtualizer, options)
    options.shouldFollowEnd.mockReturnValue(true)
    await transaction
    expect(instance.scrollToEnd).toHaveBeenCalledOnce()
    expect(instance.scrollToOffset).not.toHaveBeenCalled()
    expect(release).toHaveBeenCalledOnce()
  })

  it('uses the last stable reading key when earlier resize notifications changed the live range', async () => {
    const { instance, virtualizer, options } = harness()
    instance.getVirtualItemForOffset.mockReturnValue({ key: 'wrong-after-clamp', start: 120 })
    expect(await remeasureVirtualizer(virtualizer, {
      ...options, anchorOverride: { key: 'message-2', intraOffset: 5.5 },
    })).toBe(true)
    expect(options.keepAnchorMounted).toHaveBeenCalledWith('message-2')
    expect(instance.scrollToOffset).toHaveBeenCalledWith(285.75, { behavior: 'auto' })
  })

  it('preserves the anchor through an external layout change without discarding row measurements', async () => {
    const { instance, virtualizer, options } = harness()
    const applyLayoutChange = vi.fn()
    expect(await remeasureVirtualizer(virtualizer, { ...options, applyLayoutChange })).toBe(true)
    expect(applyLayoutChange).toHaveBeenCalledOnce()
    expect(instance.measure).not.toHaveBeenCalled()
    expect(instance.scrollToOffset).toHaveBeenCalledWith(307.75, { behavior: 'auto' })
  })

  it('does not restore a cancelled or replaced-session transaction', async () => {
    const { instance, virtualizer, options, release } = harness()
    const transaction = remeasureVirtualizer(virtualizer, options)
    options.isCurrent.mockReturnValue(false)
    expect(await transaction).toBe(false)
    expect(instance.scrollToEnd).not.toHaveBeenCalled()
    expect(instance.scrollToOffset).not.toHaveBeenCalled()
    expect(release).toHaveBeenCalledOnce()
  })

  it('does not scroll to an unrelated index when the anchor was removed', async () => {
    const { instance, virtualizer, options, release } = harness()
    instance.getVirtualItems.mockReturnValue([{ key: 'another-message', start: 280.25 }])
    await remeasureVirtualizer(virtualizer, options)
    expect(instance.scrollToOffset).not.toHaveBeenCalled()
    expect(release).toHaveBeenCalledOnce()
  })

  it('keeps the lease through a native frame and applies the synchronized row offset', async () => {
    const { instance, virtualizer, options, release } = harness()
    let frame!: FrameRequestCallback
    instance.targetWindow = { requestAnimationFrame: callback => { frame = callback; return 1 } }
    const transaction = remeasureVirtualizer(virtualizer, options)
    await nextTick()
    await nextTick()
    expect(release).not.toHaveBeenCalled()
    instance.getVirtualItems.mockReturnValue([{ key: 'message-2', start: 420.5 }])
    frame(16)
    await nextTick()
    await nextTick()
    frame(32)
    await nextTick()
    await nextTick()
    expect(release).not.toHaveBeenCalled()
    frame(48)
    await transaction
    expect(instance.scrollToOffset).toHaveBeenLastCalledWith(448, { behavior: 'auto' })
    expect(release).toHaveBeenCalledOnce()
  })

  it('yields to fresh reader input while awaiting the native frame', async () => {
    const { instance, virtualizer, options, release } = harness()
    let frame!: FrameRequestCallback
    instance.targetWindow = { requestAnimationFrame: callback => { frame = callback; return 1 } }
    const transaction = remeasureVirtualizer(virtualizer, options)
    await nextTick()
    await nextTick()
    const scrollCalls = instance.scrollToOffset.mock.calls.length
    instance.scrollElement.dispatchEvent(new Event('wheel'))
    frame(16)
    expect(await transaction).toBe(false)
    expect(instance.scrollToOffset).toHaveBeenCalledTimes(scrollCalls)
    expect(release).toHaveBeenCalledOnce()
  })
})
