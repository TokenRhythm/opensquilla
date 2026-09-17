import { beforeEach, describe, expect, it, vi } from 'vitest'
import { routeFeedbackTestDouble } from '@/testing/conversationAncillary.test-helper'

const submitRouteFeedback = vi.fn()
const routeFeedback = routeFeedbackTestDouble({ submit: submitRouteFeedback })

const pushToast = vi.fn()
vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

vi.mock('@/i18n', () => ({
  default: { global: { t: (key: string) => key } },
}))

import { useChatRouteFeedback } from './useChatRouteFeedback'

describe('useChatRouteFeedback', () => {
  beforeEach(() => {
    submitRouteFeedback.mockReset()
    pushToast.mockReset()
  })

  it('submits a rating and records optimistic state', async () => {
    submitRouteFeedback.mockResolvedValue({ accepted: true, recorded: 'up' })
    const fb = useChatRouteFeedback(routeFeedback)

    await fb.submit('dec-1', 'up')

    expect(submitRouteFeedback).toHaveBeenCalledWith('dec-1', 'up')
    expect(fb.ratingFor('dec-1')).toBe('up')
    expect(pushToast).not.toHaveBeenCalled()
  })

  it('clicking the active thumb again revokes with neutral', async () => {
    submitRouteFeedback.mockResolvedValue({ accepted: true })
    const fb = useChatRouteFeedback(routeFeedback)

    await fb.submit('dec-2', 'down')
    expect(fb.ratingFor('dec-2')).toBe('down')

    await fb.submit('dec-2', 'down')
    expect(submitRouteFeedback).toHaveBeenLastCalledWith('dec-2', 'neutral')
    expect(fb.ratingFor('dec-2')).toBeUndefined()
  })

  it('clicking the other thumb revises the rating', async () => {
    submitRouteFeedback.mockResolvedValue({ accepted: true })
    const fb = useChatRouteFeedback(routeFeedback)

    await fb.submit('dec-3', 'down')
    await fb.submit('dec-3', 'up')

    expect(submitRouteFeedback).toHaveBeenLastCalledWith('dec-3', 'up')
    expect(fb.ratingFor('dec-3')).toBe('up')
  })

  it('rolls back and toasts when the decision expired', async () => {
    submitRouteFeedback.mockResolvedValue({ accepted: false, reason: 'decision_not_found' })
    const fb = useChatRouteFeedback(routeFeedback)

    await fb.submit('dec-4', 'up')

    expect(fb.ratingFor('dec-4')).toBeUndefined()
    expect(pushToast).toHaveBeenCalledWith('chat.routeFeedback.expired', { tone: 'danger' })
  })

  it('rolls back to the previous rating on a transport error', async () => {
    submitRouteFeedback.mockResolvedValueOnce({ accepted: true })
    const fb = useChatRouteFeedback(routeFeedback)
    await fb.submit('dec-5', 'up')

    submitRouteFeedback.mockRejectedValueOnce(new Error('boom'))
    await fb.submit('dec-5', 'down')

    expect(fb.ratingFor('dec-5')).toBe('up')
    expect(pushToast).toHaveBeenCalledWith('chat.routeFeedback.failed', { tone: 'danger' })
  })
})
