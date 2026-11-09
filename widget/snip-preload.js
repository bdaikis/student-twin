// Bridge for the snip trace window: the main process sends the traced points, in this window's DIP.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('snip', {
  onPath: (cb) => ipcRenderer.on('snip:path', (_e, points) => cb(points)),
});
