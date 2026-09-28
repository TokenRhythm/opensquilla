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
  /** Privacy-safe lifecycle breadcrumbs; never include update bytes or secrets. */
  stage?(event: WindowsUpdateStage, detail?: Record<string, unknown>): void
}

export type WindowsUpdateStage =
  | 'update_started'
  | 'writers_admission_closed'
  | 'writers_drained'
  | 'gateway_stop_requested'
  | 'gateway_child_exited'
  | 'gateway_child_exit_wait'
  | 'installer_handoff'
  | 'update_recovered'

export class WindowsUpdateCoordinator {
  private active = false
  private handedOff = false

  constructor(private readonly writerTimeoutMs = 30_000) {}

  private reportStage(
    hooks: WindowsUpdateHooks,
    event: WindowsUpdateStage,
    detail?: Record<string, unknown>,
  ): void {
    try {
      hooks.stage?.(event, detail)
    } catch {
      // Diagnostics are fail-open and must never alter the handoff boundary.
    }
  }

  async run(hooks: WindowsUpdateHooks): Promise<'busy' | 'failed' | 'handed-off'> {
    if (this.active || this.handedOff || !hooks.canStart()) return 'busy'
    this.active = true
    let gatewayStopStarted = false
    try {
      hooks.started()
      this.reportStage(hooks, 'update_started')
      await hooks.verify()
      hooks.closeWriters()
      this.reportStage(hooks, 'writers_admission_closed')
      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(new WindowsUpdatePreparationError('writers_busy')), this.writerTimeoutMs)
      try {
        await hooks.waitForWriters(controller.signal)
      } finally {
        clearTimeout(timer)
      }
      this.reportStage(hooks, 'writers_drained')
      gatewayStopStarted = true
      this.reportStage(hooks, 'gateway_stop_requested')
      const gatewaysStopped = await hooks.stopGateways()
      this.reportStage(
        hooks,
        gatewaysStopped ? 'gateway_child_exited' : 'gateway_child_exit_wait',
        { exited: gatewaysStopped },
      )
      if (!gatewaysStopped) throw new WindowsUpdatePreparationError('gateway_busy')
      hooks.assertCanHandoff()
      await hooks.launchInstaller()
      this.reportStage(hooks, 'installer_handoff')
      // Process creation is the commitment boundary, not installation success.
      // Never resume writers/Gateway after NSIS could have started replacing files.
      this.handedOff = true
      hooks.committed()
      return 'handed-off'
    } catch (error) {
      if (this.handedOff) throw error
      await hooks.recover(error, gatewayStopStarted)
      this.reportStage(hooks, 'update_recovered')
      return 'failed'
    } finally {
      if (!this.handedOff) this.active = false
    }
  }
}
