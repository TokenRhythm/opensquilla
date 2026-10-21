import type { InjectionKey } from 'vue'
import type {
  RegistryResult,
  Skill,
  SkillCandidate,
  SkillDiagnostic,
  SkillLifecycle,
  SkillSourceResolution,
} from '@/types/skills'

export interface SkillInstallResult {
  readonly success: boolean
  readonly cancelled?: boolean
  readonly recoveryRequired?: boolean
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

export interface SkillInstallStatus {
  readonly operationId: string
  readonly scope: string
  readonly state: 'unknown' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'recovery_required'
  readonly phase: string
  readonly terminal: boolean
  readonly progress?: Readonly<Record<string, unknown>>
  readonly result?: SkillInstallResult
}

export interface SkillCatalog {
  subscribeInvalidation?(listener: () => void): () => void
  supportsCandidates(): boolean
  listCandidates(options?: {
    readonly sessionKey?: string
    readonly signal?: AbortSignal
  }): Promise<{ readonly generation: number; readonly candidates: readonly SkillCandidate[] }>
  supportsSetEnabled(): boolean
  setEnabled(request: {
    readonly name: string
    readonly enabled: boolean
    readonly signal?: AbortSignal
  }): Promise<{
    readonly name: string
    readonly enabled: boolean
    readonly persisted: boolean
    readonly refreshed: boolean
    readonly generation?: number
    readonly message?: string
  }>
  supportsInstallStatus?(): boolean
  installStatus?(operationId: string, options?: { readonly signal?: AbortSignal }): Promise<SkillInstallStatus>
  list(options?: { readonly signal?: AbortSignal }): Promise<readonly Skill[]>
  detail(skill: Pick<Skill, 'name' | 'kind' | 'instance_id' | 'install_id' | 'active' | 'lifecycle'>, options?: {
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
}

export const SKILL_CATALOG_KEY: InjectionKey<SkillCatalog> = Symbol('SkillCatalog')
