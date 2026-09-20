import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_NATIVE_ATTACHMENTS_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_NATIVE_ATTACHMENTS_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}
const root = await mkdtemp(join(tmpdir(), 'opensquilla-native-attachment-electron-'))
const path = join(root, 'selected.txt')
await writeFile(path, 'native selected content')
let app
try {
  app = await electron.launch({
    args: [`--user-data-dir=${join(root, 'chromium')}`, fileURLToPath(new URL('fixtures/native-attachments', import.meta.url))],
    env: { ...process.env, OPENSQUILLA_TEST_SELECTED_FILE: path, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' },
  })
  const page = await app.firstWindow()
  await page.locator('#file').setInputFiles(path)
  const result = await page.evaluate(async () => {
    const bridge = window.opensquillaDesktop
    const context = { gatewayInstanceId: 'native-test-instance', sessionKey: 'native-test-session', sessionId: 'native-test-session-id', sessionEpoch: 0 }
    const chosen = await bridge.chooseAttachments(context)
    const pickedReceipt = await bridge.importAttachmentSelection(context, chosen[0].token)
    const file = document.querySelector('#file').files[0]
    const transfer = new DataTransfer()
    transfer.items.add(file)
    const dropped = new Promise((resolve, reject) => {
      document.querySelector('#drop').addEventListener('drop', async event => {
        try {
          const selection = await bridge.selectAttachmentFile(context, event.dataTransfer.files[0])
          resolve(await bridge.importAttachmentSelection(context, selection.token))
        } catch (error) { reject(error) }
      }, { once: true })
    })
    document.querySelector('#drop').dispatchEvent(new DragEvent('drop', { dataTransfer: transfer }))
    const droppedReceipt = await dropped
    const synthetic = new File(['synthetic screenshot'], 'screenshot.png', { type: 'image/png' })
    Object.defineProperty(synthetic, 'path', { value: '/not-a-real-file' })
    const screenshotSelection = await bridge.selectAttachmentFile(context, synthetic)
    let stringRejected = false
    try { await bridge.selectAttachmentFile(context, '/not-a-file-object') } catch { stringRejected = true }
    let objectRejected = false
    try { await bridge.selectAttachmentFile(context, { path: '/not-a-file-object', name: 'forged.txt' }) } catch { objectRejected = true }
    return { pickedReceipt, droppedReceipt, screenshotSelection, stringRejected, objectRejected,
      ipcRendererExposed: 'ipcRenderer' in bridge, selectionLeaksPath: 'path' in chosen[0] }
  })
  assert.deepEqual(result.pickedReceipt, result.droppedReceipt)
  assert.equal(result.pickedReceipt.file_uuid, 'native-file')
  assert.equal(result.pickedReceipt.name, 'selected.txt')
  assert.equal(result.pickedReceipt.size, Buffer.byteLength('native selected content'))
  assert.equal(result.screenshotSelection, null)
  assert.equal(result.stringRejected, true)
  assert.equal(result.objectRejected, true)
  assert.equal(result.ipcRendererExposed, false)
  assert.equal(result.selectionLeaksPath, false)
  console.log('Electron native attachments: genuine File drop, picker broker, screenshots and forged paths passed')
} finally {
  await app?.close()
  await rm(root, { recursive: true, force: true })
}
