import { describe, expect, it } from 'vitest'
import { isConnectionRecycleError, isJobFailed } from './useCronJobs'
import { claimCronNotification, reminderToastPreview } from '@/utils/cron/notifications'

describe('cron job recovery helpers', () => {
  it('recognizes transient recycled and closed connection failures', () => {
    expect(isConnectionRecycleError('Connection recycled after workspaces.list terminated')).toBe(true)
    expect(isConnectionRecycleError('Connection closed')).toBe(true)
    expect(isConnectionRecycleError('Cannot call cron.run: not connected')).toBe(true)
    expect(isConnectionRecycleError('workspaceDir does not exist')).toBe(false)
  })

  it('recognizes failed jobs for the retry action', () => {
    expect(isJobFailed({ id: 'a', lastStatus: 'error', lastResult: 'boom', error_count: 1 })).toBe(true)
    expect(isJobFailed({ id: 'b', lastStatus: 'fail', lastResult: 'boom', error_count: 1 })).toBe(true)
    expect(isJobFailed({ id: 'c', lastStatus: 'ok', lastResult: null, error_count: 0 })).toBe(false)
  })

  it('claims one notification per run and kind rather than a time window', () => {
    expect(claimCronNotification('job-1', 'failure')).toBe(true)
    expect(claimCronNotification('job-1', 'failure')).toBe(false)
    expect(claimCronNotification('job-1', 'reminder')).toBe(true)
    expect(claimCronNotification('job-2', 'failure')).toBe(true)
  })

  it('normalizes and truncates reminder previews for transient toasts', () => {
    expect(reminderToastPreview('  drink\n\nwater  ')).toBe('drink water')
    const preview = reminderToastPreview('a'.repeat(140))
    expect(preview).toHaveLength(120)
    expect(preview.endsWith('…')).toBe(true)
  })
})
