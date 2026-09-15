import { describe, expect, it, vi } from 'vitest'
import type { RpcTransport } from './privateTransports'
import { createV4ProductActivity } from './productActivityV4'

function harness(result: unknown = { recorded: true }) {
  const request = vi.fn(async () => result)
  const supports = vi.fn(() => true)
  const markUnsupported = vi.fn()
  const activity = createV4ProductActivity({
    request: request as RpcTransport['request'], supports, markUnsupported,
  })
  return { activity, request, supports, markUnsupported }
}

describe('product activity Gateway adapter', () => {
  it.each(['desktop', 'web'] as const)('sends only the %s surface through a bounded, non-reconnecting request', async (surface) => {
    const { activity, request } = harness()
    const signal = new AbortController().signal
    await expect(activity.recordActive(surface, { signal })).resolves.toBe(true)
    expect(request).toHaveBeenCalledWith('telemetry.product_active.record', { surface }, {
      signal, timeoutMs: 5_000, timeoutAction: 'reject', abortAction: 'reject',
    })
  })

  it('retains the not-recorded result for consent and retry handling', async () => {
    await expect(harness({ recorded: false }).activity.recordActive('web')).resolves.toBe(false)
  })

  it('does not call an unadvertised method on an older Gateway', async () => {
    const { activity, request, supports } = harness()
    supports.mockReturnValue(false)
    await expect(activity.recordActive('web')).rejects.toMatchObject({ code: 'unsupported' })
    expect(request).not.toHaveBeenCalled()
  })

  it('remembers METHOD_NOT_FOUND without surfacing transport details', async () => {
    const { activity, request, markUnsupported } = harness()
    request.mockRejectedValue({ code: 'METHOD_NOT_FOUND', message: 'old Gateway' })
    await expect(activity.recordActive('web')).rejects.toMatchObject({ code: 'unsupported' })
    expect(markUnsupported).toHaveBeenCalledWith('telemetry.product_active.record')
  })

  it.each([null, {}, { recorded: 'yes' }])('rejects an invalid receipt %j', async (result) => {
    await expect(harness(result).activity.recordActive('web')).rejects.toMatchObject({ code: 'unavailable' })
  })
})
