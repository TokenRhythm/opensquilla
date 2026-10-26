import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const scriptPath = fileURLToPath(import.meta.url)
const packageRoot = resolve(dirname(scriptPath), '..')
const packageVersion = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).version
const testOutputDirectory = resolve(packageRoot, '../../dist/desktop-electron/test')

export function createLocalTestBuildConfig(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(now)
  const part = type => parts.find(item => item.type === type).value
  const stamp = `${part('year')}${part('month')}${part('day')}-${part('hour')}${part('minute')}${part('second')}`
  return {
    directories: { output: testOutputDirectory },
    artifactName: `OpenSquilla-\${version}-test-${stamp}-\${os}-\${arch}.\${ext}`,
    extraMetadata: {
      version: packageVersion,
      opensquillaTokenRhythmOrigin: 'https://gpt.tokenrhythm.studio',
    },
  }
}

export function parseLocalBuildArgs(args) {
  const [mode, ...flags] = args
  if (!['pack', 'dist'].includes(mode)
    || new Set(flags).size !== flags.length
    || flags.some(flag => !['--prepared', '--test'].includes(flag))) {
    throw new Error('Usage: node scripts/build-local.mjs pack|dist [--prepared] [--test]')
  }
  return { mode, prepared: flags.includes('--prepared'), test: flags.includes('--test') }
}

function runNpmScript(script, env = process.env) {
  const npmCli = process.env.npm_execpath
  const result = spawnSync(npmCli ? process.execPath : 'npm',
    npmCli ? [npmCli, 'run', script] : ['run', script], {
      cwd: packageRoot,
      env,
      stdio: 'inherit',
      shell: !npmCli && process.platform === 'win32',
    })
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`npm run ${script} failed (${result.signal ?? result.status ?? 'unknown exit'}).`)
  }
}

export function createLocalMacBuildConfig(identity, {
  signAsync, localMacSignOptions, verifyLocalMacSignature,
}) {
  const signedApps = new Set()
  return {
    forceCodeSigning: true,
    mac: {
      // The hook supplies the persistent identity. This sentinel avoids requiring
      // that a local self-signed certificate be globally trusted for discovery.
      identity: '-',
      notarize: false,
      sign: async (builderOpts) => {
        await signAsync(localMacSignOptions(builderOpts, identity))
        await verifyLocalMacSignature(builderOpts.app, identity)
        signedApps.add(resolve(builderOpts.app))
      },
    },
    afterSign: async (context) => {
      const appPath = join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`)
      if (!signedApps.has(resolve(appPath))) {
        throw new Error('Local macOS packaging did not run the required persistent signing step.')
      }
      // electron-builder may run additional signing work after the custom
      // signer. Re-check the final bundle here so a future builder upgrade
      // cannot silently replace the certificate-pinned identity with an
      // ad-hoc signature.
      await verifyLocalMacSignature(appPath, identity)
    },
  }
}

export async function withLocalMacBuildEnvironment(callback, env = process.env) {
  const overrides = {
    CSC_LINK: undefined,
    CSC_KEY_PASSWORD: undefined,
    CSC_KEYCHAIN: undefined,
    CSC_NAME: undefined,
    CSC_IDENTITY_AUTO_DISCOVERY: 'false',
  }
  const previous = new Map(Object.keys(overrides).map(key => [key, env[key]]))
  try {
    for (const [key, value] of Object.entries(overrides)) {
      if (value === undefined) delete env[key]
      else env[key] = value
    }
    return await callback()
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete env[key]
      else env[key] = value
    }
  }
}

async function buildLocalMac(mode, overrides = {}) {
  const [{ build, createTargets, Platform }, { signAsync }, signing] = await Promise.all([
    import('electron-builder'), import('@electron/osx-sign'), import('./mac-local-signing.mjs'),
  ])
  await signing.withLocalMacSigning(async (identity) => {
    const config = { ...createLocalMacBuildConfig(identity, { signAsync, ...signing }), ...overrides }
    const installedElectronDist = join(packageRoot, 'node_modules', 'electron', 'dist')
    if (existsSync(join(installedElectronDist, 'Electron.app'))) {
      config.electronDist = installedElectronDist
    }
    await withLocalMacBuildEnvironment(() => build({
      projectDir: packageRoot,
      targets: createTargets([Platform.MAC], mode === 'pack' ? 'dir' : null, process.arch),
      publish: 'never',
      config,
    }))
  })
}

async function buildLocalWindowsTest(mode, overrides) {
  const { build, Platform, Arch } = await import('electron-builder')
  await withLocalMacBuildEnvironment(() => build({
    projectDir: packageRoot,
    targets: Platform.WINDOWS.createTarget(mode === 'pack' ? 'dir' : 'nsis', Arch.x64),
    publish: 'never',
    config: { ...overrides, forceCodeSigning: false, win: { signExecutable: false } },
  }))
}

export async function runLocalBuild({ mode, prepared, test = false }, {
  platform = process.platform, runScript = runNpmScript, buildMac = buildLocalMac,
  buildWindowsTest = buildLocalWindowsTest,
} = {}) {
  if (platform !== 'darwin' && !test) {
    await runScript(`${mode}${prepared ? ':prepared' : ''}`)
    return
  }
  if (platform !== 'darwin' && platform !== 'win32') throw new Error('Local test packaging requires macOS or Windows.')
  if (!prepared) {
    await runScript('build:web')
    await runScript('build:gateway')
  }
  await runScript('build')
  await runScript('verify:prepared')
  const overrides = test ? createLocalTestBuildConfig() : {}
  if (test) console.log(`Local test package: ${overrides.artifactName}; app version: ${overrides.extraMetadata.version}; account/API origin: ${overrides.extraMetadata.opensquillaTokenRhythmOrigin}`)
  if (platform === 'win32') await buildWindowsTest(mode, overrides)
  else await buildMac(mode, overrides)
  await runScript('verify:package', test
    ? {
      ...process.env, OPEN_SQUILLA_PACKAGE_OUTPUT: testOutputDirectory,
      OPENSQUILLA_EXPECT_PACKAGE_VERSION: overrides.extraMetadata.version,
      OPENSQUILLA_EXPECT_ACCOUNT_ORIGIN: overrides.extraMetadata.opensquillaTokenRhythmOrigin,
    }
    : process.env)
}

if (process.argv[1] && resolve(process.argv[1]) === scriptPath) {
  try {
    await runLocalBuild(parseLocalBuildArgs(process.argv.slice(2)))
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
