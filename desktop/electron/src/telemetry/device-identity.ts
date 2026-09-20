import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, openSync, readSync } from 'node:fs'
import { win32 } from 'node:path'

import type { DesktopPlatform } from './contracts.js'

const DEVICE_ID_DOMAIN = 'opensquilla.telemetry.device.v1\0'
const MACHINE_ID_RE = /^[a-f0-9]{32}$/
const DEVICE_ID_RE = /^[a-f0-9]{64}$/

/** Hash only an OS machine identifier; profile paths and network addresses are never inputs. */
export function deriveDeviceId(platform: DesktopPlatform, machineId: string): string | null {
  const normalized = machineId.trim().replaceAll('-', '').toLowerCase()
  if (
    !['macos', 'windows', 'linux'].includes(platform)
    || !MACHINE_ID_RE.test(normalized)
    || /^0+$/.test(normalized)
    || /^f+$/.test(normalized)
  ) return null
  return createHash('sha256').update(`${DEVICE_ID_DOMAIN}${platform}\0${normalized}`, 'utf8')
    .digest('hex')
}

interface DeviceIdentityOptions {
  platform?: NodeJS.Platform
  windowsDirectory?: string
  readText?: (path: string) => string
  run?: (command: string, args: readonly string[]) => string
}

function readMachineIdFile(path: string): string {
  const descriptor = openSync(path, 'r')
  try {
    const buffer = Buffer.alloc(128)
    const length = readSync(descriptor, buffer, 0, buffer.length, 0)
    return length < buffer.length ? buffer.subarray(0, length).toString('utf8') : ''
  } finally {
    closeSync(descriptor)
  }
}

/** Lazily read once per process, only when an already-consented producer requests an ID. */
export function createDeviceIdProvider(options: DeviceIdentityOptions = {}): () => string | null {
  let cached: string | null | undefined
  const platform = options.platform ?? process.platform
  const readText = options.readText ?? readMachineIdFile
  const run = options.run ?? ((command: string, args: readonly string[]) => execFileSync(
    command, [...args], { encoding: 'utf8', timeout: 1_500, maxBuffer: 64 * 1024, windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'] },
  ))
  return () => {
    if (cached !== undefined) return cached
    cached = null
    try {
      if (platform === 'darwin') {
        const output = run('/usr/sbin/ioreg', ['-rd1', '-c', 'IOPlatformExpertDevice'])
        const match = /"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"/.exec(output)
        cached = match ? deriveDeviceId('macos', match[1]) : null
      } else if (platform === 'win32') {
        const directory = options.windowsDirectory ?? process.env.SystemRoot ?? 'C:\\Windows'
        if (!/^[A-Za-z]:[\\/]/.test(directory)) return null
        const output = run(win32.join(directory, 'System32', 'reg.exe'), [
          'query', 'HKLM\\SOFTWARE\\Microsoft\\Cryptography', '/v', 'MachineGuid', '/reg:64',
        ])
        const match = /^\s*MachineGuid\s+REG_SZ\s+([0-9A-Fa-f-]+)\s*$/im.exec(output)
        cached = match ? deriveDeviceId('windows', match[1]) : null
      } else if (platform === 'linux') {
        for (const path of ['/etc/machine-id', '/var/lib/dbus/machine-id']) {
          try { cached = deriveDeviceId('linux', readText(path)) } catch { /* try next OS file */ }
          if (cached !== null) break
        }
      }
    } catch {
      // Unavailable or malformed OS identities never fall back to random profile identities.
    }
    return cached
  }
}

export const getDeviceId = createDeviceIdProvider()

export function deviceIdentityFields(provider: () => string | null): { device_id?: string } {
  try {
    const value = provider()
    return typeof value === 'string' && DEVICE_ID_RE.test(value) ? { device_id: value } : {}
  } catch {
    return {}
  }
}
