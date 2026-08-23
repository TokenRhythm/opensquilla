// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { App } from 'vue'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

vi.mock('@/components/Icon.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    default: defineComponent({
      name: 'IconStub',
      props: { name: { type: String, default: '' } },
      setup(props) {
        return () => h('span', { 'data-icon': props.name })
      },
    }),
  }
})

const rpcCall = vi.fn()

vi.mock('@/stores/rpc', () => ({
  useRpcStore: () => ({ call: rpcCall }),
}))

const QR_SVG = '<svg viewBox="0 0 10 10"><rect width="10" height="10" /></svg>'
const mountedApps: Array<{ app: App; el: HTMLElement }> = []

async function flush(): Promise<void> {
  const { nextTick } = await import('vue')
  await nextTick()
  await Promise.resolve()
  await nextTick()
}

async function mountDialog() {
  const { createApp, defineComponent, h } = await import('vue')
  const Dialog = (await import('./RemoteControlDialog.vue')).default
  const Host = defineComponent({
    setup() {
      return () => h(Dialog, { open: true, onClose: () => {} })
    },
  })
  const el = document.createElement('div')
  document.body.appendChild(el)
  const app = createApp(Host)
  app.mount(el)
  mountedApps.push({ app, el })
  await flush()
}

function toggle(): HTMLButtonElement {
  const el = document.querySelector<HTMLButtonElement>('[role="switch"]')
  if (!el) throw new Error('remote-control toggle not found')
  return el
}

afterEach(() => {
  while (mountedApps.length) {
    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()
  }
  document.body.innerHTML = ''
  window.localStorage.clear()
  rpcCall.mockReset()
})

describe('RemoteControlDialog', () => {
  it('renders the QR code and keeps the toggle on when the gateway reports epoch seconds', async () => {
    // Regression: the component compared epoch-seconds expiresAt against
    // Date.now() milliseconds, so the QR was treated as already expired.
    const expiresAtSeconds = Math.floor(Date.now() / 1000) + 600
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.identity.get') return { machine_name: 'laptop-test-zone' }
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: expiresAtSeconds,
          publicId: 'pair-1',
        }
      }
      return {}
    })

    await mountDialog()
    toggle().click()
    await flush()

    expect(document.querySelector('.rc-qr__svg svg')).toBeTruthy()
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    expect(document.querySelector('.rc-device-name')?.getAttribute('value') ?? '')
      .toBe('laptop-test-zone')
  })

  it('accepts an explicit millisecond expiry field', async () => {
    const expiresAtSeconds = Math.floor(Date.now() / 1000) + 600
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: expiresAtSeconds,
          expiresAtMs: expiresAtSeconds * 1000,
          publicId: 'pair-1',
        }
      }
      return {}
    })

    await mountDialog()
    toggle().click()
    await flush()

    expect(document.querySelector('.rc-qr__svg svg')).toBeTruthy()
    expect(toggle().getAttribute('aria-checked')).toBe('true')
  })

  it('turns the toggle back off and surfaces the error when creation fails', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') throw new Error('Tunnel setup failed')
      return {}
    })

    await mountDialog()
    toggle().click()
    await flush()

    expect(document.querySelector('.rc-qr__svg svg')).toBeNull()
    expect(toggle().getAttribute('aria-checked')).toBe('false')
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('Tunnel setup failed')
  })

  it('passes allowHostExecute to gateway.pairing.create when opted in', async () => {
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: Math.floor(Date.now() / 1000) + 600,
          publicId: 'pair-1',
        }
      }
      return {}
    })

    await mountDialog()

    const check = document.querySelector<HTMLInputElement>('.rc-hostexec-check')
    expect(check).toBeTruthy()
    check!.checked = true
    check!.dispatchEvent(new Event('change'))
    toggle().click()
    await flush()

    const createCall = rpcCall.mock.calls.find(([m]: string[]) => m === 'gateway.pairing.create')
    expect(createCall?.[1]).toEqual({ allowHostExecute: true })
  })

  it('never inherits a previous host-execute opt-in on a later pairing', async () => {
    // Granting host command execution is a per-pairing decision. Restoring a
    // past opt-in would let one confirmation silently escalate every later
    // pairing, so a remount must start from the server-side safe default.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: Math.floor(Date.now() / 1000) + 600,
          publicId: 'pair-remount',
        }
      }
      return {}
    })

    await mountDialog()
    const first = document.querySelector<HTMLInputElement>('.rc-hostexec-check')!
    first.checked = true
    first.dispatchEvent(new Event('change'))
    await flush()

    const { app, el } = mountedApps.pop()!
    app.unmount()
    el.remove()

    await mountDialog()
    const reopened = document.querySelector<HTMLInputElement>('.rc-hostexec-check')!
    expect(reopened.checked).toBe(false)
    // Nothing may be persisted that could resurrect the grant.
    expect(window.localStorage.getItem('opensquilla.remoteControl.allowHostExecute')).toBeNull()

    toggle().click()
    await flush()
    const createCall = rpcCall.mock.calls.find(([m]: string[]) => m === 'gateway.pairing.create')
    expect(createCall?.[1]).toEqual({ allowHostExecute: false })
  })

  it('resets the host-execute opt-in after revoking a pairing', async () => {
    // Revoke must close the privilege boundary: the next code has to be
    // confirmed again rather than reusing the withdrawn grant.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: Math.floor(Date.now() / 1000) + 600,
          publicId: 'pair-revoke',
        }
      }
      return {}
    })

    await mountDialog()
    const check = document.querySelector<HTMLInputElement>('.rc-hostexec-check')!
    check.checked = true
    check.dispatchEvent(new Event('change'))
    toggle().click()
    await flush()
    expect(rpcCall.mock.calls.find(([m]: string[]) => m === 'gateway.pairing.create')?.[1])
      .toEqual({ allowHostExecute: true })

    // Toggling off revokes everything that was just paired.
    toggle().click()
    await flush()
    expect(rpcCall.mock.calls.some(([m]: string[]) => m === 'gateway.pairing.revoke')).toBe(true)

    const afterRevoke = document.querySelector<HTMLInputElement>('.rc-hostexec-check')!
    expect(afterRevoke.checked).toBe(false)

    rpcCall.mockClear()
    toggle().click()
    await flush()
    const regenerated = rpcCall.mock.calls.find(([m]: string[]) => m === 'gateway.pairing.create')
    expect(regenerated?.[1]).toEqual({ allowHostExecute: false })
  })

  it('warns when the host cannot run Safe mode for a restricted pairing', async () => {
    // A pairing without host.execute is confined to Safe mode; if the host
    // cannot run Safe, the gateway rejects every send from that phone.
    rpcCall.mockImplementation(async (method: string) => {
      if (method === 'gateway.pairing.list') return { pairings: [] }
      if (method === 'gateway.pairing.create') {
        return {
          pairingUrl: 'https://remote-control.test/control/#token=osq_demo',
          qrCodeData: QR_SVG,
          expiresAt: Math.floor(Date.now() / 1000) + 600,
          publicId: 'pair-safe',
          safeModeUnavailableReason: 'probe_failed',
        }
      }
      return {}
    })

    await mountDialog()
    toggle().click()
    await flush()

    const hints = Array.from(document.querySelectorAll('.rc-error-hint')).map(n => n.textContent)
    expect(hints).toContain('remoteControl.safeModeUnavailable')
  })
})
