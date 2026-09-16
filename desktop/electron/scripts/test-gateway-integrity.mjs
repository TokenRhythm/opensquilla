import assert from 'node:assert/strict'
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { assertGatewayResources, fileHash, gatewayInputs, verifyGatewayIntegrity, writeGatewayBuildRecord } from './gateway-integrity.mjs'

function fixture(t) {
  const repo = mkdtempSync(join(tmpdir(), 'gateway-integrity-'))
  t.after(() => rmSync(repo, { recursive: true, force: true }))
  function write(relative, content) {
    const path = join(repo, relative)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, content)
  }
  const router = 'src/opensquilla/squilla_router/models/v4.2_phase3_inference'
  write(`${router}/model.onnx`, 'model')
  write(`${router}/artifact_manifest.json`, JSON.stringify({
    schema_version: 1,
    files: [{ path: 'model.onnx', size_bytes: 5, sha256: fileHash(join(repo, router, 'model.onnx')) }],
  }))
  write('src/opensquilla/gateway/boot.py', 'current gateway')
  // Same numeric prefix is legal: compare full IDs, not max version or count.
  write('migrations/V010__one.py', 'first migration')
  write('migrations/V010__two.py', 'second migration')
  write('migrations/V040__document_resources.py', 'last migration')
  write('opensquilla-webui/dist/index.html', 'current UI')
  write('desktop/electron/scripts/pyinstaller_runtime_hooks/hook.py', 'hook')
  for (const path of [
    'pyproject.toml', 'uv.lock', 'hatch_build.py',
    'desktop/electron/package.json', 'desktop/electron/package-lock.json',
    'desktop/electron/scripts/build-gateway.mjs', 'desktop/electron/scripts/gateway-entry.py',
    'desktop/electron/scripts/gateway-integrity.mjs',
  ]) write(path, path)
  const runtime = join(repo, 'runtime')
  const packageDir = join(runtime, 'opensquilla-gateway', '_internal', 'opensquilla')
  mkdirSync(packageDir, { recursive: true })
  cpSync(join(repo, 'migrations'), join(packageDir, '_migrations'), { recursive: true })
  cpSync(join(repo, router), join(packageDir, 'squilla_router/models/v4.2_phase3_inference'), { recursive: true })
  write('runtime/opensquilla-gateway/opensquilla-gateway.exe', 'unsigned gateway')
  writeGatewayBuildRecord(repo, runtime, gatewayInputs(repo))
  return { repo, runtime, packageDir, write, router: join(packageDir, 'squilla_router/models/v4.2_phase3_inference') }
}

test('prepared output and a separately copied final bundle match source', (t) => {
  const { repo, runtime } = fixture(t)
  verifyGatewayIntegrity(repo, runtime, { prepared: true })
  const bundle = join(repo, 'final')
  cpSync(runtime, bundle, { recursive: true })
  verifyGatewayIntegrity(repo, bundle)
  // Signing is allowed only after the full prepared output check.
  writeFileSync(join(bundle, 'opensquilla-gateway/opensquilla-gateway.exe'), 'signed gateway')
  verifyGatewayIntegrity(repo, bundle)
  assert.throws(() => verifyGatewayIntegrity(repo, bundle, { prepared: true }), /prepared Gateway outputs/)
})

for (const fault of ['missing', 'changed', 'unexpected']) {
  test(`migration ${fault} fails in prepared and final outputs`, (t) => {
    const { repo, runtime, packageDir } = fixture(t)
    const migrations = join(packageDir, '_migrations')
    if (fault === 'missing') rmSync(join(migrations, 'V010__one.py'))
    if (fault === 'changed') writeFileSync(join(migrations, 'V010__one.py'), 'other migration')
    if (fault === 'unexpected') writeFileSync(join(migrations, 'V099__old.py'), 'unexpected')
    for (const prepared of [true, false]) {
      assert.throws(() => verifyGatewayIntegrity(repo, runtime, { prepared }), new RegExp(`migrations:.*${fault === 'changed' ? 'content changed' : fault}`))
    }
  })
}

for (const fault of ['missing manifest', 'empty manifest', 'missing model', 'same-size corruption', 'LFS pointer', 'traversal', 'duplicate']) {
  test(`Router ${fault} is rejected without executing a model`, (t) => {
    const { repo, runtime, router } = fixture(t)
    const manifestPath = join(router, 'artifact_manifest.json')
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
    if (fault === 'missing manifest') rmSync(manifestPath)
    if (fault === 'empty manifest') writeFileSync(manifestPath, JSON.stringify({ schema_version: 1, files: [] }))
    if (fault === 'missing model') rmSync(join(router, 'model.onnx'))
    if (fault === 'same-size corruption') writeFileSync(join(router, 'model.onnx'), 'other')
    if (fault === 'LFS pointer') writeFileSync(join(router, 'model.onnx'), 'version https://git-lfs.github.com/spec/v1')
    if (fault === 'traversal' || fault === 'duplicate') {
      if (fault === 'traversal') manifest.files[0].path = '../model.onnx'
      else manifest.files.push(manifest.files[0])
      writeFileSync(manifestPath, JSON.stringify(manifest))
      // Invalid manifests fail even when source and package agree.
      writeFileSync(join(repo, 'src/opensquilla/squilla_router/models/v4.2_phase3_inference/artifact_manifest.json'), JSON.stringify(manifest))
    }
    assert.throws(() => assertGatewayResources(repo, runtime), /manifest|Router/)
  })
}

for (const input of ['src/opensquilla/gateway/boot.py', 'uv.lock', 'opensquilla-webui/dist/index.html', 'desktop/electron/scripts/gateway-entry.py']) {
  test(`changed ${input} invalidates an otherwise complete runtime`, (t) => {
    const { repo, runtime, write } = fixture(t)
    write(input, 'changed input')
    assertGatewayResources(repo, runtime)
    assert.throws(() => verifyGatewayIntegrity(repo, runtime, { prepared: true }), /stale Gateway build inputs/)
  })
}

test('docs and Python caches do not invalidate prepared builds', (t) => {
  const { repo, runtime, write } = fixture(t)
  write('README.md', 'unrelated docs')
  write('src/opensquilla/__pycache__/boot.pyc', 'cache')
  write('runtime/opensquilla-gateway/_internal/opensquilla/__pycache__/boot.pyc', 'cache')
  verifyGatewayIntegrity(repo, runtime, { prepared: true })
})

test('build inputs changing during construction cannot leave a success record', (t) => {
  const { repo, runtime, write } = fixture(t)
  const inputs = gatewayInputs(repo)
  write('src/opensquilla/gateway/boot.py', 'changed during build')
  assert.throws(() => writeGatewayBuildRecord(repo, runtime, inputs), /inputs changed during Gateway build/)
  assert.throws(() => verifyGatewayIntegrity(repo, runtime, { prepared: true }), /gateway-build.json/)
})

test('old provenance cannot be reused for a modified executable', (t) => {
  const { repo, runtime, write } = fixture(t)
  write('runtime/opensquilla-gateway/opensquilla-gateway.exe', 'stale executable')
  assert.throws(() => verifyGatewayIntegrity(repo, runtime, { prepared: true }), /prepared Gateway outputs/)
})
