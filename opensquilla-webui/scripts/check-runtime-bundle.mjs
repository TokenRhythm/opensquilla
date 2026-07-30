import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, extname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const distDir = resolve(scriptDir, '../../src/opensquilla/gateway/static/dist')
const assetsDir = resolve(distDir, 'assets')

function javascriptFiles(root) {
  const files = []
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = resolve(root, entry.name)
    if (entry.isDirectory()) files.push(...javascriptFiles(path))
    else if (entry.isFile() && extname(entry.name).toLowerCase() === '.js') {
      files.push(path)
    }
  }
  return files
}

if (!existsSync(assetsDir)) {
  throw new Error(`Built Web UI assets are missing: ${assetsDir}`)
}

const javascript = javascriptFiles(assetsDir).map((path) => readFileSync(path, 'utf8'))
const requiredRuntimeStrings = [
  {
    label: 'Usage query RPC client',
    value: 'usage.query',
  },
]
const missing = requiredRuntimeStrings
  .filter(({ value }) => !javascript.some((source) => source.includes(value)))
  .map(({ label, value }) => `${label} (${value})`)

if (missing.length > 0) {
  throw new Error(`Built Web UI is missing runtime contracts: ${missing.join(', ')}`)
}

console.log(
  `Runtime bundle guard passed (${requiredRuntimeStrings.length} contract(s), ${javascript.length} JavaScript file(s)).`,
)
