import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { projectDirectoryDialogOptions } from '../dist/project-directory-picker.js'

const main = readFileSync(new URL('../src/main.ts', import.meta.url), 'utf8')
const preload = readFileSync(new URL('../src/preload.cts', import.meta.url), 'utf8')
const desktopPlatform = readFileSync(
  new URL('../../../opensquilla-webui/src/platform/desktop.ts', import.meta.url),
  'utf8',
)
const picker = readFileSync(
  new URL(
    '../../../opensquilla-webui/src/components/ProjectWorkspacePickerDialog.vue',
    import.meta.url,
  ),
  'utf8',
)
const workspaceHandlerStart = main.indexOf(
  "ipcMain.handle('desktop:workspace:choose-directory'",
)
assert.notEqual(workspaceHandlerStart, -1)
const nextHandlerStart = main.indexOf('\nipcMain.handle(', workspaceHandlerStart + 1)
assert.notEqual(nextHandlerStart, -1)
const workspaceHandler = main.slice(workspaceHandlerStart, nextHandlerStart)

assert.match(
  preload,
  /chooseProjectDirectory:\s*\(payload:\s*unknown\)\s*=>\s*\(\s*ipcRenderer\.invoke\('desktop:workspace:choose-directory',\s*payload\)/,
)
assert.match(
  workspaceHandler,
  /ipcMain\.handle\('desktop:workspace:choose-directory',\s*async\s*\(event,\s*payload:\s*unknown\)/,
)
assert.match(workspaceHandler, /trustedControlUiIpc\(event\)/)
assert.match(workspaceHandler, /projectDirectoryDialogOptions\(process\.platform,\s*payload\)/)
assert.doesNotMatch(workspaceHandler, /title:\s*['"]Choose a project['"]/)
assert.match(workspaceHandler, /choice\.canceled[\s\S]*return null/)
assert.match(workspaceHandler, /resolve\(choice\.filePaths\[0\]/)
assert.match(
  desktopPlatform,
  /chooseProjectDirectory\(request\)[\s\S]*api\.chooseProjectDirectory\(request\)/,
)
assert.match(picker, /nativePicker\(\{\s*initialPath:\s*props\.initialPath\?\.trim\(\)\s*\|\|\s*undefined,\s*\}\)/)
assert.match(picker, /catch \(cause\)[\s\S]*phase\.value = 'desktop-error'/)

assert.deepEqual(
  projectDirectoryDialogOptions('darwin', { initialPath: ' /repos/current ' }),
  {
    defaultPath: '/repos/current',
    properties: ['openDirectory', 'createDirectory'],
  },
)
assert.deepEqual(
  projectDirectoryDialogOptions('win32', { initialPath: 'C:\\repos\\current' }),
  {
    defaultPath: 'C:\\repos\\current',
    properties: ['openDirectory'],
  },
)
assert.deepEqual(
  projectDirectoryDialogOptions('linux', { initialPath: 42 }),
  { properties: ['openDirectory'] },
)
assert.equal(
  Object.hasOwn(projectDirectoryDialogOptions('darwin', null), 'title'),
  false,
)

console.log('project workspace picker contract checks passed')
