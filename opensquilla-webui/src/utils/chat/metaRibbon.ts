// MetaSkill run-progress ribbon: pure transforms, helpers, and derived
// selectors. The helpers are DOM-free; the SFC consumes them from its
// computed state and Vue interpolation performs output escaping.

import type {
  MetaRunAnnouncedPayload,
  MetaRunCompletedPayload,
  MetaStepRescue,
  MetaStepStatePayload,
} from '@/types/rpc'

export type MetaStepState =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'substituted'
  | 'paused'
  | 'cancelled'

export type MetaRibbonLanguage = 'zh' | 'en'

export interface MetaStep {
  id: string
  label: string
  kind: string
  dependsOn: string[]
  state: MetaStepState
  statusText: string
  error: string
  substituteFor: string | null
  rescue: MetaStepRescue
}

export interface MetaRibbonState {
  runId: string
  metaSkillName: string
  language: MetaRibbonLanguage
  steps: MetaStep[]
  total: number
  runOutcome: string | null
}

export const STATE_GLYPH: Record<MetaStepState, string> = {
  pending: '○',
  running: '⚙',
  succeeded: '✓',
  failed: '✗',
  skipped: '↷',
  substituted: '⇄',
  paused: 'Ⅱ',
  cancelled: '−',
}

export const RESCUE_ACTION_IDS = new Set<string>([
  'retry-run',
  'retry-step',
  'retry-with-partial-context',
  'switch-meta-skill',
  'install-dependency',
  'continue-text-only',
  'review-paid-submit',
])

// The backend still accepts this action for compatibility, but today it has
// the same seed semantics as retry-step. Do not render two controls that imply
// a choice the runtime does not actually provide.
const SUPPRESSED_EQUIVALENT_RESCUE_ACTION_IDS = new Set<string>([
  'retry-with-partial-context',
])

const STATE_VALUES: MetaStepState[] = [
  'pending',
  'running',
  'succeeded',
  'failed',
  'skipped',
  'substituted',
  'paused',
  'cancelled',
]

export function humanizeStepId(id: string | null | undefined): string {
  if (!id) return ''
  return id.charAt(0).toUpperCase() + id.slice(1).replace(/[_-]/g, ' ')
}

export function detectLanguage(value: unknown): MetaRibbonLanguage {
  const text = String(value || '').toLowerCase()
  if (/[㐀-鿿]/.test(text) || text.startsWith('zh')) return 'zh'
  return 'en'
}

export function normalizeStateClass(value: unknown): MetaStepState {
  const state = String(value || 'pending').toLowerCase()
  return STATE_VALUES.includes(state as MetaStepState) ? (state as MetaStepState) : 'pending'
}

export function normalizeRunOutcome(value: unknown): string {
  const outcome = String(value || '').toLowerCase()
  if (outcome === 'ok' || outcome === 'success' || outcome === 'completed') return 'succeeded'
  if (outcome === 'canceled') return 'cancelled'
  return normalizeStateClass(outcome || 'pending')
}

export interface MetaRibbonCopy {
  running: string
  preparing: string
  toggleAria: string
  expand: string
  collapse: string
  recovered: string
  stepFailed: string
  retryRun: string
  switchSkill: string
  showDetail: string
  counter: (index: number, total: number) => string
  progressAria: (name: string) => string
  stepAria: (index: number, total: number, label: string, stepState: string) => string
  failedSummary: (label: string, errText: string) => string
  outcome: (value: string) => string
}

export function ribbonCopy(language: MetaRibbonLanguage): MetaRibbonCopy {
  if (language === 'zh') {
    return {
      running: '运行中…',
      preparing: '准备步骤',
      toggleAria: '折叠/展开步骤',
      expand: '展开',
      collapse: '收起',
      recovered: '已由替代步骤恢复',
      stepFailed: '步骤失败',
      retryRun: '重试整个 run',
      switchSkill: '切换 meta-skill…',
      showDetail: '查看错误详情',
      counter: (index, total) => `第 ${index} / ${total} 步`,
      progressAria: (name) => `${name} 运行进度`,
      stepAria: (index, total, label, stepState) =>
        `第 ${index} / ${total} 步：${label}，${
          {
            pending: '等待中',
            running: '运行中',
            succeeded: '已完成',
            failed: '失败',
            skipped: '已跳过',
            substituted: '已替代',
            paused: '已暂停',
            cancelled: '已取消',
          }[normalizeStateClass(stepState)] || stepState
        }`,
      failedSummary: (label, errText) => `✗ ${label} 失败 · ${errText}`,
      outcome: (value) =>
        ({
          pending: '等待中',
          running: '运行中',
          succeeded: '已完成',
          failed: '失败',
          skipped: '已跳过',
          substituted: '已替代',
          paused: '已暂停',
          cancelled: '已取消',
        }[normalizeStateClass(value)] || humanizeStepId(value)),
    }
  }
  return {
    running: 'Running…',
    preparing: 'Preparing steps',
    toggleAria: 'Collapse/expand steps',
    expand: 'Expand',
    collapse: 'Collapse',
    recovered: 'Recovered by substitute step',
    stepFailed: 'Step failed',
    retryRun: 'Retry whole run',
    switchSkill: 'Switch meta-skill…',
    showDetail: 'View error details',
    counter: (index, total) => `Step ${index} of ${total}`,
    progressAria: (name) => `${name} run progress`,
    stepAria: (index, total, label, stepState) =>
      `step ${index} of ${total}: ${label} ${normalizeStateClass(stepState)}`,
    failedSummary: (label, errText) => `✗ ${label} failed · ${errText}`,
    outcome: (value) => humanizeStepId(normalizeStateClass(value)),
  }
}

export function stepGlyph(step: MetaStep): string {
  const state = normalizeStateClass(step.state)
  return step.substituteFor ? STATE_GLYPH.substituted : STATE_GLYPH[state] || '○'
}

export function stateIcon(state: MetaStepState): string {
  return STATE_GLYPH[state] || '○'
}

/** Build a fresh ribbon state from a meta_run_announced payload. */
export function createRibbon(announce: MetaRunAnnouncedPayload): MetaRibbonState {
  return {
    runId: announce.run_id || '',
    metaSkillName: announce.meta_skill_name || '',
    language: detectLanguage(announce.language || announce.user_language || announce.meta_language),
    steps: (announce.steps || []).map((s) => ({
      id: s.id || '',
      label: s.label || humanizeStepId(s.id),
      kind: s.kind || '',
      dependsOn: s.depends_on || [],
      state: 'pending' as MetaStepState,
      statusText: '',
      error: '',
      substituteFor: null,
      rescue: {} as MetaStepRescue,
    })),
    total: announce.total || 0,
    runOutcome: null,
  }
}

/** Apply a meta_step_state event in place; no-op on unknown step. */
export function updateStep(state: MetaRibbonState, event: MetaStepStatePayload): MetaRibbonState {
  const step = state.steps.find((s) => s.id === event.step_id)
  if (!step) return state
  step.state = normalizeStateClass(event.state)
  if (event.status_text != null) step.statusText = event.status_text
  if (event.error) step.error = event.error
  if (event.substitute_for) step.substituteFor = event.substitute_for
  if (event.rescue) step.rescue = event.rescue
  return state
}

/** Apply a meta_run_completed event in place. */
export function completeRun(state: MetaRibbonState, event: MetaRunCompletedPayload): MetaRibbonState {
  const copy = ribbonCopy(state.language)
  state.runOutcome = normalizeRunOutcome(event.outcome)
  const completed = new Set(event.completed_steps || [])
  const failed = new Set(event.failed_steps || [])
  const recovered = new Set(event.recovered_steps || [])
  const skipped = new Set(event.skipped_steps || [])
  state.steps.forEach((step) => {
    if (recovered.has(step.id)) {
      step.state = 'substituted'
      step.statusText = step.statusText || copy.recovered
    } else if (failed.has(step.id)) {
      step.state = 'failed'
    } else if (skipped.has(step.id)) {
      step.state = 'skipped'
    } else if (completed.has(step.id)) {
      step.state = 'succeeded'
    }
  })
  return state
}

export function shouldShowActions(state: MetaRibbonState): boolean {
  return state.runOutcome === 'failed' && state.steps.some((s) => s.state === 'failed')
}

/* ── Derived selectors for ribbon presentation ───────────────────────── */

export function completedCount(state: MetaRibbonState): number {
  return state.steps.filter(
    (s) => s.state === 'succeeded' || s.state === 'skipped' || s.state === 'substituted',
  ).length
}

export function runningIndex(state: MetaRibbonState): number {
  return state.steps.findIndex((s) => s.state === 'running')
}

export function headerIndex(state: MetaRibbonState): number {
  if (state.runOutcome === 'succeeded') return state.total
  const running = runningIndex(state)
  return running >= 0 ? running + 1 : completedCount(state)
}

export function currentStep(state: MetaRibbonState): MetaStep | null {
  const running = runningIndex(state)
  return running >= 0 ? state.steps[running] : null
}

export function progressPercent(state: MetaRibbonState): number {
  return state.total > 0
    ? Math.max(0, Math.min(100, Math.round((headerIndex(state) / state.total) * 100)))
    : 0
}

export function overallState(state: MetaRibbonState): MetaStepState {
  const current = currentStep(state)
  return normalizeStateClass(current ? current.state : state.runOutcome || 'pending')
}

export function statusText(state: MetaRibbonState, copy: MetaRibbonCopy): string {
  const current = currentStep(state)
  return current ? current.statusText || copy.running : ''
}

export function currentLabel(state: MetaRibbonState, copy: MetaRibbonCopy): string {
  const current = currentStep(state)
  return current
    ? current.label
    : state.runOutcome
      ? copy.outcome(state.runOutcome)
      : copy.preparing
}

export function counterText(state: MetaRibbonState, copy: MetaRibbonCopy): string {
  return copy.counter(headerIndex(state), state.total)
}

export interface RibbonRescueButton {
  action: string
  stepId: string | null
  label: string
}

/** Failed step + the resolved rescue/retry buttons + summary text. */
export function failSummary(
  state: MetaRibbonState,
  copy: MetaRibbonCopy,
): { failedStep: MetaStep | null; summary: string; buttons: RibbonRescueButton[] } {
  const failedStep = state.steps.find((s) => s.state === 'failed') || null
  if (!failedStep) return { failedStep: null, summary: '', buttons: [] }
  const errText = failedStep.error || copy.stepFailed
  const summary = copy.failedSummary(failedStep.label, truncate(errText, 80))
  const rescueActions =
    failedStep.rescue && Array.isArray(failedStep.rescue.actions)
      ? failedStep.rescue.actions.filter((action) => (
          action
          && RESCUE_ACTION_IDS.has(String(action.id))
          && !SUPPRESSED_EQUIVALENT_RESCUE_ACTION_IDS.has(String(action.id))
        ))
      : []
  const buttons: RibbonRescueButton[] =
    rescueActions.length > 0
      ? rescueActions.map((action) => ({
          action: String(action.id || ''),
          stepId: failedStep.id,
          label: action.label || humanizeStepId(action.id || 'action'),
        }))
      : [
          { action: 'retry-run', stepId: null, label: copy.retryRun },
          { action: 'switch-skill', stepId: null, label: copy.switchSkill },
        ]
  buttons.push({ action: 'show-detail', stepId: failedStep.id, label: copy.showDetail })
  return { failedStep, summary, buttons }
}

export function truncate(value: string | null | undefined, n: number): string {
  const str = String(value ?? '')
  return str.length <= n ? str : str.slice(0, n - 1) + '…'
}
