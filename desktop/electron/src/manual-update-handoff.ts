/**
 * Windows manual-update handoff planning.
 *
 * The unsigned Windows build cannot use electron-updater's native
 * quitAndInstall, so the desktop opens the verified NSIS installer itself.
 * Launching that installer while the app (and its owned gateway) are still
 * running makes the installer force-close them: NSIS stops every process
 * under the install directory with no graceful drain, and an instance it
 * cannot stop (elevated, hung, or hidden in the tray) dead-ends the update
 * in a "cannot be closed" retry dialog. These helpers describe the handoff
 * so the main process can drain writers and the gateway first — mirroring
 * the native quitAndInstall flow — and only then start the installer.
 *
 * Pure module by contract: no Electron or Node imports, so the plan and the
 * eligibility decision stay unit-testable outside an Electron process.
 */

export interface ManualInstallerLaunchPlan {
  command: string
  args: string[]
  options: { detached: true; stdio: 'ignore' }
}

/**
 * Describe how to start the verified installer for an update handoff.
 *
 * `--updated` mirrors electron-updater's own launch of an update installer:
 * NSIS then grace-waits for this process to exit instead of prompting the
 * user, and keeps existing shortcuts rather than treating the run as a
 * fresh install. Detached + ignored stdio lets the installer outlive the
 * quitting app without holding any inherited handle into it.
 */
export function manualInstallerLaunchPlan(installerPath: string): ManualInstallerLaunchPlan {
  if (!installerPath) throw new Error('The manual installer path is empty.')
  return {
    command: installerPath,
    args: ['--updated'],
    options: { detached: true, stdio: 'ignore' },
  }
}

export type ManualHandoffBlocker =
  | 'already-applying'
  | 'already-quitting'
  | 'writers-closed'
  | 'not-manual-mode'
  | 'no-verified-installer'
  | 'not-downloaded'

export interface ManualHandoffContext {
  installMode: string
  status: string
  verifiedInstallerPath: string | null
  updateApplying: boolean
  quitting: boolean
  writersClosed: boolean
}

/**
 * Return the first reason the manual handoff must not start, or null when it
 * may proceed. Order matters: lifecycle blockers are reported before mode or
 * state mismatches so a caller can distinguish "retry later" from "not this
 * flow at all".
 */
export function manualHandoffBlocker(context: ManualHandoffContext): ManualHandoffBlocker | null {
  if (context.updateApplying) return 'already-applying'
  if (context.quitting) return 'already-quitting'
  if (context.writersClosed) return 'writers-closed'
  if (context.installMode !== 'manual') return 'not-manual-mode'
  if (!context.verifiedInstallerPath) return 'no-verified-installer'
  if (context.status !== 'downloaded') return 'not-downloaded'
  return null
}
