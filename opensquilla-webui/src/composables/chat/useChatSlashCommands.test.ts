import { ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useChatSlashCommands } from './useChatSlashCommands'

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
) {
  const inputText = ref('')
  const rpc = {
    waitForConnection: vi.fn(() => waitForConnection),
    call: vi.fn().mockResolvedValue({ commands }),
  }
  const activatePlanMode = vi.fn(async () => true)
  const dispatchHidden = vi.fn()
  const dispatchPlanPrompt = vi.fn()
  const api = useChatSlashCommands({
    rpc,
    inputText,
    sessionKey: ref('agent:main:webchat:test'),
    autoResizeTextarea: vi.fn(),
    newSession: vi.fn(),
    resetCurrentSession: vi.fn(),
    setCompactInFlight: vi.fn(),
    showCompactStatus: vi.fn(),
    notify: vi.fn(),
    dispatchHidden,
    dispatchPlanPrompt,
    activatePlanMode,
    planModeAvailable: () => planModeAvailable,
  })
  return { activatePlanMode, api, dispatchHidden, dispatchPlanPrompt, inputText, rpc }
}

describe('useChatSlashCommands plan compatibility', () => {
  it('adds and executes /plan when the connected gateway advertises plan mode', async () => {
    const { api, inputText, activatePlanMode } = harness(true)
    await api.loadSlashCommands()
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
