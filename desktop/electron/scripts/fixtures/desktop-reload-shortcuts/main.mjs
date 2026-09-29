import { app, BrowserWindow } from 'electron'

import { installDesktopReloadShortcuts } from '../../../dist/desktop-reload-shortcuts.js'

app.commandLine.appendSwitch('disable-gpu')
app.on('window-all-closed', () => {})

void app.whenReady().then(async () => {
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
  installDesktopReloadShortcuts(window.webContents)
  await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`
    <title>Desktop reload shortcuts</title>
    <main id="reload-count"></main>
    <script>
      const count = Number(window.name || '0') + 1
      window.name = String(count)
      document.querySelector('#reload-count').textContent = 'Reload count ' + count
    </script>
  `)}`)
  window.show()
  window.focus()
})
