import { spawn as nodeSpawn } from 'node:child_process'
import { win32 } from 'node:path'
import { runWindowsPowerShellJson, type WindowsPowerShellRunner } from './windows-update-security.js'

// electron-builder UUID v5 for the fixed appId ai.opensquilla.desktop; covered
// by a contract test against the installed builder and package configuration.
export const WINDOWS_INSTALL_REGISTRY_KEY = 'Software\\9f633106-fcda-5e7f-93e4-8dddb967ea3e'

export class WindowsUpdateHandoffError extends Error {
  constructor(readonly code: 'installation_ambiguous' | 'installation_unavailable' | 'install_failed', message: string) {
    super(message)
    this.name = 'WindowsUpdateHandoffError'
  }
}

export const WINDOWS_INSTALL_LOCATIONS_SCRIPT = `
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module ([IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Utility.psd1'))
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$locations = @()
foreach ($hive in @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryHive]::LocalMachine)) {
  $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($hive, [Microsoft.Win32.RegistryView]::Registry64)
  try {
    $key = $base.OpenSubKey([string]$request.key, $false)
    if ($null -ne $key) {
      try {
        $location = [string]$key.GetValue('InstallLocation', '')
        if (-not [string]::IsNullOrWhiteSpace($location)) {
          $locations += [ordered]@{ scope = [string]$hive; path = $location }
        }
      } finally { $key.Dispose() }
    }
  } finally { $base.Dispose() }
}
ConvertTo-Json -InputObject @($locations) -Compress
`

export async function assertUnambiguousWindowsInstallation(
  executablePath: string,
  options: { runPowerShell?: WindowsPowerShellRunner } = {},
): Promise<void> {
  if (!win32.isAbsolute(executablePath) || executablePath.includes('\0')
    || win32.basename(executablePath).toLowerCase() !== 'opensquilla.exe') {
    throw new WindowsUpdateHandoffError('installation_ambiguous', 'The running Windows installation could not be identified.')
  }
  let raw: unknown
  try {
    raw = await (options.runPowerShell ?? runWindowsPowerShellJson)(WINDOWS_INSTALL_LOCATIONS_SCRIPT, { key: WINDOWS_INSTALL_REGISTRY_KEY })
  } catch {
    throw new WindowsUpdateHandoffError('installation_unavailable', 'The Windows installation registry could not be checked.')
  }
  if (!Array.isArray(raw) || raw.some((entry) => !entry || typeof entry !== 'object'
    || !['CurrentUser', 'LocalMachine'].includes(entry.scope) || typeof entry.path !== 'string'
    || !win32.isAbsolute(entry.path) || entry.path.includes('\0'))) {
    throw new WindowsUpdateHandoffError('installation_unavailable', 'The Windows installation registry returned invalid information.')
  }
  const normalize = (value: string) => win32.normalize(value).replace(/[\\/]+$/, '').toLowerCase()
  // Even equal paths in both scopes are ambiguous: NSIS chooses CurrentUser
  // when both registry entries exist, which can change uninstall ownership.
  if (raw.length !== 1 || normalize(raw[0].path) !== normalize(win32.dirname(executablePath))) {
    throw new WindowsUpdateHandoffError('installation_ambiguous', 'Use the installer wizard to select the Windows installation to update.')
  }
}

export async function launchWindowsInstaller(
  installerPath: string,
  options: { spawn?: typeof nodeSpawn } = {},
): Promise<void> {
  if (!win32.isAbsolute(installerPath) || installerPath.includes('\0') || !/\.exe$/i.test(installerPath)) {
    throw new WindowsUpdateHandoffError('install_failed', 'The verified installer path is invalid.')
  }
  await new Promise<void>((resolve, reject) => {
    try {
      const child = (options.spawn ?? nodeSpawn)(installerPath, ['--updated'], {
        shell: false, detached: true, stdio: 'ignore', windowsHide: false,
        cwd: win32.dirname(installerPath),
      })
      let launched = false
      child.on('error', () => {
        if (!launched) reject(new WindowsUpdateHandoffError('install_failed', 'The Windows installer could not be started.'))
      })
      child.once('spawn', () => {
        launched = true
        child.unref()
        resolve()
      })
    } catch {
      reject(new WindowsUpdateHandoffError('install_failed', 'The Windows installer could not be started.'))
    }
  })
}
