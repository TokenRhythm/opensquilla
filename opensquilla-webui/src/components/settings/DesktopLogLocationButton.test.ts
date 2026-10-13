// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import DesktopLogLocationButton from './DesktopLogLocationButton.vue'

const { gatewayApi, pushToast } = vi.hoisted(() => ({
  gatewayApi: {
    revealLog: undefined as (() => Promise<boolean>) | undefined,
    getStatus: vi.fn(),
  },
  pushToast: vi.fn(),
}))

vi.mock('@/platform', () => ({ usePlatform: () => ({ gateway: gatewayApi }) }))
vi.mock('@/composables/useToasts', () => ({ useToasts: () => ({ pushToast }) }))

const mounted: App[] = []

async function settleAction() {
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  gatewayApi.revealLog = undefined
  gatewayApi.getStatus.mockReset()
  pushToast.mockReset()
  i18n.global.locale.value = 'en'
})

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

async function mountButton() {
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(DesktopLogLocationButton)
  app.use(i18n)
  // Deliberately do not provide GatewayAccess. Native log access must remain
  // usable while there is no connected or authenticated Gateway.
  app.mount(el)
  mounted.push(app)
  await nextTick()
  return {
    el,
    button: el.querySelector<HTMLButtonElement>('[data-testid="support-open-local-logs"]'),
  }
}

describe('DesktopLogLocationButton', () => {
  it('stays hidden without the native reveal capability', async () => {
    const { el, button } = await mountButton()
    expect(button).toBeNull()
    expect(el.querySelector('#settings-gateway-local-logs')).toBeNull()
    expect(gatewayApi.getStatus).not.toHaveBeenCalled()
  })

  it('opens the native location without reading Gateway status, a log path, or connection state', async () => {
    const revealLog = vi.fn(async () => true)
    gatewayApi.revealLog = revealLog
    gatewayApi.getStatus.mockRejectedValue(new Error('Gateway is stopped'))
    const { el, button } = await mountButton()
    expect(button?.disabled).toBe(false)
    expect(el.querySelector('#settings-gateway-local-logs')?.getAttribute('tabindex')).toBe('-1')
    expect(revealLog).not.toHaveBeenCalled()
    button!.click()
    await nextTick()
    expect(revealLog).toHaveBeenCalledTimes(1)
    expect(gatewayApi.getStatus).not.toHaveBeenCalled()
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('blocks repeated clicks until the native action completes', async () => {
    let complete!: (ok: boolean) => void
    const revealLog = vi.fn(() => new Promise<boolean>(resolve => { complete = resolve }))
    gatewayApi.revealLog = revealLog
    const { button } = await mountButton()
    button!.click()
    button!.click()
    await nextTick()
    expect(revealLog).toHaveBeenCalledTimes(1)
    expect(button?.disabled).toBe(true)
    expect(button?.getAttribute('aria-busy')).toBe('true')
    complete(true)
    await settleAction()
    expect(button?.disabled).toBe(false)
  })

  it('reports a missing log and re-enables the action', async () => {
    gatewayApi.revealLog = vi.fn(async () => false)
    const { button } = await mountButton()
    button!.click()
    await settleAction()
    expect(pushToast).toHaveBeenCalledWith(i18n.global.t('setup.runtime.noLogToReveal'), { tone: 'danger' })
    expect(button?.disabled).toBe(false)
  })

  it('reports a native reveal failure and allows retry', async () => {
    const revealLog = vi.fn().mockRejectedValueOnce(new Error('Shell unavailable')).mockResolvedValue(true)
    gatewayApi.revealLog = revealLog
    const { button } = await mountButton()
    button!.click()
    await settleAction()
    expect(pushToast).toHaveBeenCalledWith(
      i18n.global.t('setup.runtime.revealFailed', { error: 'Shell unavailable' }),
      { tone: 'danger' },
    )
    expect(button?.disabled).toBe(false)
    button!.click()
    await settleAction()
    expect(revealLog).toHaveBeenCalledTimes(2)
  })
})
