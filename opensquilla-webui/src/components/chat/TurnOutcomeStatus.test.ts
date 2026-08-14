// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import i18n from '@/i18n'
import type { ChatTurnOutcome } from '@/types/chat'
import TurnOutcomeStatus from './TurnOutcomeStatus.vue'

async function renderOutcome(outcome: ChatTurnOutcome) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(TurnOutcomeStatus, { outcome })
  app.use(i18n)
  app.mount(host)
  await nextTick()
  return { app, host }
}

afterEach(() => {
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

describe('TurnOutcomeStatus', () => {
  it.each([
    {
      outcome: {
        turnId: 'turn-complete',
        status: 'succeeded',
      },
      presentation: 'completed',
      label: 'Completed',
    },
    {
      outcome: {
        turnId: 'turn-stop',
        status: 'cancelled',
        cancellationSource: 'webui_stop',
      },
      presentation: 'stopped',
      label: 'Stopped',
    },
    {
      outcome: {
        turnId: 'turn-interrupted',
        status: 'cancelled',
        cancellationSource: 'gateway_restart',
      },
      presentation: 'interrupted',
      label: 'Interrupted',
    },
    {
      outcome: {
        turnId: 'turn-timeout',
        status: 'timeout',
      },
      presentation: 'timeout',
      label: 'Timed out',
    },
    {
      outcome: {
        turnId: 'turn-failed',
        status: 'failed',
      },
      presentation: 'failed',
      label: 'Failed',
    },
  ] as const)('renders $presentation from typed outcome state', async ({
    outcome,
    presentation,
    label,
  }) => {
    const { app, host } = await renderOutcome(outcome)

    expect(host.querySelector(`[data-testid="turn-outcome-${presentation}"]`))
      .not.toBeNull()
    expect(host.textContent).toContain(label)
    app.unmount()
  })

  it('renders duration from ISO timestamps without creating an assistant bubble', async () => {
    const { app, host } = await renderOutcome({
      turnId: 'turn-stop',
      status: 'cancelled',
      cancellationSource: 'webui_escape',
      startedAt: '2026-07-29T10:00:00.000Z',
      finishedAt: '2026-07-29T10:00:45.000Z',
    })

    expect(host.textContent).toContain('Stopped')
    expect(host.textContent).toContain('45s')
    expect(host.querySelector('.msg-ai')).toBeNull()
    app.unmount()
  })
})
