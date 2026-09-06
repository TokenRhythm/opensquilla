import { expect, expectTypeOf, it, vi } from 'vitest'

import type { Result as RouteFeedbackWireResult } from '@/contracts/generated/v4/routerFeedbackSubmit'
import type { RouteFeedbackResult } from '@/modules/routeFeedback'
import { createV4RouteFeedback } from './routeFeedbackV4'

it('exposes only the reviewed data fields with their exact nullability', () => {
  expectTypeOf<RouteFeedbackResult>().toEqualTypeOf<Readonly<
    Pick<RouteFeedbackWireResult, 'accepted' | 'reason' | 'recorded'>
  >>()
  expectTypeOf<keyof RouteFeedbackResult>().toEqualTypeOf<'accepted' | 'reason' | 'recorded'>()
})

it('keeps the validated response and its nullable fields unchanged', async () => {
  const raw = { accepted: false, reason: null, recorded: null, futureField: true }
  const request = vi.fn().mockResolvedValue(raw)
  const feedback = createV4RouteFeedback({ request })

  expect(await feedback.submit('synthetic-decision', 'neutral')).toBe(raw)
  expect(request).toHaveBeenCalledExactlyOnceWith('router.feedback.submit', {
    decisionId: 'synthetic-decision', rating: 'neutral',
  })
})
