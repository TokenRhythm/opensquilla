import { app, BrowserWindow } from 'electron'
import { BrowserPlaywrightDriver } from '../../../dist/browser-playwright.js'

globalThis.__BrowserPlaywrightDriver = BrowserPlaywrightDriver

app.commandLine.appendSwitch('disable-gpu')
// The harness adds these globally; keep hidden-page behavior representative of
// the desktop application's normal renderer scheduling.
for (const name of ['disable-background-timer-throttling', 'disable-backgrounding-occluded-windows',
  'disable-renderer-backgrounding']) app.commandLine.removeSwitch(name)
app.on('window-all-closed', () => {})
void app.whenReady().then(async () => {
  const keeper = new BrowserWindow({ show: false,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } })
  await keeper.loadURL('data:text/html,<title>Browser attachment fixture</title>')
})
