import assert from 'node:assert/strict'
import fs from 'node:fs'
import { syncBuiltinESMExports } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import vm from 'node:vm'
import { parse as parseToml } from 'smol-toml'
import {
  configuredDesktopDataDirectory,
  inspectDesktopStartupMetadata,
} from '../dist/desktop-startup-preflight.js'

const root = fs.mkdtempSync(join(tmpdir(), 'opensquilla-startup-preflight-'))
let assertions = 0
function fixture(name, config = 'config_version = 2\n') {
  const userData = join(root, name)
  const home = join(userData, 'opensquilla')
  fs.mkdirSync(join(home, 'state'), { recursive: true })
  fs.mkdirSync(join(home, 'workspace'), { recursive: true })
  fs.writeFileSync(join(home, 'config.toml'), config)
  fs.writeFileSync(join(home, 'desktop-layout-v2.json'), JSON.stringify({
    schema_version: 2, migratedAt: '2026-10-14T00:00:00Z', moved: [],
  }))
  return { userData, home }
}
function check(item, expected, environment = {}) {
  const result = inspectDesktopStartupMetadata(item.userData, item.home, environment)
  assert.equal(result.canSkipMaintenance, expected, `${item.userData}: ${JSON.stringify(result)}`)
  assertions++
  return result
}
function tomlPath(path) { return JSON.stringify(path) }

try {
  const healthy = fixture('healthy')
  check(healthy, true)
  const recoveryIdentity = fixture('recovery-profiles/11234567-89ab-4cde-8fab-0123456789ab')
  assert.equal(check(recoveryIdentity, false).reason, 'recovery_profile_identity')
  for (const value of ['999', '2.0', 'true', '"2"', '[2]', 'nan', 'inf']) {
    check(fixture(`version-${assertions}`, `config_version = ${value}\n`), false)
  }
  for (const config of ['config_version = 2\nconfig_version = 2\n', 'state_dir = ["state"]\n', 'workspace_dir = { path = "workspace" }\n']) {
    check(fixture(`invalid-${assertions}`, config), false)
  }
  const utf8 = fixture('invalid-utf8')
  fs.writeFileSync(join(utf8.home, 'config.toml'), Buffer.from([0x23, 0xff]))
  check(utf8, false)
  const bom = fixture('utf8-bom')
  fs.writeFileSync(join(bom.home, 'config.toml'), '\ufeffconfig_version = 2\n')
  check(bom, false)
  const oversized = fixture('oversized-config')
  fs.writeFileSync(join(oversized.home, 'config.toml'), `#${'x'.repeat(1024 * 1024)}`)
  check(oversized, false)

  const externalState = join(root, '外置 state')
  const externalWorkspace = join(root, '外置 workspace')
  fs.mkdirSync(externalState)
  fs.mkdirSync(externalWorkspace)
  const external = fixture('external', `config_version = 2\n"state_dir" = ${tomlPath(externalState)}\n'workspace_dir' = ${tomlPath(externalWorkspace)}\n`)
  const externalResult = check(external, true)
  assert.equal(externalResult.stateDirectory, externalState)
  assert.equal(externalResult.workspaceDirectory, externalWorkspace)
  const relative = fixture('relative', 'state_dir = "state"\nworkspace_dir = "./workspace"\n')
  check(relative, true)
  for (const config of [
    'extra = { value = 1, }\n', 'extra = {\nvalue = 1\n}\n',
    'extra = "\\e"\n', 'extra = "\\x1b"\n',
    '额外 = 1\n', '[额外]\nvalue = 1\n',
    'extra = 2020-02-30\n', 'extra = 2020-02-20\n',
    'extra = 07:32\n', '# literal inline example { value = 1 }\n',
  ]) {
    const item = fixture(`toml-compatibility-${assertions}`, config)
    assert.match(check(item, false).reason, /^python_toml_/)
  }
  // Escaped Windows paths in ordinary strings are TOML 1.0, not \e/\x escapes.
  check(fixture('escaped-windows', `extra = ${tomlPath('C:\\example\\xfiles')}\n`), true)
  // A root mentioned inside a multiline string or nested table is not top-level.
  const nested = fixture('nested', 'comment = """\nstate_dir = "wrong"\n"""\n[other]\nstate_dir = "wrong"\n')
  assert.equal(check(nested, true).stateDirectory, join(nested.home, 'state'))
  const multiline = fixture('multiline-root', '"state_dir" = """state"""\nworkspace_dir = \'\'\'workspace\'\'\'\n')
  check(multiline, true)
  check(fixture('missing-external', 'workspace_dir = "../missing"\n'), false)
  for (const value of ['\ufeff', '\u0085', '\u001c']) {
    check(fixture(`unicode-root-${assertions}`, `state_dir = ${tomlPath(value)}\n`), false)
    check(healthy, false, { OPENSQUILLA_GATEWAY_STATE_DIR: value })
    assert.equal(configuredDesktopDataDirectory(healthy.home, value, 'state'), null)
  }
  for (const name of ['OPENSQUILLA_GATEWAY_STATE_DIR', 'OPENSQUILLA_GATEWAY_WORKSPACE_DIR', 'OPENSQUILLA_WORKSPACE_DIR']) {
    check(healthy, false, { [name]: externalState })
  }

  for (const marker of [
    '.opensquilla.profile-replace.json', '.opensquilla.import-commit.json',
    '.opensquilla.profile-cleanup.json', '.opensquilla.desktop-settings-transaction.json',
    '.opensquilla.workspace-patch.json', '.opensquilla-profile-consolidation.json',
    '.opensquilla-profile-consolidation-deferred', 'migration-provider-setup.json',
  ]) {
    const item = fixture(`marker-${assertions}`)
    fs.writeFileSync(join(item.userData, marker), '{}')
    check(item, false)
  }
  for (const entry of ['workspace', 'skills', 'skills-taps.json', 'skills-lock.json', 'session-archive', 'router', '.env', 'state']) {
    const item = fixture(`legacy-${assertions}`)
    fs.writeFileSync(join(item.home, 'state', entry), 'legacy')
    check(item, false)
  }
  for (const content of ['', '# ordinary provider settings\nEXAMPLE_API_KEY="fixture-only"\n', 'export EXAMPLE_MODEL=test\nEXAMPLE_URL=https://example.invalid\n']) {
    const item = fixture(`dotenv-ordinary-${assertions}`)
    fs.writeFileSync(join(item.home, '.env'), content)
    check(item, true)
  }
  for (const key of ['OPENSQUILLA_GATEWAY_STATE_DIR', 'OPENSQUILLA_GATEWAY_WORKSPACE_DIR', 'OPENSQUILLA_WORKSPACE_DIR']) {
    for (const content of [
      `${key}=../other`, `${key}=`, `export ${key}=../other`,
      `"${key}"=../other`, `${key}='../other'`,
      `${key.toLowerCase()}=../other`, `# ${key}=../other`, `EXAMPLE="${key}"`,
    ]) {
      const item = fixture(`dotenv-root-${assertions}`)
      fs.writeFileSync(join(item.home, '.env'), content)
      assert.equal(check(item, false).reason, 'root_dotenv')
    }
  }
  for (const content of [Buffer.from([0xff]), '\ufeffEXAMPLE=test', '#'.repeat(128 * 1024 + 1)]) {
    const item = fixture(`dotenv-unsafe-content-${assertions}`)
    fs.writeFileSync(join(item.home, '.env'), content)
    check(item, false)
  }
  const dotenvDirectory = fixture('dotenv-directory')
  fs.mkdirSync(join(dotenvDirectory.home, '.env'))
  check(dotenvDirectory, false)
  const dotenvLinked = fixture('dotenv-symlink')
  fs.symlinkSync(healthy.home, join(dotenvLinked.home, '.env'), process.platform === 'win32' ? 'junction' : 'dir')
  check(dotenvLinked, false)
  const dotenvHardlinked = fixture('dotenv-hardlink')
  fs.writeFileSync(join(dotenvHardlinked.home, '.env'), 'EXAMPLE=test')
  fs.linkSync(join(dotenvHardlinked.home, '.env'), join(root, 'shared.env'))
  check(dotenvHardlinked, false)
  // Change dotenv after its read, while the later config is opened. The final
  // identity check must reject edits, deletion and newly appearing root files.
  for (const change of ['edit', 'delete', 'create']) {
    const item = fixture(`dotenv-late-${change}`)
    const dotenvPath = join(item.home, '.env')
    if (change !== 'create') fs.writeFileSync(dotenvPath, 'EXAMPLE=test')
    const originalOpen = fs.openSync
    let changed = false
    fs.openSync = (path, ...args) => {
      if (!changed && path === join(item.home, 'config.toml')) {
        changed = true
        if (change === 'delete') fs.unlinkSync(dotenvPath)
        else fs.writeFileSync(dotenvPath, 'OPENSQUILLA_WORKSPACE_DIR=../changed')
      }
      return originalOpen(path, ...args)
    }
    syncBuiltinESMExports()
    try { assert.equal(check(item, false).reason, 'metadata_changed') }
    finally { fs.openSync = originalOpen; syncBuiltinESMExports() }
    assert.equal(changed, true)
  }
  const dotenvDuringRead = fixture('dotenv-changing-during-read')
  const changingDotenvPath = join(dotenvDuringRead.home, '.env')
  fs.writeFileSync(changingDotenvPath, 'EXAMPLE=test')
  const originalRead = fs.readFileSync
  let dotenvHandle
  const originalOpenDuringRead = fs.openSync
  fs.openSync = (path, ...args) => {
    const handle = originalOpenDuringRead(path, ...args)
    if (path === changingDotenvPath) dotenvHandle = handle
    return handle
  }
  fs.readFileSync = (path, ...args) => {
    const value = originalRead(path, ...args)
    if (path === dotenvHandle) fs.appendFileSync(changingDotenvPath, '\n# changed')
    return value
  }
  syncBuiltinESMExports()
  try { check(dotenvDuringRead, false) } finally {
    fs.openSync = originalOpenDuringRead
    fs.readFileSync = originalRead
    syncBuiltinESMExports()
  }
  const missingLayout = fixture('missing-layout')
  fs.unlinkSync(join(missingLayout.home, 'desktop-layout-v2.json'))
  check(missingLayout, false)
  const futureLayout = fixture('future-layout')
  fs.writeFileSync(join(futureLayout.home, 'desktop-layout-v2.json'), '{"schema_version":3,"migratedAt":"now","moved":[]}')
  check(futureLayout, false)
  const oldPointer = fixture('old-recovery-pointer')
  fs.writeFileSync(join(oldPointer.userData, 'desktop-profile-context.json'), JSON.stringify({
    schema_version: 1, active_profile_kind: 'recovery', active_recovery_id: '11234567-89ab-4cde-8fab-0123456789ab', updated_at: 'now',
  }))
  check(oldPointer, false)
  const primaryPointer = fixture('primary-pointer')
  fs.writeFileSync(join(primaryPointer.userData, 'desktop-profile-context.json'), JSON.stringify({
    schema_version: 1, active_profile_kind: 'primary', active_recovery_id: null, updated_at: 'now',
  }))
  check(primaryPointer, true)
  for (const container of ['recovery-profiles', 'backups/profile-consolidation']) {
    const item = fixture(`history-${assertions}`)
    fs.mkdirSync(join(item.userData, container), { recursive: true })
    check(item, true)
    fs.mkdirSync(join(item.userData, container, '11234567-89ab-4cde-8fab-0123456789ab'))
    check(item, false)
  }
  const linked = fixture('linked-config')
  fs.unlinkSync(join(linked.home, 'config.toml'))
  fs.symlinkSync(healthy.home, join(linked.home, 'config.toml'), process.platform === 'win32' ? 'junction' : 'dir')
  check(linked, false)
  const badDatabase = fixture('database-directory')
  fs.mkdirSync(join(badDatabase.home, 'state', 'sessions.db-wal'))
  check(badDatabase, false)
  const orphanWal = fixture('orphan-wal')
  fs.writeFileSync(join(orphanWal.home, 'state', 'sessions.db-wal'), 'pending')
  check(orphanWal, false)
  const hardlinkedDatabase = fixture('database-hardlink')
  fs.writeFileSync(join(hardlinkedDatabase.home, 'state', 'sessions.db'), 'shared')
  fs.linkSync(join(hardlinkedDatabase.home, 'state', 'sessions.db'), join(root, 'shared.db'))
  check(hardlinkedDatabase, false)

  // Instrument actual filesystem opens. No DB contents or user documents may
  // be read by this gate, even if files are large or not valid SQLite bytes.
  fs.writeFileSync(join(healthy.home, 'state', 'sessions.db'), 'not read by Electron')
  const opens = []
  const originalOpen = fs.openSync
  fs.openSync = (path, ...args) => { opens.push(String(path)); return originalOpen(path, ...args) }
  syncBuiltinESMExports()
  try { check(healthy, true) } finally { fs.openSync = originalOpen; syncBuiltinESMExports() }
  assert.deepEqual(opens.map(path => path.slice(healthy.home.length + 1)).sort(), ['config.toml', 'desktop-layout-v2.json'])

  // Execute the real startup coordinator body with bounded collaborators. This
  // proves zero maintenance calls, preservation of orphan recovery, and fallback
  // after an awaited orphan drain changes the metadata.
  const main = fs.readFileSync(new URL('../dist/main.js', import.meta.url), 'utf8')
  const stateFunctionStart = main.indexOf('function configuredDesktopStateDirectory')
  const stateFunctionEnd = main.indexOf('function desktopTelemetryDirectory', stateFunctionStart)
  assert.ok(stateFunctionEnd > stateFunctionStart)
  const stateContext = { configuredDesktopDataDirectory, parseToml, join }
  vm.createContext(stateContext)
  vm.runInContext(main.slice(stateFunctionStart, stateFunctionEnd), stateContext)
  assert.equal(stateContext.configuredDesktopStateDirectory(external, `"state_dir" = ${tomlPath(externalState)}\n`), externalState)
  assert.equal(stateContext.configuredDesktopStateDirectory(relative, 'state_dir = """state"""\n'), join(relative.home, 'state'))
  assert.throws(() => stateContext.configuredDesktopStateDirectory(healthy, 'state_dir = ["wrong"]\n'))
  const start = main.indexOf('async function inspectActiveProfileBeforeStartup')
  const end = main.indexOf('async function openOrResumeDesktopApp', start)
  const body = main.slice(start, end)
  async function startupCase(results, expected) {
    const calls = []
    const report = { outcome: 'ready', stable_code: 'canonical_workspace', allowed_actions: [] }
    const context = {
      activeDesktopProfile: () => ({ home: healthy.home }),
      inspectDesktopStartupMetadata: () => results.shift(),
      app: { getPath: () => healthy.userData }, process: { env: {} }, join,
      resolvedPathsEqual: (a, b) => resolve(a) === resolve(b),
      consolidateLegacyRecoveryProfilesBeforeStartup: async () => { calls.push('consolidate'); return null },
      liveLifecycleOwnedGatewayProcesses: () => [],
      recoverVerifiedOrphanGatewayBeforeSpawn: async () => { calls.push('orphan') },
      inspectDesktopProfile: async () => { calls.push('inspect'); return report },
      recoverPendingMigrationReconciliation: async () => ({ requiresInspection: false }),
      pendingDesktopCredentialConsolidation: null,
      recoveryOperationError: null, recoveryInspection: null, primaryRecoveryInspection: null,
      desktopGrowthTelemetry: { observeProfileInspection: () => {} },
      desktopLog: () => {}, publishRecoveryState: () => {}, createApplicationMenu: () => {},
    }
    vm.createContext(context)
    vm.runInContext(body, context)
    assert.equal(await context.inspectActiveProfileBeforeStartup(), true)
    assert.deepEqual(calls, expected)
    if (!expected.includes('inspect')) assert.equal(context.recoveryInspection, null)
  }
  const eligible = { canSkipMaintenance: true, stateDirectory: join(healthy.home, 'state'), workspaceDirectory: join(healthy.home, 'workspace') }
  await startupCase([eligible, eligible], ['orphan'])
  await startupCase([{ canSkipMaintenance: false }], ['consolidate', 'orphan', 'inspect'])
  await startupCase([eligible, { canSkipMaintenance: false }], ['orphan', 'consolidate', 'inspect'])
  assert.equal(configuredDesktopDataDirectory(healthy.home, 'state', 'state'), join(healthy.home, 'state'))
  if (process.platform !== 'win32') {
    assert.equal(configuredDesktopDataDirectory(healthy.home, 'link/../state', 'state'), null)
    assert.equal(configuredDesktopDataDirectory(healthy.home, '~/link/../state', 'state'), null)
    assert.equal(configuredDesktopDataDirectory(healthy.home, '//server/state', 'state'), null)
  }
  console.log(JSON.stringify({ ok: true, metadataCases: assertions, coordinatorCases: 3, sqliteContentReads: 0 }))
} finally {
  const resolvedRoot = resolve(root)
  assert.ok(resolvedRoot.startsWith(resolve(tmpdir()) + (process.platform === 'win32' ? '\\' : '/')))
  fs.rmSync(resolvedRoot, { recursive: true, force: true })
}
