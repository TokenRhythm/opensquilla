import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import {
  assertRuntimeSetReady,
  defaultManifestPath,
  defaultRuntimeCacheRoot,
  defaultRuntimeRoot,
  downloadVerifiedAsset,
  loadRuntimeManifest,
  packagedRuntimeTarget,
  stripExtractedComponents,
  tarExtractArgs,
  validateRuntimeManifest,
  windowsZipExtractSpec,
} from './fetch-bundled-runtimes.mjs'

const root = await mkdtemp(join(tmpdir(), 'opensquilla-runtime-test-'))

try {
  assert.equal(
    packagedRuntimeTarget(
      '/tmp/dist/desktop-electron/mac-arm64/OpenSquilla.app/Contents/Resources',
      'darwin',
      'x64',
    ),
    'darwin-arm64',
  )
  assert.equal(
    defaultRuntimeCacheRoot.startsWith(defaultRuntimeRoot),
    false,
    'download archives must stay outside packaged runtime resources',
  )
  assert.deepEqual(
    tarExtractArgs(
      String.raw`Z:\fixture\.runtime-cache\python.tar.gz`,
      String.raw`Z:\fixture\runtime\python.staging`,
      1,
      'win32',
    ),
    [
      '--force-local',
      '-xf',
      'Z:/fixture/.runtime-cache/python.tar.gz',
      '-C',
      'Z:/fixture/runtime/python.staging',
      '--strip-components=1',
    ],
    'GNU tar must treat Windows drive-letter paths as local archives',
  )
  assert.deepEqual(
    tarExtractArgs('/tmp/python.tar.gz', '/tmp/python.staging', 0, 'darwin'),
    ['-xf', '/tmp/python.tar.gz', '-C', '/tmp/python.staging'],
    'non-Windows tar arguments must stay portable',
  )
  assert.deepEqual(
    windowsZipExtractSpec(
      String.raw`Z:\fixture\.runtime-cache\node.zip`,
      String.raw`Z:\fixture\runtime\node.staging`,
    ),
    {
      command: 'powershell.exe',
      args: [
        '-NoLogo',
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        'Expand-Archive -LiteralPath $env:OPENSQUILLA_RUNTIME_ARCHIVE '
          + '-DestinationPath $env:OPENSQUILLA_RUNTIME_DESTINATION -Force',
      ],
      environment: {
        OPENSQUILLA_RUNTIME_ARCHIVE: String.raw`Z:\fixture\.runtime-cache\node.zip`,
        OPENSQUILLA_RUNTIME_DESTINATION: String.raw`Z:\fixture\runtime\node.staging`,
      },
    },
    'Windows zip extraction must use the system archive command with literal paths',
  )
  const extracted = join(root, 'extracted')
  await mkdir(join(extracted, 'node-wrapper', 'bin'), { recursive: true })
  await writeFile(join(extracted, 'node-wrapper', 'bin', 'node.exe'), 'node fixture')
  await stripExtractedComponents(extracted, 1)
  assert.equal(await readFile(join(extracted, 'bin', 'node.exe'), 'utf8'), 'node fixture')
  const releaseManifest = await loadRuntimeManifest(defaultManifestPath)
  const requiredTargets = [
    'darwin-arm64',
    'darwin-x64',
    'linux-arm64',
    'linux-x64',
    'windows-arm64',
    'windows-x64',
  ]
  assert.deepEqual(Object.keys(releaseManifest.assets).sort(), requiredTargets)
  const releaseAssetIds = new Set()
  for (const [target, assets] of Object.entries(releaseManifest.assets)) {
    assert.deepEqual(
      Object.keys(assets).sort(),
      target.startsWith('windows-')
        ? ['gitBash', 'node', 'python']
        : ['node', 'python'],
    )
    assert.equal(assets.node.version, '24.19.0')
    assert.equal(assets.python.version, '3.13.15+20260814')
    if (target.startsWith('windows-')) {
      assert.equal(assets.gitBash.version, '2.55.0.windows.5')
    }
    for (const asset of Object.values(assets)) {
      assert.equal(releaseAssetIds.has(asset.id), false, `duplicate runtime asset id: ${asset.id}`)
      releaseAssetIds.add(asset.id)
    }
  }

  const source = join(root, 'source.bin')
  const destination = join(root, 'download.bin')
  await writeFile(source, 'pinned runtime fixture')
  const sha256 = '20d19703ab25f1f20d069f1c8a30c68338cd36ec977f5462a52d14b48b375483'
  const asset = {
    id: 'fixture',
    version: '1.0.0',
    url: pathToFileURL(source).href,
    sha256,
    archiveType: 'zip',
    installDir: 'fixture',
    stripComponents: 0,
    binDirs: ['.'],
    executables: { fixture: 'fixture.exe' },
  }

  validateRuntimeManifest({
    schemaVersion: 1,
    runtimeSet: 'test',
    assets: { 'windows-x64': { python: asset, node: asset, gitBash: asset } },
  })
  validateRuntimeManifest({
    schemaVersion: 1,
    runtimeSet: 'portable-test',
    assets: { 'linux-x64': { python: asset, node: asset } },
  })
  assert.throws(
    () => validateRuntimeManifest({
      schemaVersion: 1,
      runtimeSet: 'windows-incomplete',
      assets: { 'windows-x64': { python: asset, node: asset } },
    }),
    /gitBash/,
  )

  await downloadVerifiedAsset(asset, destination)
  assert.equal(await readFile(destination, 'utf8'), 'pinned runtime fixture')

  await assert.rejects(
    downloadVerifiedAsset({ ...asset, sha256: '0'.repeat(64) }, join(root, 'bad.bin')),
    /checksum mismatch/,
  )

  await assert.rejects(
    assertRuntimeSetReady({
      manifest: {
        schemaVersion: 1,
        runtimeSet: 'test',
        assets: { 'windows-x64': { python: asset, node: asset, gitBash: asset } },
      },
      runtimeRoot: join(root, 'missing'),
      target: 'windows-x64',
      executeCommands: false,
    }),
    /runtime is missing/,
  )

  assert.throws(
    () => validateRuntimeManifest({
      schemaVersion: 1,
      runtimeSet: 'test',
      assets: { 'windows-x64': { python: { ...asset, installDir: '../escape' } } },
    }),
    /installDir/,
  )
} finally {
  await rm(root, { recursive: true, force: true })
}

console.log('Bundled runtime tests passed.')
