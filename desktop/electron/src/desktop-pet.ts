// Desktop-shell integration for the Eggy (蛋仔) desktop pet.
//
// The pet lives in `desktop/pet` as a standalone zero-dependency Electron app.
// This module lets the Desktop shell start/stop it as a child process, exposes a
// tray toggle, and persists the user's choice in its own preference file so the
// main preferences schema is untouched. When the pet directory is absent (e.g.
// packaged builds that do not bundle it) the feature gracefully no-ops.
import { app } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'

type Logger = (event: string, detail?: Record<string, unknown>) => void

let petChild: ChildProcess | null = null
let petDir: string | null = null
let enabled = false
let getGatewayWsUrl: () => string = () => ''
let onChange: (() => void) | null = null
let log: Logger = () => {}

function preferencePath(): string {
  return join(app.getPath('userData'), 'desktop-pet.json')
}

function loadPreference(): void {
  try {
    const parsed = JSON.parse(readFileSync(preferencePath(), 'utf8')) as { enabled?: unknown }
    enabled = parsed.enabled === true
  } catch {
    enabled = false
  }
}

function savePreference(): void {
  try {
    mkdirSync(dirname(preferencePath()), { recursive: true })
    writeFileSync(preferencePath(), JSON.stringify({ enabled }, null, 2), 'utf8')
  } catch (error) {
    log('desktop_pet_preference_save_failed', { error: error instanceof Error ? error.message : String(error) })
  }
}

function resolvePetDir(repoRoot: string): string | null {
  const candidate = join(repoRoot, 'desktop', 'pet')
  return existsSync(join(candidate, 'main.js')) ? candidate : null
}

function startPet(): void {
  if (petChild || !petDir) return
  const env: NodeJS.ProcessEnv = { ...process.env }
  // The shell may be launched from a harness that sets ELECTRON_RUN_AS_NODE;
  // the pet must run as a real Electron app, not plain Node.
  delete env.ELECTRON_RUN_AS_NODE
  const wsUrl = getGatewayWsUrl()
  if (wsUrl) env.EGGY_GATEWAY_URL = wsUrl
  try {
    const child = spawn(process.execPath, [petDir], { env, stdio: 'ignore' })
    petChild = child
    child.on('exit', () => {
      if (petChild === child) petChild = null
      onChange?.()
    })
    child.on('error', (error) => {
      if (petChild === child) petChild = null
      log('desktop_pet_spawn_failed', { error: error.message })
      onChange?.()
    })
    log('desktop_pet_started', { dir: petDir, pid: child.pid })
  } catch (error) {
    petChild = null
    log('desktop_pet_spawn_failed', { error: error instanceof Error ? error.message : String(error) })
  }
}

function stopPet(): void {
  const child = petChild
  if (!child) return
  petChild = null
  const pid = child.pid
  if (pid == null) return
  if (process.platform === 'win32') {
    // Kill the whole Electron process tree (main + gpu + renderer).
    try {
      spawn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' })
    } catch {
      try { child.kill() } catch { /* ignore */ }
    }
  } else {
    try { child.kill('SIGTERM') } catch { /* ignore */ }
  }
  log('desktop_pet_stopped', { pid })
}

export function initDesktopPet(options: {
  repoRoot: string
  gatewayWsUrl: () => string
  onChange?: () => void
  log?: Logger
}): void {
  petDir = resolvePetDir(options.repoRoot)
  getGatewayWsUrl = options.gatewayWsUrl
  onChange = options.onChange ?? null
  log = options.log ?? log
  loadPreference()
  log('desktop_pet_init', { available: petDir !== null, enabled })
  if (enabled && petDir) startPet()
}

export function toggleDesktopPet(): void {
  enabled = !enabled
  savePreference()
  if (enabled) startPet()
  else stopPet()
  onChange?.()
}

export function isDesktopPetEnabled(): boolean {
  return enabled
}

export function isDesktopPetRunning(): boolean {
  return petChild !== null
}

export function isDesktopPetAvailable(): boolean {
  return petDir !== null
}

export function stopDesktopPet(): void {
  stopPet()
}