/**
 * Native IndexedDB comparison harness. Read any checkout's production WAL with
 * --source-root <checkout>/opensquilla-webui, without editing that checkout.
 * Example: --counts 100 --rounds 5 --iterations 20 --output /outside/repo/result.json
 * Use --rounds 1 --iterations 1 for 1000/10000-row diagnostics on old algorithms.
 * A per-traversal deadline writes explicit incomplete evidence and exits 2.
 * No Gateway, provider, account, profile, or existing browser data is accessed.
 */
import { chromium } from '@playwright/test'
import { build, normalizePath } from 'vite'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ownRoot = fileURLToPath(new URL('..', import.meta.url))
const options = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  const name = process.argv[index]
  const value = process.argv[index + 1]
  if (!['--source-root', '--output', '--counts', '--rounds', '--iterations', '--timeout-ms'].includes(name) || !value) {
    throw new Error('Expected --source-root, --output, --counts, --rounds, --iterations, or --timeout-ms followed by a value')
  }
  options.set(name, value)
}
if (!options.has('--output')) throw new Error('--output must name a JSON evidence file outside the checkout')
const sourceRoot = resolve(options.get('--source-root') || ownRoot)
const outputPath = resolve(options.get('--output'))
function positiveInteger(name, fallback) {
  const value = Number(options.get(name) || fallback)
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${name} must be a positive integer`)
  return value
}
const counts = (options.get('--counts') || '100').split(',').map(Number)
if (counts.some(count => !Number.isSafeInteger(count) || count < 0)) throw new Error('--counts must contain nonnegative integers')
const rounds = positiveInteger('--rounds', 5)
const iterations = positiveInteger('--iterations', 20)
const timeoutMs = positiveInteger('--timeout-ms', 30_000)
const source = path => JSON.stringify(normalizePath(path))
const entry = '\0recovery-pagination-benchmark'
const bundled = await build({
  root: sourceRoot, configFile: false, logLevel: 'silent',
  resolve: { alias: { '@': resolve(sourceRoot, 'src') } },
  plugins: [{
    name: 'recovery-pagination-benchmark',
    resolveId: id => id === entry ? entry : undefined,
    load: id => id === entry ? `
      import { createPendingInputWal } from ${source(resolve(sourceRoot, 'src/utils/chat/pendingInputWal.ts'))};
      import { seedRecoveryRows, measureRecoveryTraversal, traverseRecoveryPages } from ${source(resolve(ownRoot, 'e2e/support/recovery-pagination.ts'))};
      window.recoveryBenchmark = { wal: createPendingInputWal(), seedRecoveryRows, measureRecoveryTraversal, traverseRecoveryPages };
    ` : undefined,
  }],
  build: { write: false, minify: false, rollupOptions: { input: entry, output: { format: 'es' } } },
})
const chunks = (Array.isArray(bundled) ? bundled : [bundled]).flatMap(result => result.output.filter(asset => asset.type === 'chunk'))
if (chunks.length !== 1) throw new Error('Expected exactly one benchmark fixture chunk')
const evidence = {
  generatedAt: new Date().toISOString(),
  sourceRoot,
  sourceRevision: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: sourceRoot, encoding: 'utf8' }).trim(),
  sourceDirty: execFileSync('git', ['status', '--porcelain'], { cwd: sourceRoot, encoding: 'utf8' }).trim().length > 0,
  sourceWalSha256: createHash('sha256').update(await readFile(resolve(sourceRoot, 'src/utils/chat/pendingInputWal.ts'))).digest('hex'),
  node: process.version, platform: process.platform, rounds, iterations, timeoutMs,
  warmups: 2, timingInstrumentation: 'none; structure is measured in a separate traversal',
  browser: '', scenarios: [],
}
const browser = await chromium.launch({
  ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
})
evidence.browser = browser.version()
let failed = false
let incomplete = false
try {
  for (const count of counts) {
    const scenario = { pendingCount: count, status: 'running', timing: [] }
    evidence.scenarios.push(scenario)
    const context = await browser.newContext()
    try {
      await context.route('**/*', route => route.fulfill(new URL(route.request().url()).pathname === '/fixture.mjs'
        ? { contentType: 'text/javascript', body: chunks[0].code }
        : { contentType: 'text/html', body: '<script type="module" src="/fixture.mjs"></script>' }))
      const page = await context.newPage()
      await page.goto('http://127.0.0.1:44178/fixture.html')
      await page.waitForFunction(() => !!window.recoveryBenchmark)
      await page.evaluate(async count => {
        const fixture = window.recoveryBenchmark
        fixture.expected = Array.from({ length: count }, (_, index) => `pending-${String(index).padStart(6, '0')}`)
        await fixture.seedRecoveryRows(fixture.wal, fixture.expected)
      }, count)
      async function traverse(mode) {
        let timer
        try {
          return await Promise.race([
              page.evaluate(async mode => {
                const fixture = window.recoveryBenchmark
                const started = performance.now()
                const measured = mode === 'structure'
                  ? await fixture.measureRecoveryTraversal(fixture.wal)
                  : await fixture.traverseRecoveryPages(fixture.wal)
                const wallMs = performance.now() - started
                if (measured.ids.length !== fixture.expected.length || measured.ids.some((id, index) => id !== fixture.expected[index])) {
                  throw new Error('Traversal omitted, repeated, or reordered a pending record')
                }
                return { records: measured.ids.length, pages: measured.pageSizes.length,
                  ...(mode === 'structure' ? { cursorCallbacks: measured.cursorCallbacks, seekCalls: measured.seekCalls } : { wallMs }) }
              }, mode),
              new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('traversal_deadline')), timeoutMs) }),
          ])
        } finally {
          clearTimeout(timer)
        }
      }
      scenario.phase = 'structure'
      scenario.structure = await traverse('structure')
      scenario.phase = 'warmup'
      for (let iteration = 0; iteration < evidence.warmups; iteration += 1) await traverse('timing')
      scenario.phase = 'timing'
      for (let round = 0; round < rounds; round += 1) {
        for (let iteration = 0; iteration < iterations; iteration += 1) {
          scenario.timing.push({ round, iteration, ...await traverse('timing') })
        }
      }
      const sorted = scenario.timing.map(sample => sample.wallMs).sort((left, right) => left - right)
      scenario.summary = { medianMs: sorted[Math.floor(sorted.length / 2)],
        p95Ms: sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)] }
      scenario.status = 'complete'
      delete scenario.phase
    } catch (error) {
      scenario.status = error.message === 'traversal_deadline' ? 'deadline' : 'error'
      scenario.error = error.message
      if (scenario.status === 'deadline') incomplete = true
      else failed = true
    } finally {
      await context.close()
      await writeFile(outputPath, `${JSON.stringify(evidence, null, 2)}\n`)
    }
  }
} finally {
  await browser.close()
}
process.stdout.write(`${JSON.stringify(evidence, null, 2)}\n`)
process.exitCode = failed ? 1 : incomplete ? 2 : 0
