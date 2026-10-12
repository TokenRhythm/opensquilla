import type { ToastAction, ToastTone } from '@/composables/useToasts'
import type { CronRunFinished } from '@/modules/cronScheduler'

const claimedNotifications = new Set<string>()
const MAX_CLAIMED_NOTIFICATIONS = 1024
const REMINDER_PREVIEW_MAX_CHARS = 120

export function reminderToastPreview(value: unknown): string {
  const normalized = typeof value === 'string' ? value.trim().replace(/\s+/g, ' ') : ''
  if (normalized.length <= REMINDER_PREVIEW_MAX_CHARS) return normalized
  return `${normalized.slice(0, REMINDER_PREVIEW_MAX_CHARS - 1).trimEnd()}…`
}

/** One transient notification per execution and kind, regardless of event/RPC order. */
export function claimCronNotification(runId: string, kind: 'failure' | 'reminder'): boolean {
  if (!runId) return true // Older gateways without execution identity cannot be safely deduplicated.
  const key = JSON.stringify([runId, kind])
  if (claimedNotifications.has(key)) return false
  claimedNotifications.add(key)
  if (claimedNotifications.size > MAX_CLAIMED_NOTIFICATIONS) {
    claimedNotifications.delete(claimedNotifications.values().next().value!)
  }
  return true
}

/** A route or job selection alone does not establish that this failure is visible. */
export function isCronFailureVisible(runId: string): boolean {
  if (!runId || typeof document === 'undefined' || document.visibilityState === 'hidden' || !document.hasFocus()) return false
  for (const element of document.querySelectorAll<HTMLElement>('[data-cron-failed-run]')) {
    if (element.dataset.cronFailedRun !== runId || element.getClientRects().length === 0) continue
    const style = getComputedStyle(element)
    if (style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') continue
    const rect = element.getBoundingClientRect()
    let top = Math.max(0, rect.top)
    let bottom = Math.min(window.innerHeight, rect.bottom)
    let left = Math.max(0, rect.left)
    let right = Math.min(window.innerWidth, rect.right)
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      const parentStyle = getComputedStyle(parent)
      if (parentStyle.opacity === '0' || parentStyle.visibility === 'hidden' || parentStyle.visibility === 'collapse') {
        bottom = top
        break
      }
      const bounds = parent.getBoundingClientRect()
      if (/(auto|scroll|hidden|clip)/.test(parentStyle.overflowY)) {
        top = Math.max(top, bounds.top)
        bottom = Math.min(bottom, bounds.bottom)
      }
      if (/(auto|scroll|hidden|clip)/.test(parentStyle.overflowX)) {
        left = Math.max(left, bounds.left)
        right = Math.min(right, bounds.right)
      }
    }
    if (bottom > top && right > left) {
      const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2)
      if (hit && element.contains(hit)) return true
    }
  }
  return false
}

interface CronNotificationOptions {
  t: (key: string, params?: Record<string, string>) => string
  pushToast: (message: string, options: { tone: ToastTone; duration: number; action?: ToastAction }) => void
  openJob?: (jobId: string) => ReturnType<ToastAction['onClick']>
  openSession?: (sessionKey: string) => ReturnType<ToastAction['onClick']>
  isFailureVisible?: (runId: string) => boolean
}

export function notifyCronRunFinished(
  event: CronRunFinished,
  options: CronNotificationOptions,
): 'silent' | 'duplicate' | 'inline' | 'notified' {
  const { t, pushToast, openJob, openSession } = options
  const runId = event.runId?.trim() || ''
  const jobId = event.jobId?.trim() || ''
  const jobName = event.jobName?.trim() || t('cronSkills.jobs.unnamedTask')
  const taskAction = openJob
    ? { label: t('cronSkills.jobs.toastViewTask'), onClick: () => openJob(jobId) }
    : undefined
  if (event.success === false) {
    if (!claimCronNotification(runId, 'failure')) return 'duplicate'
    if ((options.isFailureVisible || isCronFailureVisible)(runId)) return 'inline'
    pushToast(t('cronSkills.jobs.toastBackgroundFailed', { name: jobName }), {
      tone: 'danger', duration: 9_000, action: taskAction,
    })
    return 'notified'
  }
  // A reminder with no summary is still a due reminder. Ordinary successful
  // runs remain observable in task/history state without interrupting reading.
  if (event.payloadKind !== 'reminder') return 'silent'
  if (!claimCronNotification(runId, 'reminder')) return 'duplicate'
  const reminder = reminderToastPreview(event.summary)
  const sessionKey = event.sessionKey?.trim() || ''
  pushToast(reminder
    ? t('cronSkills.jobs.toastBackgroundReminder', { name: jobName, reminder })
    : t('cronSkills.jobs.toastReminderDue', { name: jobName }), {
    tone: 'info', duration: 10_000,
    action: sessionKey && openSession
      ? { label: t('cronSkills.jobs.toastViewReminder'), onClick: () => openSession(sessionKey) }
      : taskAction,
  })
  return 'notified'
}
