import { rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import proxyTlsPatch from './prepare-global-agent-tls.cjs'
import globCompat from './prepare-build-glob-compat.cjs'

const root = fileURLToPath(new URL('../', import.meta.url))
await globCompat.prepareInstalledGlobs(root)
await proxyTlsPatch.prepareInstalledProxyTls()
// TypeScript leaves removed source modules in its output directory on incremental builds.
rmSync(join(root, 'dist'), { recursive: true, force: true })
// Compile the shared placement implementation separately to preserve the
// standalone Electron dist layout and its existing source root.
for (const config of ['tsconfig.json', 'tsconfig.shared.json']) {
  const result = spawnSync(process.execPath, [
    join(root, 'node_modules', 'typescript', 'bin', 'tsc'),
    '-p', join(root, config),
  ], { cwd: root, stdio: 'inherit' })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status ?? 1)
}
