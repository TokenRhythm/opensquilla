import { app, BrowserWindow } from 'electron'

import { installDesktopZoomShortcuts } from '../../../dist/desktop-zoom-shortcuts.js'

await app.whenReady()
const window = new BrowserWindow({
  width: 640,
  height: 480,
  show: true,
  webPreferences: {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  },
})
installDesktopZoomShortcuts(window.webContents)
await window.loadURL('data:text/html;charset=utf-8,<title>Desktop zoom shortcuts</title><main>Zoom fixture</main>')
window.show()
window.focus()
