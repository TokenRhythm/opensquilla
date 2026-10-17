// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { claimCronNotification, isCronFailureVisible, notifyCronRunFinished } from './notifications'

const translate = (key: string, values?: Record<string, string>) => `${key} ${JSON.stringify(values || {})}`
function options() {
  return { t: translate, pushToast: vi.fn(), openJob: vi.fn(async () => true), openSession: vi.fn(async () => true) }
}
afterEach(() => { document.body.innerHTML = ''; vi.restoreAllMocks(); vi.useRealTimers() })

describe('cron notification ownership', () => {
  it('keeps ordinary completion silent without consuming later failure/reminder identity', () => {
    const opts = options()
    const event = { jobId: 'job-a', runId: 'ordinary-1', success: true }
    expect(notifyCronRunFinished(event, opts)).toBe('silent')
    expect(opts.pushToast).not.toHaveBeenCalled()
    expect(notifyCronRunFinished({ ...event, success: false }, opts)).toBe('notified')
  })

  it('does not re-notify a repeated failure even after a long delay or toast dismissal', () => {
    vi.useFakeTimers()
    const opts = options()
    const event = { jobId: 'job-a', runId: 'delayed-1', success: false }
    expect(notifyCronRunFinished(event, opts)).toBe('notified')
    vi.advanceTimersByTime(3600000)
    expect(notifyCronRunFinished(event, opts)).toBe('duplicate')
    expect(opts.pushToast).toHaveBeenCalledOnce()
  })

  it('retains distinct runs of one job and separate notification kinds', () => {
    const opts = options()
    notifyCronRunFinished({ jobId: 'same', runId: 'distinct-1', success: false }, opts)
    notifyCronRunFinished({ jobId: 'same', runId: 'distinct-2', success: false }, opts)
    notifyCronRunFinished({ jobId: 'same', runId: 'distinct-2', payloadKind: 'reminder', success: true }, opts)
    expect(opts.pushToast).toHaveBeenCalledTimes(3)
  })

  it('does not invent execution identity for events without runId', () => {
    const opts = options()
    notifyCronRunFinished({ jobId: 'same', success: false }, opts)
    notifyCronRunFinished({ jobId: 'same', success: false }, opts)
    expect(opts.pushToast).toHaveBeenCalledTimes(2)
  })

  it('keeps the claim store bounded at 1024 identities', () => {
    expect(claimCronNotification('evicted-first', 'failure')).toBe(true)
    for (let index = 0; index < 1024; index++) claimCronNotification(`bounded-${index}`, 'failure')
    expect(claimCronNotification('bounded-0', 'failure')).toBe(false)
    expect(claimCronNotification('evicted-first', 'failure')).toBe(true)
  })

  it('only suppresses an exact visible run and still consumes its one-time notification', () => {
    const opts = { ...options(), isFailureVisible: vi.fn(runId => runId === 'visible-1') }
    expect(notifyCronRunFinished({ runId: 'visible-1', success: false }, opts)).toBe('inline')
    expect(opts.pushToast).not.toHaveBeenCalled()
    opts.isFailureVisible.mockReturnValue(false)
    expect(notifyCronRunFinished({ runId: 'visible-1', success: false }, opts)).toBe('duplicate')
    expect(notifyCronRunFinished({ runId: 'visible-2', success: false }, opts)).toBe('notified')
  })

  it('preserves empty reminders and gives them a meaningful task action', async () => {
    const opts = options()
    notifyCronRunFinished({ runId: 'empty-reminder', jobId: 'reminder-job', jobName: 'Break', payloadKind: 'reminder', summary: '  ' }, opts)
    expect(opts.pushToast).toHaveBeenCalledWith(expect.stringContaining('toastReminderDue'), expect.objectContaining({ tone: 'info' }))
    await opts.pushToast.mock.calls[0][1].action.onClick()
    expect(opts.openJob).toHaveBeenCalledWith('reminder-job')
  })

  it('uses the reminder session when available and falls back to the task list without jobId', async () => {
    const opts = options()
    notifyCronRunFinished({ runId: 'with-session', jobId: 'reminder-job', payloadKind: 'reminder', sessionKey: 'session-a', summary: 'Break' }, opts)
    await opts.pushToast.mock.calls[0][1].action.onClick()
    expect(opts.openSession).toHaveBeenCalledWith('session-a')
    notifyCronRunFinished({ runId: 'no-job', success: false }, opts)
    await opts.pushToast.mock.calls[1][1].action.onClick()
    expect(opts.openJob).toHaveBeenCalledWith('')
  })
})

describe('visible cron failure evidence', () => {
  function visibleStatus(runId: string) {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    const element = document.createElement('span')
    element.dataset.cronFailedRun = runId
    document.body.appendChild(element)
    vi.spyOn(document, 'elementFromPoint').mockReturnValue(element)
    const rect = { top: 10, left: 10, bottom: 30, right: 100, width: 90, height: 20 } as DOMRect
    vi.spyOn(element, 'getClientRects').mockReturnValue([rect] as unknown as DOMRectList)
    vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(rect)
    return element
  }
  it('requires the same rendered execution instead of the route/job identity', () => {
    expect(isCronFailureVisible('requested')).toBe(false)
    visibleStatus('other')
    expect(isCronFailureVisible('requested')).toBe(false)
    visibleStatus('requested')
    expect(isCronFailureVisible('requested')).toBe(true)
  })
  it('does not suppress errors hidden by scrolling or a hidden document', () => {
    const element = visibleStatus('offscreen')
    vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({ top: -50, bottom: -10, left: 10, right: 100 } as DOMRect)
    expect(isCronFailureVisible('offscreen')).toBe(false)
    visibleStatus('hidden-document')
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    expect(isCronFailureVisible('hidden-document')).toBe(false)
  })
  it('does not count an invisible parent or an overlay as visible failure evidence', () => {
    const element = visibleStatus('covered')
    const parent = document.createElement('div')
    document.body.appendChild(parent)
    parent.appendChild(element)
    parent.style.opacity = '0'
    expect(isCronFailureVisible('covered')).toBe(false)
    parent.style.opacity = '1'
    vi.spyOn(document, 'elementFromPoint').mockReturnValue(document.body)
    expect(isCronFailureVisible('covered')).toBe(false)
  })
})
