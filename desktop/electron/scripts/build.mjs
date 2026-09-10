import { rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../', import.meta.url))
// TypeScript leaves removed source modules in its output directory on incremental builds.
rmSync(join(root, 'dist'), { recursive: true, force: true })
const result = spawnSync(process.execPath, [
  join(root, 'node_modules', 'typescript', 'bin', 'tsc'),
  '-p', join(root, 'tsconfig.json'),
], { cwd: root, stdio: 'inherit' })
if (result.error) throw result.error
process.exit(result.status ?? 1)
