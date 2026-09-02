import type { RpcCallOptions } from '@/lib/rpc'
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
}

const callOptions = (signal?: AbortSignal): RpcCallOptions => ({
  timeoutMs: 30_000,
  timeoutAction: 'reject',
  abortAction: 'reject',
  ...(signal ? { signal } : {}),
})

const record = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
)

const objects = <T>(value: unknown): T[] => (
  Array.isArray(value)
    ? value.filter(item => item !== null && typeof item === 'object' && !Array.isArray(item)) as T[]
    : []
)

export function createV4SkillCatalog(rpc: RpcTransport): SkillCatalog {
  return {
    async list(options) {
      await rpc.ready({ signal: options?.signal })
      const result = record(await rpc.request(
        'skills.list',
        { includeLifecycle: true },
        callOptions(options?.signal),
      ))
      return objects<Skill>(result.skills)
    },
    async detail(skill, options) {
      const result = record(await rpc.request('skills.get', {
        name: skill.name,
        includeLifecycle: true,
        ...(skill.instance_id ? { instanceId: skill.instance_id } : {}),
        ...(skill.install_id ? { installId: skill.install_id } : {}),
      }, callOptions(options?.signal)))
      return result as unknown as Skill
    },
    async search(query, options) {
      const result = record(await rpc.request('skills.search', {
        query,
        limit: options?.limit ?? 20,
        source: options?.source || 'clawhub',
      }, callOptions(options?.signal)))
      return {
        results: objects<RegistryResult>(result.results),
        diagnostics: objects<SkillDiagnostic>(result.diagnostics),
        message: typeof result.message === 'string' ? result.message : '',
      } satisfies SkillRegistrySearchResult
    },
    async reload(options) {
      return record(await rpc.request(
        'skills.reload',
        undefined,
        callOptions(options?.signal),
      )) as unknown as SkillReloadResult
    },
    async install(request) {
      return record(await rpc.request('skills.install', {
        identifier: request.identifier,
        source: request.source,
        ...(request.operationId ? { operationId: request.operationId } : {}),
        ...(request.riskConfirmation
          ? { force: true, riskConfirmation: request.riskConfirmation }
          : {}),
      }, callOptions(request.signal))) as unknown as SkillInstallResult
    },
    supportsInstallCancellation() {
      return rpc.supports('skills.install.cancel')
    },
    async cancelInstall(operationId, options) {
      return record(await rpc.request(
        'skills.install.cancel',
        { operationId },
        callOptions(options?.signal),
      )) as unknown as SkillInstallResult
    },
    async installDependencies(request) {
      return record(await rpc.request('skills.deps.install', {
        name: request.name,
        install_id: request.dependencyId,
        ...(request.skillInstallId ? { installId: request.skillInstallId } : {}),
        ...(request.instanceId ? { instanceId: request.instanceId } : {}),
      }, callOptions(request.signal))) as unknown as SkillInstallResult
    },
    async uninstall(request) {
      return record(await rpc.request('skills.uninstall', {
        ...(request.name ? { name: request.name } : {}),
        ...(request.installId ? { installId: request.installId } : {}),
      }, callOptions(request.signal))) as unknown as SkillInstallResult
    },
    async proposals(options) {
      const [listed, enabled, settings] = await Promise.allSettled([
        rpc.request('exec.proposals.list', undefined, callOptions(options?.signal)),
        rpc.request('exec.proposals.auto_enabled.list', undefined, callOptions(options?.signal)),
        rpc.request('exec.proposals.settings.get', undefined, callOptions(options?.signal)),
      ])
      const listedRecord = listed.status === 'fulfilled' ? record(listed.value) : {}
      const enabledRecord = enabled.status === 'fulfilled' ? record(enabled.value) : {}
      const settingsRecord = settings.status === 'fulfilled' ? record(settings.value) : {}
      return {
        proposals: objects<Proposal>(listedRecord.proposals),
        autoEnabledSkills: objects<AutoEnabledSkill>(enabledRecord.skills),
        settings: settingsRecord.settings && typeof settingsRecord.settings === 'object'
          ? settingsRecord.settings as ProposalsSettings
          : null,
      }
    },
    async updateProposalSettings(changes, options) {
      return record(await rpc.request(
        'exec.proposals.settings.set',
        { ...changes },
        callOptions(options?.signal),
      )) as SkillProposalAction
    },
    async proposal(proposalId, options) {
      return record(await rpc.request(
        'exec.proposals.show',
        { proposal_id: proposalId },
        callOptions(options?.signal),
      )) as SkillProposalDetail
    },
    async acceptProposal(proposalId, options) {
      return record(await rpc.request('exec.proposals.accept', {
        proposal_id: proposalId,
        ...(options?.force ? { force: true } : {}),
      }, callOptions(options?.signal))) as SkillProposalAction
    },
    async rejectProposal(proposalId, options) {
      return record(await rpc.request(
        'exec.proposals.reject',
        { proposal_id: proposalId },
        callOptions(options?.signal),
      )) as SkillProposalAction
    },
    async disableAutoEnabledSkill(name, options) {
      return record(await rpc.request(
        'exec.proposals.auto_enabled.disable',
        { name },
        callOptions(options?.signal),
      )) as SkillProposalAction
    },
  }
}
