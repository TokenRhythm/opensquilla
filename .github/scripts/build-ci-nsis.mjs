import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { mkdir, readFile, realpath, stat, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { performance } from 'node:perf_hooks'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const defaultPackageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'desktop', 'electron')

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error)
}

function runPackageVerification({ packageRoot, outputDir }) {
  // Keep the existing npm script and every verification command inside it intact.
  const windows = process.platform === 'win32'
  const command = windows ? (process.env.ComSpec || 'cmd.exe') : 'npm'
  const args = windows ? ['/d', '/s', '/c', 'npm run verify:package'] : ['run', 'verify:package']
  return new Promise((resolveCommand, rejectCommand) => {
    const child = spawn(command, args, {
      cwd: packageRoot,
      env: { ...process.env, OPEN_SQUILLA_PACKAGE_OUTPUT: outputDir },
      stdio: 'inherit',
      windowsHide: true,
    })
    child.once('error', rejectCommand)
    child.once('close', (code, signal) => {
      if (code === 0) resolveCommand()
      else rejectCommand(new Error(`npm run verify:package failed (${signal ? `signal ${signal}` : `exit ${code}`})`))
    })
  })
}

function criticalFiles(packageRoot, appOutDir) {
  const resources = join(appOutDir, 'resources')
  const runtime = join(resources, 'runtime')
  const gateway = join(runtime, 'gateway')
  return {
    sourceMain: join(packageRoot, 'src', 'main.ts'),
    compiledMain: join(packageRoot, 'dist', 'main.js'),
    packageManifest: join(packageRoot, 'package.json'),
    executable: join(appOutDir, 'OpenSquilla.exe'),
    asar: join(resources, 'app.asar'),
    appUpdate: join(resources, 'app-update.yml'),
    gatewayExecutable: join(gateway, 'opensquilla-gateway', 'opensquilla-gateway.exe'),
    dependencyInventory: join(gateway, 'dependency-inventory.json'),
    gatewayBuildRecord: join(gateway, 'gateway-build.json'),
    runtimeManifest: join(runtime, 'runtime-manifest.json'),
    runtimePackCatalog: join(runtime, 'runtime-pack-catalog.json'),
    sourceGatewayBuildRecord: join(packageRoot, 'runtime', 'gateway', 'gateway-build.json'),
    sourceRuntimeManifest: join(packageRoot, 'runtime', 'runtime-manifest.json'),
    sourceRuntimePackCatalog: join(packageRoot, 'runtime', 'runtime-pack-catalog.json'),
  }
}

async function hashCriticalFiles(files) {
  return Object.fromEntries(await Promise.all(Object.entries(files).map(async ([name, path]) => {
    const info = await stat(path)
    if (!info.isFile() || info.size === 0) throw new Error(`Missing or empty critical file: ${name}`)
    const hash = createHash('sha256')
    for await (const chunk of createReadStream(path)) hash.update(chunk)
    return [name, hash.digest('hex')]
  })))
}

function environmentIdentity() {
  return Object.fromEntries([
    'GITHUB_SHA', 'GITHUB_WORKFLOW_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT',
  ].map((name) => [name, process.env[name] || null]))
}

/**
 * Produce one unsigned CI candidate with the normal NSIS target. Only the
 * scheduling of the unchanged package verifier moves: after application
 * preparation, it overlaps NSIS compression and installer generation.
 *
 * These hashes bind critical identity files, not the complete unpacked tree.
 * NSIS still adds resources/elevate.exe after this boundary. The existing
 * verify:prepared and verify:package checks remain responsible for their full
 * input, migration, model, runtime, UI and package contracts.
 */
export async function runCiNsisBuild({
  builder,
  packageRoot = defaultPackageRoot,
  outputDir = resolve(packageRoot, '..', '..', 'dist', 'desktop-electron'),
  verifyPackage = runPackageVerification,
  identity = environmentIdentity(),
  log = (event) => console.log(typeof event === 'string' ? event : JSON.stringify(event)),
} = {}) {
  packageRoot = resolve(packageRoot)
  outputDir = resolve(outputDir)
  const appOutDir = join(outputDir, 'win-unpacked')
  const proofPath = join(outputDir, 'nsis-build-timing.json')
  const started = new Map()
  const proof = {
    schemaVersion: 1,
    kind: 'nsis-build-timing',
    identity,
    success: false,
    startedAt: new Date().toISOString(),
    finishedAt: null,
    counts: { afterSign: 0, verification: 0 },
    verificationCommand: ['npm', 'run', 'verify:package'],
    stages: {},
    criticalFileHashes: { before: null, after: null },
    errors: [],
  }
  const reportError = (stage, error) => {
    proof.errors.push({ stage, message: errorMessage(error) })
  }
  const startStage = (stage) => {
    started.set(stage, performance.now())
    proof.stages[stage] = { startedAt: new Date().toISOString(), status: 'running' }
    log({ event: 'nsis-build-stage', stage, ...proof.stages[stage] })
  }
  const finishStage = (stage, status) => {
    const entry = proof.stages[stage]
    if (!entry || entry.status !== 'running') return
    Object.assign(entry, {
      status,
      finishedAt: new Date().toISOString(),
      durationMs: Math.round(performance.now() - started.get(stage)),
    })
    log({ event: 'nsis-build-stage', stage, ...entry })
  }

  try {
    await mkdir(outputDir, { recursive: true })
    const manifest = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
    if (manifest.build?.afterSign != null) {
      throw new Error('CI overlap cannot replace an existing afterSign hook')
    }
    if (resolve(packageRoot, manifest.build?.directories?.output || 'dist') !== outputDir) {
      throw new Error('CI output directory must match the normal electron-builder output')
    }
    if (!builder) {
      builder = createRequire(join(packageRoot, 'package.json'))('electron-builder')
    }
    const files = criticalFiles(packageRoot, appOutDir)
    let completeVerification
    const verificationCompleted = new Promise((complete) => { completeVerification = complete })
    let verificationStarted = false

    const afterSign = async (context) => {
      proof.counts.afterSign += 1
      if (proof.counts.afterSign !== 1) throw new Error('Expected exactly one afterSign boundary')
      if (context.electronPlatformName !== 'win32') throw new Error('Expected a Windows afterSign boundary')
      if (context.arch !== builder.Arch.x64) throw new Error('Expected the x64 afterSign boundary')
      if (!Array.isArray(context.targets) || context.targets.length !== 1 || context.targets[0].name !== 'nsis') {
        throw new Error('Expected exactly the normal NSIS target at afterSign')
      }
      if (typeof context.appOutDir !== 'string' || resolve(context.appOutDir) !== appOutDir
        || await realpath(context.appOutDir) !== await realpath(appOutDir)) {
        throw new Error('afterSign did not prepare the expected win-unpacked directory')
      }
      proof.criticalFileHashes.before = await hashCriticalFiles(files)
      finishStage('electronPreparation', 'passed')
      startStage('nsisTail')
      proof.counts.verification += 1
      verificationStarted = true
      startStage('verification')
      // Catch immediately: verification may fail while builder is still running.
      // Neither branch cancels the other; both results are collected below.
      void (async () => {
        try {
          await verifyPackage({ packageRoot, outputDir })
          finishStage('verification', 'passed')
        } catch (error) {
          reportError('verification', error)
          finishStage('verification', 'failed')
        } finally {
          completeVerification()
        }
      })()
    }

    startStage('electronPreparation')
    const buildCompleted = (async () => {
      try {
        await builder.build({
          projectDir: packageRoot,
          targets: builder.Platform.WINDOWS.createTarget('nsis'),
          publish: 'never',
          config: { win: { signExecutable: false }, forceCodeSigning: false, afterSign },
        })
        finishStage('nsisTail', 'passed')
      } catch (error) {
        reportError('build', error)
        finishStage('electronPreparation', 'failed')
        finishStage('nsisTail', 'failed')
      } finally {
        // A failure before afterSign must not leave the verifier waiter hanging.
        if (!verificationStarted) completeVerification()
      }
    })()

    for (const result of await Promise.allSettled([buildCompleted, verificationCompleted])) {
      if (result.status === 'rejected') reportError('orchestration', result.reason)
    }
    if (proof.counts.afterSign !== 1 || proof.counts.verification !== 1) {
      reportError('coverage', `Expected afterSign=1 and verification=1; got ${JSON.stringify(proof.counts)}`)
      finishStage('electronPreparation', 'failed')
    }
    if (proof.criticalFileHashes.before) {
      startStage('criticalIdentity')
      try {
        proof.criticalFileHashes.after = await hashCriticalFiles(files)
        const changed = Object.keys(proof.criticalFileHashes.before).filter(
          (name) => proof.criticalFileHashes.before[name] !== proof.criticalFileHashes.after[name],
        )
        if (changed.length) throw new Error(`Critical package files changed during NSIS/verification overlap: ${changed.join(', ')}`)
        finishStage('criticalIdentity', 'passed')
      } catch (error) {
        reportError('criticalIdentity', error)
        finishStage('criticalIdentity', 'failed')
      }
    }
    proof.success = proof.errors.length === 0
  } catch (error) {
    reportError('orchestration', error)
  } finally {
    proof.finishedAt = new Date().toISOString()
    await mkdir(outputDir, { recursive: true })
    await writeFile(proofPath, `${JSON.stringify(proof, null, 2)}\n`)
    log({ event: 'nsis-build-result', success: proof.success, counts: proof.counts, errors: proof.errors, proofPath })
    log(`[nsis-build-timing] ${JSON.stringify(proof)}`)
  }
  return proof
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const proof = await runCiNsisBuild()
    if (!proof.success) process.exitCode = 1
  } catch (error) {
    console.error(`NSIS CI build failed: ${errorMessage(error)}`)
    process.exitCode = 1
  }
}
