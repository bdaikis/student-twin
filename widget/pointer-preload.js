// Bridge for the pointer window: the main process says what to ring and when it goes.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pointer', {
  onShow: (cb) => ipcRenderer.on('pointer:show', (_e, p) => cb(p)),
  onHide: (cb) => ipcRenderer.on('pointer:hide', () => cb()),
});
