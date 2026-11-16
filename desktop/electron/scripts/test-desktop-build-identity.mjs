import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { desktopBuildUserDataPath, resolveDesktopBuildIdentity } from '../dist/desktop-build-identity.js'

test('preview profiles remain separate for stable and legacy test version spellings', () => {
  const stable = resolveDesktopBuildIdentity({})
  const preview = resolveDesktopBuildIdentity({ opensquillaDistribution: 'preview' })
  assert.equal(stable.isolated, false)
  assert.equal(stable.updatesEnabled, true)
  assert.equal(preview.updatesEnabled, false)
  assert.notEqual(preview.appId, stable.appId)
  assert.notEqual(preview.scheme, stable.scheme)
  for (const version of ['0.5.8', '0.5.8-test.20261116', '0.5.8-preview.1']) {
    const stablePath = desktopBuildUserDataPath(stable, resolve('app-data'), resolve('stable-data'), version)
    const previewPath = desktopBuildUserDataPath(preview, resolve('app-data'), resolve('stable-data'), version)
    assert.equal(previewPath, join(resolve('app-data'), 'opensquilla-preview'))
    assert.notEqual(previewPath, stablePath)
  }
  assert.equal(desktopBuildUserDataPath(stable, 'unused', resolve('stable-data'), '0.5.8'), resolve('stable-data'))
  assert.throws(() => resolveDesktopBuildIdentity({ opensquillaDistribution: 'unsupported' }), /Unsupported/)
})

test('preview metadata and installer configuration agree on the application namespace', async () => {
  const { createLocalTestBuildConfig } = await import('./build-local.mjs')
  const config = createLocalTestBuildConfig(new Date('2026-11-01T00:00:00Z'), { preview: true, production: true })
  const identity = resolveDesktopBuildIdentity(config.extraMetadata)
  assert.equal(identity.name, config.productName)
  assert.equal(identity.appId, config.appId)
  assert.deepEqual(config.protocols, [{ name: identity.name, schemes: [identity.scheme] }])
  assert.equal(config.win.executableName, identity.name)
  assert.notEqual(config.extraMetadata.name, '@opensquilla/desktop-electron')
})

test('the preview cannot enter the production updater even when packaged on Windows', () => {
  const main = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
  const match = main.match(/function desktopUpdateManaged\(\): boolean \{([\s\S]*?)\n\}/)
  assert.ok(match)
  const managed = new Function('desktopBuildIdentity', 'app', 'process', 'desktopNetworkObservabilityDisabled', match[1])
  const app = { isPackaged: true }
  for (const platform of ['win32', 'darwin']) {
    const process = { platform, env: {} }
    assert.equal(managed(resolveDesktopBuildIdentity({}), app, process, () => false), true)
    assert.equal(managed(resolveDesktopBuildIdentity({ opensquillaDistribution: 'preview' }), app, process, () => false), false)
  }
})
