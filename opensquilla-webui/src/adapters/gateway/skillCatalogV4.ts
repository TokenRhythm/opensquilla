import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import { readTransportFailure } from './transportTypes'
import {
  META_LIST_METHOD,
  type Result as MetaListResult,
} from '@/contracts/generated/v4/metaList'
import { validateResult as validateMetaListResult } from '@/contracts/generated/v4/metaListValidators.mjs'
import {
  META_INSPECT_METHOD,
  type Params as MetaInspectParams,
  type Result as MetaInspectResult,
} from '@/contracts/generated/v4/metaInspect'
import { validateResult as validateMetaInspectResult } from '@/contracts/generated/v4/metaInspectValidators.mjs'
import {
  SKILLS_LIST_METHOD,
  type Params as SkillsListParams,
  type Result as SkillsListResult,
} from '@/contracts/generated/v4/skillsList'
import { validateResult as validateSkillsListResult } from '@/contracts/generated/v4/skillsListValidators.mjs'
import {
  SKILLS_GET_METHOD,
  type Params as SkillsGetParams,
  type Result as SkillsGetResult,
} from '@/contracts/generated/v4/skillsGet'
import { validateResult as validateSkillsGetResult } from '@/contracts/generated/v4/skillsGetValidators.mjs'
import {
  SKILLS_SEARCH_METHOD,
  type Params as SkillsSearchParams,
  type Result as SkillsSearchResult,
} from '@/contracts/generated/v4/skillsSearch'
import { validateResult as validateSkillsSearchResult } from '@/contracts/generated/v4/skillsSearchValidators.mjs'
import {
  SKILLS_RELOAD_METHOD,
  type Result as SkillsReloadResult,
} from '@/contracts/generated/v4/skillsReload'
import { validateResult as validateSkillsReloadResult } from '@/contracts/generated/v4/skillsReloadValidators.mjs'
import {
  SKILLS_INSTALL_METHOD,
  type Params as SkillsInstallParams,
  type Result as SkillsInstallResult,
} from '@/contracts/generated/v4/skillsInstall'
import { validateResult as validateSkillsInstallResult } from '@/contracts/generated/v4/skillsInstallValidators.mjs'
import {
  SKILLS_INSTALL_CANCEL_METHOD,
  type Params as SkillsInstallCancelParams,
  type Result as SkillsInstallCancelResult,
} from '@/contracts/generated/v4/skillsInstallCancel'
import { validateResult as validateSkillsInstallCancelResult } from '@/contracts/generated/v4/skillsInstallCancelValidators.mjs'
import {
  SKILLS_DEPS_INSTALL_METHOD,
  type Params as SkillsDepsInstallParams,
  type Result as SkillsDepsInstallResult,
} from '@/contracts/generated/v4/skillsDepsInstall'
import { validateResult as validateSkillsDepsInstallResult } from '@/contracts/generated/v4/skillsDepsInstallValidators.mjs'
import {
  SKILLS_UNINSTALL_METHOD,
  type Params as SkillsUninstallParams,
  type Result as SkillsUninstallResult,
} from '@/contracts/generated/v4/skillsUninstall'
import { validateResult as validateSkillsUninstallResult } from '@/contracts/generated/v4/skillsUninstallValidators.mjs'
import {
  EXEC_PROPOSALS_LIST_METHOD,
  type Result as ProposalListResult,
} from '@/contracts/generated/v4/execProposalsList'
import { validateResult as validateProposalListResult } from '@/contracts/generated/v4/execProposalsListValidators.mjs'
import {
  EXEC_PROPOSALS_AUTO_ENABLED_LIST_METHOD,
  type Result as AutoEnabledListResult,
} from '@/contracts/generated/v4/execProposalsAutoEnabledList'
import { validateResult as validateAutoEnabledListResult } from '@/contracts/generated/v4/execProposalsAutoEnabledListValidators.mjs'
import {
  EXEC_PROPOSALS_SETTINGS_GET_METHOD,
  type Settings as ProposalSettingsResult,
} from '@/contracts/generated/v4/execProposalsSettingsGet'
import { validateResult as validateProposalSettingsResult } from '@/contracts/generated/v4/execProposalsSettingsGetValidators.mjs'
import {
  EXEC_PROPOSALS_SETTINGS_SET_METHOD,
  type Params as ProposalSettingsParams,
  type Result as ProposalSettingsMutationResult,
} from '@/contracts/generated/v4/execProposalsSettingsSet'
import { validateResult as validateProposalSettingsMutationResult } from '@/contracts/generated/v4/execProposalsSettingsSetValidators.mjs'
import {
  EXEC_PROPOSALS_SHOW_METHOD,
  type Params as ProposalShowParams,
  type Result as ProposalShowResult,
} from '@/contracts/generated/v4/execProposalsShow'
import { validateResult as validateProposalShowResult } from '@/contracts/generated/v4/execProposalsShowValidators.mjs'
import {
  EXEC_PROPOSALS_ACCEPT_METHOD,
  type Params as ProposalAcceptParams,
  type Result as ProposalAcceptResult,
} from '@/contracts/generated/v4/execProposalsAccept'
import { validateResult as validateProposalAcceptResult } from '@/contracts/generated/v4/execProposalsAcceptValidators.mjs'
import {
  EXEC_PROPOSALS_REJECT_METHOD,
  type Params as ProposalRejectParams,
  type Result as ProposalRejectResult,
} from '@/contracts/generated/v4/execProposalsReject'
import { validateResult as validateProposalRejectResult } from '@/contracts/generated/v4/execProposalsRejectValidators.mjs'
import {
  EXEC_PROPOSALS_AUTO_ENABLED_DISABLE_METHOD,
  type Params as AutoEnabledDisableParams,
  type Result as AutoEnabledDisableResult,
} from '@/contracts/generated/v4/execProposalsAutoEnabledDisable'
import { validateResult as validateAutoEnabledDisableResult } from '@/contracts/generated/v4/execProposalsAutoEnabledDisableValidators.mjs'
import type {
  SkillCatalog,
  SkillInstallResult,
  SkillProposalAction,
  SkillProposalDetail,
  SkillReloadResult,
  SkillRegistrySearchResult,
} from '@/modules/skillCatalog'
import type {
  AutoEnabledSkill,
  Proposal,
  ProposalsSettings,
  RegistryResult,
  Skill,
  SkillDiagnostic,
} from '@/types/skills'

interface RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  ready(options?: { signal?: AbortSignal }): Promise<void>
  supports(method: string): boolean
  markUnsupported(method: string): void
}

const callOptions = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 30_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

const objects = <T>(value: unknown): T[] => (
  Array.isArray(value)
    ? value.filter(item => item !== null && typeof item === 'object' && !Array.isArray(item)) as T[]
    : []
)

function invalid(method: string): Error {
  return new Error(`${method} returned an invalid response`)
}

function isMeta(skill: Pick<Skill, 'kind'>): boolean {
  return skill.kind === 'meta' || skill.kind === 'meta_sop'
}

function isLifecycleOnly(skill: Pick<Skill, 'active' | 'lifecycle'>): boolean {
  return skill.active === false || Boolean(
    skill.lifecycle && skill.lifecycle.selection_state !== 'active',
  )
}

function matchesIdentity(left: Skill, right: Skill): boolean {
  if (left.name !== right.name || isMeta(left) !== isMeta(right)) return false
  if (left.instance_id && right.instance_id) return left.instance_id === right.instance_id
  if (left.install_id && right.install_id) return left.install_id === right.install_id
  if (left.instance_id || right.instance_id || left.install_id || right.install_id) return false
  return Boolean(left.kind === right.kind && left.layer && right.layer && left.layer === right.layer)
}

type MetaCatalogEntry = MetaListResult['skills'][number]

function projectMeta(entry: MetaCatalogEntry, base?: Skill): Skill {
  const missingBins = entry.missing_bins || []
  const missingEnv = entry.missing_env || []
  const missingEnvAny = entry.missing_env_any || []
  const summary = base?.dependency_summary
  // Project only public UI fields. In particular, the wire invocation mode is
  // a string whereas Skill.invocation is the existing capability object.
  return {
    ...base,
    name: entry.name,
    kind: base?.kind ?? 'meta',
    description: entry.description ?? base?.description,
    layer: base?.layer || entry.layer,
    instance_id: base?.instance_id || entry.instance_id,
    install_id: base?.install_id || entry.install_id,
    source: base?.source ?? entry.source,
    generation: entry.generation ?? base?.generation,
    digest: entry.digest ?? base?.digest,
    visibility: entry.visibility ?? base?.visibility,
    eligible: entry.ready,
    status: entry.status,
    status_detail: entry.ready ? '' : (entry.reasons || []).join('; '),
    lifecycle: base?.lifecycle && !isLifecycleOnly(base)
      ? { ...base.lifecycle, readiness_state: entry.status }
      : base?.lifecycle,
    missing_bins: missingBins,
    missing_env: missingEnv,
    missing_env_any: missingEnvAny,
    dependency_summary: summary ? {
      ...summary,
      missing: {
        binaries: { all: missingBins, any: [] },
        api_env: { all: missingEnv, any: missingEnvAny },
        count: missingBins.length + missingEnv.length + missingEnvAny.length,
      },
      sub_skill_dependencies: {
        skills: [],
        missing_count: (entry.missing_skills || []).length,
        inferred_count: 0,
        missing_references: entry.missing_skills || [],
      },
    } : undefined,
    dependency_count: entry.dependency_count ?? base?.dependency_count,
  }
}

function combineCatalog(skills: Skill[], metas: MetaListResult): Skill[] {
  if (metas.disabled) return skills.filter(skill => !isMeta(skill) || isLifecycleOnly(skill))
  const combined = [...skills]
  for (const entry of metas.skills) {
    const meta = projectMeta(entry)
    const matches = combined.flatMap((skill, index) => matchesIdentity(skill, meta) ? [index] : [])
    if (matches.length === 1) {
      const index = matches[0]!
      combined[index] = projectMeta(entry, combined[index])
    } else {
      combined.push(meta)
    }
  }
  return combined
}

export function createV4SkillCatalog(rpc: RpcTransport): SkillCatalog {
  async function compatibleMetaRead<T>(method: string, read: () => Promise<T>): Promise<T | null> {
    if (!rpc.supports(method)) return null
    try {
      return await read()
    } catch (error) {
      if (readTransportFailure(error).code !== 'METHOD_NOT_FOUND') throw error
      rpc.markUnsupported(method)
      return null
    }
  }

  return {
    async list(options) {
      await rpc.ready({ signal: options?.signal })
      const params: SkillsListParams = { includeLifecycle: true }
      const [result, metas] = await Promise.all([
        rpc.request<SkillsListResult>(SKILLS_LIST_METHOD, { ...params }, callOptions(options?.signal)),
        // meta.list predates the split catalog and lacks exact identity on old
        // Gateways. Only the inspect capability identifies the new projection.
        rpc.supports(META_INSPECT_METHOD)
          ? compatibleMetaRead(META_LIST_METHOD, async () => {
            const response = await rpc.request<MetaListResult>(
              META_LIST_METHOD, undefined, callOptions(options?.signal),
            )
            if (!validateMetaListResult(response)) throw invalid(META_LIST_METHOD)
            return response
          })
          : null,
      ])
      if (!validateSkillsListResult(result)) throw invalid(SKILLS_LIST_METHOD)
      const skills = result.skills as unknown as Skill[]
      return metas ? combineCatalog(skills, metas) : skills
    },
    async detail(skill, options) {
      await rpc.ready({ signal: options?.signal })
      if (isMeta(skill) && !isLifecycleOnly(skill)) {
        const params: MetaInspectParams = {
          name: skill.name,
          ...(skill.instance_id ? { instanceId: skill.instance_id } : {}),
          ...(skill.install_id ? { installId: skill.install_id } : {}),
        }
        const detail = await compatibleMetaRead(META_INSPECT_METHOD, async () => {
          const response = await rpc.request<MetaInspectResult>(
            META_INSPECT_METHOD, { ...params }, callOptions(options?.signal),
          )
          if (!validateMetaInspectResult(response)) throw invalid(META_INSPECT_METHOD)
          return response
        })
        if (detail) {
          if ('disabled' in detail && detail.disabled) throw new Error('MetaSkill catalog is disabled.')
          if (!('name' in detail)) throw invalid(META_INSPECT_METHOD)
          if (detail.name !== skill.name
            || (skill.instance_id && detail.instance_id !== skill.instance_id)
            || (skill.install_id && detail.install_id !== skill.install_id)) {
            throw new Error('MetaSkill identity changed; refresh the catalog.')
          }
          return {
            ...projectMeta(detail, skill),
            dependencies: detail.dependencies,
            sub_skills: (detail.dependencies || []).map(item => item.name),
          }
        }
      }
      const params: SkillsGetParams = {
        name: skill.name,
        includeLifecycle: true,
        ...(skill.instance_id ? { instanceId: skill.instance_id } : {}),
        ...(skill.install_id ? { installId: skill.install_id } : {}),
      }
      const result = await rpc.request<SkillsGetResult>(
        SKILLS_GET_METHOD,
        { ...params },
        callOptions(options?.signal),
      )
      if (!validateSkillsGetResult(result)) throw invalid(SKILLS_GET_METHOD)
      return result as unknown as Skill
    },
    async search(query, options) {
      const params: SkillsSearchParams = {
        query,
        limit: options?.limit ?? 20,
        source: options?.source || 'clawhub',
      }
      const result = await rpc.request<SkillsSearchResult>(
        SKILLS_SEARCH_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateSkillsSearchResult(result)) throw invalid(SKILLS_SEARCH_METHOD)
      return {
        results: result.results as unknown as RegistryResult[],
        diagnostics: objects<SkillDiagnostic>(result.diagnostics),
        message: typeof result.message === 'string' ? result.message : '',
      } satisfies SkillRegistrySearchResult
    },
    async reload(options) {
      const result = await rpc.request<SkillsReloadResult>(
        SKILLS_RELOAD_METHOD,
        undefined,
        callOptions(options?.signal),
      )
      if (!validateSkillsReloadResult(result)) throw invalid(SKILLS_RELOAD_METHOD)
      return result as unknown as SkillReloadResult
    },
    async install(request) {
      const params: SkillsInstallParams = {
        identifier: request.identifier,
        source: request.source,
        ...(request.operationId ? { operationId: request.operationId } : {}),
        ...(request.riskConfirmation
          ? { force: true, riskConfirmation: request.riskConfirmation }
          : {}),
      }
      const result = await rpc.request<SkillsInstallResult>(
        SKILLS_INSTALL_METHOD,
        params,
        callOptions(request.signal),
      )
      if (!validateSkillsInstallResult(result)) throw invalid(SKILLS_INSTALL_METHOD)
      return result as unknown as SkillInstallResult
    },
    supportsInstallCancellation() {
      return rpc.supports(SKILLS_INSTALL_CANCEL_METHOD)
    },
    async cancelInstall(operationId, options) {
      const params: SkillsInstallCancelParams = { operationId }
      const result = await rpc.request<SkillsInstallCancelResult>(
        SKILLS_INSTALL_CANCEL_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateSkillsInstallCancelResult(result)) {
        throw invalid(SKILLS_INSTALL_CANCEL_METHOD)
      }
      return result as unknown as SkillInstallResult
    },
    async installDependencies(request) {
      const params: SkillsDepsInstallParams = {
        name: request.name,
        install_id: request.dependencyId,
        ...(request.skillInstallId ? { installId: request.skillInstallId } : {}),
        ...(request.instanceId ? { instanceId: request.instanceId } : {}),
      }
      const result = await rpc.request<SkillsDepsInstallResult>(
        SKILLS_DEPS_INSTALL_METHOD,
        params,
        callOptions(request.signal),
      )
      if (!validateSkillsDepsInstallResult(result)) throw invalid(SKILLS_DEPS_INSTALL_METHOD)
      return result as unknown as SkillInstallResult
    },
    async uninstall(request) {
      // Missing identifiers still reach the Gateway's existing error path.
      const params: Partial<SkillsUninstallParams> = {
        ...(request.name ? { name: request.name } : {}),
        ...(request.installId ? { installId: request.installId } : {}),
      }
      const result = await rpc.request<SkillsUninstallResult>(
        SKILLS_UNINSTALL_METHOD,
        params,
        callOptions(request.signal),
      )
      if (!validateSkillsUninstallResult(result)) throw invalid(SKILLS_UNINSTALL_METHOD)
      return result as unknown as SkillInstallResult
    },
    async proposals(options) {
      const [listed, enabled, settings] = await Promise.allSettled([
        rpc.request<ProposalListResult>(EXEC_PROPOSALS_LIST_METHOD, undefined, callOptions(options?.signal)),
        rpc.request<AutoEnabledListResult>(EXEC_PROPOSALS_AUTO_ENABLED_LIST_METHOD, undefined, callOptions(options?.signal)),
        rpc.request<ProposalSettingsResult>(EXEC_PROPOSALS_SETTINGS_GET_METHOD, undefined, callOptions(options?.signal)),
      ])
      const listedResult = listed.status === 'fulfilled' && validateProposalListResult(listed.value)
        ? listed.value
        : null
      const enabledResult = enabled.status === 'fulfilled' && validateAutoEnabledListResult(enabled.value)
        ? enabled.value
        : null
      const settingsResult = settings.status === 'fulfilled' && validateProposalSettingsResult(settings.value)
        ? settings.value
        : null
      return {
        proposals: (listedResult?.proposals || []) as unknown as Proposal[],
        autoEnabledSkills: (enabledResult?.skills || []) as unknown as AutoEnabledSkill[],
        settings: settingsResult as unknown as ProposalsSettings | null,
      }
    },
    async updateProposalSettings(changes, options) {
      const params: ProposalSettingsParams = { ...changes }
      const result = await rpc.request<ProposalSettingsMutationResult>(
        EXEC_PROPOSALS_SETTINGS_SET_METHOD,
        { ...params },
        callOptions(options?.signal),
      )
      if (!validateProposalSettingsMutationResult(result)) throw invalid(EXEC_PROPOSALS_SETTINGS_SET_METHOD)
      return result as unknown as SkillProposalAction
    },
    async proposal(proposalId, options) {
      const params: ProposalShowParams = { proposal_id: proposalId }
      const result = await rpc.request<ProposalShowResult>(
        EXEC_PROPOSALS_SHOW_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateProposalShowResult(result)) throw invalid(EXEC_PROPOSALS_SHOW_METHOD)
      return result as unknown as SkillProposalDetail
    },
    async acceptProposal(proposalId, options) {
      const params: ProposalAcceptParams = {
        proposal_id: proposalId,
        ...(options?.force ? { force: true } : {}),
      }
      const result = await rpc.request<ProposalAcceptResult>(
        EXEC_PROPOSALS_ACCEPT_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateProposalAcceptResult(result)) throw invalid(EXEC_PROPOSALS_ACCEPT_METHOD)
      return result as unknown as SkillProposalAction
    },
    async rejectProposal(proposalId, options) {
      const params: ProposalRejectParams = { proposal_id: proposalId }
      const result = await rpc.request<ProposalRejectResult>(
        EXEC_PROPOSALS_REJECT_METHOD,
        params,
        callOptions(options?.signal),
      )
      if (!validateProposalRejectResult(result)) throw invalid(EXEC_PROPOSALS_REJECT_METHOD)
      return result as unknown as SkillProposalAction
    },
    async disableAutoEnabledSkill(name, options) {
      const params: AutoEnabledDisableParams = { name }
      const result = await rpc.request<AutoEnabledDisableResult>(
        EXEC_PROPOSALS_AUTO_ENABLED_DISABLE_METHOD,
        { ...params },
        callOptions(options?.signal),
      )
      if (!validateAutoEnabledDisableResult(result)) throw invalid(EXEC_PROPOSALS_AUTO_ENABLED_DISABLE_METHOD)
      return result as unknown as SkillProposalAction
    },
  }
}
