import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import {
  SKILLS_CANDIDATES_METHOD,
  type Result as SkillsCandidatesResult,
} from '@/contracts/generated/v4/skillsCandidates'
import { validateResult as validateSkillsCandidatesResult } from '@/contracts/generated/v4/skillsCandidatesValidators.mjs'
import {
  SKILLS_SET_ENABLED_METHOD,
  type Result as SkillsSetEnabledResult,
} from '@/contracts/generated/v4/skillsSetEnabled'
import { validateResult as validateSkillsSetEnabledResult } from '@/contracts/generated/v4/skillsSetEnabledValidators.mjs'
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
import { SKILLS_INSTALL_STATUS_METHOD, type Result as SkillsInstallStatusResult } from '@/contracts/generated/v4/skillsInstallStatus'
import { validateResult as validateSkillsInstallStatusResult } from '@/contracts/generated/v4/skillsInstallStatusValidators.mjs'
import type { SkillInstallStatus } from '@/modules/skillCatalog'

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
import type {
  SkillCatalog,
  SkillInstallResult,
  SkillReloadResult,
  SkillRegistrySearchResult,
} from '@/modules/skillCatalog'
import type {
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

export function createV4SkillCatalog(rpc: RpcTransport): SkillCatalog {
  const invalidationListeners = new Set<() => void>()
  function invalidate() {
    for (const listener of invalidationListeners) {
      // A view callback must not turn a committed mutation into a failed RPC.
      try { listener() } catch { /* The next palette open also refreshes. */ }
    }
  }

  return {
    subscribeInvalidation(listener) {
      invalidationListeners.add(listener)
      return () => { invalidationListeners.delete(listener) }
    },
    supportsCandidates() {
      return rpc.supports(SKILLS_CANDIDATES_METHOD)
    },
    async listCandidates(options) {
      await rpc.ready({ signal: options?.signal })
      if (!rpc.supports(SKILLS_CANDIDATES_METHOD)) {
        throw new Error('Explicit skill selection requires an updated Gateway.')
      }
      const result = await rpc.request<SkillsCandidatesResult>(
        SKILLS_CANDIDATES_METHOD,
        options?.sessionKey ? { sessionKey: options.sessionKey } : {},
        callOptions(options?.signal),
      )
      if (!validateSkillsCandidatesResult(result)) throw invalid(SKILLS_CANDIDATES_METHOD)
      return result
    },
    supportsSetEnabled() {
      return rpc.supports(SKILLS_SET_ENABLED_METHOD)
    },
    async setEnabled(request) {
      await rpc.ready({ signal: request.signal })
      if (!rpc.supports(SKILLS_SET_ENABLED_METHOD)) {
        throw new Error('Changing skill availability requires an updated Gateway.')
      }
      const result = await rpc.request<SkillsSetEnabledResult>(
        SKILLS_SET_ENABLED_METHOD,
        { name: request.name, enabled: request.enabled },
        callOptions(request.signal),
      )
      if (!validateSkillsSetEnabledResult(result)) throw invalid(SKILLS_SET_ENABLED_METHOD)
      if (result.persisted) invalidate()
      return result
    },
    async list(options) {
      await rpc.ready({ signal: options?.signal })
      const params: SkillsListParams = { includeLifecycle: true }
      const result = await rpc.request<SkillsListResult>(
        SKILLS_LIST_METHOD, { ...params }, callOptions(options?.signal),
      )
      if (!validateSkillsListResult(result)) throw invalid(SKILLS_LIST_METHOD)
      const skills = result.skills as unknown as Skill[]
      return skills
    },
    async detail(skill, options) {
      await rpc.ready({ signal: options?.signal })
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
      invalidate()
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
      if (result.success || result.installed) invalidate()
      return result as unknown as SkillInstallResult
    },
    supportsInstallStatus() {
      return rpc.supports(SKILLS_INSTALL_STATUS_METHOD)
    },
    async installStatus(operationId, options) {
      const result = await rpc.request<SkillsInstallStatusResult>(
        SKILLS_INSTALL_STATUS_METHOD, { operationId }, callOptions(options?.signal),
      )
      if (!validateSkillsInstallStatusResult(result)) throw invalid(SKILLS_INSTALL_STATUS_METHOD)
      if (result.state === 'succeeded') invalidate()
      return result as unknown as SkillInstallStatus
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
      invalidate()
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
      if (result.success) invalidate()
      return result as unknown as SkillInstallResult
    },
  }
}
