import {contextBridge, ipcRenderer} from 'electron';
import type {DesktopApi, StudioEvent} from '../shared/types';

const api: DesktopApi = {
  ping: () => ipcRenderer.invoke('studio:ping'),
  newProject: (name) => ipcRenderer.invoke('studio:new-project', name),
  openProject: () => ipcRenderer.invoke('studio:open-project'),
  openProjectAt: (path) => ipcRenderer.invoke('studio:open-project-at', path),
  updateProject: (path, choices) => ipcRenderer.invoke('studio:update-project', path, choices),
  importMedia: (path) => ipcRenderer.invoke('studio:import-media', path),
  importLogo: (path) => ipcRenderer.invoke('studio:import-logo', path),
  importVoice: (path) => ipcRenderer.invoke('studio:import-voice', path),
  removeMedia: (path, mediaId) => ipcRenderer.invoke('studio:remove-media', path, mediaId),
  startJob: (path, options) => ipcRenderer.invoke('studio:start-job', path, options ?? {}),
  getPreview: (path) => ipcRenderer.invoke('studio:get-preview', path),
  cancelJob: (jobId) => ipcRenderer.invoke('studio:cancel-job', jobId),
  getJob: (jobId) => ipcRenderer.invoke('studio:get-job', jobId),
  credentialStatus: () => ipcRenderer.invoke('studio:credential-status'),
  saveCredentials: (values) => ipcRenderer.invoke('studio:save-credentials', values),
  revealPath: (path) => ipcRenderer.invoke('studio:reveal-path', path),
  fileUrl: (path) => ipcRenderer.invoke('studio:file-url', path),
  onEvent: (listener) => {
    const handler = (_event: unknown, payload: StudioEvent) => listener(payload);
    ipcRenderer.on('studio:event', handler);
    return () => ipcRenderer.removeListener('studio:event', handler);
  },
};

contextBridge.exposeInMainWorld('studio', api);
