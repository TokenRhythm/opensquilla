import type { InjectionKey } from 'vue'
import type {
  AutoEnabledSkill,
  Proposal,
  ProposalsSettings,
  RegistryResult,
  Skill,
  SkillDiagnostic,
  SkillLifecycle,
  SkillSourceResolution,
} from '@/types/skills'

export interface SkillInstallResult {
  readonly success: boolean
  readonly cancelled?: boolean
  readonly unchanged?: boolean
  readonly name?: string
  readonly message?: string
  readonly installed?: boolean
  readonly active?: boolean
  readonly instruction_usable?: boolean
  readonly installId?: string
  readonly lifecycle?: SkillLifecycle
  readonly resolution?: SkillSourceResolution
  readonly diagnostics?: readonly SkillDiagnostic[]
  readonly rollbackPerformed?: boolean
  readonly catalogGeneration?: number
  readonly effectiveFrom?: 'next_turn' | 'next_start' | string
  readonly missing_still?: {
    readonly bins?: readonly string[]
    readonly env?: readonly string[]
    readonly env_any?: readonly (readonly string[])[]
  }
}

export interface SkillRegistrySearchResult {
  readonly results: readonly RegistryResult[]
  readonly diagnostics: readonly SkillDiagnostic[]
  readonly message: string
}

export interface SkillReloadError {
  readonly path?: string
  readonly message?: string
  readonly kept_previous?: boolean
}

export interface SkillReloadResult {
  readonly success: boolean
  readonly changed: boolean
  readonly partial: boolean
  readonly generation: number
  readonly added?: readonly string[]
  readonly removed?: readonly string[]
  readonly modified?: readonly string[]
  readonly errors?: readonly SkillReloadError[]
}

export interface SkillProposalSnapshot {
  readonly proposals: readonly Proposal[]
  readonly autoEnabledSkills: readonly AutoEnabledSkill[]
  readonly settings: ProposalsSettings | null
}

export interface SkillProposalDetail extends Partial<Proposal> {
  readonly status?: string
  readonly reason?: string
}

export interface SkillProposalAction {
  readonly status?: string
  readonly reason?: string
  readonly settings?: ProposalsSettings
}

export interface SkillCatalog {
  list(options?: { readonly signal?: AbortSignal }): Promise<readonly Skill[]>
  detail(skill: Pick<Skill, 'name' | 'instance_id' | 'install_id'>, options?: {
    readonly signal?: AbortSignal
  }): Promise<Skill>
  search(query: string, options?: {
    readonly limit?: number
    readonly source?: string
    readonly signal?: AbortSignal
  }): Promise<SkillRegistrySearchResult>
  reload(options?: { readonly signal?: AbortSignal }): Promise<SkillReloadResult>
  install(request: {
    readonly identifier: string
    readonly source: string
    readonly operationId?: string
    readonly riskConfirmation?: string
    readonly signal?: AbortSignal
  }): Promise<SkillInstallResult>
  supportsInstallCancellation(): boolean
  cancelInstall(operationId: string, options?: { readonly signal?: AbortSignal }): Promise<SkillInstallResult>
  installDependencies(request: {
    readonly name: string
    readonly dependencyId: string
    readonly skillInstallId?: string
    readonly instanceId?: string
    readonly signal?: AbortSignal
  }): Promise<SkillInstallResult>
  uninstall(request: {
    readonly name?: string
    readonly installId?: string
    readonly signal?: AbortSignal
  }): Promise<SkillInstallResult>
  proposals(options?: { readonly signal?: AbortSignal }): Promise<SkillProposalSnapshot>
  updateProposalSettings(changes: Readonly<Record<string, boolean | string>>, options?: {
    readonly signal?: AbortSignal
  }): Promise<SkillProposalAction>
  proposal(proposalId: string, options?: { readonly signal?: AbortSignal }): Promise<SkillProposalDetail>
  acceptProposal(proposalId: string, options?: {
    readonly force?: boolean
    readonly signal?: AbortSignal
  }): Promise<SkillProposalAction>
  rejectProposal(proposalId: string, options?: { readonly signal?: AbortSignal }): Promise<SkillProposalAction>
  disableAutoEnabledSkill(name: string, options?: { readonly signal?: AbortSignal }): Promise<SkillProposalAction>
}

export const SKILL_CATALOG_KEY: InjectionKey<SkillCatalog> = Symbol('SkillCatalog')
