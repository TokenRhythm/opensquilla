import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  runCommandWithTelemetry,
  startCaseTelemetry,
} from './ci-case-telemetry.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const helperPath = join(scriptDir, 'ci-case-telemetry.mjs')
const root = await mkdtemp(join(tmpdir(), 'opensquilla-ci-case-telemetry-'))
const outputPath = join(root, 'reports', 'cases.jsonl')
const emitted = []

try {
  const telemetry = startCaseTelemetry({
    caseName: 'direct-case',
    os: 'TestOS',
    shard: 'unit',
    attempt: 2,
    outputPath,
    emit: line => emitted.push(line),
  })
  const direct = await telemetry.finish('passed')
  assert.equal(direct.case, 'direct-case')
  assert.equal(direct.os, 'TestOS')
  assert.equal(direct.shard, 'unit')
  assert.equal(direct.attempt, 2)
  assert.equal(direct.status, 'passed')
  assert.equal(direct.duration_unit, 'ms')
  assert.ok(direct.duration >= 0)
  assert.ok(Date.parse(direct.end) >= Date.parse(direct.start))
  await assert.rejects(() => telemetry.finish('passed'), /already finished/)

  const passed = await runCommandWithTelemetry({
    caseName: 'command-pass',
    os: 'TestOS',
    shard: 'unit',
    attempt: 1,
    outputPath,
    emit: line => emitted.push(line),
    command: process.execPath,
    args: ['-e', 'process.exit(0)'],
  })
  assert.equal(passed.exitCode, 0)
  assert.equal(passed.record.status, 'passed')

  const failed = await runCommandWithTelemetry({
    caseName: 'command-fail',
    os: 'TestOS',
    shard: 'unit',
    attempt: 3,
    outputPath,
    emit: line => emitted.push(line),
    command: process.execPath,
    args: ['-e', 'process.exit(7)'],
  })
  assert.equal(failed.exitCode, 7)
  assert.equal(failed.record.status, 'failed')
  assert.deepEqual(failed.record.details, { exit_code: 7, signal: null })

  const cliPassed = spawnSync(process.execPath, [
    helperPath,
    'run',
    '--case', 'cli-pass',
    '--os', 'TestOS',
    '--shard', 'cli',
    '--attempt', '4',
    '--output', outputPath,
    '--',
    process.execPath,
    '-e',
    'process.exit(process.env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT === "4" '
      + '&& process.env.OPENSQUILLA_DESKTOP_E2E_SHARD === "cli" '
      + `&& process.env.OPENSQUILLA_CI_CASE_TELEMETRY_PATH === ${JSON.stringify(outputPath)} `
      + '? 0 : 8)',
  ], { encoding: 'utf8' })
  assert.equal(cliPassed.status, 0, cliPassed.stderr)
  const cliPassedRecord = JSON.parse(cliPassed.stdout.trim())
  assert.equal(cliPassedRecord.status, 'passed')

  const cliFailed = spawnSync(process.execPath, [
    helperPath,
    'run',
    '--case', 'cli-fail',
    '--os', 'TestOS',
    '--shard', 'cli',
    '--attempt', '5',
    '--output', outputPath,
    '--',
    process.execPath,
    '-e',
    'process.exit(9)',
  ], { encoding: 'utf8' })
  assert.equal(cliFailed.status, 9, cliFailed.stderr)
  const cliFailedRecord = JSON.parse(cliFailed.stdout.trim())
  assert.equal(cliFailedRecord.status, 'failed')

  const records = (await readFile(outputPath, 'utf8'))
    .trim()
    .split('\n')
    .map(line => JSON.parse(line))
  assert.deepEqual(records, [
    ...emitted.map(line => JSON.parse(line)),
    cliPassedRecord,
    cliFailedRecord,
  ])
  assert.deepEqual(records.map(record => record.case), [
    'direct-case',
    'command-pass',
    'command-fail',
    'cli-pass',
    'cli-fail',
  ])
  console.log('Desktop E2E case telemetry checks passed')
} finally {
  await rm(root, { recursive: true, force: true })
}
