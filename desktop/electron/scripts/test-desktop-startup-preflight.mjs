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
function archivedReceipt(item, status = 'not_required', index = 0, overrides = {}) {
  const transactionId = `11234567-89ab-4cde-8fab-${String(index).padStart(12, '0')}`
  const sourceId = '21234567-89ab-4cde-8fab-0123456789ab'
  const backup = join(item.userData, 'backups', 'profile-consolidation', transactionId)
  const path = join(backup, 'receipt.json')
  const credential = status !== 'not_required'
  const payload = {
    schema_version: 1, outcome: 'consolidated', stable_code: 'profile_consolidation_complete',
    transaction_id: transactionId, primary_home: item.home, backup_path: backup, receipt_path: path,
    credential_adoption_status: status, consumed_recovery_ids: [sourceId], revision: 1, errors: [],
    configuration_source_recovery_id: credential ? sourceId : null,
    configuration_source_credential_path: credential ? join(backup, 'recovery-profiles', sourceId, 'desktop-credential.json') : null,
    configuration_source_credential_sha256: credential ? 'a'.repeat(64) : null,
    configuration_source_credential_size: credential ? 42 : null,
    session_merges: [], ...overrides,
  }
  fs.mkdirSync(backup, { recursive: true })
  fs.writeFileSync(path, JSON.stringify(payload))
  return { path, backup, payload }
}

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

  for (const [name, maximumBytes] of [['.env', 128 * 1024], ['config.toml', 1024 * 1024]]) {
    const item = fixture(`growing-${assertions}`)
    const path = join(item.home, name)
    fs.writeFileSync(path, '# initial\n')
    const originalOpen = fs.openSync
    const originalStat = fs.fstatSync
    const originalRead = fs.readSync
    let handle
    let grew = false
    let bytesRead = 0
    fs.openSync = (pathToOpen, ...args) => {
      const fd = originalOpen(pathToOpen, ...args)
      if (pathToOpen === path && handle === undefined) handle = fd
      return fd
    }
    fs.fstatSync = (fd, ...args) => {
      const info = originalStat(fd, ...args)
      if (fd === handle && !grew) {
        // The initial descriptor check sees the original size; the actual read
        // races with an in-place append that exceeds either metadata limit.
        grew = true
        fs.appendFileSync(path, Buffer.alloc(2 * 1024 * 1024, 0x20))
      }
      return info
    }
    fs.readSync = (fd, ...args) => {
      const count = originalRead(fd, ...args)
      if (fd === handle) bytesRead += count
      return count
    }
    syncBuiltinESMExports()
    try {
      assert.equal(check(item, false).reason, 'metadata_unavailable')
      assert.equal(grew, true)
      assert.ok(bytesRead > 0 && bytesRead <= maximumBytes + 1, `unbounded metadata read: ${bytesRead}`)
    } finally {
      fs.openSync = originalOpen
      fs.fstatSync = originalStat
      fs.readSync = originalRead
      syncBuiltinESMExports()
    }
    // Exact-limit files remain eligible, including when reads return short.
    fs.writeFileSync(path, `#${'x'.repeat(maximumBytes - 1)}`)
    fs.readSync = (fd, buffer, offset, length, position) =>
      originalRead(fd, buffer, offset, Math.min(length, 4096), position)
    syncBuiltinESMExports()
    try { check(item, true) } finally {
      fs.readSync = originalRead
      syncBuiltinESMExports()
    }
  }

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
    'extra = 07:32\n',
  ]) {
    const item = fixture(`toml-compatibility-${assertions}`, config)
    assert.match(check(item, false).reason, /^python_toml_/)
  }
  for (const config of [
    '# literal inline example { value = 1 }\n',
    'extra = { value = 1, nested = { enabled = true }, list = [1, 2,] }\n',
    'extra = "braces {}, # comments and 额外 are literal"\n',
    '"额外" = { "中文" = "value" }\n',
    'config_version = 0\n', 'config_version = 1\n', '',
  ]) check(fixture(`compatible-${assertions}`, config), true)
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
  const originalRead = fs.readSync
  let dotenvHandle
  const originalOpenDuringRead = fs.openSync
  fs.openSync = (path, ...args) => {
    const handle = originalOpenDuringRead(path, ...args)
    if (path === changingDotenvPath && dotenvHandle === undefined) dotenvHandle = handle
    return handle
  }
  fs.readSync = (handle, ...args) => {
    const count = originalRead(handle, ...args)
    if (handle === dotenvHandle) fs.appendFileSync(changingDotenvPath, '\n# changed')
    return count
  }
  syncBuiltinESMExports()
  try { check(dotenvDuringRead, false) } finally {
    fs.openSync = originalOpenDuringRead
    fs.readSync = originalRead
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
  for (const status of ['not_required', 'complete', 'pending']) {
    const item = fixture(`archived-${status}`)
    const receipt = archivedReceipt(item, status)
    // Completed credentials may already have been removed from the archive.
    const opens = []
    const originalOpen = fs.openSync
    fs.openSync = (path, ...args) => { opens.push(String(path)); return originalOpen(path, ...args) }
    syncBuiltinESMExports()
    try { check(item, status !== 'pending') } finally {
      fs.openSync = originalOpen
      syncBuiltinESMExports()
    }
    assert.deepEqual(opens.filter(path => path.startsWith(receipt.backup)), [receipt.path])
    assert.deepEqual(JSON.parse(fs.readFileSync(receipt.path, 'utf8')), receipt.payload)
  }
  const mixedReceipts = fixture('mixed-receipts')
  archivedReceipt(mixedReceipts, 'pending')
  archivedReceipt(mixedReceipts, 'complete', 1)
  check(mixedReceipts, false)
  for (const overrides of [
    { schema_version: 2 }, { outcome: 'noop' }, { stable_code: 'unknown' },
    { transaction_id: 'wrong' }, { primary_home: healthy.home },
    { backup_path: root }, { receipt_path: root }, { revision: -1 },
    { errors: ['unfinished'] }, { consumed_recovery_ids: ['../escape'] },
    { configuration_source_recovery_id: '31234567-89ab-4cde-8fab-0123456789ab' },
    { credential_adoption_status: 'unknown' }, { credential_adoption_status: 'not_required' },
    { configuration_source_credential_path: join(root, 'credential.json') },
    { configuration_source_credential_sha256: null }, { configuration_source_credential_size: -1 },
  ]) {
    const item = fixture(`invalid-receipt-${assertions}`)
    archivedReceipt(item, 'complete', 0, overrides)
    check(item, false)
  }
  const manyReceipts = fixture('many-receipts')
  for (let index = 0; index < 32; index++) archivedReceipt(manyReceipts, 'complete', index)
  check(manyReceipts, true)
  archivedReceipt(manyReceipts, 'complete', 32)
  check(manyReceipts, false)
  const receiptBudget = fixture('receipt-byte-budget')
  for (let index = 0; index < 7; index++) archivedReceipt(receiptBudget, 'complete', index, { padding: ' '.repeat(64 * 1024) })
  check(receiptBudget, true)
  archivedReceipt(receiptBudget, 'complete', 7, { padding: ' '.repeat(64 * 1024) })
  check(receiptBudget, false)
  const oversizedReceipt = fixture('oversized-receipt')
  archivedReceipt(oversizedReceipt, 'complete', 0, { padding: ' '.repeat(128 * 1024) })
  check(oversizedReceipt, false)
  for (const change of ['receipt-pending', 'new-transaction', 'new-recovery', 'new-backups']) {
    const item = fixture(`archive-race-${change}`)
    const receipt = change === 'new-backups' ? null : archivedReceipt(item, 'complete')
    fs.mkdirSync(join(item.userData, 'recovery-profiles'))
    const originalOpen = fs.openSync
    let changed = false
    fs.openSync = (path, ...args) => {
      if (!changed && path === join(item.home, 'config.toml')) {
        changed = true
        if (change === 'receipt-pending') fs.writeFileSync(receipt.path, JSON.stringify({ ...receipt.payload, credential_adoption_status: 'pending' }))
        else if (change === 'new-recovery') fs.mkdirSync(join(item.userData, 'recovery-profiles', 'new-source'))
        else archivedReceipt(item, 'pending', 1)
      }
      return originalOpen(path, ...args)
    }
    syncBuiltinESMExports()
    try { assert.equal(check(item, false).reason, 'metadata_changed') } finally {
      fs.openSync = originalOpen
      syncBuiltinESMExports()
    }
    assert.equal(changed, true)
  }
  for (const linkAt of ['backups', 'transaction', 'receipt']) {
    const item = fixture(`archive-link-${linkAt}`)
    const receipt = archivedReceipt(item, 'complete')
    const path = linkAt === 'backups' ? join(item.userData, 'backups') : linkAt === 'transaction' ? receipt.backup : receipt.path
    const relocated = `${path}-original`
    fs.renameSync(path, relocated)
    if (linkAt === 'receipt') fs.linkSync(relocated, path)
    else fs.symlinkSync(relocated, path, process.platform === 'win32' ? 'junction' : 'dir')
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
  // Synchronizing both telemetry consumers must not reparse TOML for each
  // derived filename, or retain a previous config's external state directory.
  const consentEnd = main.indexOf('async function runDesktopTelemetryConsentSideEffect', stateFunctionStart)
  let syncConfig = ''
  let parseCalls = 0
  const mirrors = []
  const reliabilityPaths = []
  const growthPaths = []
  const consent = () => ({ reliability: { enabled: null }, growth: { enabled: null } })
  const consentContext = {
    configuredDesktopDataDirectory, join,
    parseToml: (raw) => { parseCalls++; return parseToml(raw) },
    readOptionalDesktopText: async (path) => path === join(healthy.home, 'config.toml') ? syncConfig : null,
    desktopTelemetryRuntimeGate: { close() {}, openAfterConsentSync() {} },
    resolveDesktopTelemetryConsent: consent, emptyDesktopTelemetryConsent: consent,
    CONSENT_MIRROR_SCHEMA_VERSION: 1,
    writeConsentMirror: async (path) => { mirrors.push(path) },
    desktopReliabilityTelemetry: { synchronize: (paths) => { reliabilityPaths.push(paths) } },
    desktopGrowthTelemetry: { synchronize: (paths) => { growthPaths.push(paths) } },
    refreshDesktopReliabilityForegroundState() {},
  }
  vm.createContext(consentContext)
  vm.runInContext(main.slice(stateFunctionStart, consentEnd), consentContext)
  for (const state of [externalState, join(healthy.home, 'state')]) {
    syncConfig = `state_dir = ${tomlPath(state)}\n`
    parseCalls = 0
    await consentContext.syncDesktopConsentMirror(healthy)
    const directory = join(state, 'telemetry')
    const mirror = join(directory, 'desktop-consent-mirror.json')
    assert.equal(parseCalls, 2, 'one path resolution per mirror write and consumer synchronization')
    assert.equal(mirrors.at(-1), mirror)
    assert.equal(reliabilityPaths.at(-1).consentMirrorPath, mirror)
    assert.equal(growthPaths.at(-1).consentMirrorPath, mirror)
    assert.equal(growthPaths.at(-1).telemetryDirectory, directory)
    assert.equal(reliabilityPaths.at(-1).spoolRoot, join(directory, 'desktop-early-spool'))
    assert.equal(growthPaths.at(-1).spoolRoot, join(directory, 'desktop-early-spool'))
  }
  const start = main.indexOf('async function inspectActiveProfileBeforeStartup')
  const end = main.indexOf('async function openOrResumeDesktopApp', start)
  const body = main.slice(start, end)
  async function startupCase(results, expected, consolidationFailure = null) {
    const calls = []
    const report = { outcome: 'ready', stable_code: 'canonical_workspace', allowed_actions: [] }
    const context = {
      activeDesktopProfile: () => ({ home: healthy.home }),
      inspectDesktopStartupMetadata: () => results.shift(),
      app: { getPath: () => healthy.userData }, process: { env: {} }, join,
      resolvedPathsEqual: (a, b) => resolve(a) === resolve(b),
      consolidateLegacyRecoveryProfilesBeforeStartup: async () => { calls.push('consolidate'); return consolidationFailure },
      deferProfileConsolidationMaintenance: () => { calls.push('defer') },
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
  // Historical maintenance findings must not block a healthy primary profile.
  await startupCase([{ canSkipMaintenance: false }], ['consolidate', 'orphan', 'inspect', 'defer'],
    { outcome: 'blocked', stable_code: 'synthetic_historical_warning' })
  assert.equal(configuredDesktopDataDirectory(healthy.home, 'state', 'state'), join(healthy.home, 'state'))
  if (process.platform !== 'win32') {
    assert.equal(configuredDesktopDataDirectory(healthy.home, 'link/../state', 'state'), null)
    assert.equal(configuredDesktopDataDirectory(healthy.home, '~/link/../state', 'state'), null)
    assert.equal(configuredDesktopDataDirectory(healthy.home, '//server/state', 'state'), null)
  }
  console.log(JSON.stringify({ ok: true, metadataCases: assertions, coordinatorCases: 4, telemetryPathCases: 2, sqliteContentReads: 0 }))
} finally {
  const resolvedRoot = resolve(root)
  assert.ok(resolvedRoot.startsWith(resolve(tmpdir()) + (process.platform === 'win32' ? '\\' : '/')))
  fs.rmSync(resolvedRoot, { recursive: true, force: true })
}
