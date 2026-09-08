import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import {
  WINDOWS_UPDATE_SIGNING_POLICY,
  WINDOWS_INSTALLER_SIGNATURE_SCRIPT,
  WindowsUpdateSecurityError,
  verifyWindowsInstaller,
  windowsPowerShellPath,
  runWindowsPowerShellJson,
} from '../dist/windows-update-security.js'

const releasePolicy = JSON.parse(await readFile(new URL('../../../.github/signing/windows-signing-policy.json', import.meta.url), 'utf8'))
assert.equal(WINDOWS_UPDATE_SIGNING_POLICY.certificateSha1, releasePolicy.certificateSha1)
assert.equal(WINDOWS_UPDATE_SIGNING_POLICY.publisherSubjectContains, releasePolicy.publisherSubjectContains)
assert.equal(windowsPowerShellPath('C:\\Windows'), 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe')
assert.throws(() => windowsPowerShellPath('relative'), (error) => error.code === 'signature_unavailable')

const valid = {
  status: 'Valid', thumbprint: releasePolicy.certificateSha1,
  subject: `CN=${releasePolicy.publisherSubjectContains}, O=Example`, timestamped: true,
}
// Characters with meaning in PowerShell remain data on stdin. They must not
// alter the fixed command or be escaped into a generated source program.
const path = "C:\\安装 cache\\OpenSquilla '$() ` &.exe"
let requests = 0
await verifyWindowsInstaller(path, undefined, { runPowerShell: async (script, input) => {
  requests += 1
  assert.equal(script, WINDOWS_INSTALLER_SIGNATURE_SCRIPT)
  assert.equal(script.includes(path), false)
  assert.deepEqual(input, { path })
  assert.match(script, /Get-AuthenticodeSignature -LiteralPath/)
  return valid
} })
assert.equal(requests, 1)

for (const response of [
  { ...valid, status: 'NotSigned' }, { ...valid, status: 'HashMismatch' },
  { ...valid, thumbprint: 'F'.repeat(40) }, { ...valid, subject: 'CN=Other Publisher' },
  { ...valid, timestamped: false },
]) {
  await assert.rejects(verifyWindowsInstaller(path, undefined, { runPowerShell: async () => response }),
    (error) => error instanceof WindowsUpdateSecurityError && error.code === 'signature_invalid')
}
for (const response of [null, [], {}, { ...valid, status: 'UnknownError' }, { ...valid, timestamped: 'true' }]) {
  await assert.rejects(verifyWindowsInstaller(path, undefined, { runPowerShell: async () => response }),
    (error) => error.code === 'signature_unavailable')
}
await assert.rejects(verifyWindowsInstaller(path, undefined, { runPowerShell: async () => { throw new Error('blocked') } }),
  (error) => error.code === 'signature_unavailable')
await assert.rejects(verifyWindowsInstaller('relative.exe', undefined, { runPowerShell: async () => valid }),
  (error) => error.code === 'signature_invalid')
if (process.platform === 'win32') {
  const directory = await mkdtemp(join(tmpdir(), 'opensquilla-signature-check-'))
  try {
    const unsignedPath = join(directory, "unsigned ' & 测试.exe")
    await writeFile(unsignedPath, 'This is a synthetic non-executable test file.')
    const actual = await runWindowsPowerShellJson(WINDOWS_INSTALLER_SIGNATURE_SCRIPT, { path: unsignedPath })
    assert.ok(['NotSigned', 'UnknownError', 'NotSupportedFileFormat'].includes(actual.status),
      'the fixed verifier must read the literal file and fail closed for non-PE bytes')
    assert.equal(actual.thumbprint, '')
    await assert.rejects(verifyWindowsInstaller(unsignedPath),
      (error) => error.code === (actual.status === 'UnknownError' ? 'signature_unavailable' : 'signature_invalid'))
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
}
console.log('Windows update security checks passed (includes Windows OS verification when available; no installer executed).')
