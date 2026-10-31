import assert from 'node:assert/strict'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import test from 'node:test'
import { readFileSync } from 'node:fs'

import {
  createLocalMacBuildConfig, createLocalTestBuildConfig, parseLocalBuildArgs, runLocalBuild,
  withLocalMacBuildEnvironment,
} from './build-local.mjs'

const packageVersion = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version

test('local CLI accepts only a packaging target and optional prepared mode', () => {
  assert.deepEqual(parseLocalBuildArgs(['pack']), { mode: 'pack', prepared: false, test: false, production: false })
  assert.deepEqual(parseLocalBuildArgs(['dist', '--prepared']), { mode: 'dist', prepared: true, test: false, production: false })
  assert.deepEqual(parseLocalBuildArgs(['dist', '--test', '--prepared']), { mode: 'dist', prepared: true, test: true, production: false })
  assert.deepEqual(parseLocalBuildArgs(['dist', '--test', '--production']), { mode: 'dist', prepared: false, test: true, production: true })
  for (const args of [[], ['release'], ['pack', '--unsigned'], ['dist', '--prepared', '--prepared'], ['dist', '--production']]) {
    assert.throws(() => parseLocalBuildArgs(args), /Usage:/)
  }
})

for (const platform of ['darwin', 'win32']) {
  test(`${platform} production test packages embed production and verify their separate output`, async () => {
    const calls = []
    const verifyBuild = async (mode, config) => {
      assert.equal(mode, 'dist')
      assert.equal(config.extraMetadata.opensquillaTokenRhythmOrigin, 'https://tokenrhythm.studio')
      assert.equal(config.extraMetadata.version, packageVersion)
      assert.match(config.artifactName, /-test-production-\d{8}-\d{6}-/)
      assert.ok(config.directories.output.replaceAll('\\', '/').endsWith('/test/production'))
      calls.push('package')
    }
    await runLocalBuild({ mode: 'dist', prepared: true, test: true, production: true }, {
      platform,
      buildMac: verifyBuild,
      buildWindowsTest: verifyBuild,
      runScript: async (script, env) => {
        calls.push(script)
        if (script === 'verify:package') {
          assert.equal(env.OPENSQUILLA_EXPECT_ACCOUNT_ORIGIN, 'https://tokenrhythm.studio')
          assert.equal(env.OPENSQUILLA_EXPECT_PACKAGE_VERSION, packageVersion)
          assert.ok(env.OPEN_SQUILLA_PACKAGE_OUTPUT.replaceAll('\\', '/').endsWith('/test/production'))
        }
      },
    })
    assert.deepEqual(calls, ['build', 'verify:prepared', 'package', 'verify:package'])
  })
}

test('test packages keep app identity and version, stamping only the installer filename', () => {
  const config = createLocalTestBuildConfig(new Date('2026-10-26T07:02:03Z'))
  assert.deepEqual(config.extraMetadata, {
    version: packageVersion,
    opensquillaTokenRhythmOrigin: 'https://gpt.tokenrhythm.studio',
  })
  assert.equal(config.artifactName, 'OpenSquilla-${version}-test-20261026-150203-${os}-${arch}.${ext}')
  assert.equal(config.productName, undefined)
  assert.equal(config.appId, undefined)
  assert.ok(config.directories.output.replaceAll('\\', '/').endsWith('/dist/desktop-electron/test'))
})

test('test packaging forwards embedded configuration and verifies only its output', async () => {
  const calls = []
  await runLocalBuild({ mode: 'dist', prepared: true, test: true }, {
    platform: 'darwin',
    buildMac: async (mode, config) => {
      assert.equal(mode, 'dist')
      assert.equal(config.extraMetadata.version, packageVersion)
      assert.match(config.artifactName, /-test-\d{8}-\d{6}-/)
      assert.equal(config.extraMetadata.opensquillaTokenRhythmOrigin, 'https://gpt.tokenrhythm.studio')
      calls.push('package')
    },
    runScript: async (script, env) => {
      calls.push(script)
      if (script === 'verify:package') {
        assert.ok(env.OPEN_SQUILLA_PACKAGE_OUTPUT.replaceAll('\\', '/').endsWith('/dist/desktop-electron/test'))
        assert.equal(env.OPENSQUILLA_EXPECT_PACKAGE_VERSION, packageVersion)
        assert.equal(env.OPENSQUILLA_EXPECT_ACCOUNT_ORIGIN, 'https://gpt.tokenrhythm.studio')
      }
    },
  })
  assert.deepEqual(calls, ['build', 'verify:prepared', 'package', 'verify:package'])
})

test('Windows and Linux retain their existing packaging entries', async () => {
  for (const platform of ['win32', 'linux']) {
    for (const mode of ['pack', 'dist']) {
      for (const prepared of [false, true]) {
        const commands = []
        await runLocalBuild({ mode, prepared }, {
          platform,
          runScript: async script => commands.push(script),
          buildMac: async () => assert.fail('macOS signing must not run on another platform'),
        })
        assert.deepEqual(commands, [`${mode}${prepared ? ':prepared' : ''}`])
      }
    }
  }
})

test('Windows test packages build fresh resources and keep UAT metadata and verification', async () => {
  const calls = []
  await runLocalBuild({ mode: 'dist', prepared: false, test: true }, {
    platform: 'win32',
    buildMac: async () => assert.fail('Windows must not use macOS signing'),
    buildWindowsTest: async (mode, config) => {
      assert.equal(mode, 'dist')
      assert.equal(config.extraMetadata.version, packageVersion)
      assert.match(config.artifactName, /-test-\d{8}-\d{6}-/)
      assert.equal(config.extraMetadata.opensquillaTokenRhythmOrigin, 'https://gpt.tokenrhythm.studio')
      calls.push('windows-package')
    },
    runScript: async (script, env) => {
      calls.push(script)
      if (script === 'verify:package') {
        assert.equal(env.OPENSQUILLA_EXPECT_ACCOUNT_ORIGIN, 'https://gpt.tokenrhythm.studio')
        assert.ok(env.OPEN_SQUILLA_PACKAGE_OUTPUT.replaceAll('\\', '/').endsWith('/test'))
      }
    },
  })
  assert.deepEqual(calls, ['build:web', 'build:gateway', 'build', 'verify:prepared', 'windows-package', 'verify:package'])
})

test('unsupported test platforms stop before building any resources', async () => {
  await assert.rejects(runLocalBuild({ mode: 'dist', test: true }, {
    platform: 'linux', runScript: async () => assert.fail('must stop before building'),
  }), /requires macOS or Windows/)
})

test('full local macOS builds prepare the runtime before packaging', async () => {
  const commands = []
  await runLocalBuild({ mode: 'dist', prepared: false }, {
    platform: 'darwin',
    runScript: async script => commands.push(script),
    buildMac: async mode => commands.push(`package:${mode}`),
  })
  assert.deepEqual(commands, [
    'build:web', 'build:gateway', 'build', 'verify:prepared', 'package:dist', 'verify:package',
  ])
})

test('prepared builds reject stale runtime inputs before entering signing', async () => {
  const commands = []
  await assert.rejects(runLocalBuild({ mode: 'pack', prepared: true }, {
    platform: 'darwin',
    runScript: async script => {
      commands.push(script)
      if (script === 'verify:prepared') throw new Error('stale runtime')
    },
    buildMac: async () => assert.fail('stale runtime must not be packaged'),
  }), /stale runtime/)
  assert.deepEqual(commands, ['build', 'verify:prepared'])
})

test('local signing failure cannot fall back to another packaging entry', async () => {
  const commands = []
  await assert.rejects(runLocalBuild({ mode: 'pack', prepared: true }, {
    platform: 'darwin',
    runScript: async script => commands.push(script),
    buildMac: async () => { throw new Error('signature rejected') },
  }), /signature rejected/)
  assert.deepEqual(commands, ['build', 'verify:prepared'])
})

test('release certificate settings are isolated and restored after successful and failed builds', async () => {
  for (const shouldThrow of [false, true]) {
    for (const autoDiscovery of [undefined, 'true']) {
      const env = {
        CSC_LINK: 'synthetic-release-certificate',
        CSC_KEY_PASSWORD: 'synthetic-password',
        CSC_KEYCHAIN: 'synthetic-release-keychain',
        CSC_NAME: 'synthetic-release-signer',
        UNRELATED: 'preserved',
      }
      if (autoDiscovery !== undefined) env.CSC_IDENTITY_AUTO_DISCOVERY = autoDiscovery
      const previous = { ...env }
      const operation = withLocalMacBuildEnvironment(async () => {
        assert.deepEqual(env, { UNRELATED: 'preserved', CSC_IDENTITY_AUTO_DISCOVERY: 'false' })
        await Promise.resolve()
        if (shouldThrow) throw new Error('builder failed')
        return 'packaged'
      }, env)
      if (shouldThrow) await assert.rejects(operation, /builder failed/)
      else assert.equal(await operation, 'packaged')
      assert.deepEqual(env, previous)
    }
  }
})

test('only a successfully signed and verified application passes the artifact gate', async () => {
  const identity = { certificateSha1: 'a'.repeat(40), keychainPath: 'synthetic-build-keychain' }
  const appOutDir = join(tmpdir(), 'opensquilla-local-build-fixture')
  const appPath = join(appOutDir, 'OpenSquilla.app')
  const context = { appOutDir, packager: { appInfo: { productFilename: 'OpenSquilla' } } }
  for (const failureAt of [null, 'sign', 'verify']) {
    const calls = []
    const config = createLocalMacBuildConfig(identity, {
      localMacSignOptions: (options, signer) => ({ ...options, identity: signer.certificateSha1 }),
      signAsync: async options => {
        assert.equal(options.identity, identity.certificateSha1)
        calls.push('sign')
        if (failureAt === 'sign') throw new Error('sign failed')
      },
      verifyLocalMacSignature: async (path, signer) => {
        assert.equal(path, appPath)
        assert.equal(signer, identity)
        calls.push('verify')
        if (failureAt === 'verify') throw new Error('verify failed')
      },
    })
    assert.equal(config.forceCodeSigning, true)
    assert.equal(config.mac.notarize, false)
    await assert.rejects(config.afterSign(context), /required persistent signing/)
    if (failureAt) {
      await assert.rejects(config.mac.sign({ app: appPath }), new RegExp(`${failureAt} failed`))
      await assert.rejects(config.afterSign(context), /required persistent signing/)
    } else {
      await config.mac.sign({ app: appPath })
      await config.afterSign(context)
    }
    assert.deepEqual(calls, failureAt === 'sign' ? ['sign'] : ['sign', 'verify', ...(failureAt ? [] : ['verify'])])
  }
})
