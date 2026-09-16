import {
  CHAT_CLARIFY_SUBMIT_METHOD,
  type Params as ClarificationParams,
  type Result as ClarificationWireResult,
} from '@/contracts/generated/v4/chatClarifySubmit'
import { validateResult as validateClarificationResult } from '@/contracts/generated/v4/chatClarifySubmitValidators.mjs'
import type {
  ClarificationSubmission,
  ClarificationSubmissionResult,
} from '@/modules/clarificationSubmission'

interface ClarificationSubmissionTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>
}

export function createV4ClarificationSubmission(
  transport: ClarificationSubmissionTransport,
): ClarificationSubmission {
  return {
    async submit(command) {
      const params: ClarificationParams = {
        sessionKey: command.sessionKey,
        fields: command.fields,
        ...(command.requestId !== undefined ? { requestId: command.requestId } : {}),
        ...(command.runId !== undefined ? { run_id: command.runId } : {}),
      }
      const raw = await transport.request<ClarificationWireResult>(
        CHAT_CLARIFY_SUBMIT_METHOD,
        params,
      )
      if (!validateClarificationResult(raw)) {
        throw new Error(`${CHAT_CLARIFY_SUBMIT_METHOD} returned an invalid response`)
      }
      return raw as ClarificationSubmissionResult
    },
  }
}
