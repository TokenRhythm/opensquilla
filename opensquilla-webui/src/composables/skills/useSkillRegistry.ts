import { computed, ref, type ComputedRef, type Ref } from 'vue'
import i18n from '@/i18n'
import type { useRpcStore } from '@/stores/rpc'
import { useToasts } from '@/composables/useToasts'
import {
  createSkillMutationGate,
  type SkillMutationGate,
} from '@/composables/skills/useSkillMutationGate'
import type {
  RegistryResult,
  SkillDependencyInstallOutcome,
  SkillDiagnostic,
  SkillLifecycle,
  SkillSourceResolution,
} from '@/types/skills'

interface RegistrySearchData {
  results?: RegistryResult[]
  diagnostics?: SkillDiagnostic[]
  message?: string
}

export interface InstallResult {
  success: boolean
  cancelled?: boolean
  unchanged?: boolean
  name?: string
  message?: string
  installed?: boolean
  active?: boolean
  instruction_usable?: boolean
  installId?: string
  lifecycle?: SkillLifecycle
  resolution?: SkillSourceResolution
  diagnostics?: SkillDiagnostic[]
  rollbackPerformed?: boolean
  catalogGeneration?: number
  effectiveFrom?: 'next_turn' | 'next_start' | string
  missing_still?: {
    bins?: string[]
    env?: string[]
    env_any?: string[][]
  }
}

export type SkillInstallQueueStatus =
  | 'queued'
  | 'installing'
  | 'cancelling'
  | 'cancelled'
  | 'installed'
  | 'unchanged'
  | 'deferred'
  | 'unknown'
  | 'failed'

export interface SkillInstallQueueItem {
  id: string
  identifier: string
  source: string
  displayName: string
  status: SkillInstallQueueStatus
  result?: InstallResult
  error?: string
}

export function skillInstallRequiresRiskAcknowledgement(
  result: InstallResult | undefined,
): boolean {
  return Boolean(skillInstallRiskConfirmation(result))
}

export function skillInstallRiskConfirmation(
  result: InstallResult | undefined,
): string {
  const diagnostic = result?.diagnostics?.find(candidate =>
    candidate.code === 'SCAN_CONFIRMATION_REQUIRED')
  const token = diagnostic?.details?.confirmationToken
  return typeof token === 'string' ? token.trim() : ''
}

export type SkillInstallSource = 'clawhub' | 'github'

export const GITHUB_BATCH_MAX_REFERENCES = 10

const GITHUB_RATE_LIMIT_DIAGNOSTIC_CODES = new Set([
  'SOURCE_RATE_LIMITED',
  'FETCH_RATE_LIMITED',
])

function createInstallOperationId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID()
  }
  const bytes = new Uint8Array(16)
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    globalThis.crypto.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0'))
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10).join(''),
  ].join('-')
}

export function skillInstallWasRateLimited(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false
  if (Array.isArray(value)) return value.some(skillInstallWasRateLimited)

  const record = value as Record<string, unknown>
  if (typeof record.code === 'string'
    && GITHUB_RATE_LIMIT_DIAGNOSTIC_CODES.has(record.code)) return true
  return skillInstallWasRateLimited(record.diagnostics)
    || skillInstallWasRateLimited(record.details)
    || skillInstallWasRateLimited(record.resolution)
}

export type SkillInstallActivityPhase = 'installing' | 'refreshing' | 'terminal'

export interface SkillInstallActivity {
  items: SkillInstallQueueItem[]
  refreshWarning: string
  /** Optional while older in-memory fixtures and clients age out. */
  phase?: SkillInstallActivityPhase
}

export type SkillInstallActivities = Record<SkillInstallSource, SkillInstallActivity>

interface SkillInstallRequest {
  identifier: string
  source: string
  displayName?: string
}

const GENERIC_GITHUB_SKILL_SEGMENTS = new Set([
  'skill',
  'skills',
  'skill.md',
  'skills.md',
])

function decodedPathSegment(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

function concisePathLabel(segments: string[], fallback: string): string {
  for (let index = segments.length - 1; index >= 0; index -= 1) {
    const segment = decodedPathSegment(segments[index]).trim()
    if (!segment || GENERIC_GITHUB_SKILL_SEGMENTS.has(segment.toLowerCase())) continue
    return segment
  }
  return fallback
}

/**
 * Derive a short queue label without rewriting the exact GitHub install
 * identifier. The production resolver still receives the original reference.
 */
export function githubSkillDisplayName(identifier: string): string {
  const trimmed = identifier.trim()
  if (!trimmed) return trimmed

  try {
    const url = new URL(trimmed)
    if (url.hostname.toLowerCase() === 'github.com') {
      const segments = url.pathname.split('/').filter(Boolean)
      const repository = decodedPathSegment(segments[1] || '').replace(/\.git$/i, '')
      const treeMarker = segments.findIndex(segment =>
        segment.toLowerCase() === 'tree' || segment.toLowerCase() === 'blob')
      const skillPath = treeMarker >= 0
        ? segments.slice(treeMarker + 2)
        : segments.slice(2)
      return concisePathLabel(skillPath, repository || trimmed)
    }
  } catch {
    // Exact GitHub references also have a non-URL owner/repo@revision:path form.
  }

  const separator = trimmed.indexOf(':')
  const packageReference = separator >= 0 ? trimmed.slice(0, separator) : trimmed
  const skillPath = separator >= 0 ? trimmed.slice(separator + 1) : ''
  const revisionMarker = packageReference.lastIndexOf('@')
  const packageIdentifier = revisionMarker > packageReference.lastIndexOf('/')
    ? packageReference.slice(0, revisionMarker)
    : packageReference
  const packageSegments = packageIdentifier.split('/').filter(Boolean)
  const repository = decodedPathSegment(
    packageSegments[packageSegments.length - 1] || '',
  ).replace(/\.git$/i, '')

  return concisePathLabel(skillPath.split('/').filter(Boolean), repository || trimmed)
}

export function skillRegistryOperationKey(identifier: string, source: string): string {
  return JSON.stringify([source || 'clawhub', identifier])
}

export interface SkillRegistry {
  registryQuery: Ref<string>
  githubUrl: Ref<string>
  registryResults: Ref<RegistryResult[]>
  registryLoading: Ref<boolean>
  registryDiagnostics: Ref<SkillDiagnostic[]>
  registrySearchError: Ref<string>
  installingId: Ref<string | null>
  installActivities: Ref<SkillInstallActivities>
  runningSource: Ref<SkillInstallSource | null>
  cancellableInstallSource: ComputedRef<SkillInstallSource | null>
  cancellingSource: Ref<SkillInstallSource | null>
  queueRunning: ComputedRef<boolean>
  mutationBusy: ComputedRef<boolean>
  installingDepsId: Ref<string | null>
  uninstallingName: Ref<string | null>
  searchRegistry: () => Promise<void>
  installGithub: () => Promise<void>
  installSkill: (identifier: string, source: string, displayName?: string) => Promise<void>
  retryQueueItem: (id: string, acknowledgeRisk?: boolean) => Promise<void>
  cancelInstall: (source: SkillInstallSource) => Promise<void>
  clearInstallActivity: (source: SkillInstallSource) => void
  installDeps: (
    name: string,
    installId: string,
    skillInstallId?: string,
    instanceId?: string,
  ) => Promise<SkillDependencyInstallOutcome>
  uninstallSkill: (name: string, installId?: string) => Promise<boolean>
}

export function useSkillRegistry(
  rpc: ReturnType<typeof useRpcStore>,
  loadData: () => Promise<boolean>,
  mutationGate: SkillMutationGate = createSkillMutationGate(),
): SkillRegistry {
  const { pushToast } = useToasts()
  const t = i18n.global.t
  const registryQuery = ref('')
  const githubUrl = ref('')
  const registryResults = ref<RegistryResult[]>([])
  const registryLoading = ref(false)
  const registryDiagnostics = ref<SkillDiagnostic[]>([])
  const registrySearchError = ref('')
  const installingId = ref<string | null>(null)
  const installActivities = ref<SkillInstallActivities>({
    clawhub: { items: [], refreshWarning: '', phase: 'terminal' },
    github: { items: [], refreshWarning: '', phase: 'terminal' },
  })
  const runningSource = ref<SkillInstallSource | null>(null)
  const cancellingSource = ref<SkillInstallSource | null>(null)
  const installCancellationSupported = computed(() =>
    typeof rpc.supportsMethod === 'function'
    && rpc.supportsMethod('skills.install.cancel'))
  const activeInstallOperation = ref<{
    id: string
    itemId: string
    source: SkillInstallSource
  } | null>(null)
  const cancellableInstallSource = computed(() => activeInstallOperation.value?.source || null)
  const queueRunning = computed(() => runningSource.value !== null)
  const mutationBusy = computed(() => mutationGate.busy.value)
  const installingDepsId = ref<string | null>(null)
  const uninstallingName = ref<string | null>(null)
  let searchRequestId = 0
  let cancelRequestedOperationId = ''

  async function searchRegistry() {
    const query = registryQuery.value.trim()
    const requestId = ++searchRequestId
    if (!query) {
      registryLoading.value = false
      return
    }
    registryLoading.value = true
    registryResults.value = []
    registryDiagnostics.value = []
    registrySearchError.value = ''
    try {
      const data = await rpc.call<RegistrySearchData>('skills.search', {
        query,
        limit: 20,
        source: 'clawhub',
      })
      if (requestId !== searchRequestId) return
      registryResults.value = data.results || []
      registryDiagnostics.value = data.diagnostics || []
      registrySearchError.value = data.message || ''
    } catch (err) {
      if (requestId !== searchRequestId) return
      registrySearchError.value = (err as Error).message
      pushToast(t('cronSkills.registry.toastSearchFailed', { error: registrySearchError.value }), { tone: 'danger' })
    } finally {
      if (requestId === searchRequestId) registryLoading.value = false
    }
  }

  function uniqueRequests(requests: SkillInstallRequest[]): SkillInstallRequest[] {
    const seen = new Set<string>()
    return requests.flatMap((request) => {
      const identifier = request.identifier.trim()
      const source = (request.source || 'clawhub').trim() || 'clawhub'
      if (!identifier) return []
      const key = skillRegistryOperationKey(identifier, source)
      if (seen.has(key)) return []
      seen.add(key)
      return [{ ...request, identifier, source }]
    })
  }

  function requestToQueueItem(request: SkillInstallRequest): SkillInstallQueueItem {
    return {
      id: skillRegistryOperationKey(request.identifier, request.source),
      identifier: request.identifier,
      source: request.source,
      displayName: request.displayName
        || (request.source === 'github'
          ? githubSkillDisplayName(request.identifier)
          : request.identifier),
      status: 'queued',
    }
  }

  function activitySource(source: string): SkillInstallSource {
    return source === 'github' ? 'github' : 'clawhub'
  }

  function removeSuccessfulGithubLines(items: SkillInstallQueueItem[]) {
    const successful = new Set(
      items
        .filter(item => item.source === 'github'
          && (item.status === 'installed' || item.status === 'unchanged'))
        .map(item => item.identifier),
    )
    if (!successful.size) return
    githubUrl.value = githubUrl.value
      .split(/\r?\n/)
      .filter(line => !successful.has(line.trim()))
      .join('\n')
      .trim()
  }

  async function refreshCatalogAfterBatch(
    source: SkillInstallSource,
    items: SkillInstallQueueItem[],
  ) {
    const changed = items.some(item => item.status === 'installed' || item.status === 'unchanged')
    if (!changed) return
    installActivities.value[source].phase = 'refreshing'
    let refreshed = false
    try {
      refreshed = await loadData()
    } catch {
      // A durable installation remains successful even when its follow-up
      // catalog read cannot complete. Surface that as the existing warning.
    }
    if (!refreshed) {
      const message = t('cronSkills.skillsView.reloadListFailed')
      installActivities.value[source].refreshWarning = message
      pushToast(message, { tone: 'warn' })
    }
  }

  async function processQueueItems(
    items: SkillInstallQueueItem[],
    riskConfirmation = '',
  ) {
    for (const [index, item] of items.entries()) {
      item.status = 'installing'
      item.error = ''
      item.result = undefined
      installingId.value = item.id
      let rateLimited = false
      const operationId = installCancellationSupported.value
        ? createInstallOperationId()
        : ''
      if (operationId) {
        activeInstallOperation.value = {
          id: operationId,
          itemId: item.id,
          source: activitySource(item.source),
        }
      }
      try {
        const res = await rpc.call<InstallResult>('skills.install', {
          identifier: item.identifier,
          source: item.source,
          ...(operationId ? { operationId } : {}),
          ...(riskConfirmation
            ? { force: true, riskConfirmation }
            : {}),
        })
        item.result = res
        item.displayName = res.name || item.displayName
        item.status = res.cancelled
          ? 'cancelled'
          : res.success
            ? (res.unchanged ? 'unchanged' : 'installed')
            : 'failed'
        item.error = res.success || res.cancelled
          ? ''
          : (res.message || t('cronSkills.registry.installFailed'))
        if (!res.cancelled) markRegistryResultOutcome(item.identifier, item.source, res)
        rateLimited = item.source === 'github' && skillInstallWasRateLimited(res)
      } catch (err) {
        rateLimited = item.source === 'github' && skillInstallWasRateLimited(err)
        // A stable rate-limit diagnostic means this attempt did not run. For
        // other transport interruptions, the Gateway may already have committed.
        item.status = rateLimited ? 'failed' : 'unknown'
        item.error = (err as Error).message
      } finally {
        installingId.value = null
        if (activeInstallOperation.value?.id === operationId) {
          activeInstallOperation.value = null
          cancellingSource.value = null
        }
      }
      if (operationId && cancelRequestedOperationId === operationId) break
      if (!rateLimited) continue
      for (const deferred of items.slice(index + 1)) {
        deferred.status = 'deferred'
        deferred.error = ''
      }
      break
    }
  }

  function settleInterruptedItems(source: SkillInstallSource) {
    for (const item of installActivities.value[source].items) {
      if (item.status !== 'queued'
        && item.status !== 'installing'
        && item.status !== 'cancelling') continue
      item.status = 'unknown'
      item.error ||= t('cronSkills.registry.installResultUnknown')
    }
    installActivities.value[source].phase = 'terminal'
  }

  async function runNewBatch(requests: SkillInstallRequest[]) {
    const unique = uniqueRequests(requests)
    if (!unique.length) return
    if (!mutationGate.acquire('install_queue')) return
    const source = activitySource(unique[0].source)
    const items = unique.map(requestToQueueItem)
    installActivities.value[source] = { items, refreshWarning: '', phase: 'installing' }
    const activityItems = installActivities.value[source].items
    runningSource.value = source
    try {
      await processQueueItems(activityItems)
      removeSuccessfulGithubLines(activityItems)
      await refreshCatalogAfterBatch(source, activityItems)
    } finally {
      settleInterruptedItems(source)
      runningSource.value = null
      installingId.value = null
      activeInstallOperation.value = null
      cancellingSource.value = null
      cancelRequestedOperationId = ''
      mutationGate.release('install_queue')
    }
  }

  async function installGithub() {
    const requests = uniqueRequests(
      githubUrl.value
        .split(/\r?\n/)
        .map(identifier => ({ identifier, source: 'github' })),
    )
    if (requests.length > GITHUB_BATCH_MAX_REFERENCES) return
    await runNewBatch(requests)
  }

  function markRegistryResultOutcome(
    identifier: string,
    source: string,
    installResult: InstallResult,
  ) {
    const installSource = source || 'clawhub'
    registryResults.value = registryResults.value.map((registryResult) => {
      const resultSource = registryResult.source || 'clawhub'
      const resultIdentifier = registryResult.installReference
        || registryResult.install_reference
        || registryResult.identifier
        || registryResult.name
      const sameSource = resultSource === installSource
      const sameIdentifier = resultIdentifier === identifier

      if (!sameSource || !sameIdentifier) return registryResult
      return {
        ...registryResult,
        installed: installResult.installed ?? installResult.success,
        lifecycle: installResult.lifecycle,
        instruction_usable: installResult.instruction_usable,
        diagnostics: installResult.diagnostics,
      }
    })
  }

  async function installSkill(identifier: string, source: string, displayName?: string) {
    await runNewBatch([{ identifier, source, displayName }])
  }

  async function retryQueueItem(id: string, acknowledgeRisk = false) {
    const source = (['clawhub', 'github'] as const).find(candidate =>
      installActivities.value[candidate].items.some(item => item.id === id))
    if (!source) return
    const item = installActivities.value[source].items.find(candidate => candidate.id === id)
    if (!item || (item.status !== 'failed' && item.status !== 'cancelled')) return
    const riskConfirmation = acknowledgeRisk
      ? skillInstallRiskConfirmation(item.result)
      : ''
    if (acknowledgeRisk && !riskConfirmation) return
    if (!mutationGate.acquire('install_queue')) return
    installActivities.value[source].refreshWarning = ''
    installActivities.value[source].phase = 'installing'
    runningSource.value = source
    try {
      await processQueueItems([item], riskConfirmation)
      removeSuccessfulGithubLines([item])
      await refreshCatalogAfterBatch(source, [item])
    } finally {
      settleInterruptedItems(source)
      runningSource.value = null
      installingId.value = null
      activeInstallOperation.value = null
      cancellingSource.value = null
      cancelRequestedOperationId = ''
      mutationGate.release('install_queue')
    }
  }

  async function cancelInstall(source: SkillInstallSource) {
    const operation = activeInstallOperation.value
    if (!operation || operation.source !== source || cancellingSource.value) return
    const activity = installActivities.value[source]
    const activeItem = activity.items.find(item => item.id === operation.itemId)
    if (!activeItem) return

    cancelRequestedOperationId = operation.id
    cancellingSource.value = source
    activeItem.status = 'cancelling'
    for (const item of activity.items) {
      if (item.status === 'queued') {
        item.status = 'cancelled'
        item.error = ''
      }
    }
    try {
      await rpc.call<InstallResult>('skills.install.cancel', {
        operationId: operation.id,
      })
    } catch (err) {
      if (activeInstallOperation.value?.id === operation.id) {
        activeItem.status = 'installing'
        cancellingSource.value = null
      }
      pushToast(t('cronSkills.registry.cancelInstallFailed', {
        error: (err as Error).message,
      }), { tone: 'danger' })
    }
  }

  function clearInstallActivity(source: SkillInstallSource) {
    if (runningSource.value) return
    installActivities.value[source] = { items: [], refreshWarning: '', phase: 'terminal' }
  }

  async function installDeps(
    name: string,
    installId: string,
    skillInstallId = '',
    instanceId = '',
  ): Promise<SkillDependencyInstallOutcome> {
    const failed = (message = ''): SkillDependencyInstallOutcome => ({
      success: false,
      complete: false,
      message,
      missingStill: { bins: [], env: [], env_any: [] },
    })
    if (!name || !installId || !mutationGate.acquire('dependency_install')) return failed()
    installingDepsId.value = installId
    try {
      const res = await rpc.call<InstallResult>('skills.deps.install', {
        name,
        install_id: installId,
        ...(skillInstallId ? { installId: skillInstallId } : {}),
        ...(instanceId ? { instanceId } : {}),
      })
      if (res.success) {
        pushToast(res.message || t('cronSkills.registry.installed'), { tone: 'ok' })
        const still = res.missing_still || {}
        const missingStill = {
          bins: still.bins || [],
          env: still.env || [],
          env_any: still.env_any || [],
        }
        const stillMissing = missingStill.bins.length
          + missingStill.env.length
          + missingStill.env_any.length
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return {
          success: true,
          complete: stillMissing === 0,
          message: res.message || '',
          missingStill,
        }
      }
      pushToast(res.message || t('cronSkills.registry.installFailed'), { tone: 'danger' })
      return failed(res.message || '')
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return failed((err as Error).message)
    } finally {
      installingDepsId.value = null
      mutationGate.release('dependency_install')
    }
  }

  async function uninstallSkill(name: string, installId = ''): Promise<boolean> {
    if ((!name && !installId) || !mutationGate.acquire('uninstall')) return false
    uninstallingName.value = name
    try {
      const res = await rpc.call<InstallResult>('skills.uninstall', {
        ...(name ? { name } : {}),
        ...(installId ? { installId } : {}),
      })
      if (res.success) {
        if (!(await loadData())) {
          pushToast(t('cronSkills.skillsView.reloadListFailed'), { tone: 'warn' })
        }
        return true
      }
      pushToast(res.message || t('cronSkills.registry.uninstallFailed'), { tone: 'danger' })
      return false
    } catch (err) {
      pushToast((err as Error).message, { tone: 'danger' })
      return false
    } finally {
      uninstallingName.value = null
      mutationGate.release('uninstall')
    }
  }

  return {
    registryQuery,
    githubUrl,
    registryResults,
    registryLoading,
    registryDiagnostics,
    registrySearchError,
    installingId,
    installActivities,
    runningSource,
    cancellableInstallSource,
    cancellingSource,
    queueRunning,
    mutationBusy,
    installingDepsId,
    uninstallingName,
    searchRegistry,
    installGithub,
    installSkill,
    retryQueueItem,
    cancelInstall,
    clearInstallActivity,
    installDeps,
    uninstallSkill,
  }
}
