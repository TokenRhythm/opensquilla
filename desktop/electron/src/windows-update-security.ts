import { execFile } from 'node:child_process'
import { win32 } from 'node:path'

export interface WindowsUpdateSigningPolicy {
  readonly certificateSha1: string
  readonly publisherSubjectContains: string
}

// Public identity embedded in the application, never read from a downloaded
// manifest or the source repository at runtime. The contract test compares it
// with the release signing policy so certificate rotation is an explicit edit.
export const WINDOWS_UPDATE_SIGNING_POLICY: WindowsUpdateSigningPolicy = Object.freeze({
  certificateSha1: 'CBF0846AB04712002132A2991F57416639B70AF3',
  publisherSubjectContains: 'Beijing TokenRhythm Technologies Co., Ltd.',
})

export class WindowsUpdateSecurityError extends Error {
  constructor(readonly code: 'signature_invalid' | 'signature_unavailable', message: string) {
    super(message)
    this.name = 'WindowsUpdateSecurityError'
  }
}

export type WindowsPowerShellRunner = (script: string, input: unknown) => Promise<unknown>

export const WINDOWS_INSTALLER_SIGNATURE_SCRIPT = `
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Import-Module ([IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Utility.psd1'))
Import-Module ([IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Security', 'Microsoft.PowerShell.Security.psd1'))
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$signature = Get-AuthenticodeSignature -LiteralPath ([string]$request.path)
[ordered]@{
  status = [string]$signature.Status
  thumbprint = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Thumbprint } else { '' }
  subject = if ($null -ne $signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '' }
  timestamped = $null -ne $signature.TimeStamperCertificate
} | ConvertTo-Json -Compress
`

export function windowsPowerShellPath(systemRoot = process.env.SystemRoot || 'C:\\Windows'): string {
  if (!/^[A-Za-z]:\\/.test(systemRoot) || systemRoot.includes('\0')) {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'The Windows system directory is unavailable.')
  }
  return win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
}

// The command is fixed application code. All paths and other request data are
// JSON on stdin, never interpolated into PowerShell source or a command shell.
export const runWindowsPowerShellJson: WindowsPowerShellRunner = async (script, input) => {
  if (process.platform !== 'win32') {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'Windows verification is not available on this platform.')
  }
  const executable = windowsPowerShellPath()
  return await new Promise<unknown>((resolve, reject) => {
    const child = execFile(executable, [
      '-NoLogo', '-NoProfile', '-NonInteractive',
      '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64'),
    ], { windowsHide: true, timeout: 20_000, maxBuffer: 64 * 1024, encoding: 'utf8' }, (error, stdout) => {
      if (error) {
        reject(new WindowsUpdateSecurityError('signature_unavailable', 'Windows verification could not be completed.'))
        return
      }
      try {
        resolve(JSON.parse(stdout.replace(/^\uFEFF/, '').trim()))
      } catch {
        reject(new WindowsUpdateSecurityError('signature_unavailable', 'Windows verification returned an invalid response.'))
      }
    })
    // Avoid an unhandled EPIPE if PowerShell exits before consuming the input.
    child.stdin?.on('error', () => {})
    child.stdin?.end(JSON.stringify(input), 'utf8')
  })
}

export async function verifyWindowsInstaller(
  installerPath: string,
  policy: WindowsUpdateSigningPolicy = WINDOWS_UPDATE_SIGNING_POLICY,
  options: { runPowerShell?: WindowsPowerShellRunner } = {},
): Promise<void> {
  if (!win32.isAbsolute(installerPath) || installerPath.includes('\0')) {
    throw new WindowsUpdateSecurityError('signature_invalid', 'The installer path is invalid.')
  }
  if (!/^[0-9a-f]{40}$/i.test(policy.certificateSha1) || !policy.publisherSubjectContains.trim()) {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'The embedded Windows signing policy is invalid.')
  }
  let raw: unknown
  try {
    raw = await (options.runPowerShell ?? runWindowsPowerShellJson)(WINDOWS_INSTALLER_SIGNATURE_SCRIPT, { path: installerPath })
  } catch (error) {
    if (error instanceof WindowsUpdateSecurityError) throw error
    throw new WindowsUpdateSecurityError('signature_unavailable', 'Windows signature verification is unavailable.')
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'Windows signature verification returned an invalid response.')
  }
  const result = raw as Record<string, unknown>
  if (typeof result.status !== 'string' || typeof result.thumbprint !== 'string'
    || typeof result.subject !== 'string' || typeof result.timestamped !== 'boolean') {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'Windows signature verification returned an incomplete response.')
  }
  if (result.status === 'UnknownError') {
    throw new WindowsUpdateSecurityError('signature_unavailable', 'Windows could not determine the installer signature status.')
  }
  if (result.status !== 'Valid'
    || result.thumbprint.toUpperCase() !== policy.certificateSha1.toUpperCase()
    || !result.subject.includes(policy.publisherSubjectContains)
    || result.timestamped !== true) {
    throw new WindowsUpdateSecurityError('signature_invalid', 'The installer does not satisfy the OpenSquilla signing policy.')
  }
}
