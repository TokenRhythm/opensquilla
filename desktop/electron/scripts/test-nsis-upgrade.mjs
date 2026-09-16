import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const packageJson = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
const { getMakeNsisPath } = require('app-builder-lib/out/toolsets/windows.js')

if (process.platform !== 'win32') {
  throw new Error('NSIS environment regression requires native Windows')
}
const tooling = await getMakeNsisPath(packageJson.build?.toolsets?.nsis, packageJson.build?.nsis?.customNsisBinary)
assert.ok(tooling.env?.NSISDIR, 'The locked NSIS compiler must expose NSISDIR')
const evidence = await mkdtemp(join(process.env.RUNNER_TEMP || tmpdir(), 'nsis-upgrade-native-'))
const result = spawnSync(process.env.PYTHON || 'python', [
  join(packageRoot, 'scripts/nsis/test-legacy-upgrade-native.py'),
  '--nsis-root', tooling.env.NSISDIR,
  '--evidence-root', evidence,
], { stdio: 'inherit', windowsHide: true })
if (result.error) throw result.error
assert.equal(result.status, 0, `Native NSIS regression failed; evidence: ${evidence}`)
