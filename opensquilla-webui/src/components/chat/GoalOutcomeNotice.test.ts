// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, h } from 'vue'
import { createI18n } from 'vue-i18n'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'
import en from '@/locales/en.json'
import GoalOutcomeNotice from './GoalOutcomeNotice.vue'

const apps: ReturnType<typeof createApp>[] = []
const i18n = createI18n({ legacy: false, locale: 'en', messages: { en } })

function completedGoal(): GoalSnapshot {
  return {
    goalId: 'goal-complete',
    sessionKey: 'agent:main:webchat:test',
    sessionId: 'session-1',
    epoch: 1,
    objective: 'Ship the completed Goal controls',
    status: 'complete',
    stateRevision: 5,
    objectiveRevision: 1,
    progressRevision: 1,
    progress: null,
    continuationSeq: 0,
    activeTaskId: null,
    executionState: 'idle',
    continuationDeferredReason: null,
    turnsStarted: 2,
    turnsSettled: 2,
    windowTurnsStarted: 2,
    activeTimeMs: 63_000,
    windowActiveTimeMs: 63_000,
    usage: {
      inputTokens: 10,
      outputTokens: 5,
      reasoningTokens: 0,
      cacheReadTokens: 0,
      cacheWriteTokens: 0,
      totalTokens: 15,
    },
    pauseReason: null,
    blockedReason: null,
    terminalReason: 'model_complete',
    createdAt: 1,
    updatedAt: 2,
    finishedAt: 2,
  }
}

function mountNotice(props: Record<string, unknown> = {}) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp({
    render: () => h(GoalOutcomeNotice, {
      goal: completedGoal(),
      elapsed: '1m 03s',
      ...props,
    }),
  })
  apps.push(app)
  app.use(i18n)
  app.mount(host)
  return host
}

afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  document.body.innerHTML = ''
})

describe('GoalOutcomeNotice', () => {
  it('renders a settled Goal as a read-only outcome', () => {
    const host = mountNotice()
    expect(host.textContent).toContain('Goal complete')
    expect(host.textContent).toContain('Ship the completed Goal controls')
    expect(host.textContent).toContain('1m 03s active')
    expect(host.querySelector('button')).toBeNull()
    expect(host.querySelector('textarea')).toBeNull()
  })

  it('uses the compact achieved label when embedded in the assistant footer', () => {
    const host = mountNotice({ inline: true })

    expect(host.textContent).toContain('Goal achieved · 1m 03s active')
    expect(host.querySelector('.goal-outcome--inline')).not.toBeNull()
    expect(host.querySelector('button')).toBeNull()
  })
})
