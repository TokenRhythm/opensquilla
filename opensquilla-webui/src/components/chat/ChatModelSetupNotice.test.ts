// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const routerPush = vi.fn(async () => {})
const cleanups: Array<() => void> = []
const settle = () => new Promise(resolve => setTimeout(resolve, 10))

async function mountNotice(initial: Record<string, unknown> | Error, held = false) {
  vi.resetModules()
  vi.doMock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))
  const response = { value: initial }
  const status = vi.fn(async () => {
    if (response.value instanceof Error) throw response.value
    return response.value
  })
  const { createApp, h, nextTick, reactive } = await import('vue')
  const { acquireSessionBootstrapAdmission } = await import('@/composables/chat/sessionBootstrapAdmission')
  const releaseAdmission = held ? acquireSessionBootstrapAdmission() : () => {}
  cleanups.push(releaseAdmission)
  const i18n = (await import('@/i18n')).default
  i18n.global.locale.value = 'en'
  const Component = (await import('./ChatModelSetupNotice.vue')).default
  const { SETUP_WORKFLOW_KEY } = await import('@/modules/setupWorkflow')
  const { GATEWAY_ACCESS_KEY } = await import('@/modules/gatewayAccess')
  const gateway = reactive({ isAvailable: true, subscriptionEpoch: 1 })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const { useReadinessConnectionSync } = await import('@/composables/setup/useReadinessConnectionSync')
  const app = createApp({
    setup() {
      useReadinessConnectionSync(gateway)
      return () => h(Component)
    },
  })
  app.provide(SETUP_WORKFLOW_KEY, {
    status,
  } as unknown as import('@/modules/setupWorkflow').SetupWorkflow)
  app.provide(GATEWAY_ACCESS_KEY, gateway as unknown as import('@/modules/gatewayAccess').GatewayAccess)
  app.use(i18n)
  app.mount(el)
  let mounted = true
  const unmount = () => {
    if (mounted) app.unmount()
    mounted = false
  }
  cleanups.push(unmount)
  await settle()
  await nextTick()
  return { el, status, response, gateway, releaseAdmission, unmount }
}

beforeEach(() => {
  routerPush.mockClear()
  document.body.innerHTML = ''
})
afterEach(() => {
  for (const cleanup of cleanups.splice(0).reverse()) cleanup()
})

describe('ChatModelSetupNotice', () => {
  it('offers the existing model settings overlay when local configuration is missing', async () => {
    const { el, status } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')?.textContent).toContain('Configure a model service before chatting.')
    el.querySelector('button')!.click()
    await settle()
    expect(routerPush).toHaveBeenCalledWith('/settings/provider')
    // The notice only requests local status; it does not require a probe.
    expect(status).toHaveBeenCalledOnce()
  })

  it.each([
    { llmConfigured: true },
    {},
    new Error('Status unavailable'),
  ])('does not show a missing-model notice for ready or unknown state: %j', async response => {
    const { el } = await mountNotice(response)
    expect(el.querySelector('[role="status"]')).toBeNull()
    expect(el.querySelector('button')).toBeNull()
  })

  it('refreshes after a settings save and clears without a client restart', async () => {
    const { el, response } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')).not.toBeNull()
    response.value = { llmConfigured: true }
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    await settle()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('keeps setup reads behind chat session recovery and applies pending invalidation', async () => {
    const { el, status, response, releaseAdmission } = await mountNotice({ llmConfigured: false }, true)
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    expect(status).not.toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
    response.value = { llmConfigured: true }
    releaseAdmission()
    await settle()
    expect(status).toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })

  it('stops observing settings saves after unmount', async () => {
    const { status, unmount } = await mountNotice({ llmConfigured: false })
    unmount()
    status.mockClear()
    const { invalidateReadiness } = await import('@/composables/setup/readinessInvalidation')
    invalidateReadiness()
    await settle()
    expect(status).not.toHaveBeenCalled()
  })

  it('refreshes after native onboarding restarts the Gateway without a WebUI save event', async () => {
    const { el, status, response, gateway } = await mountNotice({ llmConfigured: false })
    expect(el.querySelector('[role="status"]')).not.toBeNull()
    status.mockClear()
    gateway.isAvailable = false
    gateway.subscriptionEpoch += 1
    response.value = { llmConfigured: true }
    await settle()
    expect(status).not.toHaveBeenCalled()
    gateway.isAvailable = true
    await settle()
    expect(status).toHaveBeenCalled()
    expect(el.querySelector('[role="status"]')).toBeNull()
  })
})
