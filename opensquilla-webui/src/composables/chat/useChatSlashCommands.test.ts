import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { parseMetaCommandInvocation, useChatSlashCommands } from './useChatSlashCommands'
import type { RpcCallOptions } from '@/lib/rpc'

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function harness(
  planModeAvailable: boolean,
  commands: Array<Record<string, unknown>> = [],
  waitForConnection: Promise<void> = Promise.resolve(),
  catalogCallOptions?: RpcCallOptions,
) {
  const inputText = ref('')
  const rpc = {
    waitForConnection: vi.fn(() => waitForConnection),
    call: vi.fn().mockResolvedValue({ commands }),
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
  const api = useChatSlashCommands({
    rpc,
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
    notify,
    rpc,
    setCodingModeEnabled,
  }
}

describe('useChatSlashCommands plan compatibility', () => {
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
    expect(rpc.waitForConnection).toHaveBeenCalledWith(
      2_000,
      undefined,
      {
        timeoutAction: 'reconnect',
        abortAction: 'reconnect',
      },
    )
    expect(rpc.call).toHaveBeenCalledWith(
      'commands.list_for_surface',
      { surface: 'web_chat' },
      catalogCallOptions,
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

    expect(api.filteredSlashCmds.value.map(command => command.name)).toEqual(['/plan'])
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
    const { api, rpc } = harness(false, [{ name: '/reset', aliases: [] }])

    await expect(api.classifySlashCommand('/reset')).resolves.toBe('registered')
    await expect(api.executeSlashCommand('/reset', 'registered')).resolves.toBe(true)

    expect(rpc.call).toHaveBeenCalledWith('sessions.reset', {
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
