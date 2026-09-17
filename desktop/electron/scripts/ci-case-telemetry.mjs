import { spawn } from 'node:child_process'
import { appendFile, mkdir } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptPath = fileURLToPath(import.meta.url)
const VALID_STATUSES = new Set(['passed', 'failed', 'cancelled', 'skipped'])
const DEFAULT_CASE_TIMEOUT_MS = 15 * 60 * 1000
const TERMINATION_GRACE_MS = 3_000

function defaultOutputPath() {
  const reportDir = process.env.CI_REPORT_DIR?.trim()
  return reportDir ? join(reportDir, 'desktop-e2e-cases.jsonl') : ''
}

function positiveAttempt(value) {
  const attempt = Number.parseInt(String(value || '1'), 10)
  if (!Number.isInteger(attempt) || attempt < 1) {
    throw new Error(`Desktop E2E case attempt must be a positive integer; got ${value}`)
  }
  return attempt
}

function requiredText(value, label) {
  const text = String(value || '').trim()
  if (!text) throw new Error(`${label} is required`)
  return text
}

function resolveTimeoutMs(value, env = process.env) {
  const raw = value ?? env.OPENSQUILLA_DESKTOP_CASE_TIMEOUT_MS
  if (raw === undefined || raw === null || String(raw).trim() === '') {
    return DEFAULT_CASE_TIMEOUT_MS
  }
  const timeoutMs = Number.parseInt(String(raw), 10)
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error(
      `Desktop E2E case timeout must be a positive integer in milliseconds; got ${raw}`,
    )
  }
  return timeoutMs
}

function terminateChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  const pid = child.pid
  if (process.platform === 'win32' && pid) {
    const killer = spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
    })
    let fallbackTimer
    const fallback = () => {
      if (fallbackTimer) clearTimeout(fallbackTimer)
      if (child.exitCode === null && child.signalCode === null) child.kill()
    }
    // ``taskkill`` can report a non-zero exit without emitting an ``error``
    // event (for example when the child already disappeared).  Do not let
    // that turn the case watchdog into another unbounded wait.
    killer.once('error', fallback)
    killer.once('close', code => {
      if (code !== 0) fallback()
    })
    fallbackTimer = setTimeout(fallback, TERMINATION_GRACE_MS)
    fallbackTimer.unref()
    return
  }

  if (process.platform !== 'win32' && pid) {
    try {
      process.kill(-pid, 'SIGTERM')
    } catch {
      child.kill('SIGTERM')
    }
    const forceTimer = setTimeout(() => {
      if (child.exitCode === null && child.signalCode === null) {
        try {
          process.kill(-pid, 'SIGKILL')
        } catch {
          child.kill('SIGKILL')
        }
      }
    }, TERMINATION_GRACE_MS)
    forceTimer.unref()
    return
  }

  child.kill()
}

async function writeRecord(record, outputPath, emit) {
  const line = JSON.stringify(record)
  emit(line)
  if (!outputPath) return
  await mkdir(dirname(outputPath), { recursive: true })
  await appendFile(outputPath, `${line}\n`, { encoding: 'utf8' })
}

export function startCaseTelemetry({
  caseName,
  os = process.env.RUNNER_OS || process.platform,
  shard = process.env.OPENSQUILLA_DESKTOP_E2E_SHARD || process.env.CI_E2E_SHARD || 'local',
  attempt = process.env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT
    || process.env.GITHUB_RUN_ATTEMPT
    || '1',
  outputPath = process.env.OPENSQUILLA_CI_CASE_TELEMETRY_PATH || defaultOutputPath(),
  emit = line => console.log(line),
} = {}) {
  const identity = {
    case: requiredText(caseName, 'Desktop E2E case name'),
    os: requiredText(os, 'Desktop E2E runner OS'),
    shard: requiredText(shard, 'Desktop E2E shard'),
    attempt: positiveAttempt(attempt),
  }
  const start = new Date()
  const monotonicStart = process.hrtime.bigint()
  let finished = false

  return {
    async finish(status, details = undefined) {
      if (finished) throw new Error(`Desktop E2E case ${identity.case} was already finished`)
      if (!VALID_STATUSES.has(status)) {
        throw new Error(`Unsupported Desktop E2E case status: ${status}`)
      }
      finished = true
      const end = new Date()
      const duration = Number(process.hrtime.bigint() - monotonicStart) / 1_000_000
      const record = {
        ...identity,
        start: start.toISOString(),
        end: end.toISOString(),
        duration: Math.round(duration * 1000) / 1000,
        duration_unit: 'ms',
        status,
        ...(details === undefined ? {} : { details }),
      }
      await writeRecord(record, outputPath, emit)
      return record
    },
  }
}

export async function runCommandWithTelemetry({
  command,
  args = [],
  cwd = process.cwd(),
  env = process.env,
  timeoutMs,
  ...telemetryOptions
}) {
  const executable = requiredText(command, 'Desktop E2E case command')
  const shard = telemetryOptions.shard
    ?? env.OPENSQUILLA_DESKTOP_E2E_SHARD
    ?? env.CI_E2E_SHARD
    ?? 'local'
  const attempt = telemetryOptions.attempt
    ?? env.OPENSQUILLA_DESKTOP_E2E_ATTEMPT
    ?? env.GITHUB_RUN_ATTEMPT
    ?? '1'
  const telemetry = startCaseTelemetry({ ...telemetryOptions, shard, attempt })
  const childEnv = {
    ...env,
    OPENSQUILLA_DESKTOP_E2E_ATTEMPT: String(positiveAttempt(attempt)),
    OPENSQUILLA_DESKTOP_E2E_SHARD: requiredText(shard, 'Desktop E2E shard'),
    ...(telemetryOptions.outputPath
      ? { OPENSQUILLA_CI_CASE_TELEMETRY_PATH: telemetryOptions.outputPath }
      : {}),
  }
  const resolvedTimeoutMs = resolveTimeoutMs(timeoutMs, env)
  let child
  try {
    child = spawn(executable, args, {
      cwd,
      env: childEnv,
      stdio: 'inherit',
      detached: process.platform !== 'win32',
      windowsHide: true,
    })
  } catch (error) {
    await telemetry.finish('failed', { spawn_error: String(error?.message || error) })
    throw error
  }

  let timedOut = false
  let timeoutHandle
  const outcome = await new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('close', (exitCode, signal) => resolve({ exitCode, signal }))
    timeoutHandle = setTimeout(() => {
      timedOut = true
      console.error(
        `Desktop E2E case ${telemetryOptions.caseName || 'unknown'} exceeded `
        + `${resolvedTimeoutMs}ms; terminating its process tree.`,
      )
      terminateChild(child)
    }, resolvedTimeoutMs)
  }).catch(async error => {
    if (timeoutHandle) clearTimeout(timeoutHandle)
    await telemetry.finish('failed', {
      spawn_error: String(error?.message || error),
      ...(timedOut ? { timed_out: true, timeout_ms: resolvedTimeoutMs } : {}),
    })
    throw error
  })
  if (timeoutHandle) clearTimeout(timeoutHandle)

  const status = outcome.exitCode === 0 ? 'passed' : 'failed'
  const details = {
    exit_code: outcome.exitCode,
    signal: outcome.signal,
    ...(timedOut ? { timed_out: true, timeout_ms: resolvedTimeoutMs } : {}),
  }
  const record = await telemetry.finish(status, details)
  return { ...outcome, record }
}

function parseRunArguments(argv) {
  const options = {}
  let index = 0
  for (; index < argv.length; index += 1) {
    const argument = argv[index]
    if (argument === '--') {
      index += 1
      break
    }
    const value = argv[index + 1]
    if (!argument.startsWith('--') || value === undefined) {
      throw new Error(`Invalid ci-case-telemetry argument: ${argument}`)
    }
    const key = argument.slice(2)
    if (!['case', 'os', 'shard', 'attempt', 'output'].includes(key)) {
      throw new Error(`Unknown ci-case-telemetry option: ${argument}`)
    }
    options[key] = value
    index += 1
  }
  const command = argv[index]
  if (!command) throw new Error('ci-case-telemetry run requires a command after --')
  return { options, command, args: argv.slice(index + 1) }
}

async function main(argv) {
  const [subcommand, ...rest] = argv
  if (subcommand !== 'run') {
    throw new Error(
      'usage: node ci-case-telemetry.mjs run --case NAME [--os OS] '
      + '[--shard SHARD] [--attempt N] [--output PATH] -- COMMAND [ARGS...]',
    )
  }
  const { options, command, args } = parseRunArguments(rest)
  const result = await runCommandWithTelemetry({
    caseName: options.case,
    os: options.os,
    shard: options.shard,
    attempt: options.attempt,
    outputPath: options.output,
    command,
    args,
  })
  process.exitCode = result.exitCode ?? 1
}

if (process.argv[1] && resolve(process.argv[1]) === scriptPath) {
  main(process.argv.slice(2)).catch(error => {
    console.error(error?.stack || String(error))
    process.exitCode = 1
  })
}
