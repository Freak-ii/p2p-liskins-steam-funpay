// Мост между страницей мастера настройки и main-процессом. На страницы панели ничего не прокидывается.
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('lisbot', {
    get: () => ipcRenderer.invoke('setup:get'),
    save: (data) => ipcRenderer.invoke('setup:save', data),
    cancel: () => ipcRenderer.invoke('setup:cancel'),
    testLis: (key) => ipcRenderer.invoke('setup:testLis', key),
    tgFindChat: (token) => ipcRenderer.invoke('setup:tgFindChat', token),
    tgTest: (token, chatId) => ipcRenderer.invoke('setup:tgTest', token, chatId),
    importMaFile: () => ipcRenderer.invoke('setup:importMaFile'),
    open: (url) => ipcRenderer.invoke('app:openExternal', url),
  });
}
