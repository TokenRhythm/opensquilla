import type { InjectionKey } from 'vue'
import type { RouteFeedbackResult } from '@/contracts/publicData'

export type { RouteFeedbackResult } from '@/contracts/publicData'

export type RouteFeedbackRating = 'up' | 'down' | 'neutral'

export interface RouteFeedback {
  submit(decisionId: string, rating: RouteFeedbackRating): Promise<RouteFeedbackResult>
}

export const ROUTE_FEEDBACK_KEY: InjectionKey<RouteFeedback> = Symbol('RouteFeedback')
