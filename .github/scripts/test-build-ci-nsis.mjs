import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import test from 'node:test'

import { runCiNsisBuild } from './build-ci-nsis.mjs'

const identity = {
  GITHUB_SHA: 'source-sha', GITHUB_WORKFLOW_SHA: 'workflow-sha',
  GITHUB_RUN_ID: '123', GITHUB_RUN_ATTEMPT: '1',
}

function deferred() {
  let resolve
  const promise = new Promise((complete) => { resolve = complete })
  return { promise, resolve }
}

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'opensquilla ci nsis '))
  t.after(() => rm(root, { recursive: true, force: true }))
  const packageRoot = join(root, 'desktop', 'electron')
  const outputDir = join(root, 'dist', 'desktop-electron')
  const appOutDir = join(outputDir, 'win-unpacked')
  const resources = join(appOutDir, 'resources')
  const gateway = join(resources, 'runtime', 'gateway')
  const manifest = {
    name: 'fixture', version: '1.0.0',
    scripts: { 'verify:package': 'npm run verify:icons && node scripts/verify-package.mjs' },
    build: {
      beforePack: 'scripts/nsis/prepare-upgrade-template.cjs',
      directories: { output: '../../dist/desktop-electron' },
      win: { target: ['nsis'] },
    },
  }
  const files = [
    join(packageRoot, 'src', 'main.ts'), join(packageRoot, 'dist', 'main.js'),
    join(packageRoot, 'runtime', 'gateway', 'gateway-build.json'),
    join(packageRoot, 'runtime', 'runtime-manifest.json'),
    join(packageRoot, 'runtime', 'runtime-pack-catalog.json'),
    join(appOutDir, 'OpenSquilla.exe'), join(resources, 'app.asar'),
    join(resources, 'app-update.yml'),
    join(gateway, 'opensquilla-gateway', 'opensquilla-gateway.exe'),
    join(gateway, 'dependency-inventory.json'), join(gateway, 'gateway-build.json'),
    join(resources, 'runtime', 'runtime-manifest.json'),
    join(resources, 'runtime', 'runtime-pack-catalog.json'),
  ]
  for (const path of files) {
    await mkdir(dirname(path), { recursive: true })
    await writeFile(path, `fixture ${path}\n`)
  }
  await writeFile(join(packageRoot, 'package.json'), JSON.stringify(manifest))
  const events = []
  const context = { appOutDir, electronPlatformName: 'win32', arch: 1, targets: [{ name: 'nsis' }] }
  const target = new Map([['windows', new Map([[1, ['nsis']]])]])
  const builder = {
    Arch: { x64: 1 },
    Platform: { WINDOWS: { createTarget(name) { assert.equal(name, 'nsis'); return target } } },
    async build(options) { await options.config.afterSign(context) },
  }
  const run = (options = {}) => runCiNsisBuild({
    builder, packageRoot, outputDir, identity, log: (event) => events.push(event),
    verifyPackage: async () => {}, ...options,
  })
  return { root, packageRoot, outputDir, appOutDir, resources, manifest, context, target, builder, events, run }
}

test('verification overlaps the NSIS tail, runs once, and preserves the normal target/configuration', { timeout: 5000 }, async (t) => {
  const f = await fixture(t)
  const verifierStarted = deferred()
  const tailReached = deferred()
  let count = 0
  f.builder.build = async (options) => {
    assert.equal(options.targets, f.target)
    assert.equal(options.projectDir, f.packageRoot)
    assert.equal(options.publish, 'never')
    assert.deepEqual(Object.keys(options.config).sort(), ['afterSign', 'forceCodeSigning', 'win'])
    assert.deepEqual(options.config.win, { signExecutable: false })
    assert.equal(options.config.forceCodeSigning, false)
    assert.equal(options.prepackaged, undefined)
    await options.config.afterSign(f.context)
    await verifierStarted.promise
    tailReached.resolve()
    // electron-builder adds this after afterSign; the existing verifier does not
    // read it, and the proof deliberately does not claim a complete-tree hash.
    await writeFile(join(f.resources, 'elevate.exe'), 'normal NSIS helper')
  }
  const proof = await f.run({ verifyPackage: async (options) => {
    count += 1
    assert.deepEqual(options, { packageRoot: f.packageRoot, outputDir: f.outputDir })
    verifierStarted.resolve()
    await tailReached.promise
  } })
  assert.equal(proof.success, true)
  assert.equal(count, 1)
  assert.deepEqual(proof.counts, { afterSign: 1, verification: 1 })
  assert.deepEqual(proof.verificationCommand, ['npm', 'run', 'verify:package'])
  assert.deepEqual(proof.identity, identity)
  assert.deepEqual(proof.criticalFileHashes.before, proof.criticalFileHashes.after)
  assert.equal(Object.keys(proof.criticalFileHashes.before).length, 14)
  assert.equal(proof.stages.criticalIdentity.status, 'passed')
  assert.equal(f.events.filter((event) => event.stage === 'verification' && event.status === 'running').length, 1)
  assert.deepEqual(JSON.parse(await readFile(join(f.outputDir, 'nsis-build-timing.json'), 'utf8')), proof)
  const proofLine = f.events.find((event) => typeof event === 'string' && event.startsWith('[nsis-build-timing] '))
  assert.deepEqual(JSON.parse(proofLine.slice('[nsis-build-timing] '.length)), proof)
})

test('a build failure after the boundary still waits for verification and records both failures', { timeout: 5000 }, async (t) => {
  const f = await fixture(t)
  const buildFailed = deferred()
  let verificationFinished = false
  f.builder.build = async ({ config }) => {
    await config.afterSign(f.context)
    buildFailed.resolve()
    throw new Error('compression failed')
  }
  const proof = await f.run({ verifyPackage: async () => {
    await buildFailed.promise
    await new Promise((complete) => setImmediate(complete))
    verificationFinished = true
    throw new Error('package check failed')
  } })
  assert.equal(proof.success, false)
  assert.equal(verificationFinished, true)
  assert.deepEqual(proof.errors.map((error) => error.stage).sort(), ['build', 'verification'])
  assert.equal(proof.stages.nsisTail.status, 'failed')
  assert.equal(proof.stages.verification.status, 'failed')
})

test('verification failure does not interrupt successful NSIS completion', { timeout: 5000 }, async (t) => {
  const f = await fixture(t)
  const verificationFailed = deferred()
  let tailFinished = false
  f.builder.build = async ({ config }) => {
    await config.afterSign(f.context)
    await verificationFailed.promise
    await new Promise((complete) => setImmediate(complete))
    tailFinished = true
  }
  const proof = await f.run({ verifyPackage: async () => {
    verificationFailed.resolve()
    throw new Error('verification failed early')
  } })
  assert.equal(proof.success, false)
  assert.equal(tailFinished, true)
  assert.equal(proof.stages.nsisTail.status, 'passed')
  assert.deepEqual(proof.errors.map((error) => error.stage), ['verification'])
})

test('duplicate afterSign is rejected without running the verifier twice', async (t) => {
  const f = await fixture(t)
  let count = 0
  f.builder.build = async ({ config }) => {
    await config.afterSign(f.context)
    await config.afterSign(f.context)
  }
  const proof = await f.run({ verifyPackage: async () => { count += 1 } })
  assert.equal(proof.success, false)
  assert.equal(count, 1)
  assert.deepEqual(proof.counts, { afterSign: 2, verification: 1 })
  assert.match(proof.errors[0].message, /exactly one afterSign/)
})

test('missing afterSign fails closed without hanging or claiming verification', { timeout: 5000 }, async (t) => {
  const f = await fixture(t)
  f.builder.build = async () => {}
  const proof = await f.run()
  assert.equal(proof.success, false)
  assert.deepEqual(proof.counts, { afterSign: 0, verification: 0 })
  assert.equal(proof.errors[0].stage, 'coverage')
})

test('build failure before afterSign is recorded and does not wait forever', { timeout: 5000 }, async (t) => {
  const f = await fixture(t)
  f.builder.build = async () => { throw new Error('preparation failed') }
  const proof = await f.run()
  assert.equal(proof.success, false)
  assert.deepEqual(proof.counts, { afterSign: 0, verification: 0 })
  assert.deepEqual(proof.errors.map((error) => error.stage), ['build', 'coverage'])
})

test('critical file modification during overlap is rejected', async (t) => {
  const f = await fixture(t)
  f.builder.build = async ({ config }) => {
    await config.afterSign(f.context)
    await writeFile(join(f.resources, 'app.asar'), 'changed after verification began')
  }
  const proof = await f.run()
  assert.equal(proof.success, false)
  assert.match(proof.errors[0].message, /Critical package files changed.*asar/)
  assert.equal(proof.stages.criticalIdentity.status, 'failed')
})

test('missing app-update.yml rejects an incomplete package boundary', async (t) => {
  const f = await fixture(t)
  await rm(join(f.resources, 'app-update.yml'))
  const proof = await f.run()
  assert.equal(proof.success, false)
  assert.equal(proof.counts.verification, 0)
  assert.match(proof.errors[0].message, /app-update.yml/)
})

for (const [name, changed] of [
  ['platform', { electronPlatformName: 'linux' }],
  ['architecture', { arch: 0 }],
  ['target', { targets: [{ name: 'dir' }] }],
  ['output directory', { appOutDir: 'unexpected' }],
]) {
  test(`unexpected ${name} rejects the verification boundary`, async (t) => {
    const f = await fixture(t)
    f.builder.build = async ({ config }) => config.afterSign({ ...f.context, ...changed })
    const proof = await f.run()
    assert.equal(proof.success, false)
    assert.equal(proof.counts.verification, 0)
  })
}

test('an existing configured afterSign hook is not silently replaced and a failed proof is written', async (t) => {
  const f = await fixture(t)
  f.manifest.build.afterSign = 'existing-hook.mjs'
  await writeFile(join(f.packageRoot, 'package.json'), JSON.stringify(f.manifest))
  const proof = await f.run()
  assert.equal(proof.success, false)
  assert.match(proof.errors[0].message, /cannot replace an existing afterSign/)
  assert.equal(proof.errors[0].stage, 'orchestration')
  assert.deepEqual(JSON.parse(await readFile(join(f.outputDir, 'nsis-build-timing.json'), 'utf8')), proof)
})

for (const [name, firstExit, secondExit, expectedCalls] of [
  ['both original npm stages succeed once', 0, 0, ['icons', 'package']],
  ['second stage failure propagates', 0, 23, ['icons', 'package']],
  ['first stage failure retains the original && short circuit', 19, 0, ['icons']],
]) {
  test(`actual npm invocation with spaces in cwd: ${name}`, { timeout: 15000 }, async (t) => {
    const f = await fixture(t)
    f.manifest.scripts['verify:icons'] = 'node "first probe.mjs"'
    f.manifest.scripts['verify:package'] = 'npm run verify:icons && node "second probe.mjs"'
    await writeFile(join(f.packageRoot, 'package.json'), JSON.stringify(f.manifest))
    for (const [script, marker, exitCode] of [
      ['first probe.mjs', 'icons', firstExit], ['second probe.mjs', 'package', secondExit],
    ]) {
      await writeFile(join(f.packageRoot, script), [
        "import { appendFileSync } from 'node:fs'",
        `appendFileSync('verification calls.jsonl', ${JSON.stringify(`${marker}\n`)})`,
        `process.exitCode = ${exitCode}`,
      ].join('\n'))
    }
    // Explicit undefined selects the production cmd/npm launcher rather than
    // the fixture's injected verifier. No packaged Gateway is executed.
    const proof = await f.run({ verifyPackage: undefined })
    const calls = (await readFile(join(f.packageRoot, 'verification calls.jsonl'), 'utf8')).trim().split('\n')
    assert.deepEqual(calls, expectedCalls)
    assert.equal(proof.counts.verification, 1)
    assert.equal(proof.success, firstExit === 0 && secondExit === 0)
    assert.equal(proof.stages.nsisTail.status, 'passed')
    if (firstExit || secondExit) {
      assert.deepEqual(proof.errors.map((error) => error.stage), ['verification'])
      assert.match(proof.errors[0].message, new RegExp(`exit ${firstExit || secondExit}`))
    }
  })
}
