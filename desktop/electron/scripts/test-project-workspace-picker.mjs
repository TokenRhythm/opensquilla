import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const main = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const preload = readFileSync(new URL('../src/preload.cts', import.meta.url), 'utf8')

assert.match(
  preload,
  /chooseProjectDirectory:\s*\(\)\s*=>\s*ipcRenderer\.invoke\('desktop:workspace:choose-directory'\)/,
)
assert.match(
  main,
  /ipcMain\.handle\('desktop:workspace:choose-directory',\s*async\s*\(event\)/,
)
assert.match(main, /trustedControlUiIpc\(event\)/)
assert.match(main, /properties:\s*\['openDirectory'\]/)
assert.match(main, /choice\.canceled[\s\S]*return null/)
assert.match(main, /resolve\(choice\.filePaths\[0\]/)

console.log('project workspace picker contract checks passed')
