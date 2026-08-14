import { describe, expect, it } from 'vitest'
import chatViewSource from './ChatView.vue?raw'

describe('ChatView Goal outcome placement', () => {
  it('checks settlement-backed rendered anchors before using the tail fallback', () => {
    expect(chatViewSource).toContain(
      'v-if="goalOutcomeGoal && !goalOutcomeHasMessageAnchor"',
    )
    expect(chatViewSource).toContain(
      'goalHasRenderedTerminalAnchor(goalOutcomeGoal.value, renderedMessages.value)',
    )
    expect(chatViewSource).toContain(
      'const goalOutcomeGoal = computed(() => lastGoalRun.value)',
    )
  })

  it('keeps settled outcomes read-only while active Goal controls remain available', () => {
    expect(chatViewSource).not.toContain('@goal-edit=')
    expect(chatViewSource).not.toContain('@goal-clear=')
    expect(chatViewSource).toContain('<GoalRibbon')
    expect(chatViewSource).toContain('@edit="editGoalFromRibbon"')
    expect(chatViewSource).toContain('@clear="clearGoal"')
    expect(chatViewSource).toContain("title: t('chat.goal.removeConfirmTitle')")
    expect(chatViewSource).toContain("body: t('chat.goal.removeConfirmBody')")
    expect(chatViewSource).toContain("primaryClass: 'btn--danger'")
    expect(chatViewSource).toContain('const requestedSessionKey = sessionKey.value')
    expect(chatViewSource).toContain('const requestedGoalIdentity = {')
    expect(chatViewSource).toContain('sessionKey.value !== requestedSessionKey')
    expect(chatViewSource).toContain('current.goalId !== requestedGoalIdentity.goalId')
  })
})
