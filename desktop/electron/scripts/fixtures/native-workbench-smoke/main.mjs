import { app, BrowserWindow, protocol } from 'electron'
import { NativeWorkbenchSurfaceManager } from '../../../dist/native-workbench-surface.js'

globalThis.__opensquillaNativeWorkbenchSurfaceManager = NativeWorkbenchSurfaceManager

protocol.registerSchemesAsPrivileged([{
  scheme: 'opensquilla-artifact',
  privileges: {
    standard: true,
    secure: true,
    supportFetchAPI: true,
    corsEnabled: true,
    stream: true,
  },
}])

app.commandLine.appendSwitch('disable-gpu')
app.on('window-all-closed', () => {})

// Do not top-level-await app.whenReady(): Playwright's Electron loader defers
// that promise until its control channel is attached. Returning from this main
// module lets the loader finish the handshake first.
void app.whenReady().then(async () => {
  // Playwright waits for an Electron window before returning from launch. Keep
  // a minimal hidden window alive while the test creates its owner window.
  const keeper = new BrowserWindow({
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  await keeper.loadURL('data:text/html,<title>Native Workbench smoke host</title>')
})
