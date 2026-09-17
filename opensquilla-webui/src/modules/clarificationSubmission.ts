import type { InjectionKey } from 'vue'

export interface SubmitClarification {
  readonly sessionKey: string
  readonly fields: Readonly<Record<string, string | boolean>>
  readonly requestId?: string
  readonly runId?: string
}

export interface ClarificationSubmissionResult {
  readonly accepted?: boolean
  readonly resolved?: boolean
  readonly replayed?: boolean
  readonly requestId?: string
  readonly sessionKey?: string
  readonly ok?: boolean
}

export interface ClarificationSubmission {
  submit(command: SubmitClarification): Promise<ClarificationSubmissionResult>
}

export const CLARIFICATION_SUBMISSION_KEY: InjectionKey<ClarificationSubmission> =
  Symbol('ClarificationSubmission')
