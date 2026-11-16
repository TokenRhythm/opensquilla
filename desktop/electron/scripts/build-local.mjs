import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const scriptPath = fileURLToPath(import.meta.url)
const packageRoot = resolve(dirname(scriptPath), '..')
const packageVersion = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).version
const testOutputDirectory = resolve(packageRoot, '../../dist/desktop-electron/test')
const previewOutputDirectory = resolve(packageRoot, '../../dist/desktop-electron/preview')
const previewProductName = 'OpenSquilla Preview'
const previewInstallerGuid = '277ac0ed-967c-587e-920a-ccf5ab1172f6'

export function createLocalTestBuildConfig(now = new Date(), { production = false, preview = false } = {}) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(now)
  const part = type => parts.find(item => item.type === type).value
  const stamp = `${part('year')}${part('month')}${part('day')}-${part('hour')}${part('minute')}${part('second')}`
  const outputDirectory = preview ? previewOutputDirectory : testOutputDirectory
  return {
    directories: { output: production ? join(outputDirectory, 'production') : outputDirectory },
    artifactName: `OpenSquilla-${preview ? 'Preview-' : ''}\${version}-test-${production ? 'production-' : ''}${stamp}-\${os}-\${arch}.\${ext}`,
    extraMetadata: {
      version: packageVersion,
      opensquillaTokenRhythmOrigin: production ? 'https://tokenrhythm.studio' : 'https://gpt.tokenrhythm.studio',
      ...(preview ? {
        name: 'opensquilla-preview',
        productName: previewProductName,
        opensquillaDistribution: 'preview',
      } : {}),
    },
    ...(preview ? {
      appId: 'ai.opensquilla.desktop.preview',
      productName: previewProductName,
      win: { executableName: previewProductName },
      nsis: {
        guid: previewInstallerGuid,
        shortcutName: previewProductName,
        allowToChangeInstallationDirectory: false,
      },
      protocols: [{ name: previewProductName, schemes: ['opensquilla-preview'] }],
    } : {}),
  }
}

export function parseLocalBuildArgs(args) {
  const [mode, ...flags] = args
  if (!['pack', 'dist'].includes(mode)
    || new Set(flags).size !== flags.length
    || flags.some(flag => !['--prepared', '--test', '--production', '--preview'].includes(flag))
    || ((flags.includes('--production') || flags.includes('--preview')) && !flags.includes('--test'))) {
    throw new Error('Usage: node scripts/build-local.mjs pack|dist [--prepared] [--test [--production] [--preview]]')
  }
  return { mode, prepared: flags.includes('--prepared'), test: flags.includes('--test'), production: flags.includes('--production'), preview: flags.includes('--preview') }
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

export function createLocalWindowsTestBuildConfig(overrides = {}) {
  return { ...overrides, forceCodeSigning: false, win: { ...overrides.win, signExecutable: false } }
}

async function buildLocalWindowsTest(mode, overrides) {
  const { build, Platform, Arch } = await import('electron-builder')
  await withLocalMacBuildEnvironment(() => build({
    projectDir: packageRoot,
    targets: Platform.WINDOWS.createTarget(mode === 'pack' ? 'dir' : 'nsis', Arch.x64),
    publish: 'never',
    config: createLocalWindowsTestBuildConfig(overrides),
  }))
}

export async function runLocalBuild({ mode, prepared, test = false, production = false, preview = false }, {
  platform = process.platform, runScript = runNpmScript, buildMac = buildLocalMac,
  buildWindowsTest = buildLocalWindowsTest,
} = {}) {
  if (production && !test) throw new Error('Production test packaging requires --test')
  if (preview && !test) throw new Error('Preview packaging requires --test')
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
  const overrides = test ? createLocalTestBuildConfig(new Date(), { production, preview }) : {}
  if (test) console.log(`Local test package: ${overrides.artifactName}; app version: ${overrides.extraMetadata.version}; account/API origin: ${overrides.extraMetadata.opensquillaTokenRhythmOrigin}`)
  if (platform === 'win32') await buildWindowsTest(mode, overrides)
  else await buildMac(mode, overrides)
  await runScript('verify:package', test
    ? {
      ...process.env, OPEN_SQUILLA_PACKAGE_OUTPUT: overrides.directories.output,
      OPENSQUILLA_EXPECT_PACKAGE_VERSION: overrides.extraMetadata.version,
      OPENSQUILLA_EXPECT_ACCOUNT_ORIGIN: overrides.extraMetadata.opensquillaTokenRhythmOrigin,
      ...(preview ? {
        OPENSQUILLA_EXPECT_DISTRIBUTION: overrides.extraMetadata.opensquillaDistribution,
        OPENSQUILLA_EXPECT_PACKAGE_NAME: overrides.extraMetadata.name,
        OPENSQUILLA_EXPECT_PRODUCT_NAME: overrides.productName,
      } : {}),
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
