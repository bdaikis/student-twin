// Narrow bridge between the sandboxed renderer and the main process.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('twin', {
  hide: () => ipcRenderer.send('widget:hide'),
  wantFocus: () => ipcRenderer.send('widget:want-focus'),
  onFocusGranted: (cb) => ipcRenderer.on('focus-granted', () => cb()),
  ask: (id, text) => ipcRenderer.send('twin:ask', { id, text }),
  cancel: (id) => ipcRenderer.send('twin:cancel', { id }),
  onDelta: (cb) => ipcRenderer.on('twin:delta', (_e, m) => cb(m)),
  onDone: (cb) => ipcRenderer.on('twin:done', (_e, m) => cb(m)),
  onError: (cb) => ipcRenderer.on('twin:error', (_e, m) => cb(m)),
  onMic: (cb) => ipcRenderer.on('mic', (_e, action) => cb(action)),
  sendAudio: (id, wav) => ipcRenderer.send('mic:audio', { id, wav }),
  onTranscript: (cb) => ipcRenderer.on('voice:transcript', (_e, m) => cb(m)),
  onVoiceStatus: (cb) => ipcRenderer.on('voice:status', (_e, m) => cb(m)),
  onVisibility: (cb) => ipcRenderer.on('visibility', (_e, visible) => cb(visible)),
  onConfirm: (cb) => ipcRenderer.on('confirm:show', (_e, m) => cb(m)),
  onConfirmGone: (cb) => ipcRenderer.on('confirm:gone', (_e, m) => cb(m)),
  answerConfirm: (id, decision) => ipcRenderer.send('confirm:answer', { id, decision }),
});
