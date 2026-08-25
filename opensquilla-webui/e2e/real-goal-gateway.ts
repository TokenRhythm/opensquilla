import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

type ProviderCall = {
  event: 'provider.call'
  callNumber: number
  toolNames: string[]
  objectiveInRequestContext: boolean
  progressIsNull: boolean
  firstReplyInAssistantHistory: boolean
  requestHasInternalContinuation: boolean
  historyHasSilentSentinel: boolean
  silentVisibleBodyInAssistantHistory: boolean
}

type ProviderGateEvent = {
  event: 'provider.waiting' | 'provider.released'
  callNumber: number
}

export type GoalProviderEvent = ProviderCall | ProviderGateEvent

export type RealGoalGateway = {
  wsUrl: string
  readProviderEvents: () => Promise<GoalProviderEvent[]>
  readProviderCalls: () => Promise<ProviderCall[]>
  releaseFirstTask: () => Promise<void>
  releaseSecondTask: () => Promise<void>
  stop: () => Promise<void>
}

export type RealGoalGatewayScenario = 'continuation' | 'lifecycle' | 'silent-reply'

async function reserveLoopbackPort(): Promise<number> {
  const server = createServer()
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Unable to reserve a loopback port for the Goal Gateway')
  }
  const port = address.port
  server.close()
  await once(server, 'close')
  return port
}

function processDiagnostics(
  child: ChildProcessWithoutNullStreams,
  stdout: string[],
  stderr: string[],
  spawnError: Error | null = null,
): string {
  return [
    `exitCode=${String(child.exitCode)}`,
    `signalCode=${String(child.signalCode)}`,
    `spawnError=${spawnError?.message || 'none'}`,
    `stdout:\n${stdout.join('').slice(-8_000)}`,
    `stderr:\n${stderr.join('').slice(-8_000)}`,
  ].join('\n')
}

async function signalAndWait(
  child: ChildProcessWithoutNullStreams,
  signal: NodeJS.Signals,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null || child.pid === undefined) return true
  return await new Promise<boolean>((resolve) => {
    let settled = false
    const finish = (exited: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      child.off('exit', onExit)
      resolve(exited)
    }
    const onExit = () => finish(true)
    const timer = setTimeout(() => finish(false), timeoutMs)
    child.once('exit', onExit)
    try {
      const signalled = child.kill(signal)
      if (!signalled && (child.exitCode !== null || child.signalCode !== null)) finish(true)
    } catch {
      if (child.exitCode !== null || child.signalCode !== null) finish(true)
    }
  })
}

async function stopProcess(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (await signalAndWait(child, 'SIGTERM', 5_000)) return
  if (!await signalAndWait(child, 'SIGKILL', 5_000)) {
    throw new Error('Goal Gateway did not exit after SIGTERM and SIGKILL')
  }
}

export async function startRealGoalGateway(options: {
  outputDir: string
  webuiOrigin: string
  scenario?: RealGoalGatewayScenario
}): Promise<RealGoalGateway> {
  const repoRoot = fileURLToPath(new URL('../..', import.meta.url))
  const fixturePath = fileURLToPath(new URL('./goal-mode-gateway.py', import.meta.url))
  const stateDir = join(options.outputDir, 'state')
  const eventLog = join(options.outputDir, 'provider-events.jsonl')
  const firstReleaseFile = join(options.outputDir, 'release-first-task')
  const secondReleaseFile = join(options.outputDir, 'release-second-task')
  const logDir = join(options.outputDir, 'logs')
  const tempDir = join(options.outputDir, 'tmp')
  const appDataDir = join(stateDir, 'appdata')
  const localAppDataDir = join(stateDir, 'local-appdata')
  await mkdir(stateDir, { recursive: true })
  await mkdir(logDir, { recursive: true })
  await mkdir(tempDir, { recursive: true })
  await mkdir(appDataDir, { recursive: true })
  await mkdir(localAppDataDir, { recursive: true })

  const port = await reserveLoopbackPort()
  const python = process.env.OPENSQUILLA_WEBUI_E2E_PYTHON
    || join(
      repoRoot,
      '.venv',
      process.platform === 'win32' ? 'Scripts' : 'bin',
      process.platform === 'win32' ? 'python.exe' : 'python',
    )
  const child = spawn(python, ['-u', fixturePath], {
    // Keep OpenSquilla's normal dotenv bootstrap away from both the checkout
    // and the user's profile.  The editable virtualenv still resolves the
    // package by absolute path, while the fixture gets a private home/cwd.
    cwd: stateDir,
    env: Object.assign(
      Object.fromEntries(
        [
          'PATH',
          'LANG',
          'LC_ALL',
          'SYSTEMROOT',
          'WINDIR',
          'COMSPEC',
          'PATHEXT',
        ]
          .filter(key => process.env[key] !== undefined)
          .map(key => [key, process.env[key]]),
      ),
      {
        // Never let the fixture discover the developer's profile, dotenv,
        // cache, or platform application-data directories. All persistence is
        // disposable Playwright output owned by this test invocation.
        HOME: stateDir,
        USERPROFILE: stateDir,
        APPDATA: appDataDir,
        LOCALAPPDATA: localAppDataDir,
        XDG_CONFIG_HOME: join(stateDir, 'xdg-config'),
        XDG_CACHE_HOME: join(stateDir, 'xdg-cache'),
        XDG_DATA_HOME: join(stateDir, 'xdg-data'),
        TMPDIR: tempDir,
        TEMP: tempDir,
        TMP: tempDir,
        PYTHONNOUSERSITE: '1',
        OPENSQUILLA_WEBUI_GOAL_E2E_PORT: String(port),
        OPENSQUILLA_WEBUI_GOAL_E2E_STATE: stateDir,
        OPENSQUILLA_WEBUI_GOAL_E2E_EVENT_LOG: eventLog,
        OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE_FIRST: firstReleaseFile,
        OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE: secondReleaseFile,
        OPENSQUILLA_WEBUI_GOAL_E2E_SCENARIO: options.scenario || 'continuation',
        OPENSQUILLA_WEBUI_GOAL_E2E_ORIGIN: options.webuiOrigin,
        OPENSQUILLA_HOME: stateDir,
        OPENSQUILLA_STATE_DIR: stateDir,
        OPENSQUILLA_LOG_DIR: logDir,
        OPENSQUILLA_OPENROUTER_LIVE_PRICING: '0',
        OPENSQUILLA_MEMORY_DREAM_DISABLED: '1',
        OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY: 'true',
      },
    ),
    stdio: 'pipe',
  })
  const stdout: string[] = []
  const stderr: string[] = []
  let spawnError: Error | null = null
  child.once('error', error => {
    spawnError = error
  })
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', chunk => stdout.push(String(chunk)))
  child.stderr.on('data', chunk => stderr.push(String(chunk)))

  const healthUrl = `http://127.0.0.1:${port}/health`
  const deadline = Date.now() + 45_000
  try {
    while (Date.now() < deadline) {
      if (spawnError) {
        throw new Error(`Goal Gateway failed to spawn: ${spawnError.message}`)
      }
      if (child.exitCode !== null || child.signalCode !== null) {
        throw new Error(
          `Goal Gateway exited during startup\n${processDiagnostics(child, stdout, stderr, spawnError)}`,
        )
      }
      try {
        const response = await fetch(healthUrl)
        if (response.ok) break
      } catch {
        // The listener is not ready yet.
      }
      await new Promise(resolve => setTimeout(resolve, 100))
    }
    const response = await fetch(healthUrl)
    if (!response.ok) {
      throw new Error(`Goal Gateway health check returned ${response.status}`)
    }
  } catch (error) {
    await stopProcess(child)
    throw new Error(
      `${String(error)}\n${processDiagnostics(child, stdout, stderr, spawnError)}`,
    )
  }

  return {
    wsUrl: `ws://127.0.0.1:${port}/ws`,
    async readProviderEvents() {
      let raw = ''
      try {
        raw = await readFile(eventLog, 'utf8')
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []
        throw error
      }
      return raw
        .split('\n')
        .filter(Boolean)
        .map(line => JSON.parse(line) as GoalProviderEvent)
    },
    async readProviderCalls() {
      return (await this.readProviderEvents())
        .filter((event): event is ProviderCall => event.event === 'provider.call')
    },
    async releaseFirstTask() {
      await writeFile(firstReleaseFile, 'continue\n', 'utf8')
    },
    async releaseSecondTask() {
      await writeFile(secondReleaseFile, 'continue\n', 'utf8')
    },
    async stop() {
      await stopProcess(child)
      await Promise.all([
        writeFile(join(options.outputDir, 'gateway-stdout.log'), stdout.join(''), 'utf8'),
        writeFile(join(options.outputDir, 'gateway-stderr.log'), stderr.join(''), 'utf8'),
      ])
    },
  }
}
