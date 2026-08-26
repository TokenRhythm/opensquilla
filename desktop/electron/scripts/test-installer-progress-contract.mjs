import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import {
  INSTALLER_PROGRESS_INCLUDE,
  validateInstallerProgressSource,
  verifyInstallerProgressPolicy,
} from './installer-progress-policy.mjs'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const packageRoot = resolve(scriptDir, '..')
const packageJson = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
const includePath = resolve(packageRoot, INSTALLER_PROGRESS_INCLUDE)
const includeSource = await readFile(includePath, 'utf8')
const appBuilderLib = join(packageRoot, 'node_modules', 'app-builder-lib')
const assistedInstallerSource = await readFile(join(appBuilderLib, 'templates', 'nsis', 'assistedInstaller.nsh'), 'utf8')
const installSectionSource = await readFile(join(appBuilderLib, 'templates', 'nsis', 'installSection.nsh'), 'utf8')

assert.deepEqual(await verifyInstallerProgressPolicy(packageRoot, packageJson), [])
assert.ok(
  validateInstallerProgressSource(`${includeSource}\nDelete "$TEMP\\unexpected"`).some((failure) =>
    failure.includes('forbidden file-system mutation'),
  ),
  'display-only policy must reject file-system mutation',
)
assert.ok(
  validateInstallerProgressSource(includeSource.replace('!ifndef BUILD_UNINSTALLER\n', '')).some((failure) =>
    failure.includes('fully guarded by BUILD_UNINSTALLER'),
  ),
  'display-only policy must reject an unguarded installer include',
)

const policyFixtureRoot = await mkdtemp(join(tmpdir(), 'opensquilla-installer-policy-'))
try {
  const policyIncludePath = resolve(policyFixtureRoot, INSTALLER_PROGRESS_INCLUDE)
  const defaultInstallerInclude = join(policyFixtureRoot, 'build', 'installer.nsh')
  const linkedInstallerInclude = join(policyFixtureRoot, 'linked-installer')
  await mkdir(dirname(policyIncludePath), { recursive: true })
  await mkdir(dirname(defaultInstallerInclude), { recursive: true })
  await writeFile(policyIncludePath, includeSource, 'utf8')
  await mkdir(linkedInstallerInclude)
  await symlink(linkedInstallerInclude, defaultInstallerInclude, process.platform === 'win32' ? 'junction' : 'dir')
  assert.ok(
    (await verifyInstallerProgressPolicy(policyFixtureRoot, packageJson)).some((failure) =>
      failure.includes('default build/installer.nsh override must not be present'),
    ),
    'display-only policy must reject a symlinked default installer include',
  )
} finally {
  await rm(policyFixtureRoot, { recursive: true, force: true })
}

const pageHookIndex = assistedInstallerSource.indexOf('!insertmacro customPageAfterChangeDir')
const installFilesPageIndex = assistedInstallerSource.indexOf('!insertmacro MUI_PAGE_INSTFILES')
assert.ok(pageHookIndex >= 0, 'electron-builder must expose customPageAfterChangeDir')
assert.ok(
  pageHookIndex < installFilesPageIndex,
  'customPageAfterChangeDir must run before electron-builder declares the install-files page',
)

const installApplicationFilesIndex = installSectionSource.indexOf('!insertmacro installApplicationFiles')
const customInstallIndex = installSectionSource.indexOf('!insertmacro customInstall')
assert.ok(installApplicationFilesIndex >= 0, 'electron-builder installApplicationFiles hook is missing')
assert.ok(
  installApplicationFilesIndex < customInstallIndex,
  'customInstall must remain after electron-builder installs application files',
)

function nsisPath(path) {
  return process.platform === 'win32' ? path : path.replaceAll('\\', '/')
}

async function loadNsisTooling() {
  const appBuilderLibOutput = join(appBuilderLib, 'out')
  const nsisUtilImport = await import(
    pathToFileURL(join(appBuilderLibOutput, 'targets', 'nsis', 'nsisUtil.js')).href
  )
  const windowsToolsetImport = await import(pathToFileURL(join(appBuilderLibOutput, 'toolsets', 'windows.js')).href)
  const nsisUtil = nsisUtilImport.default ?? nsisUtilImport
  const windowsToolset = windowsToolsetImport.default ?? windowsToolsetImport
  const makensis = await windowsToolset.getMakeNsisPath(
    packageJson.build?.toolsets?.nsis,
    packageJson.build?.nsis?.customNsisBinary,
  )
  return {
    executable: makensis.path,
    env: makensis.env ?? {},
    templatesDir: nsisUtil.nsisTemplatesDir,
  }
}

async function verifyInstallFilesControls(tooling) {
  const nsisDir = tooling.env.NSISDIR
  assert.ok(nsisDir, 'locked legacy NSIS tooling must expose NSISDIR')
  const installFilesPage = await readFile(
    join(nsisDir, 'Contrib', 'Modern UI 2', 'Pages', 'InstallFiles.nsh'),
    'utf8',
  )
  const progressControlIndex = installFilesPage.indexOf('GetDlgItem $mui.InstFilesPage.ProgressBar $mui.InstFilesPage 1004')
  const textControlIndex = installFilesPage.indexOf('GetDlgItem $mui.InstFilesPage.Text $mui.InstFilesPage 1006')
  const showHookIndex = installFilesPage.indexOf('!insertmacro MUI_PAGE_FUNCTION_CUSTOM SHOW')
  assert.ok(progressControlIndex >= 0, 'NSIS install-files progress control id changed')
  assert.ok(textControlIndex >= 0, 'NSIS install-files text control id changed')
  assert.ok(
    progressControlIndex < showHookIndex && textControlIndex < showHookIndex,
    'NSIS must resolve install-files controls before invoking the custom show callback',
  )
}

function compileFixture(tooling, fixturePath) {
  const result = spawnSync(tooling.executable, ['-V2', fixturePath], {
    cwd: tooling.templatesDir,
    encoding: 'utf8',
    env: { ...process.env, ...tooling.env },
    windowsHide: true,
  })
  if (result.error) throw result.error
  assert.equal(
    result.status,
    0,
    `makensis failed for ${fixturePath}\n${result.stdout ?? ''}\n${result.stderr ?? ''}`,
  )
}

const temporaryRoot = await mkdtemp(join(tmpdir(), 'opensquilla-installer-progress-'))
try {
  const installerOutput = nsisPath(join(temporaryRoot, 'installer-fixture.exe'))
  const uninstallerOutput = nsisPath(join(temporaryRoot, 'uninstaller-fixture.exe'))
  const normalizedInclude = nsisPath(includePath)
  const installerFixture = join(temporaryRoot, 'installer-fixture.nsi')
  const uninstallerFixture = join(temporaryRoot, 'uninstaller-fixture.nsi')

  await writeFile(
    installerFixture,
    `Unicode true
!include "${normalizedInclude}"
!include "MUI2.nsh"
Name "OpenSquilla installer progress contract"
OutFile "${installerOutput}"
RequestExecutionLevel user
!insertmacro MUI_PAGE_WELCOME
!insertmacro customPageAfterChangeDir
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"
Section
  !insertmacro customInstall
SectionEnd
`,
    'utf8',
  )
  await writeFile(
    uninstallerFixture,
    `Unicode true
!define BUILD_UNINSTALLER
!include "${normalizedInclude}"
Name "OpenSquilla uninstaller progress contract"
OutFile "${uninstallerOutput}"
RequestExecutionLevel user
Section
SectionEnd
`,
    'utf8',
  )

  const tooling = await loadNsisTooling()
  await verifyInstallFilesControls(tooling)
  compileFixture(tooling, installerFixture)
  compileFixture(tooling, uninstallerFixture)
} finally {
  await rm(temporaryRoot, { recursive: true, force: true })
}

console.log('Installer progress display contract passed')
