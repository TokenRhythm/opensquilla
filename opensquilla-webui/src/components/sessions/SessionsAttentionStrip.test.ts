// @vitest-environment happy-dom

import { createApp, nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import i18n from '@/i18n'
import SessionsAttentionStrip from './SessionsAttentionStrip.vue'

const mounted: Array<ReturnType<typeof createApp>> = []

async function render(props: {
  approvalsCount: number
  runningCount: number
  queuedCount: number
  costUsd: number | null
  costPeriod: 'today' | 'total'
}) {
  i18n.global.locale.value = 'en'
  const root = document.createElement('div')
  document.body.appendChild(root)
  const app = createApp(SessionsAttentionStrip, props)
  app.use(i18n)
  app.mount(root)
  mounted.push(app)
  await nextTick()
  return root
}

afterEach(() => {
  mounted.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('SessionsAttentionStrip cost period', () => {
  it('labels legacy usage.status fallback as a cumulative total', async () => {
    const root = await render({
      approvalsCount: 0,
      runningCount: 0,
      queuedCount: 0,
      costUsd: 1.23,
      costPeriod: 'total',
    })

    expect(root.textContent).toContain('$1.23 total')
    expect(root.textContent).not.toContain('today')
  })

  it('labels ledger usage.query as today', async () => {
    const root = await render({
      approvalsCount: 0,
      runningCount: 1,
      queuedCount: 0,
      costUsd: 0.42,
      costPeriod: 'today',
    })

    expect(root.textContent).toContain('Cost today')
    expect(root.textContent).toContain('$0.42')
  })
})
