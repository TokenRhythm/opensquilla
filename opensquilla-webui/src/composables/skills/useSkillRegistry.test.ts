import { afterEach, describe, expect, it, vi } from 'vitest'
import { watch } from 'vue'
import {
  GITHUB_BATCH_MAX_REFERENCES,
  githubSkillDisplayName,
  skillInstallRiskConfirmation,
  skillInstallRequiresRiskAcknowledgement,
  skillInstallWasRateLimited,
  skillRegistryOperationKey,
  useSkillRegistry,
} from './useSkillRegistry'
import { createSkillMutationGate } from './useSkillMutationGate'
import { useSkillProposals } from './useSkillProposals'

const pushToast = vi.hoisted(() => vi.fn())

vi.mock('@/composables/useToasts', () => ({
  useToasts: () => ({ pushToast }),
}))

afterEach(() => {
  vi.restoreAllMocks()
  pushToast.mockClear()
})

describe('useSkillRegistry install state', () => {
  it('marks the matching community result installed after a successful install', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'skills.install') {
        return {
          success: true,
          name: 'Development Coding Agent',
          message: 'installed',
          installed: true,
          instruction_usable: false,
          lifecycle: {
            install_state: 'tracked',
            load_state: 'loaded',
            selection_state: 'shadowed',
            compatibility_state: 'degraded',
            readiness_state: 'ready',
          },
          diagnostics: [{
            code: 'TOOL_PREAPPROVAL_IGNORED',
            severity: 'warning',
            phase: 'compatibility',
            blocking: false,
            message: 'Scoped tool pre-approval is not applied.',
          }],
        }
      }
      throw new Error(`Unexpected RPC method: ${method}`)
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)

    registry.registryResults.value = [
      {
        name: 'Development Coding Agent',
        description: 'Enhanced coding agent',
        identifier: 'development-coding-agent',
        installReference: '@alice/development-coding-agent',
        source: 'clawhub',
        installed: false,
      },
      {
        name: 'Development Coding Agent',
        identifier: 'development-coding-agent',
        installReference: '@bob/development-coding-agent',
        source: 'clawhub',
        installed: false,
      },
    ]

    await registry.installSkill('@alice/development-coding-agent', 'clawhub')

    expect(call).toHaveBeenCalledWith('skills.install', {
      identifier: '@alice/development-coding-agent',
      source: 'clawhub',
    })
    expect(loadData).toHaveBeenCalledOnce()
    expect(registry.registryResults.value.map(result => result.installed)).toEqual([true, false])
    expect(registry.registryResults.value[0].instruction_usable).toBe(false)
    expect(registry.registryResults.value[0].lifecycle?.selection_state).toBe('shadowed')
    expect(registry.registryResults.value[0].diagnostics?.[0].code)
      .toBe('TOOL_PREAPPROVAL_IGNORED')
    expect(registry.registryResults.value[1].lifecycle).toBeUndefined()
    expect(registry.installingId.value).toBeNull()
  })

  it('honors snake_case install references before non-unique registry identifiers', async () => {
    const call = vi.fn(async () => ({ success: true, installed: true }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.registryResults.value = [
      {
        name: 'Demo',
        identifier: 'shared-demo',
        install_reference: '@alice/shared-demo',
        source: 'clawhub',
        installed: false,
      },
      {
        name: 'Demo',
        identifier: 'shared-demo',
        install_reference: '@bob/shared-demo',
        source: 'clawhub',
        installed: false,
      },
    ]

    await registry.installSkill('@alice/shared-demo', 'clawhub')

    expect(registry.registryResults.value.map(result => result.installed)).toEqual([true, false])
  })

  it('keeps in-flight identity distinct across community sources', () => {
    expect(skillRegistryOperationKey('demo', 'clawhub'))
      .not.toBe(skillRegistryOperationKey('demo', 'github'))
  })

  it('derives concise GitHub queue labels without changing exact identifiers', async () => {
    const references = [
      'https://github.com/obra/superpowers/tree/main/brainstorming',
      'https://github.com/shadcn-ui/ui/blob/main/skills/shadcn/SKILL.md',
      'shadcn-ui/ui@6261bd89f72d794aea491482cc2acfd8dc3d63e2:skills/shadcn/SKILL.md',
      'owner/repository@0123456789abcdef:skill',
    ]
    const call = vi.fn(async (_method: string, _params: { identifier: string }) => ({
      success: true,
      installed: true,
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.githubUrl.value = references.join('\n')

    await registry.installGithub()

    expect(references.map(githubSkillDisplayName)).toEqual([
      'brainstorming',
      'shadcn',
      'shadcn',
      'repository',
    ])
    expect(registry.installActivities.value.github.items.map(item => item.displayName)).toEqual([
      'brainstorming',
      'shadcn',
      'shadcn',
      'repository',
    ])
    expect(call.mock.calls.map(([, params]) => params.identifier)).toEqual(references)
  })

  it('installs a deduplicated multi-line GitHub batch serially and refreshes once', async () => {
    let activeCalls = 0
    let maxActiveCalls = 0
    const identifiers: string[] = []
    const call = vi.fn(async (method: string, params: { identifier?: string }) => {
      if (method !== 'skills.install') throw new Error(`Unexpected RPC method: ${method}`)
      activeCalls += 1
      maxActiveCalls = Math.max(maxActiveCalls, activeCalls)
      identifiers.push(String(params.identifier))
      await Promise.resolve()
      activeCalls -= 1
      if (params.identifier === 'https://github.com/acme/skill-3') {
        throw new Error('fixture fetch failed')
      }
      return {
        success: true,
        name: String(params.identifier).split('/').slice(-1)[0],
        installed: true,
      }
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)
    const lines = Array.from({ length: GITHUB_BATCH_MAX_REFERENCES }, (_, index) =>
      `https://github.com/acme/skill-${index + 1}`)
    registry.githubUrl.value = [lines[0], ...lines, lines[4]].join('\n')

    await registry.installGithub()

    expect(identifiers).toEqual(lines)
    expect(maxActiveCalls).toBe(1)
    expect(loadData).toHaveBeenCalledOnce()
    expect(registry.installActivities.value.github.items).toHaveLength(GITHUB_BATCH_MAX_REFERENCES)
    expect(registry.installActivities.value.github.items[2].status).toBe('unknown')
    expect(registry.installActivities.value.github.items[3].status).toBe('installed')
    expect(registry.githubUrl.value).toBe('https://github.com/acme/skill-3')
    expect(registry.queueRunning.value).toBe(false)
  })

  it('cancels the active backend install and does not start queued items', async () => {
    let finishInstall: ((result: { success: boolean; cancelled: boolean }) => void) | undefined
    const call = vi.fn((method: string, _params: Record<string, unknown>) => {
      if (method === 'skills.install') {
        return new Promise(resolve => { finishInstall = resolve })
      }
      if (method === 'skills.install.cancel') {
        finishInstall?.({ success: false, cancelled: true })
        return Promise.resolve({ success: false, cancelled: true })
      }
      return Promise.reject(new Error(`Unexpected RPC method: ${method}`))
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({
      call,
      supportsMethod: (method: string) => method === 'skills.install.cancel',
    } as never, loadData)
    registry.githubUrl.value = [
      'https://github.com/acme/one',
      'https://github.com/acme/two',
    ].join('\n')

    const installing = registry.installGithub()
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1))
    const operationId = call.mock.calls[0][1].operationId

    const cancelling = registry.cancelInstall('github')
    expect(registry.installActivities.value.github.items.map(item => item.status))
      .toEqual(['cancelling', 'cancelled'])
    expect(call).toHaveBeenLastCalledWith('skills.install.cancel', { operationId })

    await cancelling
    await installing

    expect(String(operationId)).toMatch(/^[0-9a-f-]{36}$/)
    expect(registry.installActivities.value.github.items.map(item => item.status))
      .toEqual(['cancelled', 'cancelled'])
    expect(call.mock.calls.filter(([method]) => method === 'skills.install')).toHaveLength(1)
    expect(loadData).not.toHaveBeenCalled()
    expect(registry.queueRunning.value).toBe(false)
  })

  it('preserves a success that commits while cancellation races and refreshes it', async () => {
    let finishInstall: ((result: { success: boolean; installed: boolean }) => void) | undefined
    const call = vi.fn((method: string) => {
      if (method === 'skills.install') {
        return new Promise(resolve => { finishInstall = resolve })
      }
      if (method === 'skills.install.cancel') {
        finishInstall?.({ success: true, installed: true })
        return Promise.resolve({ success: false, cancelled: true })
      }
      return Promise.reject(new Error(`Unexpected RPC method: ${method}`))
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({
      call,
      supportsMethod: (method: string) => method === 'skills.install.cancel',
    } as never, loadData)
    registry.githubUrl.value = [
      'https://github.com/acme/one',
      'https://github.com/acme/two',
    ].join('\n')

    const installing = registry.installGithub()
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1))
    await registry.cancelInstall('github')
    await installing

    expect(registry.installActivities.value.github.items.map(item => item.status))
      .toEqual(['installed', 'cancelled'])
    expect(registry.githubUrl.value).toBe('https://github.com/acme/two')
    expect(loadData).toHaveBeenCalledOnce()
  })

  it('rejects more than ten unique GitHub references without truncating or starting RPCs', async () => {
    const call = vi.fn(async () => ({ success: true, installed: true }))
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)
    const references = Array.from({ length: GITHUB_BATCH_MAX_REFERENCES + 1 }, (_, index) =>
      `https://github.com/acme/skill-${index + 1}`)
    const input = [references[0], '', ...references, references[3]].join('\n')
    registry.githubUrl.value = input

    await registry.installGithub()

    expect(call).not.toHaveBeenCalled()
    expect(loadData).not.toHaveBeenCalled()
    expect(registry.installActivities.value.github.items).toEqual([])
    expect(registry.githubUrl.value).toBe(input)
    expect(registry.queueRunning.value).toBe(false)
  })

  it.each([
    {
      label: 'a direct source diagnostic',
      diagnostics: [{
        code: 'SOURCE_RATE_LIMITED',
        severity: 'error',
        phase: 'source',
        blocking: true,
        message: 'GitHub rate limited this request.',
      }],
    },
    {
      label: 'a nested fetch diagnostic',
      diagnostics: [{
        code: 'SOURCE_FETCH_FAILED',
        severity: 'error',
        phase: 'source',
        blocking: true,
        message: 'GitHub fetch failed.',
        details: {
          diagnostics: [{
            code: 'FETCH_RATE_LIMITED',
            severity: 'error',
            phase: 'fetch',
            blocking: true,
            message: 'GitHub rate limited the archive request.',
          }],
        },
      }],
    },
  ])('pauses a GitHub batch after $label and preserves unattempted lines', async ({ diagnostics }) => {
    const references = Array.from({ length: 4 }, (_, index) =>
      `https://github.com/acme/skill-${index + 1}`)
    const call = vi.fn(async (_method: string, params: { identifier: string }) => {
      if (params.identifier === references[0]) return { success: true, installed: true }
      if (params.identifier === references[1]) {
        return {
          success: false,
          installed: false,
          message: 'GitHub rate limited this batch.',
          diagnostics,
        }
      }
      throw new Error(`unexpected install attempt: ${params.identifier}`)
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)
    registry.githubUrl.value = references.join('\n')

    await registry.installGithub()

    expect(call.mock.calls.map(([, params]) => params.identifier)).toEqual(references.slice(0, 2))
    expect(registry.installActivities.value.github.items.map(item => item.status)).toEqual([
      'installed',
      'failed',
      'deferred',
      'deferred',
    ])
    expect(registry.installActivities.value.github.items.slice(2).every(item =>
      !item.error)).toBe(true)
    expect(registry.githubUrl.value).toBe(references.slice(1).join('\n'))
    expect(loadData).toHaveBeenCalledOnce()
  })

  it('pauses remaining GitHub references when a thrown RPC error carries rate-limit details', async () => {
    const references = [
      'https://github.com/acme/skill-1',
      'https://github.com/acme/skill-2',
      'https://github.com/acme/skill-3',
    ]
    const rpcError = Object.assign(new Error('GitHub rate limited this request.'), {
      details: {
        diagnostics: [{
          code: 'SOURCE_RATE_LIMITED',
          severity: 'error',
          phase: 'source',
          blocking: true,
          message: 'Try again later.',
        }],
      },
    })
    const call = vi.fn(async () => { throw rpcError })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)
    registry.githubUrl.value = references.join('\n')

    await registry.installGithub()

    expect(call).toHaveBeenCalledOnce()
    expect(registry.installActivities.value.github.items.map(item => item.status)).toEqual([
      'failed',
      'deferred',
      'deferred',
    ])
    expect(registry.githubUrl.value).toBe(references.join('\n'))
    expect(loadData).not.toHaveBeenCalled()
  })

  it('recognizes rate limits nested in stable RPC error details', () => {
    expect(skillInstallWasRateLimited({
      details: {
        diagnostics: [{ code: 'FETCH_RATE_LIMITED' }],
      },
    })).toBe(true)
    expect(skillInstallWasRateLimited({
      resolution: {
        diagnostics: [{ code: 'SOURCE_RATE_LIMITED' }],
      },
    })).toBe(true)
    expect(skillInstallWasRateLimited({
      diagnostics: [{ code: 'FETCH_SERVER_FAILED' }],
    })).toBe(false)
  })

  it('ignores a rapid second submit while the first immutable install is in flight', async () => {
    let release: (() => void) | undefined
    const pending = new Promise<void>((resolve) => { release = resolve })
    const call = vi.fn(async () => {
      await pending
      return { success: true, installed: true }
    })
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.githubUrl.value = 'https://github.com/acme/demo'

    const first = registry.installGithub()
    const second = registry.installGithub()
    expect(registry.queueRunning.value).toBe(true)
    release?.()
    await Promise.all([first, second])

    expect(call).toHaveBeenCalledOnce()
  })

  it('publishes queue item status changes through the reactive source activity', async () => {
    let release: ((value: { success: boolean; installed: boolean }) => void) | undefined
    const pending = new Promise<{ success: boolean; installed: boolean }>((resolve) => {
      release = resolve
    })
    const registry = useSkillRegistry(
      { call: vi.fn(async () => pending) } as never,
      vi.fn(async () => true),
    )
    const observed: Array<string | undefined> = []
    watch(
      () => registry.installActivities.value.clawhub.items[0]?.status,
      status => observed.push(status),
      { flush: 'sync' },
    )

    const installing = registry.installSkill('@acme/reactive', 'clawhub')
    expect(observed).toEqual(['queued', 'installing'])
    release?.({ success: true, installed: true })
    await installing

    expect(observed).toEqual(['queued', 'installing', 'installed'])
  })

  it('separates installation from catalog refresh and then settles terminally', async () => {
    let finishRefresh: ((value: boolean) => void) | undefined
    const refreshPending = new Promise<boolean>((resolve) => { finishRefresh = resolve })
    const registry = useSkillRegistry(
      { call: vi.fn(async () => ({ success: true, installed: true })) } as never,
      vi.fn(async () => refreshPending),
    )

    const installing = registry.installSkill('@acme/phase', 'clawhub', 'Phase Skill')
    await vi.waitFor(() => {
      expect(registry.installActivities.value.clawhub.phase).toBe('refreshing')
    })

    expect(registry.runningSource.value).toBe('clawhub')
    expect(registry.installActivities.value.clawhub.items[0].status).toBe('installed')
    finishRefresh?.(true)
    await installing

    expect(registry.runningSource.value).toBeNull()
    expect(registry.installActivities.value.clawhub.phase).toBe('terminal')
    expect(registry.installActivities.value.clawhub.items.some(item =>
      item.status === 'queued' || item.status === 'installing')).toBe(false)
  })

  it('marks an interrupted install response unknown instead of claiming it was not installed', async () => {
    const registry = useSkillRegistry(
      { call: vi.fn(async () => { throw new Error('connection closed') }) } as never,
      vi.fn(async () => true),
    )

    await registry.installSkill('@acme/uncertain', 'clawhub', 'Uncertain Skill')

    const item = registry.installActivities.value.clawhub.items[0]
    expect(item.status).toBe('unknown')
    expect(item.error).toBe('connection closed')
    expect(registry.installActivities.value.clawhub.phase).toBe('terminal')
    expect(registry.runningSource.value).toBeNull()
  })

  it('refuses queue starts while dependency, uninstall, or reload owns the mutation gate', async () => {
    const pending = new Map<string, (value: { success: boolean; installed?: boolean }) => void>()
    const call = vi.fn((method: string) => {
      if (method === 'skills.install') return Promise.resolve({ success: true, installed: true })
      return new Promise<{ success: boolean }>((resolve) => { pending.set(method, resolve) })
    })
    const gate = createSkillMutationGate()
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const dependency = registry.installDeps('demo', 'node')
    expect(gate.owner.value).toBe('dependency_install')
    await registry.installSkill('@acme/during-dependency', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['skills.deps.install'])
    pending.get('skills.deps.install')?.({ success: true })
    await dependency

    const uninstall = registry.uninstallSkill('demo')
    expect(gate.owner.value).toBe('uninstall')
    await registry.installSkill('@acme/during-uninstall', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'skills.deps.install',
      'skills.uninstall',
    ])
    pending.get('skills.uninstall')?.({ success: true })
    await uninstall

    expect(gate.acquire('reload')).toBe(true)
    await registry.installSkill('@acme/during-reload', 'clawhub')
    expect(call.mock.calls.some(([method]) => method === 'skills.install')).toBe(false)
    gate.release('reload')

    await registry.installSkill('@acme/after-release', 'clawhub')
    expect(call.mock.calls[call.mock.calls.length - 1]?.[0]).toBe('skills.install')
  })

  it('keeps dependency and uninstall mutations out while the queue owns the gate', async () => {
    let finishInstall: ((value: { success: boolean; installed: boolean }) => void) | undefined
    const installPending = new Promise<{ success: boolean; installed: boolean }>((resolve) => {
      finishInstall = resolve
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'skills.install') return installPending
      return { success: true }
    })
    const gate = createSkillMutationGate()
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const queue = registry.installSkill('@acme/demo', 'clawhub')
    expect(gate.owner.value).toBe('install_queue')
    const dependency = await registry.installDeps('demo', 'node')
    const uninstalled = await registry.uninstallSkill('demo')

    expect(dependency.success).toBe(false)
    expect(uninstalled).toBe(false)
    expect(call.mock.calls.map(([method]) => method)).toEqual(['skills.install'])

    finishInstall?.({ success: true, installed: true })
    await queue
    expect(gate.owner.value).toBeNull()
  })

  it('shares proposal mutation ownership with the install queue', async () => {
    let finishProposal: ((value: { settings: Record<string, unknown> }) => void) | undefined
    const proposalPending = new Promise<{ settings: Record<string, unknown> }>((resolve) => {
      finishProposal = resolve
    })
    const call = vi.fn(async (method: string) => {
      if (method === 'exec.proposals.settings.set') return proposalPending
      if (method === 'skills.install') return { success: true, installed: true }
      throw new Error(`Unexpected method ${method}`)
    })
    const gate = createSkillMutationGate()
    const proposals = useSkillProposals({ call } as never, vi.fn(async () => {}), gate)
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true), gate)

    const proposal = proposals.toggleAutoPropose('enabled', true)
    expect(gate.owner.value).toBe('proposal')
    await registry.installSkill('@acme/during-proposal', 'clawhub')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['exec.proposals.settings.set'])

    finishProposal?.({ settings: { enabled: true } })
    await proposal

    expect(gate.acquire('install_queue')).toBe(true)
    await proposals.setAutoEnableRisk('low')
    expect(call.mock.calls.map(([method]) => method)).toEqual(['exec.proposals.settings.set'])
    gate.release('install_queue')

    await registry.installSkill('@acme/after-proposal', 'clawhub')
    expect(call.mock.calls[call.mock.calls.length - 1]?.[0]).toBe('skills.install')
  })

  it('keeps terminal results and retries only the selected source item', async () => {
    const attempts = new Map<string, number>()
    const call = vi.fn(async (_method: string, params: { identifier: string }) => {
      const count = (attempts.get(params.identifier) || 0) + 1
      attempts.set(params.identifier, count)
      return count === 1
        ? { success: false, message: 'not compatible' }
        : { success: true, unchanged: true, name: 'demo' }
    })
    const loadData = vi.fn(async () => true)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('@acme/demo', 'clawhub', 'Demo')
    registry.githubUrl.value = 'https://github.com/acme/demo'
    await registry.installGithub()
    const clawHubItem = registry.installActivities.value.clawhub.items[0]
    const githubItem = registry.installActivities.value.github.items[0]
    expect(clawHubItem.status).toBe('failed')
    expect(githubItem.status).toBe('failed')

    await registry.retryQueueItem(clawHubItem.id)

    expect(registry.installActivities.value.clawhub.items[0].status).toBe('unchanged')
    expect(registry.installActivities.value.clawhub.items[0].displayName).toBe('demo')
    expect(registry.installActivities.value.github.items[0]).toBe(githubItem)
    expect(registry.installActivities.value.github.items[0].status).toBe('failed')
    expect(loadData).toHaveBeenCalledOnce()
    expect(call.mock.calls[call.mock.calls.length - 1]?.[1]).toEqual({
      identifier: '@acme/demo',
      source: 'clawhub',
    })
  })

  it('sends force only after an explicit scanner-risk acknowledgement', async () => {
    const calls: Array<Record<string, unknown>> = []
    const confirmationToken = 'reviewed-artifact-confirmation'
    const call = vi.fn(async (_method: string, params: Record<string, unknown>) => {
      calls.push(params)
      if (params.force === true && params.riskConfirmation === confirmationToken) {
        return { success: true, installed: true }
      }
      return {
        success: false,
        installed: false,
        message: 'Review scanner findings before continuing.',
        diagnostics: [{
          code: 'SCAN_CONFIRMATION_REQUIRED',
          severity: 'warning',
          phase: 'security',
          blocking: true,
          message: 'The local scanner found content that requires review.',
          details: {
            confirmationToken,
            resolvedIdentifier: '@acme/review-me@1.0.0',
            artifactDigest: 'artifact-digest',
            treeDigest: 'tree-digest',
          },
        }],
      }
    })
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    await registry.installSkill('@acme/review-me', 'clawhub', 'Review me')
    const item = registry.installActivities.value.clawhub.items[0]

    expect(item.status).toBe('failed')
    expect(skillInstallRequiresRiskAcknowledgement(item.result)).toBe(true)
    expect(skillInstallRiskConfirmation(item.result)).toBe(confirmationToken)
    expect(calls[0]).toEqual({
      identifier: '@acme/review-me',
      source: 'clawhub',
    })

    await registry.retryQueueItem(item.id, true)

    expect(calls[1]).toEqual({
      identifier: '@acme/review-me',
      source: 'clawhub',
      force: true,
      riskConfirmation: confirmationToken,
    })
    expect(item.status).toBe('installed')
  })

  it('does not send bare force for an unbound legacy scanner diagnostic', async () => {
    const call = vi.fn(async () => ({
      success: false,
      message: 'Review scanner findings before continuing.',
      diagnostics: [{
        code: 'SCAN_CONFIRMATION_REQUIRED',
        severity: 'warning',
        phase: 'security',
        blocking: true,
        message: 'The scanner found content that requires review.',
      }],
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    await registry.installSkill('@acme/legacy-review', 'clawhub')
    const item = registry.installActivities.value.clawhub.items[0]
    expect(skillInstallRequiresRiskAcknowledgement(item.result)).toBe(false)

    await registry.retryQueueItem(item.id, true)

    expect(call).toHaveBeenCalledOnce()
  })

  it('does not allow risk acknowledgement to force an unrelated failure', async () => {
    const call = vi.fn(async () => ({
      success: false,
      message: 'Archive integrity failed.',
      diagnostics: [{
        code: 'ARTIFACT_DIGEST_MISMATCH',
        severity: 'error',
        phase: 'security',
        blocking: true,
        message: 'Digest mismatch.',
      }],
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    await registry.installSkill('@acme/bad-digest', 'clawhub')
    const item = registry.installActivities.value.clawhub.items[0]
    await registry.retryQueueItem(item.id, true)

    expect(call).toHaveBeenCalledOnce()
    expect(skillInstallRequiresRiskAcknowledgement(item.result)).toBe(false)
  })

  it('retains terminal batches across sources and replaces only the same source', async () => {
    const call = vi.fn(async (_method: string, params: { identifier: string }) => ({
      success: true,
      installed: true,
      name: params.identifier,
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    await registry.installSkill('@acme/clawhub-one', 'clawhub', 'ClawHub one')
    registry.githubUrl.value = 'https://github.com/acme/github-one'
    await registry.installGithub()

    const githubActivity = registry.installActivities.value.github
    expect(registry.installActivities.value.clawhub.items.map(item => item.identifier))
      .toEqual(['@acme/clawhub-one'])
    expect(githubActivity.items.map(item => item.identifier))
      .toEqual(['https://github.com/acme/github-one'])

    await registry.installSkill('@acme/clawhub-two', 'clawhub', 'ClawHub two')

    expect(registry.installActivities.value.clawhub.items.map(item => item.identifier))
      .toEqual(['@acme/clawhub-two'])
    expect(registry.installActivities.value.github).toBe(githubActivity)
    expect(registry.installActivities.value.github.items.map(item => item.identifier))
      .toEqual(['https://github.com/acme/github-one'])
  })

  it('clears only one terminal activity and blocks every clear while a source runs', async () => {
    let holdNext = false
    let release: (() => void) | undefined
    const pending = new Promise<void>((resolve) => { release = resolve })
    const call = vi.fn(async () => {
      if (holdNext) await pending
      return { success: true, installed: true }
    })
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    await registry.installSkill('@acme/clawhub-one', 'clawhub')
    registry.githubUrl.value = 'https://github.com/acme/github-one'
    await registry.installGithub()

    registry.clearInstallActivity('clawhub')
    expect(registry.installActivities.value.clawhub.items).toEqual([])
    expect(registry.installActivities.value.github.items).toHaveLength(1)

    holdNext = true
    const running = registry.installSkill('@acme/clawhub-two', 'clawhub')
    expect(registry.runningSource.value).toBe('clawhub')
    expect(registry.installActivities.value.clawhub.items[0].status).toBe('installing')

    registry.clearInstallActivity('clawhub')
    registry.clearInstallActivity('github')
    expect(registry.installActivities.value.clawhub.items).toHaveLength(1)
    expect(registry.installActivities.value.github.items).toHaveLength(1)

    release?.()
    await running
    expect(registry.runningSource.value).toBeNull()
  })

  it('keeps catalog refresh warnings scoped to their source activity', async () => {
    const call = vi.fn(async () => ({ success: true, installed: true }))
    const loadData = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('@acme/clawhub-one', 'clawhub')
    const clawHubWarning = registry.installActivities.value.clawhub.refreshWarning
    expect(clawHubWarning).not.toBe('')
    expect(registry.installActivities.value.github.refreshWarning).toBe('')

    registry.githubUrl.value = 'https://github.com/acme/github-one'
    await registry.installGithub()

    expect(registry.installActivities.value.clawhub.refreshWarning).toBe(clawHubWarning)
    expect(registry.installActivities.value.github.refreshWarning).toBe('')
    expect(pushToast).toHaveBeenCalledTimes(1)
  })

  it('searches ClawHub explicitly and retains source diagnostics', async () => {
    const diagnostic = {
      code: 'SOURCE_RATE_LIMITED',
      severity: 'error',
      phase: 'source',
      blocking: true,
      message: 'Try again later.',
    }
    const call = vi.fn(async () => ({ results: [], diagnostics: [diagnostic] }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))
    registry.registryQuery.value = 'demo'

    await registry.searchRegistry()

    expect(call).toHaveBeenCalledWith('skills.search', {
      query: 'demo',
      limit: 20,
      source: 'clawhub',
    })
    expect(registry.registryDiagnostics.value).toEqual([diagnostic])
  })

  it('keeps only the latest ClawHub search response', async () => {
    let finishFirst: ((value: RegistrySearchDataFixture) => void) | undefined
    let finishSecond: ((value: RegistrySearchDataFixture) => void) | undefined
    type RegistrySearchDataFixture = { results: Array<{ name: string }> }
    const first = new Promise<RegistrySearchDataFixture>((resolve) => { finishFirst = resolve })
    const second = new Promise<RegistrySearchDataFixture>((resolve) => { finishSecond = resolve })
    const call = vi.fn()
      .mockReturnValueOnce(first)
      .mockReturnValueOnce(second)
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    registry.registryQuery.value = 'first'
    const firstSearch = registry.searchRegistry()
    registry.registryQuery.value = 'second'
    const secondSearch = registry.searchRegistry()
    finishSecond?.({ results: [{ name: 'Second result' }] })
    await secondSearch
    finishFirst?.({ results: [{ name: 'Stale first result' }] })
    await firstSearch

    expect(registry.registryResults.value.map(result => result.name)).toEqual(['Second result'])
    expect(registry.registryLoading.value).toBe(false)
  })

  it('warns when installation succeeds but the catalog list cannot refresh', async () => {
    const call = vi.fn(async () => ({
      success: true,
      name: 'Development Coding Agent',
      message: 'installed',
    }))
    const loadData = vi.fn(async () => false)
    const registry = useSkillRegistry({ call } as never, loadData)

    await registry.installSkill('development-coding-agent', 'clawhub')

    expect(pushToast).toHaveBeenCalledWith(expect.any(String), { tone: 'warn' })
  })

  it('treats an unresolved envAny group as an incomplete dependency install', async () => {
    const call = vi.fn(async () => ({
      success: true,
      message: 'binary installed',
      missing_still: {
        bins: [],
        env: [],
        env_any: [['OPENROUTER_API_KEY', 'ARK_API_KEY']],
      },
    }))
    const registry = useSkillRegistry({ call } as never, vi.fn(async () => true))

    const outcome = await registry.installDeps('audio-cog', 'ffmpeg')

    expect(outcome.success).toBe(true)
    expect(outcome.complete).toBe(false)
    expect(outcome.missingStill.env_any).toEqual([
      ['OPENROUTER_API_KEY', 'ARK_API_KEY'],
    ])
  })
})
