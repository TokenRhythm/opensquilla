import type { InjectionKey } from 'vue'

export type JevMode = 'suggest' | 'trust'
export type JevDecisionStatus = 'pending' | 'accepted' | 'dismissed' | 'applied' | 'expired'

export interface JevModel {
  provider: string
  model: string
}

export interface JevEnsemble {
  tier: 'c4'
  label: string
  strategy: string
  proposers: JevModel[]
  aggregator: JevModel
  configurationFingerprint: string
}

export interface JevSessionHandoff {
  sourceSessionKey: string
  contextTokens: number
  contextWindowTokens: number
  completedTurns?: number
  targetSessionKey?: string
}

export interface JevSessionNavigation {
  decisionId: string
  sourceSessionKey: string
  targetSessionKey: string
}

export interface JevDecision {
  id: string
  sessionKey: string
  action: 'switch' | 'review' | 'keep' | 'ensemble' | 'new_session'
  status: JevDecisionStatus
  current: JevModel
  target: JevModel | null
  reason: string
  evidence: string[]
  risk: 'low' | 'medium' | 'high'
  createdAt: number
  costHint?: string
  trigger?: string
  autoSwitchAt?: number
  source?: 'input' | 'loop'
  ensemble?: JevEnsemble
  handoff?: JevSessionHandoff
}

export interface JevSnapshot {
  sessionKey: string
  enabled: boolean
  mode: JevMode
  budgetPerHour: number
  model: string
  provider: string
  available: boolean
  status: 'ready' | 'checking' | 'unavailable' | 'disabled'
  recommendation: JevDecision | null
  decisions: JevDecision[]
  loopInterval?: 3 | 5
  autoSwitchSeconds?: number
  blocked?: boolean
  draftId?: string
  ensembleAvailable?: boolean
  ensemblePlan?: JevEnsemble
  contextRestartRatio?: number
  contextRestartMinTurns?: number
}

export interface JevConfiguration {
  contextRestartMinTurns?: number
  enabled?: boolean
  mode?: JevMode
  budgetPerHour?: number
  model?: string
  provider?: string
  loopInterval?: 3 | 5
  autoSwitchSeconds?: number
}

export interface JevRouting {
  available(): boolean
  get(sessionKey: string): Promise<JevSnapshot>
  previewAvailable?(): boolean
  preview?(sessionKey: string, draftId: string, text: string, signal?: AbortSignal): Promise<JevSnapshot>
  configure(sessionKey: string, settings: JevConfiguration): Promise<JevSnapshot>
  respond(sessionKey: string, decisionId: string, accepted: boolean): Promise<JevSnapshot>
  subscribe(listener: (snapshot: JevSnapshot) => void): { close(): void }
  dispose(): void
}

export const JEV_ROUTING_KEY: InjectionKey<JevRouting> = Symbol('JevRouting')
