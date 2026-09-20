import { nextTick, ref, watch } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import i18n, { loadLocaleMessages } from '@/i18n'

import { parseMetaCommandInvocation, useChatSlashCommands, type UseChatSlashCommandsOptions } from './useChatSlashCommands'
import { useChatCompaction } from './useChatCompaction'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import type { RpcCallOptions } from '@/lib/rpc'
import type { MetaRunCenter } from '@/modules/metaRunCenter'
import type { SessionMaintenance } from '@/modules/sessionMaintenance'
import { usageReportingDouble } from '@/testing/usage.test-helper'
import type { MetaSetupReadiness } from '@/types/metaSetup'
import {
  commandCatalogFromTestRpc,
} from '@/testing/conversationAncillary.test-helper'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function harness(
  planModeAvailable: boolean,
  commands: unknown = [],
  ready: Promise<void> = Promise.resolve(),
  catalogCallOptions?: RpcCallOptions,
  extra: Partial<UseChatSlashCommandsOptions> = {},
) {
  const inputText = ref('')
  const call = vi.fn(async (
    _method: string,
    _params?: Record<string, unknown>,
    _options?: RpcCallOptions,
  ): Promise<unknown> => {
    await ready
    return { commands }
  })
  const rpc = {
    ready: vi.fn(() => ready),
    call,
  }
  const metaRunCenter: MetaRunCenter = {
    launch: vi.fn(async input => {
      const raw = await rpc.call('meta.run', input) as Record<string, unknown>
      return {
        ok: raw.ok === true,
        error: typeof raw.error === 'string' ? raw.error : undefined,
        drafted: raw.drafted === true,
        setupRequired: raw.setup_required === true,
        readiness: raw.readiness as MetaSetupReadiness | undefined,
      }
    }),
    listDrafts: vi.fn(async () => ({ drafts: [], durable: true })),
    discardDraft: vi.fn(async () => ({ discarded: true, accepted: false })),
    recover: vi.fn(async () => null),
    confirmPreflight: vi.fn(async () => ({})),
    replay: vi.fn(async () => ({})),
    setupPlan: vi.fn(async () => { throw new Error('Setup planning is not configured') }),
    setupStatus: vi.fn(async () => { throw new Error('Setup polling is not configured') }),
    setupInstall: vi.fn(async () => { throw new Error('Installation is not configured') }),
    subscribe: vi.fn(() => ({ close: vi.fn() })),
  }
  const activatePlanMode = vi.fn(async () => true)
  const codingModeEnabled = ref(false)
  const setCodingModeEnabled = vi.fn(async (enabled: boolean) => {
    codingModeEnabled.value = enabled
    return true
  })
  const dispatchHidden = vi.fn()
  const dispatchPlanPrompt = vi.fn()
  const notify = vi.fn()
  const armGoal = vi.fn(async () => true)
  const startGoal = vi.fn(async () => true)
  const goalStatus = vi.fn(async () => null)
  const goalEdit = vi.fn(async () => true)
  const goalPause = vi.fn(async () => true)
  const goalResume = vi.fn(async () => true)
  const goalClear = vi.fn(async () => true)
  const sessionMaintenance: SessionMaintenance = {
    reset: vi.fn(async command => ({
      key: command.key,
      reset: true as const,
      rotated: true,
      previousSessionId: 'before',
      sessionId: 'after',
      epoch: 1,
    })),
    compact: vi.fn(async command => ({
      key: command.key,
      compactionId: 'cmp-test',
      status: 'started',
      compacted: false,
      applied: false,
      durability: 'none',
      userVisible: true,
    })),
  }
  const api = useChatSlashCommands({
    commandCatalog: commandCatalogFromTestRpc(rpc),
    usageReporting: usageReportingDouble(),
    sessionMaintenance,
    metaRunCenter,
    catalogCallOptions,
    inputText,
    sessionKey: ref('agent:main:webchat:test'),
    autoResizeTextarea: vi.fn(),
    newSession: vi.fn(),
    resetCurrentSession: vi.fn(),
    setCompactInFlight: vi.fn(),
    showCompactStatus: vi.fn(),
    showCompactionToast: vi.fn(),
    notify,
    dispatchHidden,
    dispatchPlanPrompt,
    activatePlanMode,
    planModeAvailable: () => planModeAvailable,
    codingModeEnabled,
    setCodingModeEnabled,
    armGoal,
    startGoal,
    goalStatus,
    goalEdit,
    goalPause,
    goalResume,
    goalClear,
    ...extra,
  })
  return {
    activatePlanMode,
    api,
    armGoal,
    startGoal,
    codingModeEnabled,
    dispatchHidden,
    dispatchPlanPrompt,
    inputText,
    goalStatus,
    goalEdit,
    goalPause,
    goalResume,
    goalClear,
    metaRunCenter,
    notify,
    rpc,
    sessionMaintenance,
    setCodingModeEnabled,
  }
}

describe('useChatSlashCommands compaction lifecycle', () => {
  it('keeps a failed receipt terminal when a second manual compaction starts', async () => {
    const sessionKey = ref('agent:main:webchat:test')
    const compaction = useChatCompaction({
      sessionKey,
      schedulePendingDrainAfterTerminal: vi.fn(),
      popAllPendingIntoComposer: vi.fn(() => true),
    })
    const receipts = new Map<string, string>()
    const stop = watch(compaction.compactStatus, status => {
      if (status.visible && status.compactionId) {
        receipts.set(status.compactionId, status.status)
      }
    }, { flush: 'sync' })
    try {
      compaction.showCompactionToast({
        key: sessionKey.value,
        source: 'manual',
        compaction_id: 'cmp-failed',
        status: 'failed',
      })
      const { api, sessionMaintenance } = harness(false, [], Promise.resolve(), undefined, {
        sessionKey,
        setCompactInFlight: compaction.setCompactInFlight,
        showCompactStatus: compaction.showCompactStatus,
        showCompactionToast: compaction.showCompactionToast,
      })
      api.selectSlashCmd({ name: '/compact', cmd: '/compact', label: 'Compact', desc: '', aliases: [] })

      expect(compaction.compactStatus.value).toMatchObject({
        status: 'started',
        compactionId: '',
        source: 'manual',
      })
      expect(receipts.get('cmp-failed')).toBe('failed')

      await Promise.resolve()
      expect(sessionMaintenance.compact).toHaveBeenCalledWith({ key: sessionKey.value, wait: false })
      expect(receipts).toEqual(new Map([
        ['cmp-failed', 'failed'],
        ['cmp-test', 'started'],
      ]))
      compaction.showCompactionToast({
        key: sessionKey.value,
        source: 'manual',
        compaction_id: 'cmp-test',
        status: 'completed',
      })
      expect(receipts.get('cmp-failed')).toBe('failed')
      expect(receipts.get('cmp-test')).toBe('completed')
      expect(compaction.isCompactInFlightForCurrentSession()).toBe(false)
    } finally {
      stop()
      compaction.cleanup()
    }
  })
})

describe('useChatSlashCommands plan compatibility', () => {
  it('allows command completion with skill tags but blocks direct menu execution', async () => {
    const skill = { name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }
    const selectedSkills = ref([skill])
    const { api, inputText, activatePlanMode, notify } = harness(true, [], Promise.resolve(), undefined, { selectedSkills })
    await api.loadSlashCommands()
    inputText.value = '/pl'
    api.handleSlashInput()
    api.completeSlashCmd(api.filteredSlashCmds.value[0]!)
    expect(inputText.value).toBe('/plan')
    expect(notify).not.toHaveBeenCalled()

    api.handleSlashInput()
    api.activateSlashCmd(api.filteredSlashCmds.value[0]!)
    await Promise.resolve()

    expect(activatePlanMode).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledOnce()
    expect(inputText.value).toBe('/plan')
    expect(selectedSkills.value).toEqual([skill])
  })

  it('requires an explicit -- separator before treating Meta trailing text as a request', () => {
    expect(parseMetaCommandInvocation('meta-paper-write')).toEqual({
      skillName: 'meta-paper-write',
      launchText: '/meta meta-paper-write',
    })
    expect(parseMetaCommandInvocation('meta-paper-write accidental trailing words')).toEqual({
      skillName: 'meta-paper-write',
      launchText: '/meta meta-paper-write',
    })
    expect(parseMetaCommandInvocation('meta-paper-write -- preserve this request')).toEqual({
      skillName: 'meta-paper-write',
      launchText: '/meta meta-paper-write -- preserve this request',
    })
  })

  it('adds and executes /plan when the connected gateway advertises plan mode', async () => {
    const catalogCallOptions: RpcCallOptions = {
      timeoutMs: 2_000,
      timeoutAction: 'reconnect',
      abortAction: 'reconnect',
    }
    const { api, inputText, activatePlanMode, rpc } = harness(
      true,
      [],
      Promise.resolve(),
      catalogCallOptions,
    )
    await api.loadSlashCommands()
    expect(rpc.call).toHaveBeenCalledWith(
      'commands.list_for_surface',
      { surface: 'web_chat' },
      {
        timeoutMs: 2_000,
        signal: undefined,
        timeoutAction: 'reject',
        abortAction: 'reject',
      },
    )
    inputText.value = '/pl'
    api.handleSlashInput()

    expect(api.filteredSlashCmds.value.map(command => command.name)).toEqual(['/plan'])
    api.selectSlashCmd(api.filteredSlashCmds.value[0])
    await Promise.resolve()
    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(inputText.value).toBe('')
  })

  it('does not advertise a synthetic /plan command to an older gateway', async () => {
    const { api, inputText } = harness(false)
    await api.loadSlashCommands()
    inputText.value = '/pl'
    api.handleSlashInput()

    expect(api.filteredSlashCmds.value).toEqual([])
  })

  it('prefers the exact /plan candidate over longer command prefixes', async () => {
    const { api, inputText } = harness(true, [{
      name: '/planning',
      description: 'A different command',
      aliases: [],
      execution: { action: 'plans.setMode' },
    }])
    await api.loadSlashCommands()
    inputText.value = '/plan'
    api.handleSlashInput()

    expect(api.filteredSlashCmds.value.map(command => command.name)).toEqual(['/plan', '/planning'])
  })

  it('does not inject a duplicate when the gateway exposes /plan as an alias', async () => {
    const { api, inputText } = harness(true, [{
      name: '/planning',
      description: 'Enter Plan mode',
      aliases: ['/plan'],
      execution: { action: 'plans.setMode' },
    }])
    await api.loadSlashCommands()
    inputText.value = '/plan'
    api.handleSlashInput()

    expect(api.filteredSlashCmds.value).toHaveLength(1)
    expect(api.filteredSlashCmds.value[0].name).toBe('/planning')
  })

  it('recomputes candidates when the command catalog arrives after the input', async () => {
    const connection = deferred()
    const { api, inputText } = harness(true, [], connection.promise)
    const loading = api.loadSlashCommands()
    inputText.value = '/plan'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value).toEqual([])

    connection.resolve()
    await loading

    expect(api.filteredSlashCmds.value.map(command => command.name)).toEqual(['/plan'])
  })

  it('activates Plan mode before dispatching an optional Plan prompt', async () => {
    const {
      activatePlanMode,
      api,
      dispatchHidden,
      dispatchPlanPrompt,
      inputText,
    } = harness(true)
    inputText.value = '/plan inspect the logging flow'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(dispatchPlanPrompt).toHaveBeenCalledWith(
      'inspect the logging flow',
      '/plan inspect the logging flow',
    )
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/plan inspect the logging flow')
  })

  it('preserves the command when Plan mode cannot be activated', async () => {
    const {
      activatePlanMode,
      api,
      dispatchHidden,
      dispatchPlanPrompt,
      inputText,
    } = harness(true)
    activatePlanMode.mockResolvedValueOnce(false)
    await api.loadSlashCommands()
    inputText.value = '/plan'
    api.handleSlashInput()

    api.selectSlashCmd(api.filteredSlashCmds.value[0])
    await Promise.resolve()

    expect(inputText.value).toBe('/plan')
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(dispatchPlanPrompt).not.toHaveBeenCalled()
  })
})

describe('useChatSlashCommands meta requests', () => {
  const metaCommand = {
    name: '/meta',
    description: 'Run a meta-skill.',
    aliases: [],
    execution: { action: 'meta.menu' },
    argument_choices: [
      { value: 'meta-skill-creator', description: 'Create a meta-skill.' },
    ],
  }

  it('keeps the concrete request after the explicit Meta request separator', async () => {
    const { api, dispatchHidden, rpc } = harness(false, [metaCommand])
    rpc.call.mockImplementation(async (method: string) => {
      if (method === 'commands.list_for_surface') return { commands: [metaCommand] }
      if (method === 'meta.run') return { ok: true }
      return {}
    })

    await api.executeSlashCommand(
      '/meta meta-skill-creator -- create a competitor research meta-skill',
    )
    await vi.waitFor(() => expect(dispatchHidden).toHaveBeenCalledOnce())

    expect(rpc.call).toHaveBeenCalledWith('meta.run', expect.objectContaining({
      name: 'meta-skill-creator',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: expect.any(String),
      launchText: '/meta meta-skill-creator -- create a competitor research meta-skill',
    }))
    expect(dispatchHidden).toHaveBeenCalledWith(
      '/meta meta-skill-creator -- create a competitor research meta-skill',
      '/meta meta-skill-creator -- create a competitor research meta-skill',
      expect.any(String),
      'agent:main:webchat:test',
    )
  })
})

describe('unified Meta request collector', () => {
  const metaCommand = {
    name: '/meta',
    description: 'Run a workflow.',
    aliases: [],
    execution: { action: 'meta.menu' },
    argument_choices: [{ value: 'meta-research', description: 'Research a topic.' }],
  }
  const selectedSkill = { name: 'tables', instanceId: 'skill:tables', digest: 'a'.repeat(64) }

  async function collector(extra: Partial<UseChatSlashCommandsOptions> = {}) {
    const result = harness(false, [metaCommand], Promise.resolve(), undefined, extra)
    await result.api.loadSlashCommands()
    result.inputText.value = 'Compare products /meta-research'
    result.api.handleSlashInput()
    const choice = result.api.filteredSlashCmds.value.find(item => item.kind === 'meta')!
    expect(choice?.argValue).toBe('meta-research')
    return { ...result, choice }
  }

  it('opens a concrete workflow request without sending or changing the composer', async () => {
    const original = 'Compare /meta-research two products'
    const { api, inputText, metaRunCenter, dispatchHidden } = await collector({
      getCaret: () => original.indexOf(' two'),
    })
    inputText.value = original
    api.handleSlashInput()
    api.activateSlashCmd(api.filteredSlashCmds.value.find(item => item.kind === 'meta')!)

    expect(api.metaDraft.value).toEqual({
      name: 'meta-research',
      label: 'meta-research',
      text: 'Compare  two products',
      originalText: original,
      sessionKey: 'agent:main:webchat:test',
    })
    expect(inputText.value).toBe(original)
    expect(api.slashOpen.value).toBe(false)
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
    expect(dispatchHidden).not.toHaveBeenCalled()

    api.metaDraft.value = null
    expect(inputText.value).toBe(original)
  })

  it.each(['attachments', 'skill tags'])('blocks workflow selection with %s', async kind => {
    const selectedSkills = ref<SelectedSkillRef[]>(kind === 'skill tags' ? [selectedSkill] : [])
    const { api, inputText, choice, metaRunCenter, dispatchHidden, notify } = await collector({
      hasNonTextInput: () => kind === 'attachments',
      selectedSkills,
    })
    const original = inputText.value

    api.activateSlashCmd(choice)

    expect(api.metaDraft.value).toBeNull()
    expect(inputText.value).toBe(original)
    expect(selectedSkills.value).toEqual(kind === 'skill tags' ? [selectedSkill] : [])
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledOnce()
  })

  it('keeps edited workflow text through reconnect and catalog invalidation', async () => {
    const { api, choice, inputText, metaRunCenter } = await collector()
    api.activateSlashCmd(choice)
    api.metaDraft.value!.text = 'Compare battery life\nand price'
    const original = inputText.value

    api.invalidateSkillCandidates()
    api.invalidateSkillCandidates()

    expect(api.metaDraft.value?.text).toBe('Compare battery life\nand price')
    expect(inputText.value).toBe(original)
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
  })

  it('restores edited workflow text only to its source session when navigating away', async () => {
    const sessionKey = ref('agent:main:webchat:test')
    const restoreDraft = vi.fn()
    const { api, choice, inputText, metaRunCenter } = await collector({ sessionKey, restoreDraft })
    api.activateSlashCmd(choice)
    api.metaDraft.value!.text = 'Compare battery life\nand price'

    sessionKey.value = 'agent:main:webchat:other'
    inputText.value = 'Unrelated draft in another session'
    api.invalidateSkillCandidates()
    await nextTick()

    expect(restoreDraft).toHaveBeenCalledExactlyOnceWith(
      '/meta meta-research -- Compare battery life\nand price',
      'agent:main:webchat:test',
    )
    expect(api.metaDraft.value).toBeNull()
    expect(inputText.value).toBe('Unrelated draft in another session')
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
  })

  it.each(['attachments', 'skill tags'])('rechecks %s added after the request dialog opens', async kind => {
    const hasAttachment = ref(false)
    const selectedSkills = ref<SelectedSkillRef[]>([])
    const { api, inputText, choice, metaRunCenter, notify } = await collector({
      hasNonTextInput: () => hasAttachment.value,
      selectedSkills,
    })
    api.activateSlashCmd(choice)
    const draft = { ...api.metaDraft.value! }
    const original = inputText.value
    if (kind === 'attachments') hasAttachment.value = true
    else selectedSkills.value = [selectedSkill]

    await api.launchMetaDraft()

    expect(api.metaDraft.value).toEqual(draft)
    expect(inputText.value).toBe(original)
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledOnce()
  })

  it('submits edited request text through the existing launch with one stable identity', async () => {
    const { api, inputText, choice, metaRunCenter, dispatchHidden } = await collector()
    vi.mocked(metaRunCenter.launch).mockResolvedValue({ ok: true })
    api.activateSlashCmd(choice)
    api.metaDraft.value!.text = '  Compare battery life\nand price  '

    await api.launchMetaDraft()

    expect(metaRunCenter.launch).toHaveBeenCalledExactlyOnceWith({
      name: 'meta-research',
      sessionKey: 'agent:main:webchat:test',
      clientRequestId: expect.any(String),
      launchText: '/meta meta-research -- Compare battery life\nand price',
    })
    const request = vi.mocked(metaRunCenter.launch).mock.calls[0]![0]
    expect(dispatchHidden).toHaveBeenCalledExactlyOnceWith(
      request.launchText, request.launchText, request.clientRequestId, request.sessionKey,
    )
    expect(api.metaDraft.value).toBeNull()
    expect(inputText.value).toBe('')
  })

  it('keeps newer composer input while the workflow launch is pending', async () => {
    const launch = deferred()
    const { api, inputText, choice, metaRunCenter, dispatchHidden } = await collector()
    vi.mocked(metaRunCenter.launch).mockImplementation(async () => {
      await launch.promise
      return { ok: true }
    })
    api.activateSlashCmd(choice)
    const sending = api.launchMetaDraft()
    inputText.value = 'A separate next request'
    launch.resolve()
    await sending

    expect(dispatchHidden).toHaveBeenCalledOnce()
    expect(inputText.value).toBe('A separate next request')
  })

  it('passes setup and durable recovery the exact same request identity', async () => {
    const readiness: MetaSetupReadiness = {
      ready: false,
      status: 'needs_setup',
      missing_bins: ['research-tool'],
    }
    const requestMetaSetup = vi.fn(async () => 'visible' as const)
    const { api, inputText, choice, metaRunCenter, dispatchHidden } = await collector({ requestMetaSetup })
    vi.mocked(metaRunCenter.launch)
      .mockResolvedValueOnce({ ok: false, setupRequired: true, readiness })
      .mockResolvedValueOnce({ ok: true })
    api.activateSlashCmd(choice)

    await api.launchMetaDraft()

    const request = vi.mocked(metaRunCenter.launch).mock.calls[0]![0]
    expect(requestMetaSetup).toHaveBeenCalledExactlyOnceWith(
      request.name, readiness, request.sessionKey, request.launchText, request.clientRequestId,
    )
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(inputText.value).toBe('')

    await expect(api.restoreDurableMetaDrafts([{
      ...request,
      clientRequestId: request.clientRequestId!,
      launchText: request.launchText!,
      createdAt: 1,
      expiresAt: 2,
      sessionExists: true,
    }])).resolves.toEqual([request.clientRequestId])
    expect(metaRunCenter.launch).toHaveBeenLastCalledWith(request)
    expect(dispatchHidden).toHaveBeenCalledExactlyOnceWith(
      request.launchText, request.launchText, request.clientRequestId, request.sessionKey,
    )
  })

  it('keeps a failed staged launch on the existing retry path', async () => {
    const requestMetaSetup = vi.fn(async () => 'visible' as const)
    const restoreDraft = vi.fn()
    const { api, choice, metaRunCenter, dispatchHidden } = await collector({ requestMetaSetup, restoreDraft })
    vi.mocked(metaRunCenter.launch).mockResolvedValue({
      ok: false,
      drafted: true,
      error: 'Setup interrupted',
    })
    api.activateSlashCmd(choice)

    await api.launchMetaDraft()

    const request = vi.mocked(metaRunCenter.launch).mock.calls[0]![0]
    expect(requestMetaSetup).toHaveBeenCalledExactlyOnceWith(
      request.name,
      expect.objectContaining({ status: 'needs_setup', reasons: ['Setup interrupted'] }),
      request.sessionKey,
      request.launchText,
      request.clientRequestId,
    )
    expect(dispatchHidden).not.toHaveBeenCalled()
    expect(restoreDraft).not.toHaveBeenCalled()
  })

  it('does not launch an empty request or one collected for another session', async () => {
    const sessionKey = ref('agent:main:webchat:test')
    const { api, choice, metaRunCenter } = await collector({ sessionKey })
    api.activateSlashCmd(choice)
    api.metaDraft.value!.text = '  '
    await api.launchMetaDraft()
    expect(metaRunCenter.launch).not.toHaveBeenCalled()

    api.metaDraft.value!.text = 'Compare products'
    sessionKey.value = 'agent:main:webchat:other'
    await api.launchMetaDraft()
    expect(metaRunCenter.launch).not.toHaveBeenCalled()
  })
})

describe('useChatSlashCommands Coding mode', () => {
  const codingCommand = {
    name: '/coding',
    description: 'Turn Coding mode on or off.',
    aliases: [],
    execution: { action: 'coding.mode' },
  }

  it('toggles Coding mode when /coding is entered without arguments', async () => {
    const {
      api,
      codingModeEnabled,
      inputText,
      setCodingModeEnabled,
    } = harness(false, [codingCommand])
    inputText.value = '/coding'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(setCodingModeEnabled).toHaveBeenCalledWith(true)
    expect(inputText.value).toBe('')

    codingModeEnabled.value = true
    await api.executeSlashCommand('/coding')
    await Promise.resolve()
    expect(setCodingModeEnabled).toHaveBeenLastCalledWith(false)
  })

  it('keeps explicit on, off, and status arguments compatible without advertising them', async () => {
    const {
      api,
      codingModeEnabled,
      inputText,
      setCodingModeEnabled,
    } = harness(false, [codingCommand])

    await api.loadSlashCommands()
    inputText.value = '/coding '
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value).toEqual([])

    await api.executeSlashCommand('/coding on')
    await Promise.resolve()
    expect(setCodingModeEnabled).toHaveBeenCalledWith(true)

    await api.executeSlashCommand('/coding off')
    await Promise.resolve()
    expect(setCodingModeEnabled).toHaveBeenCalledWith(false)

    codingModeEnabled.value = true
    await api.executeSlashCommand('/coding status')
    expect(setCodingModeEnabled).toHaveBeenCalledTimes(2)
    expect(inputText.value).toBe('')
  })

  it('describes the next /coding action from the current global state', async () => {
    const { api, codingModeEnabled, inputText } = harness(false, [codingCommand])
    await api.loadSlashCommands()
    inputText.value = '/coding'

    api.handleSlashInput()
    expect(api.filteredSlashCmds.value[0].desc).toBe('Enable Coding mode.')

    codingModeEnabled.value = true
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value[0].desc).toBe('Disable Coding mode.')
  })

  it('completes a partial /coding candidate without toggling the mode', async () => {
    const {
      api,
      inputText,
      setCodingModeEnabled,
    } = harness(false, [codingCommand])
    await api.loadSlashCommands()
    inputText.value = '/co'
    api.handleSlashInput()
    const candidate = api.filteredSlashCmds.value[0]

    api.activateSlashCmd(candidate)

    expect(inputText.value).toBe('/coding')
    expect(setCodingModeEnabled).not.toHaveBeenCalled()
    expect(api.slashOpen.value).toBe(false)
  })

  it('executes an exact /coding candidate only after completion', async () => {
    const {
      api,
      inputText,
      setCodingModeEnabled,
    } = harness(false, [codingCommand])
    await api.loadSlashCommands()
    inputText.value = '/coding'
    api.handleSlashInput()

    api.activateSlashCmd(api.filteredSlashCmds.value[0])
    await Promise.resolve()

    expect(setCodingModeEnabled).toHaveBeenCalledWith(true)
    expect(inputText.value).toBe('')
  })
})

describe('useChatSlashCommands recovery', () => {
  it.each([
    {},
    { commands: null },
    { commands: [{}] },
    { commands: [null] },
    {
      commands: [{
        name: '/goal',
        aliases: [{}],
        execution: { action: 'goal.set' },
      }],
    },
    { commands: [{ name: '/foo', aliases: [], execution: {} }] },
    {
      commands: [{
        name: '/reset',
        aliases: [],
        execution: { action: 'unsupported.action' },
      }],
    },
  ])('keeps a malformed command catalog unavailable', async response => {
    const { api, armGoal, notify, rpc } = harness(false)
    rpc.call.mockResolvedValue(response)

    await expect(api.classifySlashCommand('/goal')).resolves.toBe('unavailable')
    await expect(api.executeSlashCommand('/goal')).resolves.toBe(true)

    expect(armGoal).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledOnce()
  })

  it('keeps a legacy supported command without execution metadata registered', async () => {
    const { api, sessionMaintenance } = harness(false, [{ name: '/reset', aliases: [] }])

    await expect(api.classifySlashCommand('/reset')).resolves.toBe('registered')
    await expect(api.executeSlashCommand('/reset', 'registered')).resolves.toBe(true)

    expect(sessionMaintenance.reset).toHaveBeenCalledWith({
      key: 'agent:main:webchat:test',
    })
  })

  it('executes a legacy Goal command without execution metadata', async () => {
    const { api, armGoal, goalStatus } = harness(false, [{
      name: '/goal',
      aliases: [],
    }])

    await expect(api.classifySlashCommand('/goal')).resolves.toBe('registered')
    await expect(api.executeSlashCommand('/goal', 'registered')).resolves.toBe(true)
    await Promise.resolve()

    expect(goalStatus).toHaveBeenCalledTimes(1)
    expect(armGoal).toHaveBeenCalledTimes(1)
  })

  it('falls through silently for unknown slash input', async () => {
    const {
      api,
      inputText,
      notify,
    } = harness(false)
    inputText.value = '/gamemode creative'

    const handled = await api.executeSlashCommand(inputText.value)

    expect(handled).toBe(false)
    expect(notify).not.toHaveBeenCalled()
    await expect(api.classifySlashCommand(inputText.value)).resolves.toBe('unknown')
  })

  it('still executes a registered slash command as a command', async () => {
    const goalCommand = {
      name: '/goal',
      cmd: '/goal',
      label: '/goal',
      desc: 'Set a long-running goal for the agent to pursue.',
      aliases: [],
      execution: { action: 'goal.set' },
    }
    const { api, inputText, armGoal } = harness(false, [goalCommand])
    inputText.value = '/goal'

    const handled = await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    // Registered commands stay handled: they run as commands, not messages.
    expect(handled).toBe(true)
    expect(armGoal).toHaveBeenCalledTimes(1)
  })
})

describe('useChatSlashCommands goal', () => {
  const goalCommand = {
    name: '/goal',
    cmd: '/goal',
    label: '/goal',
    desc: 'Set a long-running goal for the agent to pursue.',
    aliases: [],
    execution: { action: 'goal.set' },
  }

  it('keeps menu selection as a Goal composer shortcut', async () => {
    const { api, inputText, armGoal } = harness(false, [goalCommand])
    inputText.value = '/go'

    api.completeSlashCmd(goalCommand)
    await Promise.resolve()

    expect(armGoal).toHaveBeenCalledTimes(1)
    expect(inputText.value).toBe('')
  })

  it('preserves the slash draft when Goal mode cannot be armed', async () => {
    const { api, inputText, armGoal } = harness(false, [goalCommand])
    armGoal.mockResolvedValueOnce(false)
    inputText.value = '/go'

    api.completeSlashCmd(goalCommand)
    await Promise.resolve()

    expect(armGoal).toHaveBeenCalledTimes(1)
    expect(inputText.value).toBe('/go')
  })

  it('starts a fully specified /goal command immediately', async () => {
    const { api, inputText, armGoal, startGoal, rpc } = harness(false, [goalCommand])
    inputText.value = '/goal 完成迁移文档'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(startGoal).toHaveBeenCalledWith('完成迁移文档')
    expect(armGoal).not.toHaveBeenCalled()
    expect(inputText.value).toBe('')
    expect(rpc.call).not.toHaveBeenCalledWith('goals.set', expect.anything())
  })

  it('accepts the explicit /goal set spelling without keeping set in the objective', async () => {
    const { api, inputText, armGoal, startGoal } = harness(false, [goalCommand])
    inputText.value = '/goal set 完成迁移文档'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(startGoal).toHaveBeenCalledWith('完成迁移文档')
    expect(armGoal).not.toHaveBeenCalled()
    expect(inputText.value).toBe('')
  })

  it('arms Goal mode when bare /goal has no current Goal', async () => {
    const { api, inputText, armGoal, goalStatus, notify } = harness(false, [goalCommand])
    inputText.value = '/goal'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(goalStatus).toHaveBeenCalledTimes(1)
    expect(armGoal).toHaveBeenCalledTimes(1)
    expect(notify).not.toHaveBeenCalled()
    expect(inputText.value).toBe('')
  })

  it('reports the current Goal instead of arming a replacement for bare /goal', async () => {
    const { api, inputText, armGoal, goalStatus, notify } = harness(false, [goalCommand])
    goalStatus.mockResolvedValue({
      objective: '完成迁移文档',
      status: 'active',
      turnsSettled: 3,
    } as never)
    inputText.value = '/goal'

    await api.executeSlashCommand(inputText.value)
    await Promise.resolve()

    expect(goalStatus).toHaveBeenCalledTimes(1)
    expect(armGoal).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('active'))
  })

  it('reports the active goal for /goal status', async () => {
    const { api, inputText, notify, goalStatus } = harness(false, [goalCommand])
    goalStatus.mockResolvedValue({
      objective: '完成迁移文档',
      status: 'active',
      turnsSettled: 3,
    } as never)
    inputText.value = '/goal status'

    await api.executeSlashCommand(inputText.value)

    expect(goalStatus).toHaveBeenCalledTimes(1)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('active'))
  })

  it('clears the active goal for /goal clear', async () => {
    const { api, inputText, notify, goalClear } = harness(false, [goalCommand])
    inputText.value = '/goal clear'

    await api.executeSlashCommand(inputText.value)

    expect(goalClear).toHaveBeenCalledTimes(1)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('cleared'))
  })

  it('pauses and resumes via /goal pause and /goal resume', async () => {
    const { api, inputText, notify, goalPause, goalResume } = harness(false, [goalCommand])
    inputText.value = '/goal pause'
    await api.executeSlashCommand(inputText.value)
    expect(goalPause).toHaveBeenCalledTimes(1)

    inputText.value = '/goal resume'
    await api.executeSlashCommand(inputText.value)
    expect(goalResume).toHaveBeenCalledTimes(1)
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('resumed'))
  })

  it('edits the current Goal through the authoritative Goal composable', async () => {
    const { api, inputText, goalEdit, notify } = harness(false, [goalCommand])
    inputText.value = '/goal edit 更新迁移目标'

    await api.executeSlashCommand(inputText.value)

    expect(goalEdit).toHaveBeenCalledWith('更新迁移目标')
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('next safe boundary'))
  })
})

describe('unified skill palette', () => {
  const candidate = { name: 'xlsx', instanceId: 'skill:tables', digest: 'a'.repeat(64), generation: 1,
    description: 'Create spreadsheets', descriptionZh: '创建表格', aliases: ['spreadsheet'],
    kind: 'skill' as const, source: 'workspace' as const, disabled: false, manualOnly: false, ready: true }
  function skills(extra: Partial<UseChatSlashCommandsOptions> = {}) {
    const selectedSkills = ref<SelectedSkillRef[]>([])
    const listCandidates = vi.fn(async () => ({ generation: 1, candidates: [candidate] }))
    const skillCatalog = { supportsCandidates: () => true, listCandidates } as unknown as SkillCatalog
    return { ...harness(false, [], Promise.resolve(), undefined, { skillCatalog, selectedSkills, ...extra }), selectedSkills, listCandidates }
  }
  it('omits reset and usage from the menu while keeping all skills and workflows browseable', async () => {
    const candidates = ['pdf-toolkit', 'github', 'docx', 'html-coder', 'pptx', 'xlsx', 'custom-skill']
      .map(name => ({ ...candidate, name, instanceId: `skill:${name}`,
        description: 'A brief purpose. Later details contain unique-search-term.' }))
    const skillCatalog = {
      supportsCandidates: () => true,
      listCandidates: vi.fn(async () => ({ generation: 1, candidates })),
    } as unknown as SkillCatalog
    const commands = ['/usage', '/goal', '/new', '/coding', '/compact', '/reset'].map(name => ({ name, aliases: [] }))
    const { api, inputText } = harness(false, [...commands, {
      name: '/meta', aliases: [], execution: { action: 'meta.menu' },
      argument_choices: ['meta-paper-write', 'meta-skill-creator', 'meta-short-drama', 'AwesomeWebpageMetaSkill']
        .map(value => ({ value, description: 'A complete workflow description.' })),
    }], Promise.resolve(), undefined, { skillCatalog })
    await api.loadSlashCommands()
    inputText.value = '/'
    api.handleSlashInput()
    await Promise.resolve()

    expect(api.filteredSlashCmds.value.map(item => item.name)).toEqual([
      '/goal', '/new', '/coding', '/compact', '/meta',
      'pdf-toolkit', 'github', 'docx', 'html-coder', 'pptx', 'xlsx', 'custom-skill',
      '/meta meta-paper-write', '/meta meta-skill-creator', '/meta meta-short-drama',
      '/meta AwesomeWebpageMetaSkill',
    ])
    expect(api.filteredSlashCmds.value.map(item => item.kind)).toEqual([
      ...commands.filter(command => !['/usage', '/reset'].includes(command.name)).map(() => 'command'), 'command',
      ...candidates.map(() => 'skill'),
      'meta', 'meta', 'meta', 'meta',
    ])
    expect(api.filteredSlashCmds.value.find(item => item.name === 'custom-skill')?.desc).toBe('A brief purpose.')

    for (const name of ['/usage', '/reset']) {
      inputText.value = name
      api.handleSlashInput()
      expect(api.filteredSlashCmds.value.filter(item => item.kind === 'command')).toEqual([])
      await expect(api.classifySlashCommand(name)).resolves.toBe('registered')
    }
    inputText.value = '/unique-search-term'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value).toHaveLength(candidates.length)
    expect(api.filteredSlashCmds.value.find(item => item.name === 'custom-skill')?.desc).toBe('A brief purpose.')
    expect(skillCatalog.listCandidates).toHaveBeenCalledOnce()
  })

  it('preserves catalog skill order without promoting built-ins or hiding later entries', async () => {
    const candidates = ['custom-first', 'github', 'docx', 'custom-last'].map(name => ({ ...candidate, name }))
    const { api, inputText } = skills({ skillCatalog: {
      supportsCandidates: () => true,
      listCandidates: vi.fn(async () => ({ generation: 1, candidates })),
    } as unknown as SkillCatalog })
    inputText.value = '/'
    api.handleSlashInput()
    await Promise.resolve()
    expect(api.filteredSlashCmds.value.map(item => item.name)).toEqual(['custom-first', 'github', 'docx', 'custom-last'])
    inputText.value = '/custom'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value.map(item => item.name)).toEqual(['custom-first', 'custom-last'])
    inputText.value = '/'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value.map(item => item.name)).toEqual(['custom-first', 'github', 'docx', 'custom-last'])
  })

  it('uses maintained Chinese product copy while preserving bilingual search', async () => {
    const previousLocale = i18n.global.locale.value
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    try {
      const { api, inputText } = harness(false, ['/new', '/coding', '/compact'].map(name => ({ name, aliases: [] })),
        Promise.resolve(), undefined, { skillCatalog: {
          supportsCandidates: () => true,
          listCandidates: vi.fn(async () => ({ generation: 1, candidates: [{ ...candidate, descriptionZh: 'Use an implementation package and lengthy trigger rules.' }] })),
        } as unknown as SkillCatalog })
      await api.loadSlashCommands()
      inputText.value = '/'
      api.handleSlashInput()
      await Promise.resolve()
      expect(api.filteredSlashCmds.value.slice(0, 3).map(item => item.desc)).toEqual(['新建聊天', '开启编程模式', '压缩当前对话上下文'])
      expect(api.filteredSlashCmds.value.find(item => item.name === 'xlsx')).toMatchObject({ label: 'Excel 表格', desc: '创建、编辑与分析电子表格' })
      inputText.value = '/EXCEL'
      api.handleSlashInput()
      expect(api.filteredSlashCmds.value[0]?.name).toBe('xlsx')
    } finally {
      i18n.global.locale.value = previousLocale
    }
  })
  it('loads lazily, searches Chinese and English locally, and only replaces the query', async () => {
    const { api, inputText, selectedSkills, listCandidates } = skills()
    await api.loadSlashCommands()
    expect(listCandidates).not.toHaveBeenCalled()
    inputText.value = '分析 /表格'
    api.handleSlashInput()
    await Promise.resolve()
    expect(api.filteredSlashCmds.value[0]?.name).toBe('xlsx')
    inputText.value = '分析 /SPREADSHEET'
    api.handleSlashInput()
    expect(listCandidates).toHaveBeenCalledOnce()
    api.completeSlashCmd(api.filteredSlashCmds.value[0]!)
    expect(inputText.value).toBe('分析 ')
    expect(selectedSkills.value).toEqual([{ name: candidate.name, instanceId: candidate.instanceId, digest: candidate.digest }])
    expect(api.slashOpen.value).toBe(false)
  })
  it('does not turn disabled candidates into selected skills', async () => {
    const manageSkill = vi.fn()
    const { api, inputText, selectedSkills } = skills({ manageSkill })
    inputText.value = '/xlsx'
    api.handleSlashInput()
    await Promise.resolve()
    const item = api.filteredSlashCmds.value[0]!
    api.completeSlashCmd({ ...item, skill: { ...candidate, disabled: true } })
    expect(manageSkill).toHaveBeenCalledWith('xlsx')
    expect(selectedSkills.value).toEqual([])
    expect(inputText.value).toBe('/xlsx')
  })
  it('refreshes external directory changes on reopening but keeps typing local', async () => {
    const { api, inputText, listCandidates } = skills()
    inputText.value = '/'
    api.handleSlashInput()
    await Promise.resolve()
    expect(api.filteredSlashCmds.value.some(item => item.name === 'xlsx')).toBe(true)
    inputText.value = '/spread'
    api.handleSlashInput()
    expect(listCandidates).toHaveBeenCalledOnce()
    api.closeSlashMenu()
    listCandidates.mockResolvedValueOnce({ generation: 2, candidates: [] })
    inputText.value = '/'
    api.handleSlashInput()
    await Promise.resolve()
    expect(listCandidates).toHaveBeenCalledTimes(2)
    expect(api.filteredSlashCmds.value.some(item => item.name === 'xlsx')).toBe(false)
  })
  it('keeps an empty search visible and refreshes candidates after invalidation', async () => {
    const { api, inputText, listCandidates } = skills()
    inputText.value = '/no-match'
    api.handleSlashInput()
    await Promise.resolve()
    expect(api.slashOpen.value).toBe(true)
    expect(api.filteredSlashCmds.value).toEqual([])
    api.invalidateSkillCandidates()
    api.handleSlashInput()
    await Promise.resolve()
    expect(listCandidates).toHaveBeenCalledTimes(2)
  })
  it('keeps ordinary commands available when the gateway does not support skills', async () => {
    const skillCatalog = { supportsCandidates: () => false } as SkillCatalog
    const { api, inputText } = harness(true, [{ name: '/new', aliases: [] }], Promise.resolve(), undefined, { skillCatalog })
    await api.loadSlashCommands()
    inputText.value = '/'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value.some(item => item.name === '/new')).toBe(true)
    expect(api.skillsError.value).not.toBe('')
  })
  it('shows usage in the UI and sends new-chat to the new session action', async () => {
    const newSession = vi.fn()
    const { api, notify, sessionMaintenance } = harness(false, [
      { name: '/new', aliases: [], execution: { action: 'new_chat' } },
      { name: '/usage', aliases: [], execution: { action: 'usage.status' } },
    ], Promise.resolve(), undefined, { newSession })
    await api.loadSlashCommands()
    await api.executeSlashCommand('/new')
    expect(newSession).toHaveBeenCalledOnce()
    expect(sessionMaintenance.reset).not.toHaveBeenCalled()
    await api.executeSlashCommand('/usage')
    await Promise.resolve()
    expect(notify).toHaveBeenCalled()
  })
})
