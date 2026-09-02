// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'
import { createApp, type App } from 'vue'

import MetaPreflightCard from './MetaPreflightCard.vue'
import MetaRibbon from './MetaRibbon.vue'
import { completeRun, createRibbon, updateStep } from '@/utils/chat/metaRibbon'

const apps: App<Element>[] = []

function mount(component: Parameters<typeof createApp>[0], props: Record<string, unknown>) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(component, props)
  app.mount(host)
  apps.push(app)
  return host
}

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
})

describe('Meta run read-only actions', () => {
  it('disables preflight turn actions while retaining dismiss', () => {
    const actions: string[] = []
    const host = mount(MetaPreflightCard, {
      state: {
        runId: 'preflight-run',
        metaSkillName: 'meta-short-drama',
        language: 'en',
        interpretedRequest: 'Create a short drama',
        missingFields: [],
        assumptions: [],
        fields: [],
        outcome: 'video',
        canSkip: true,
        requiresGate: true,
      },
      phase: 'ready',
      turnActionsDisabled: true,
      onAction: (payload: { action: string }) => actions.push(payload.action),
    })

    const defaults = host.querySelector<HTMLButtonElement>('[data-action="defaults"]')
    const dismiss = host.querySelector<HTMLButtonElement>('[data-action="dismiss"]')
    const continueButton = host.querySelector<HTMLButtonElement>('[data-action="continue"]')
    expect(defaults?.disabled).toBe(true)
    expect(continueButton?.disabled).toBe(true)
    expect(dismiss?.disabled).toBe(false)

    defaults?.click()
    continueButton?.click()
    dismiss?.click()
    expect(actions).toEqual(['dismiss'])
  })

  it('disables ribbon rescue actions while retaining inspection controls', () => {
    const ribbon = createRibbon({
      runId: 'failed-run',
      metaSkillName: 'meta-paper-write',
      language: 'en',
      total: 1,
      steps: [{ id: 'compile', label: 'Compile', kind: 'skill_exec', dependsOn: [] }],
    })
    updateStep(ribbon, {
      runId: 'failed-run',
      stepId: 'compile',
      state: 'failed',
      error: 'Compilation failed',
      rescue: {
        actions: [
          { id: 'retry-step', label: 'Retry failed step' },
          { id: 'review-paid-submit', label: 'Review paid submission' },
        ],
      },
    })
    completeRun(ribbon, {
      runId: 'failed-run',
      outcome: 'failed',
      completedSteps: [],
      failedSteps: ['compile'],
      recoveredSteps: [],
      skippedSteps: [],
    })
    const actions: string[] = []
    const host = mount(MetaRibbon, {
      run: ribbon,
      turnActionsDisabled: true,
      onAction: (payload: { action: string }) => actions.push(payload.action),
    })

    const rescueButtons = [...host.querySelectorAll<HTMLButtonElement>('.meta-ribbon-actions button')]
    const showDetail = rescueButtons.find(button => button.dataset.action === 'show-detail')
    expect(showDetail?.disabled).toBe(false)
    expect(rescueButtons.filter(button => button !== showDetail).every(button => button.disabled))
      .toBe(true)
    expect(host.querySelector<HTMLButtonElement>('.meta-ribbon-toggle')?.disabled).toBe(false)

    rescueButtons.forEach(button => button.click())
    expect(actions).toEqual(['show-detail'])
  })
})
