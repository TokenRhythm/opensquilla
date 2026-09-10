/** Coordinates only the pre-installer boundary. NSIS owns everything after spawn. */
export class WindowsUpdatePreparationError extends Error {
  constructor(readonly reason: 'writers_busy' | 'gateway_busy') {
    super(reason === 'writers_busy' ? 'Desktop writes did not finish before the deadline.' : 'The local runtime could not be stopped.')
  }
}

export interface WindowsUpdateHooks {
  canStart(): boolean
  started(): void
  verify(): Promise<void>
  closeWriters(): void
  waitForWriters(signal: AbortSignal): Promise<void>
  stopGateways(): Promise<boolean>
  assertCanHandoff(): void
  launchInstaller(): Promise<void>
  committed(): void
  recover(error: unknown, gatewayStopStarted: boolean): Promise<void>
}

export class WindowsUpdateCoordinator {
  private active = false
  private handedOff = false

  constructor(private readonly writerTimeoutMs = 30_000) {}

  async run(hooks: WindowsUpdateHooks): Promise<'busy' | 'failed' | 'handed-off'> {
    if (this.active || this.handedOff || !hooks.canStart()) return 'busy'
    this.active = true
    let gatewayStopStarted = false
    try {
      hooks.started()
      await hooks.verify()
      hooks.closeWriters()
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(new WindowsUpdatePreparationError('writers_busy')), this.writerTimeoutMs)
      try {
        await hooks.waitForWriters(controller.signal)
      } finally {
        clearTimeout(timer)
      }
      gatewayStopStarted = true
      if (!await hooks.stopGateways()) throw new WindowsUpdatePreparationError('gateway_busy')
      hooks.assertCanHandoff()
      await hooks.launchInstaller()
      // Process creation is the commitment boundary, not installation success.
      // Never resume writers/Gateway after NSIS could have started replacing files.
      this.handedOff = true
      hooks.committed()
      return 'handed-off'
    } catch (error) {
      if (this.handedOff) throw error
      await hooks.recover(error, gatewayStopStarted)
      return 'failed'
    } finally {
      if (!this.handedOff) this.active = false
    }
  }
}
