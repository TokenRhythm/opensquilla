import { describe, expect, it } from 'vitest'
import { ref } from 'vue'
import { createHistoryNavigationScrollLock } from './historyNavigationScrollLock'

describe('createHistoryNavigationScrollLock', () => {
  it('ignores near-bottom smooth-scroll frames until minimap navigation ends', () => {
    const autoScroll = ref(true)
    const lock = createHistoryNavigationScrollLock(autoScroll)

    lock.start()
    lock.updateFromScroll(12)
    expect(autoScroll.value).toBe(false)
    expect(lock.locked).toBe(true)

    lock.finish()
    lock.updateFromScroll(12)
    expect(autoScroll.value).toBe(true)
    expect(lock.locked).toBe(false)

    lock.start()
    lock.updateFromScroll(12)
    lock.finish()
    lock.updateFromScroll(180)
    expect(autoScroll.value).toBe(false)
  })

  it('reports reader interruption without restoring follow at navigation end', () => {
    const autoScroll = ref(true)
    const lock = createHistoryNavigationScrollLock(autoScroll)

    lock.start()
    expect(lock.interrupt()).toBe(true)
    expect(lock.interrupt()).toBe(false)
    lock.updateFromScroll(12)

    expect(autoScroll.value).toBe(false)
    expect(lock.finish()).toBe(true)
    expect(lock.locked).toBe(false)

    lock.start()
    expect(lock.finish()).toBe(false)
  })
})
