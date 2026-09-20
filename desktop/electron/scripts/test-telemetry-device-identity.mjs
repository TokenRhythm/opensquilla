import assert from 'node:assert/strict'

import {
  createDeviceIdProvider, deriveDeviceId, deviceIdentityFields,
} from '../dist/telemetry/device-identity.js'
import { validateDesktopEarlyTelemetryEvent } from '../dist/telemetry/contracts.js'

const MACHINE_ID = '00112233-4455-6677-8899-AABBCCDDEEFF'
const EXPECTED = {
  macos: 'd6a0f2626efbe042e31ae116edd1642222afac62f715f2474a8b7bb7ed444c2f',
  windows: 'b2bd06260e4c5d10d1d0bc93da5f5c2239f9eef53e259fa6e2093131579dffc9',
  linux: 'a9617d1526c2d976cb9da454451d2f15b12990202b2238e19072429c466b025f',
}
for (const platform of ['macos', 'windows', 'linux']) {
  assert.equal(deriveDeviceId(platform, `  ${MACHINE_ID}\n`), EXPECTED[platform])
  assert.equal(deriveDeviceId(platform, MACHINE_ID.replaceAll('-', '').toLowerCase()), EXPECTED[platform])
  assert.notEqual(deriveDeviceId(platform, 'ffeeddccbbaa99887766554433221100'), EXPECTED[platform])
  for (const invalid of ['', 'uninitialized', '0'.repeat(32), 'f'.repeat(32), 'a'.repeat(31),
    'machine.local', '00:11:22:33:44:55', '192.0.2.1']) {
    assert.equal(deriveDeviceId(platform, invalid), null)
  }
}
assert.equal(deriveDeviceId('unsupported', MACHINE_ID), null)

let calls = 0
const mac = createDeviceIdProvider({
  platform: 'darwin',
  run(command, args) {
    calls += 1
    assert.equal(command, '/usr/sbin/ioreg')
    assert.deepEqual(args, ['-rd1', '-c', 'IOPlatformExpertDevice'])
    return `"IOPlatformUUID" = "${MACHINE_ID}"`
  },
})
assert.equal(calls, 0)
assert.equal(mac(), EXPECTED.macos)
assert.equal(mac(), EXPECTED.macos)
assert.equal(calls, 1)

const windows = createDeviceIdProvider({
  platform: 'win32', windowsDirectory: 'C:\\Windows',
  run(command, args) {
    assert.equal(command, 'C:\\Windows\\System32\\reg.exe')
    assert.deepEqual(args, [
      'query', 'HKLM\\SOFTWARE\\Microsoft\\Cryptography', '/v', 'MachineGuid', '/reg:64',
    ])
    return `\r\n    MachineGuid    REG_SZ    ${MACHINE_ID}\r\n`
  },
})
assert.equal(windows(), EXPECTED.windows)

const linuxPaths = []
const linux = createDeviceIdProvider({
  platform: 'linux',
  readText(path) {
    linuxPaths.push(path)
    if (path === '/etc/machine-id') throw new Error('synthetic absent file')
    return `${MACHINE_ID.replaceAll('-', '')}\n`
  },
})
assert.equal(linux(), EXPECTED.linux)
assert.deepEqual(linuxPaths, ['/etc/machine-id', '/var/lib/dbus/machine-id'])
assert.equal(createDeviceIdProvider({ platform: 'linux', readText: () => '0'.repeat(32) })(), null)
let unavailableReads = 0
const unavailable = createDeviceIdProvider({
  platform: 'darwin', run() { unavailableReads += 1; throw new Error('synthetic unavailable OS') },
})
assert.equal(unavailable(), null)
assert.equal(unavailable(), null)
assert.equal(unavailableReads, 1)
assert.equal(createDeviceIdProvider({ platform: 'freebsd' })(), null)
assert.deepEqual(deviceIdentityFields(() => null), {})
assert.deepEqual(deviceIdentityFields(() => MACHINE_ID), {})
assert.deepEqual(deviceIdentityFields(() => { throw new Error('synthetic unavailable provider') }), {})

const legacyEvent = {
  event_name: 'first_app_ready', event_version: 1,
  event_id: '00000000-0000-4000-8000-000000000001',
  occurred_at_utc: '2026-09-02T01:02:03.004Z', source: 'desktop', app_version: '0.5.4',
  platform: 'macos', outcome: null, error_code: null, duration_ms: null,
  consent_scope: 'growth', notice_version: 'growth-v2', sample_rate: 1,
  analytics_user_id: '00000000-0000-4000-8000-000000000002',
}
const bytes = JSON.stringify(legacyEvent)
assert.equal(JSON.stringify(validateDesktopEarlyTelemetryEvent(legacyEvent)), bytes)
assert.equal(validateDesktopEarlyTelemetryEvent({ ...legacyEvent, device_id: EXPECTED.macos }).device_id,
  EXPECTED.macos)
for (const invalid of [null, undefined, MACHINE_ID, EXPECTED.macos.toUpperCase(), '', 'a'.repeat(63)]) {
  assert.throws(() => validateDesktopEarlyTelemetryEvent({ ...legacyEvent, device_id: invalid }))
}
assert.throws(() => validateDesktopEarlyTelemetryEvent({ ...legacyEvent, machine_id: MACHINE_ID }))
console.log('Device identity and legacy payload compatibility passed')
