/** Toast copy for the scheduler's terminal `cron.run.finished` event.
 *
 * Kept out of `App.vue` so the decision this makes — which of the two success
 * strings to use, and how much of the summary survives — is testable without
 * mounting the shell.
 */

export interface CronRunFinishedEvent {
  jobId?: string
  jobName?: string
  runId?: string
  success?: boolean
  /** The scheduler's result summary. For a reminder job this *is* the payload:
   *  `make_static_message_handler` sends the configured text as the summary. */
  summary?: string
}

export interface CronRunToast {
  message: string
  tone: 'ok' | 'danger'
  duration: number
}

type Translate = (key: string, params?: Record<string, unknown>) => string

// The scheduler caps summaries at 500 characters, which is still far more than
// a toast should carry. A reminder is normally one short line and survives
// whole; a long agent summary is trimmed here and stays complete in run
// history.
const SUMMARY_MAX_CHARS = 160

const SUCCESS_DURATION_MS = 7_000
const FAILURE_DURATION_MS = 9_000

/** Collapse a summary into one toast-sized line, or "" when there is nothing. */
export function cronToastSummary(value: unknown): string {
  if (typeof value !== 'string') return ''
  // Newlines would otherwise stretch the toast; the full text stays in history.
  const text = value.replace(/\s+/g, ' ').trim()
  if (!text) return ''
  if (text.length <= SUMMARY_MAX_CHARS) return text
  return `${text.slice(0, SUMMARY_MAX_CHARS - 1).trimEnd()}…`
}

export function cronRunFinishedToast(
  event: CronRunFinishedEvent,
  t: Translate,
): CronRunToast {
  const jobName = event.jobName?.trim() || t('cronSkills.jobs.unnamedTask')
  if (event.success === false) {
    return {
      message: t('cronSkills.jobs.toastBackgroundFailed', { name: jobName }),
      tone: 'danger',
      duration: FAILURE_DURATION_MS,
    }
  }
  // A reminder's whole point is the text it carries. Pointing the user at run
  // history for a single line they asked to be shown is the reason this event
  // felt broken.
  const summary = cronToastSummary(event.summary)
  return {
    message: summary
      ? t('cronSkills.jobs.toastBackgroundCompleteWithSummary', {
          name: jobName,
          summary,
        })
      : t('cronSkills.jobs.toastBackgroundComplete', { name: jobName }),
    tone: 'ok',
    duration: SUCCESS_DURATION_MS,
  }
}
