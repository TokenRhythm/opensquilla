import i18n, { loadLocaleMessages } from '@/i18n'
import { nextTick, ref, watch } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSlashCommands, type UseChatSlashCommandsOptions } from './useChatSlashCommands'
import { useChatCompaction } from './useChatCompaction'
import { useLocalPathDraft } from './useLocalPathDraft'
import type { SkillCatalog } from '@/modules/skillCatalog'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import type { RpcCallOptions } from '@/lib/rpc'
import type { SessionMaintenance } from '@/modules/sessionMaintenance'
import { usageReportingDouble } from '@/testing/usage.test-helper'
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

const initCatalogCommand = {
  name: '/init', aliases: [], category: 'turn', busy_policy: 'next_turn', presentation: 'turn',
  execution: { kind: 'local', action: 'project.init' },
}

describe('project initialization command intent', () => {
  it('requires catalog support and resolves only an explicitly typed init command', async () => {
    const { api } = harness(false, [initCatalogCommand])
    await expect(api.classifySlashCommand('/init keep existing sections')).resolves.toBe('registered')
    expect(api.resolveTurnCommand('/init keep existing sections')).toEqual({ name: 'project.init', args: 'keep existing sections' })
    expect(api.resolveTurnCommand('//init')).toBeNull()
    expect(api.resolveTurnCommand('Please explain /init')).toBeNull()
    expect(api.resolveTurnCommand('> /init')).toBeNull()
  })

  it('keeps init unavailable on an older Gateway instead of sending ordinary text', async () => {
    const { api, inputText, notify } = harness(false)
    inputText.value = '/init'
    await expect(api.classifySlashCommand('/init')).resolves.toBe('unavailable')
    await expect(api.executeSlashCommand('/init')).resolves.toBe(true)
    expect(notify).toHaveBeenCalledWith(i18n.global.t('chat.slashCommands.initUnavailable'))
    expect(inputText.value).toBe('/init')
  })

  it('does not treat an init action without ordinary-turn scheduling evidence as supported', async () => {
    const { api } = harness(false, [{ ...initCatalogCommand, category: undefined }])
    await expect(api.supportsTurnCommand({ name: 'project.init', args: '' })).resolves.toBe(false)
  })

  it('invalidates command support when the Gateway scope changes', async () => {
    const scope = ref('profile-a')
    const { api, rpc } = harness(false, [initCatalogCommand], Promise.resolve(), undefined, { catalogScope: () => scope.value })
    await api.loadSlashCommands()
    expect(api.resolveTurnCommand('/init')).not.toBeNull()
    scope.value = 'profile-b'
    await nextTick()
    expect(api.resolveTurnCommand('/init')).toBeNull()
    rpc.call.mockResolvedValue({ commands: [] })
    await expect(api.supportsTurnCommand({ name: 'project.init', args: '' })).resolves.toBe(false)
  })

  it('reloads an invalidated catalog when the palette reopens and shares the pending request', async () => {
    const scope = ref('epoch-1')
    const { api, rpc, inputText } = harness(true, [initCatalogCommand, { name: '/compact', aliases: [] }],
      Promise.resolve(), undefined, { catalogScope: () => scope.value })
    await api.loadSlashCommands()
    inputText.value = '/'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value.map(command => command.cmd)).toEqual(['/init', '/compact', '/plan'])
    scope.value = 'epoch-2'
    const ready = deferred()
    rpc.call.mockImplementationOnce(async () => { await ready.promise; return { commands: [initCatalogCommand] } })
    api.handleSlashInput()
    expect(rpc.call).toHaveBeenCalledTimes(2)
    const supported = api.supportsTurnCommand({ name: 'project.init', args: '' })
    inputText.value = '/in'
    api.handleSlashInput()
    expect(rpc.call).toHaveBeenCalledTimes(2)
    ready.resolve()
    await expect(supported).resolves.toBe(true)
    expect(inputText.value).toBe('/in')
    expect(api.filteredSlashCmds.value.map(command => command.cmd)).toEqual(['/init'])
  })

  it('discards a late catalog from the previous scope', async () => {
    const scope = ref('epoch-1')
    const ready = deferred()
    const { api, rpc } = harness(false, [initCatalogCommand], ready.promise, undefined, { catalogScope: () => scope.value })
    const previous = api.loadSlashCommands()
    scope.value = 'epoch-2'
    rpc.call.mockResolvedValue({ commands: [] })
    await api.loadSlashCommands()
    ready.resolve()
    await previous
    expect(api.resolveTurnCommand('/init')).toBeNull()
    await expect(api.supportsTurnCommand({ name: 'project.init', args: '' })).resolves.toBe(false)
  })

  it('retries a failed catalog when the palette is opened again', async () => {
    const { api, rpc, inputText } = harness(false, [initCatalogCommand])
    rpc.call.mockRejectedValueOnce(new Error('temporarily unavailable'))
    await api.loadSlashCommands()
    inputText.value = '/'
    api.handleSlashInput()
    await vi.waitFor(() => expect(api.filteredSlashCmds.value.some(command => command.cmd === '/init')).toBe(true))
    expect(rpc.call).toHaveBeenCalledTimes(2)
  })
})

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
  const activatePlanMode = vi.fn(async () => true)
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
    activatePlanMode,
    planModeAvailable: () => planModeAvailable,
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
    inputText,
    goalStatus,
    goalEdit,
    goalPause,
    goalResume,
    goalClear,
    notify,
    rpc,
    sessionMaintenance,
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

describe('local side chat command palette', () => {
  it('has one primary entry searchable by its alias before the remote catalog loads', async () => {
    const run = vi.fn(async () => true)
    const ready = deferred()
    const { api, inputText, rpc } = harness(false, [], ready.promise, undefined, { sideChat: { run } })
    for (const query of ['/si', '/btw']) {
      inputText.value = query
      api.handleSlashInput()
      expect(api.filteredSlashCmds.value.filter(command => command.execution?.action === 'workbench.openSideChat')).toHaveLength(1)
      expect(api.filteredSlashCmds.value[0]?.cmd).toBe('/side')
    }
    await expect(api.classifySlashCommand('/btw question')).resolves.toBe('registered')
    await expect(api.executeSlashCommand('/btw question')).resolves.toBe(true)
    expect(run).toHaveBeenCalledWith('/btw question')
    expect(rpc.call).toHaveBeenCalledOnce()
    ready.resolve()
  })

  it('completes only the leading token and preserves all prose after the caret', () => {
    const run = vi.fn(async () => true)
    const { api, inputText } = harness(false, [], Promise.resolve(), undefined, { sideChat: { run }, getCaret: () => 3 })
    inputText.value = '/si keep this question\nincluding this line'
    api.handleSlashInput()
    api.completeSlashCmd(api.filteredSlashCmds.value[0]!)
    expect(run).toHaveBeenCalledWith('/side keep this question\nincluding this line')
    expect(inputText.value).toBe('/si keep this question\nincluding this line')
  })

  it('filters recursive side creation while retaining explicit command classification', async () => {
    const run = vi.fn(async () => true)
    const { api, inputText } = harness(false, [], Promise.resolve(), undefined, {
      sideChat: { run }, commandAllowed: command => command.execution?.action !== 'workbench.openSideChat',
    })
    inputText.value = '/btw'
    api.handleSlashInput()
    expect(api.filteredSlashCmds.value).toEqual([])
    await expect(api.classifySlashCommand('/btw question')).resolves.toBe('unavailable')
  })
})

describe('useChatSlashCommands plan compatibility', () => {
  it('keeps skill tags when completing and activating Plan mode', async () => {
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

    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(notify).not.toHaveBeenCalled()
    expect(inputText.value).toBe('')
    expect(selectedSkills.value).toEqual([skill])
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

  it('returns the optional Plan prompt to the active sender after activating the mode', async () => {
    const {
      activatePlanMode,
      api,
      inputText,
    } = harness(true)
    inputText.value = '/plan inspect the logging flow'

    await expect(api.executeSlashCommand(inputText.value)).resolves.toEqual({
      kind: 'composer-prompt',
      prompt: 'inspect the logging flow',
      composerText: '/plan inspect the logging flow',
    })

    expect(activatePlanMode).toHaveBeenCalledOnce()
    expect(inputText.value).toBe('/plan inspect the logging flow')
  })

  it('recognizes Plan aliases by their supported catalog action, not a command prefix', async () => {
    const { api } = harness(true, [{
      name: '/planning', aliases: ['/outline'], execution: { action: 'plans.setMode' },
    }, { name: '/compact', aliases: [] }])
    await api.loadSlashCommands()
    expect(api.isPlanModeCommand('/outline inspect')).toBe(true)
    expect(api.isPlanModeCommand('/planning inspect')).toBe(true)
    expect(api.isPlanModeCommand('/plan-other inspect')).toBe(false)
    expect(api.isPlanModeCommand('/compact')).toBe(false)
  })

  it('does not continue a Plan command that is unavailable in the current workbench', async () => {
    const { api, activatePlanMode, inputText } = harness(true, [], Promise.resolve(), undefined, {
      commandAllowed: () => false,
    })
    inputText.value = '/plan inspect the runtime'
    await api.loadSlashCommands()
    expect(api.isPlanModeCommand(inputText.value)).toBe(false)
    await expect(api.executeSlashCommand(inputText.value)).resolves.toBe(true)
    expect(activatePlanMode).not.toHaveBeenCalled()
    expect(inputText.value).toBe('/plan inspect the runtime')
  })

  it('preserves the command when Plan mode cannot be activated', async () => {
    const {
      activatePlanMode,
      api,
      inputText,
    } = harness(true)
    activatePlanMode.mockResolvedValueOnce(false)
    await api.loadSlashCommands()
    inputText.value = '/plan'
    api.handleSlashInput()

    api.selectSlashCmd(api.filteredSlashCmds.value[0])
    await Promise.resolve()

    expect(inputText.value).toBe('/plan')
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
    expect(notify).not.toHaveBeenCalled()
  })

  it('pauses and resumes via /goal pause and /goal resume', async () => {
    const { api, inputText, notify, goalPause, goalResume } = harness(false, [goalCommand])
    inputText.value = '/goal pause'
    await api.executeSlashCommand(inputText.value)
    expect(goalPause).toHaveBeenCalledTimes(1)
    expect(notify).not.toHaveBeenCalled()

    inputText.value = '/goal resume'
    await api.executeSlashCommand(inputText.value)
    expect(goalResume).toHaveBeenCalledTimes(1)
    expect(notify).not.toHaveBeenCalled()
  })

  it('edits the current Goal through the authoritative Goal composable', async () => {
    const { api, inputText, goalEdit, notify } = harness(false, [goalCommand])
    inputText.value = '/goal edit 更新迁移目标'

    await api.executeSlashCommand(inputText.value)

    expect(goalEdit).toHaveBeenCalledWith('更新迁移目标')
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('next safe boundary'))
  })
})

describe('slash commands with explicit local paths', () => {
  function localDraft(command: string) {
    const draft = useLocalPathDraft()
    draft.composerText.value = command
    draft.appendLocalPaths('C:\\项目\\report.pdf')
    return draft
  }

  it('dispatches a Plan prompt with paths exactly once and the canonical submission snapshot', async () => {
    const draft = localDraft('/plan Compare this report')
    const original = draft.inputText.value
    const { api } = harness(true, [], Promise.resolve(), undefined, {
      inputText: draft.composerText,
      submissionText: draft.inputText,
    })

    await expect(api.executeSlashCommand(original)).resolves.toEqual({
      kind: 'composer-prompt', prompt: 'Compare this report\nC:\\项目\\report.pdf', composerText: original,
    })
    // The normal send path, not the slash parser, owns accepted-draft cleanup.
    expect(draft.composerText.value).toBe('/plan Compare this report')
    expect(draft.localPaths.value).toEqual(['C:\\项目\\report.pdf'])
  })

  it('clears both visible text and explicit paths after an accepted Goal command', async () => {
    const draft = localDraft('/goal Summarize this report')
    const { api, startGoal } = harness(false, [{ name: '/goal', aliases: [] }], Promise.resolve(), undefined, {
      inputText: draft.composerText,
      submissionText: draft.inputText,
    })

    await api.executeSlashCommand(draft.inputText.value)
    await Promise.resolve()

    expect(startGoal).toHaveBeenCalledExactlyOnceWith('Summarize this report\nC:\\项目\\report.pdf')
    expect(draft.composerText.value).toBe('')
    expect(draft.localPaths.value).toEqual([])
    expect(draft.inputText.value).toBe('')
  })

  it('preserves changed path references when an older Goal command finishes accepting', async () => {
    const draft = localDraft('/goal Summarize this report')
    const accepted = deferred()
    const startGoal = vi.fn(() => accepted.promise.then(() => true))
    const { api } = harness(false, [{ name: '/goal', aliases: [] }], Promise.resolve(), undefined, {
      inputText: draft.composerText,
      submissionText: draft.inputText,
      startGoal,
    })

    await api.executeSlashCommand(draft.inputText.value)
    expect(startGoal).toHaveBeenCalledExactlyOnceWith('Summarize this report\nC:\\项目\\report.pdf')
    draft.localPaths.value = ['C:\\项目\\new-report.pdf']
    accepted.resolve()
    await accepted.promise
    await Promise.resolve()

    expect(draft.composerText.value).toBe('/goal Summarize this report')
    expect(draft.localPaths.value).toEqual(['C:\\项目\\new-report.pdf'])
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

  it('selects a current palette skill synchronously at the caret without another request or rewriting text', async () => {
    const onSelectSkill = vi.fn()
    const { api, inputText, selectedSkills, listCandidates } = skills({ getCaret: () => 3, onSelectSkill })
    inputText.value = '前半句，后半句'
    await api.loadSkillCandidates()
    const picked = api.skillPaletteItems.value[0]!
    expect(listCandidates).toHaveBeenCalledOnce()

    expect(api.selectPaletteSkill(picked)).toBe(true)

    const reference = { name: candidate.name, instanceId: candidate.instanceId, digest: candidate.digest }
    expect(selectedSkills.value).toEqual([reference])
    expect(onSelectSkill).toHaveBeenCalledExactlyOnceWith(reference, 3)
    expect(inputText.value).toBe('前半句，后半句')
    expect(listCandidates).toHaveBeenCalledOnce()
    // Selecting an already present capability is idempotent, including position.
    expect(api.selectPaletteSkill(picked)).toBe(true)
    expect(selectedSkills.value).toHaveLength(1)
    expect(onSelectSkill).toHaveBeenCalledOnce()
    expect(listCandidates).toHaveBeenCalledOnce()
  })

  it.each(['name', 'instanceId', 'digest'] as const)(
    'rejects a palette reference with a changed %s without fetching or selecting it', async (field) => {
      const onSelectSkill = vi.fn()
      const { api, inputText, selectedSkills, listCandidates } = skills({ onSelectSkill })
      inputText.value = '保留草稿'
      await api.loadSkillCandidates()
      const picked = api.skillPaletteItems.value[0]!
      const changed = { ...picked, skill: { ...picked.skill!, [field]: `changed-${picked.skill![field]}` } }

      expect(api.selectPaletteSkill(changed)).toBe(false)
      expect(selectedSkills.value).toEqual([])
      expect(onSelectSkill).not.toHaveBeenCalled()
      expect(inputText.value).toBe('保留草稿')
      expect(listCandidates).toHaveBeenCalledOnce()
    },
  )

  it('rejects a formerly valid palette row after its catalog is invalidated', async () => {
    const onSelectSkill = vi.fn()
    const { api, inputText, selectedSkills, listCandidates } = skills({ onSelectSkill })
    inputText.value = '目录刷新中也保留草稿'
    await api.loadSkillCandidates()
    const picked = api.skillPaletteItems.value[0]!
    api.invalidateSkillCandidates()

    expect(api.selectPaletteSkill(picked)).toBe(false)
    expect(selectedSkills.value).toEqual([])
    expect(onSelectSkill).not.toHaveBeenCalled()
    expect(inputText.value).toBe('目录刷新中也保留草稿')
    expect(listCandidates).toHaveBeenCalledOnce()
  })

  it.each([{ disabled: true, ready: true }, { disabled: false, ready: false }])(
    'uses current catalog readiness instead of a caller-supplied ready flag: %j', async status => {
      const listCandidates = vi.fn(async () => ({ generation: 1, candidates: [{ ...candidate, ...status }] }))
      const onSelectSkill = vi.fn()
      const { api, inputText, selectedSkills } = skills({ onSelectSkill, skillCatalog: {
        supportsCandidates: () => true, listCandidates,
      } as unknown as SkillCatalog })
      inputText.value = '保留正文'
      await api.loadSkillCandidates()
      const picked = api.skillPaletteItems.value[0]!

      expect(api.selectPaletteSkill({ ...picked, skill: { ...picked.skill!, ready: true, disabled: false } })).toBe(false)
      expect(selectedSkills.value).toEqual([])
      expect(onSelectSkill).not.toHaveBeenCalled()
      expect(inputText.value).toBe('保留正文')
      expect(listCandidates).toHaveBeenCalledOnce()
    },
  )

  it('inserts a slash-selected reference at the query start and preserves both surrounding text fragments', async () => {
    const prefix = '前面的说明 '
    const query = '/SPREADSHEET'
    const suffix = ' 后面的补充 /原样保留'
    const onSelectSkill = vi.fn()
    const setCaret = vi.fn()
    const { api, inputText, selectedSkills, listCandidates } = skills({
      getCaret: () => prefix.length + query.length, onSelectSkill, setCaret,
    })
    inputText.value = prefix + query + suffix
    api.handleSlashInput()
    await Promise.resolve()
    const picked = api.filteredSlashCmds.value.find(command => command.skill?.instanceId === candidate.instanceId)!
    expect(picked).toBeDefined()

    api.completeSlashCmd(picked)

    const reference = { name: candidate.name, instanceId: candidate.instanceId, digest: candidate.digest }
    expect(onSelectSkill).toHaveBeenCalledExactlyOnceWith(reference, prefix.length)
    expect(selectedSkills.value).toEqual([reference])
    expect(inputText.value).toBe(prefix + suffix)
    expect(setCaret).toHaveBeenCalledExactlyOnceWith(prefix.length)
    expect(api.slashOpen.value).toBe(false)
    expect(listCandidates).toHaveBeenCalledOnce()
  })

  it('omits reset and usage from the menu while keeping all ordinary skills browseable', async () => {
    const candidates = ['pdf-toolkit', 'github', 'docx', 'html-coder', 'pptx', 'xlsx', 'custom-skill']
      .map(name => ({ ...candidate, name, instanceId: `skill:${name}`,
        description: 'A brief purpose. Later details contain unique-search-term.' }))
    const skillCatalog = {
      supportsCandidates: () => true,
      listCandidates: vi.fn(async () => ({ generation: 1, candidates })),
    } as unknown as SkillCatalog
    const commands = ['/usage', '/goal', '/new', '/compact', '/reset'].map(name => ({ name, aliases: [] }))
    const { api, inputText } = harness(false, commands, Promise.resolve(), undefined, { skillCatalog })
    await api.loadSlashCommands()
    inputText.value = '/'
    api.handleSlashInput()
    await Promise.resolve()

    expect(api.filteredSlashCmds.value.map(item => item.name)).toEqual([
      '/goal', '/new', '/compact',
      'pdf-toolkit', 'github', 'docx', 'html-coder', 'pptx', 'xlsx', 'custom-skill',
    ])
    expect(api.filteredSlashCmds.value.map(item => item.kind)).toEqual([
      ...commands.filter(command => !['/usage', '/reset'].includes(command.name)).map(() => 'command'),
      ...candidates.map(() => 'skill'),
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

  it('uses maintained Chinese product copy while preserving bilingual search', async () => {
    const previousLocale = i18n.global.locale.value
    await loadLocaleMessages('zh-Hans')
    i18n.global.locale.value = 'zh-Hans'
    try {
      const { api, inputText } = harness(false, ['/new', '/compact'].map(name => ({ name, aliases: [] })),
        Promise.resolve(), undefined, { skillCatalog: {
          supportsCandidates: () => true,
          listCandidates: vi.fn(async () => ({ generation: 1, candidates: [{ ...candidate, descriptionZh: 'Use an implementation package and lengthy trigger rules.' }] })),
        } as unknown as SkillCatalog })
      await api.loadSlashCommands()
      inputText.value = '/'
      api.handleSlashInput()
      await Promise.resolve()
      expect(api.filteredSlashCmds.value.slice(0, 2).map(item => item.desc)).toEqual(['新建聊天', '压缩当前对话上下文'])
      expect(api.filteredSlashCmds.value.find(item => item.name === 'xlsx')).toMatchObject({ label: 'Excel 表格', desc: '创建、编辑与分析电子表格' })
      inputText.value = '/EXCEL'
      api.handleSlashInput()
      expect(api.filteredSlashCmds.value[0]?.name).toBe('xlsx')
    } finally {
      i18n.global.locale.value = previousLocale
    }
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
  it('selects a named work-style skill using catalog identity without changing the draft', async () => {
    const browser = { ...candidate, name: 'browser-use', instanceId: 'bundled:browser-use' }
    const selectedSkills = ref<SelectedSkillRef[]>([])
    const listCandidates = vi.fn(async () => ({ generation: 1, candidates: [browser] }))
    const { api, inputText } = harness(false, [], Promise.resolve(), undefined, {
      selectedSkills,
      skillCatalog: { supportsCandidates: () => true, listCandidates } as unknown as SkillCatalog,
    })
    inputText.value = 'Find the latest release notes'

    expect(await api.selectSkillByName('browser-use')).toBe(true)
    expect(inputText.value).toBe('Find the latest release notes')
    expect(selectedSkills.value).toEqual([{
      name: browser.name, instanceId: browser.instanceId, digest: browser.digest,
    }])
    expect(listCandidates).toHaveBeenCalledWith({ sessionKey: 'agent:main:webchat:test' })
    expect(await api.selectSkillByName('browser-use')).toBe(true)
    expect(selectedSkills.value).toHaveLength(1)
  })

  it('does not select an unavailable or stale named skill', async () => {
    const selectedSkills = ref<SelectedSkillRef[]>([])
    const manageSkill = vi.fn()
    const sessionKey = ref('agent:main:webchat:first')
    let release!: (value: { generation: number; candidates: typeof candidate[] }) => void
    const listCandidates = vi.fn(() => new Promise<{ generation: number; candidates: typeof candidate[] }>(resolve => {
      release = resolve
    }))
    const { api } = harness(false, [], Promise.resolve(), undefined, {
      selectedSkills, sessionKey, manageSkill,
      skillCatalog: { supportsCandidates: () => true, listCandidates } as unknown as SkillCatalog,
    })
    const pending = api.selectSkillByName('browser-use')
    sessionKey.value = 'agent:main:webchat:second'
    release({ generation: 1, candidates: [{ ...candidate, name: 'browser-use' }] })
    expect(await pending).toBe(false)
    expect(selectedSkills.value).toEqual([])

    listCandidates.mockImplementation(async () => ({ generation: 2, candidates: [{
      ...candidate, name: 'browser-use', ready: false,
    }] }))
    expect(await api.selectSkillByName('browser-use')).toBe(false)
    expect(manageSkill).toHaveBeenCalledWith('browser-use')
    expect(selectedSkills.value).toEqual([])
  })
  it('reports unavailable tool requirements without sending users to Skill settings', async () => {
    await loadLocaleMessages('en')
    const selectedSkills = ref<SelectedSkillRef[]>([])
    const manageSkill = vi.fn()
    const notify = vi.fn()
    const browser = {
      ...candidate, name: 'browser-use', ready: false, reasonCode: 'tools_unavailable',
    }
    const listCandidates = vi.fn(async () => ({ generation: 1, candidates: [browser] }))
    const { api, inputText } = harness(false, [], Promise.resolve(), undefined, {
      selectedSkills, manageSkill, notify,
      skillCatalog: { supportsCandidates: () => true, listCandidates } as unknown as SkillCatalog,
    })

    expect(await api.selectSkillByName('browser-use')).toBe(false)
    expect(notify).toHaveBeenCalledWith('Required tools unavailable')
    expect(manageSkill).not.toHaveBeenCalled()
    expect(selectedSkills.value).toEqual([])

    inputText.value = '/browser-use'
    api.handleSlashInput()
    await Promise.resolve()
    api.completeSlashCmd(api.filteredSlashCmds.value[0]!)
    expect(manageSkill).not.toHaveBeenCalled()
    expect(selectedSkills.value).toEqual([])
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
