// @vitest-environment happy-dom

import { afterEach, describe, expect, it } from 'vitest'
import { createApp, nextTick, type App } from 'vue'
import i18n from '@/i18n'
import type { GoalSnapshot } from '@/composables/chat/useChatGoals'
import zhHans from '@/locales/zh-Hans.json'
import type { ChatRenderedMessage } from '@/types/chat'
import ChatMessageList from './ChatMessageList.vue'

const apps: App<Element>[] = []

afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  i18n.global.locale.value = 'en'
})

function completedGoal(overrides: Partial<GoalSnapshot> = {}): GoalSnapshot {
  return {
    goalId: 'goal-1',
    sessionKey: 'agent:main:webchat:test',
    sessionId: 'session-1',
    epoch: 0,
    objective: 'Ship the durable Goal display',
    status: 'complete',
    stateRevision: 3,
    objectiveRevision: 1,
    progressRevision: 1,
    progress: null,
    continuationSeq: 0,
    activeTaskId: null,
    sourceMessageId: 'message-goal-source',
    terminalTurnId: 'turn-goal-terminal',
    executionState: 'idle',
    continuationDeferredReason: null,
    turnsStarted: 1,
    turnsSettled: 1,
    windowTurnsStarted: 1,
    activeTimeMs: 63_000,
    windowActiveTimeMs: 63_000,
    usage: {
      inputTokens: 10,
      outputTokens: 5,
      reasoningTokens: 2,
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
    ...overrides,
  }
}

function message(overrides: Partial<ChatRenderedMessage>): ChatRenderedMessage {
  return {
    id: 'message',
    role: 'assistant',
    displayRole: 'assistant',
    roleLabel: 'Assistant',
    text: 'Done.',
    timeStr: '',
    ts: null,
    showHeader: false,
    ...overrides,
  }
}

async function mountList(
  goal: GoalSnapshot | null,
  messages: ChatRenderedMessage[] = [
    message({
      id: 'goal-source',
      role: 'user',
      displayRole: 'user',
      roleLabel: 'You',
      text: 'Ship the durable Goal display',
      messageId: 'message-goal-source',
      turnId: 'turn-goal-terminal',
    }),
    message({
      id: 'goal-terminal-tool-loop',
      messageId: 'message-goal-terminal-tool-loop',
      turnId: 'turn-goal-terminal',
      text: 'The final checks passed.',
    }),
    message({
      id: 'goal-terminal',
      messageId: 'message-goal-terminal',
      turnId: 'turn-goal-terminal',
      text: 'The goal is complete.',
    }),
    message({
      id: 'later-assistant',
      messageId: 'message-later',
      turnId: 'turn-later',
      text: 'A later ordinary reply.',
    }),
  ],
) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const app = createApp(ChatMessageList, {
    messages,
    goal,
    goalElapsed: '1m 03s',
    shareMode: false,
    selectedMessageIds: new Set<string>(),
    stripTimePrefix: (value: string) => value,
    renderMarkdown: (value: string) => value,
    fmtTok: (value: number) => String(value),
    subagentSummary: (value: string) => value,
    subagentBody: (value: string) => value,
    toolCallGroups: () => [],
    isToolGroupOpen: () => false,
    isToolItemOpen: () => false,
    toolGroupStatusText: () => '',
    toolStatusText: () => '',
    toolSecondaryText: () => '',
    copyMessage: async () => true,
    downloadAttachment: async () => true,
  })
  app.use(i18n)
  app.mount(host)
  apps.push(app)
  await nextTick()
  return host
}

describe('ChatMessageList Goal anchors', () => {
  it('binds the origin and completion labels to durable message identities', async () => {
    const host = await mountList(completedGoal())

    expect(host.querySelector('.msg-user-goal-origin')?.textContent).toContain('Sent as goal')
    const assistants = host.querySelectorAll('.msg-ai')
    expect(assistants).toHaveLength(3)
    expect(assistants[0]?.querySelector('.msg-goal-outcome')).toBeNull()
    expect(assistants[1]?.querySelector('.msg-goal-outcome')?.textContent)
      .toContain('Goal achieved · 1 turns · 15 tokens')
    expect(assistants[2]?.querySelector('.msg-goal-outcome')).toBeNull()
  })

  it('waits for terminal task settlement before rendering the outcome', async () => {
    const working = await mountList(completedGoal({
      activeTaskId: 'task-goal-terminal',
      executionState: 'working',
    }))
    expect(working.querySelector('.msg-goal-outcome')).toBeNull()

    const queued = await mountList(completedGoal({
      activeTaskId: null,
      executionState: 'queued',
    }))
    expect(queued.querySelector('.msg-goal-outcome')).toBeNull()
  })

  it('does not guess anchors when an older backend omits identities', async () => {
    const host = await mountList(completedGoal({
      sourceMessageId: null,
      terminalTurnId: null,
    }))

    expect(host.querySelector('.msg-user-goal-origin')).toBeNull()
    expect(host.querySelector('.msg-goal-outcome')).toBeNull()
  })

  it('localizes both lightweight labels', async () => {
    i18n.global.setLocaleMessage('zh-Hans', zhHans)
    i18n.global.locale.value = 'zh-Hans'
    const host = await mountList(completedGoal())

    expect(host.querySelector('.msg-user-goal-origin')?.textContent).toContain('已作为目标发送')
    expect(host.querySelector('.msg-goal-outcome')?.textContent)
      .toContain('目标已完成 · 1 轮 · 15 个令牌')
  })

})
