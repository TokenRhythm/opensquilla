import { app, BrowserWindow, ipcMain } from 'electron'
import { fileURLToPath } from 'node:url'
import { NativeAttachmentSelections } from '../../../dist/native-attachments.js'

const connection = { instanceId: 'native-test-instance', profile: 'native-test-profile',
  url: 'http://127.0.0.1:54321', authToken: 'test-token', nonce: 'test-native-private-nonce' }
let window
const broker = new NativeAttachmentSelections({
  connection: sender => sender === window?.webContents.id ? connection : null,
  fetch: async () => Response.json({ file_uuid: 'native-file' }),
})
const sender = event => {
  if (event.sender !== window?.webContents || event.senderFrame !== event.sender.mainFrame) throw new Error('Untrusted fixture sender')
  return event.sender.id
}
ipcMain.handle('desktop:attachments:select-file', (event, request, path) => broker.select(sender(event), request, path))
ipcMain.handle('desktop:attachments:import', (event, request, token) => broker.import(sender(event), request, token))
ipcMain.handle('desktop:attachments:choose', (event, request) => broker.choose(sender(event), request, async () => [process.env.OPENSQUILLA_TEST_SELECTED_FILE]))
ipcMain.handle('desktop:attachments:cancel', event => broker.cancel(sender(event)))
app.commandLine.appendSwitch('disable-gpu')
void app.whenReady().then(async () => {
  window = new BrowserWindow({ show: true, width: 600, height: 400, webPreferences: {
    contextIsolation: true, nodeIntegration: false, sandbox: true,
    preload: fileURLToPath(new URL('../../../dist/preload.cjs', import.meta.url)),
  } })
  await window.loadURL('data:text/html,<title>Native attachment test</title><input type="file" id="file"><div id="drop">Drop files</div>')
})
