import type { InjectionKey } from 'vue'

export type RouteFeedbackRating = 'up' | 'down' | 'neutral'

export interface RouteFeedbackResult {
  readonly accepted: boolean
  readonly reason?: string | null
  readonly recorded?: string | null
}

export interface RouteFeedback {
  submit(decisionId: string, rating: RouteFeedbackRating): Promise<RouteFeedbackResult>
}

export const ROUTE_FEEDBACK_KEY: InjectionKey<RouteFeedback> = Symbol('RouteFeedback')
