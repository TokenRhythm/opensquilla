// @vitest-environment happy-dom
import { createApp, nextTick, reactive } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { acquireSessionBootstrapAdmission } from '@/composables/chat/sessionBootstrapAdmission'
import { onReadinessInvalidated } from './readinessInvalidation'
import { useReadinessConnectionSync } from './useReadinessConnectionSync'

const cleanups: Array<() => void> = []
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup() })

function mount() {
  const access = reactive({ isAvailable: true, subscriptionEpoch: 1 })
  const settingsRefresh = vi.fn()
  const composerRefresh = vi.fn()
  cleanups.push(onReadinessInvalidated(settingsRefresh), onReadinessInvalidated(composerRefresh))
  const app = createApp({ setup() { useReadinessConnectionSync(access); return () => null } })
  const el = document.createElement('div')
  app.mount(el)
  cleanups.push(() => app.unmount())
  return { access, settingsRefresh, composerRefresh }
}

describe('app readiness synchronization', () => {
  it('broadcasts one refresh to every subscriber after a Gateway reconnect', async () => {
    const { access, settingsRefresh, composerRefresh } = mount()
    access.isAvailable = false
    access.subscriptionEpoch += 1
    await nextTick()
    expect(settingsRefresh).not.toHaveBeenCalled()
    access.isAvailable = true
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
    expect(composerRefresh).toHaveBeenCalledOnce()
  })

  it('defers reconnect refresh until critical session recovery releases admission', async () => {
    const { access, settingsRefresh, composerRefresh } = mount()
    const release = acquireSessionBootstrapAdmission()
    cleanups.push(release)
    access.subscriptionEpoch += 1
    await nextTick()
    expect(settingsRefresh).not.toHaveBeenCalled()
    release()
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
    expect(composerRefresh).toHaveBeenCalledOnce()

    const releaseUnrelatedHold = acquireSessionBootstrapAdmission()
    cleanups.push(releaseUnrelatedHold)
    await nextTick()
    releaseUnrelatedHold()
    await nextTick()
    expect(settingsRefresh).toHaveBeenCalledOnce()
  })
})
