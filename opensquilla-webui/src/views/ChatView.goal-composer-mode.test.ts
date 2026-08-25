import { describe, expect, it } from 'vitest'

import chatViewSource from './ChatView.vue?raw'

function functionSource(name: string, nextName: string): string {
  const start = chatViewSource.indexOf(`async function ${name}(`)
  const end = chatViewSource.indexOf(`\nasync function ${nextName}(`, start)
  expect(start).toBeGreaterThanOrEqual(0)
  expect(end).toBeGreaterThan(start)
  return chatViewSource.slice(start, end)
}

describe('ChatView Goal and Plan composer mode exclusivity', () => {
  it('materializes a provisional bare-chat session before Goal registration', () => {
    const start = chatViewSource.indexOf('const chatGoals = useChatGoals({')
    const end = chatViewSource.indexOf('\napplyGoalSnapshot =', start)
    const source = chatViewSource.slice(start, end)
    const provisionalGuard = source.indexOf(
      "pendingSessionIntent.value !== 'new_chat'",
    )
    const createSession = source.indexOf("rpc.call<{ key?: string }>('sessions.create'")
    const captureRouting = source.indexOf('const draftInitialRoutingMode = initialRoutingMode.value')
    const persistRouting = source.indexOf("rpc.call('sessions.routing.set'")
    const preserveAgent = source.indexOf('agentId: agentIdFromSessionKey(sourceKey)')
    const preserveProject = source.indexOf('...(workspaceId ? { workspaceId } : {})')
    const staleNavigationFence = source.indexOf('sessionKey.value !== sourceKey')
    const staleProjectFence = source.indexOf('pendingWorkspaceId.value !== workspaceId')
    const bindProject = source.indexOf(
      'freshTaskDraft.bindMaterializedProjectTask(key, workspaceId)',
    )
    const switchSession = source.indexOf('await switchToSession(key)')

    expect(start).toBeGreaterThanOrEqual(0)
    expect(end).toBeGreaterThan(start)
    expect(provisionalGuard).toBeGreaterThanOrEqual(0)
    expect(source).not.toContain('sessionKey.value && !isDraftRoute()')
    expect(createSession).toBeGreaterThan(provisionalGuard)
    expect(captureRouting).toBeGreaterThan(provisionalGuard)
    expect(captureRouting).toBeLessThan(createSession)
    expect(preserveAgent).toBeGreaterThan(createSession)
    expect(preserveProject).toBeGreaterThan(createSession)
    expect(staleNavigationFence).toBeGreaterThan(createSession)
    expect(staleProjectFence).toBeGreaterThan(staleNavigationFence)
    expect(persistRouting).toBeGreaterThan(staleProjectFence)
    expect(persistRouting).toBeLessThan(switchSession)
    expect(bindProject).toBeGreaterThan(staleProjectFence)
    expect(bindProject).toBeLessThan(switchSession)
    expect(switchSession).toBeGreaterThan(createSession)
  })

  it('projects the durably accepted Goal source row before history catches up', () => {
    const start = chatViewSource.indexOf('function projectAcceptedGoalMessage({')
    const end = chatViewSource.indexOf('\nconst chatGoals = useChatGoals({', start)
    const source = chatViewSource.slice(start, end)
    const sessionFence = source.indexOf('response.sessionKey !== sessionKey.value')
    const durableId = source.indexOf(
      "response.userMessageId || response.goal?.sourceMessageId || ''",
    )
    const messageIdDedupe = source.indexOf(
      'message => message.messageId === messageId',
    )
    const clientIdDedupe = source.indexOf(
      'message => message.clientId === clientMessageId',
    )
    const append = source.indexOf('messages.value.push({')
    const sync = source.lastIndexOf('scheduleHistorySync()')

    expect(start).toBeGreaterThanOrEqual(0)
    expect(end).toBeGreaterThan(start)
    expect(sessionFence).toBeGreaterThanOrEqual(0)
    expect(durableId).toBeGreaterThan(sessionFence)
    expect(messageIdDedupe).toBeGreaterThan(durableId)
    expect(clientIdDedupe).toBeGreaterThan(messageIdDedupe)
    expect(append).toBeGreaterThan(clientIdDedupe)
    expect(source).toContain('messageId,')
    expect(source).toContain('clientId: clientMessageId')
    expect(source).toContain('turnId: taskId')
    expect(source).toContain("role: 'user'")
    expect(source).toContain('text: objective')
    expect(sync).toBeGreaterThan(append)

    const host = chatViewSource.slice(end, chatViewSource.indexOf('\napplyGoalSnapshot =', end))
    expect(host).toContain('onSetAccepted: projectAcceptedGoalMessage')
  })

  it('disarms a Goal draft only after Plan mode is accepted', () => {
    const source = functionSource('activatePlanComposerMode', 'activateGoalComposerMode')
    const setPlan = source.indexOf("await chatPlans.setMode('plan')")
    const disarmGoal = source.indexOf('if (accepted) disarmGoalMode()')

    expect(setPlan).toBeGreaterThanOrEqual(0)
    expect(disarmGoal).toBeGreaterThan(setPlan)
  })

  it('leaves Plan mode before arming a Goal draft', () => {
    const start = chatViewSource.indexOf('async function activateGoalComposerMode(')
    const end = chatViewSource.indexOf('\nfunction setCollaborationMode(', start)
    const source = chatViewSource.slice(start, end)
    const setDefault = source.indexOf("await chatPlans.setMode('default')")
    const rejectFailedSwitch = source.indexOf('if (!accepted) return false', setDefault)
    const armGoal = source.indexOf('armGoalMode()')

    expect(start).toBeGreaterThanOrEqual(0)
    expect(end).toBeGreaterThan(start)
    expect(setDefault).toBeGreaterThanOrEqual(0)
    expect(rejectFailedSwitch).toBeGreaterThan(setDefault)
    expect(armGoal).toBeGreaterThan(rejectFailedSwitch)
  })
})
