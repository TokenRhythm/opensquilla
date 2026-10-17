const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('quitAcceptance', {
  command: (action, options) => ipcRenderer.invoke('quit-acceptance:command', action, options),
  snapshot: () => ipcRenderer.invoke('quit-acceptance:snapshot'),
})
