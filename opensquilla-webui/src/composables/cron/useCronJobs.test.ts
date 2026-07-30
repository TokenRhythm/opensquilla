import { describe, expect, it } from 'vitest'
import { isConnectionRecycleError, isJobFailed } from './useCronJobs'
import { markCronFinishNotified, wasCronFinishNotified } from '@/utils/cron/notifications'

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

  it('suppresses duplicate completion toasts for the same recent run', () => {
    markCronFinishNotified('job-1', 1_000)
    expect(wasCronFinishNotified('job-1', 10_000)).toBe(true)
    expect(wasCronFinishNotified('job-1', 20_000)).toBe(false)
    expect(wasCronFinishNotified('job-2', 10_000)).toBe(false)
  })
})
