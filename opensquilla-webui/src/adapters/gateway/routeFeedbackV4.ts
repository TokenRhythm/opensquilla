import {
  ROUTER_FEEDBACK_SUBMIT_METHOD,
  type Params as RouteFeedbackParams,
  type Result as RouteFeedbackWireResult,
} from '@/contracts/generated/v4/routerFeedbackSubmit'
import { validateResult as validateRouteFeedbackResult } from '@/contracts/generated/v4/routerFeedbackSubmitValidators.mjs'
import type { RouteFeedback, RouteFeedbackResult } from '@/modules/routeFeedback'

interface RouteFeedbackTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
}
export function createV4RouteFeedback(transport: RouteFeedbackTransport): RouteFeedback {
  return {
    async submit(decisionId, rating) {
      const params: RouteFeedbackParams = { decisionId, rating }
      const raw = await transport.request<RouteFeedbackWireResult>(
        ROUTER_FEEDBACK_SUBMIT_METHOD,
        params,
      )
      if (!validateRouteFeedbackResult(raw)) {
        throw new Error(`${ROUTER_FEEDBACK_SUBMIT_METHOD} returned an invalid response`)
      }
      return raw as RouteFeedbackResult
    },
  }
}
