import type { Result as RouteFeedbackWireResult } from './generated/v4/routerFeedbackSubmit'

export type RouteFeedbackResult = Readonly<
  Pick<RouteFeedbackWireResult, 'accepted' | 'reason' | 'recorded'>
>
