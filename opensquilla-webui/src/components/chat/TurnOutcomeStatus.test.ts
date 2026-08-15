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

  it.each([
    { reason: 'process_restart' },
    { errorClass: 'process_restart' },
  ])('shows restart-specific cause and recovery guidance for $reason$errorClass', async (proof) => {
    const { app, host } = await renderOutcome({
      turnId: 'turn-restart',
      status: 'abandoned',
      kind: 'interrupted',
      ...proof,
    })

    expect(host.querySelector('.turn-outcome--process-restart')).not.toBeNull()
    expect(host.querySelector('.turn-outcome__title')?.textContent)
      .toContain('OpenSquilla restarted and interrupted this task.')
    expect(host.querySelector('.turn-outcome__guidance')?.textContent)
      .toContain("This task won't continue automatically.")
    expect(host.textContent).toContain('Review any existing results or tool activity')
    app.unmount()
  })

  it('does not treat a generic gateway cancellation as a process restart', async () => {
    const { app, host } = await renderOutcome({
      turnId: 'turn-disconnect',
      status: 'cancelled',
      cancellationSource: 'gateway_restart',
    })

    expect(host.querySelector('.turn-outcome--process-restart')).toBeNull()
    expect(host.textContent).toContain('Interrupted')
    expect(host.textContent).not.toContain("won't continue automatically")
    app.unmount()
  })

  it('requires the exact process restart reason', async () => {
    const { app, host } = await renderOutcome({
      turnId: 'turn-lookalike',
      status: 'interrupted',
      reason: 'PROCESS_RESTART',
    })

    expect(host.querySelector('.turn-outcome--process-restart')).toBeNull()
    expect(host.textContent).toContain('Interrupted')
    app.unmount()
  })
})
