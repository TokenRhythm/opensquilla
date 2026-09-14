import { fork } from 'node:child_process'
import { access, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'

const scriptPath = fileURLToPath(import.meta.url)
const packageRoot = fileURLToPath(new URL('../', import.meta.url))

// Keep electron-builder's pinned versions, download cache and checksum checks.
export async function loadNsisTooling() {
  const packageJson = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'))
  const output = join(packageRoot, 'node_modules', 'app-builder-lib', 'out')
  const nsisImport = await import(pathToFileURL(join(output, 'targets', 'nsis', 'nsisUtil.js')).href)
  const windowsImport = await import(pathToFileURL(join(output, 'toolsets', 'windows.js')).href)
  const nsis = nsisImport.default ?? nsisImport
  const windows = windowsImport.default ?? windowsImport
  const makensis = await windows.getMakeNsisPath(
    packageJson.build?.toolsets?.nsis, packageJson.build?.nsis?.customNsisBinary,
  )
  const tooling = {
    executable: makensis.path,
    env: makensis.env ?? {},
    templatesDir: nsis.nsisTemplatesDir,
  }
  await access(tooling.executable)
  return tooling
}

export function isTransientDownloadError(error) {
  // Integrity/configuration errors must never be turned into network retries.
  if (/checksum|integrity|hash mismatch/i.test(error?.message ?? '')) return false
  return [502, 503, 504].includes(error?.statusCode ?? error?.response?.statusCode)
    || ['ETIMEDOUT', 'ESOCKETTIMEDOUT', 'ECONNRESET', 'EAI_AGAIN'].includes(error?.code)
}

export async function prepareWithRetry(attempt, { sleep = delay, warn = console.warn } = {}) {
  for (let number = 1; number <= 3; number++) {
    try {
      return await attempt()
    } catch (error) {
      if (number === 3 || !isTransientDownloadError(error)) throw error
      warn(`Installer tooling download failed (${error.statusCode ?? error.code}); retry ${number}/2`)
      await sleep(number * 1000)
    }
  }
}

// A fresh process is necessary: electron-builder caches rejected download promises.
export function prepareAttempt(workerPath = scriptPath) {
  return new Promise((resolveResult, reject) => {
    const child = fork(workerPath, ['--worker'], {
      stdio: ['ignore', 'inherit', 'inherit', 'ipc'],
      execArgv: [],
      timeout: 120_000,
    })
    let result
    child.on('message', (message) => { result = message })
    child.on('error', reject)
    child.on('exit', (code, signal) => {
      if (code === 0 && result?.tooling) resolveResult(result.tooling)
      else if (result?.error) reject(Object.assign(new Error(result.error.message), result.error))
      else reject(new Error(`Installer tooling worker failed (${signal ?? code}); no result`))
    })
  })
}

if (process.argv[1] && resolve(process.argv[1]) === scriptPath) {
  if (process.argv[2] === '--worker') {
    try {
      const tooling = await loadNsisTooling()
      process.send({ tooling }, () => process.exit(0))
    } catch (error) {
      // Do not serialize request headers, signed URLs or runtime tokens.
      process.send({ error: {
        message: /checksum|integrity|hash mismatch/i.test(error.message ?? '')
          ? 'Installer tooling integrity check failed' : 'Installer tooling preparation failed',
        code: error.code,
        statusCode: error.response?.statusCode,
      } }, () => process.exit(1))
    }
  } else {
    const output = process.argv[2]
    if (!output) throw new Error('Usage: prepare-installer-tooling.mjs <output.json>')
    const tooling = await prepareWithRetry(() => prepareAttempt())
    await writeFile(output, `${JSON.stringify(tooling)}\n`, 'utf8')
    console.log('Installer tooling ready; compilation tests have not run yet')
  }
}
