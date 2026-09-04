import i18n from '@/i18n'
import { GoalCenterError, type GoalCenterFailureReason } from '@/modules/goalCenter'

const GOAL_ERROR_KEYS: Readonly<Record<GoalCenterFailureReason, string>> = {
  'invalid-objective': 'chat.goal.errors.invalidObjective',
  'invalid-command': 'chat.goal.errors.invalidCommand',
  'not-found': 'chat.goal.errors.notFound',
  'session-changed': 'chat.goal.errors.sessionChanged',
  changed: 'chat.goal.errors.changed',
  'already-active': 'chat.goal.errors.alreadyActive',
  busy: 'chat.goal.errors.busy',
  'not-resumable': 'chat.goal.errors.notResumable',
  'execution-disabled': 'chat.goal.errors.executionDisabled',
  'connection-required': 'chat.goal.errors.connectionRequired',
  'plan-mode-active': 'chat.goal.errors.planModeActive',
  'plan-run-active': 'chat.goal.errors.planRunActive',
  'request-conflict': 'chat.goal.errors.requestConflict',
}

export function goalErrorMessage(error: unknown): string {
  if (error instanceof GoalCenterError && error.reason) {
    return i18n.global.t(GOAL_ERROR_KEYS[error.reason])
  }
  return error instanceof Error ? error.message : String(error ?? '')
}
