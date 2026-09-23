import {app, BrowserWindow, dialog, ipcMain, shell, protocol, net} from 'electron';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
import {EngineClient} from './engine-client';
import {SecretStore} from './secret-store';

protocol.registerSchemesAsPrivileged([{scheme: 'studio-file', privileges: {standard: true, secure: true, supportFetchAPI: true, stream: true}}]);

let mainWindow: BrowserWindow | null = null;
let engine: EngineClient;
let secrets: SecretStore;

const createWindow = (): void => {
  mainWindow = new BrowserWindow({
    width: 1520,
    height: 960,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: '#0b0d12',
    title: 'Auto-Editor PRO Studio',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  if (!app.isPackaged && process.env.VITE_DEV_SERVER_URL) {
    void mainWindow.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else if (!app.isPackaged) {
    void mainWindow.loadURL('http://127.0.0.1:5173');
  } else {
    void mainWindow.loadFile(path.join(app.getAppPath(), 'dist', 'renderer', 'index.html'));
  }
};

const sanitizeFolderName = (value: string): string => {
  const cleaned = value.trim().replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').replace(/[. ]+$/g, '');
  return cleaned || 'Untitled Project';
};

app.whenReady().then(async () => {
  protocol.handle('studio-file', (request) => {
    try {
      const url = new URL(request.url);
      if (url.hostname !== 'asset') return new Response('Not found', {status: 404});
      const parts = url.pathname.split('/').filter(Boolean);
      if (parts.length < 2) return new Response('Bad asset URL', {status: 400});
      const root = Buffer.from(parts[0], 'base64url').toString('utf8');
      const relative = parts.slice(1).map(decodeURIComponent).join(path.sep);
      const resolvedRoot = path.resolve(root);
      const target = path.resolve(resolvedRoot, relative);
      const prefix = resolvedRoot.endsWith(path.sep) ? resolvedRoot : resolvedRoot + path.sep;
      if (target !== resolvedRoot && !target.startsWith(prefix)) return new Response('Forbidden', {status: 403});
      return net.fetch(pathToFileURL(target).toString());
    } catch {
      return new Response('Bad asset URL', {status: 400});
    }
  });
  secrets = new SecretStore();
  engine = new EngineClient(secrets.toEnvironment());
  engine.onEvent((event) => mainWindow?.webContents.send('studio:event', event));
  engine.start();

  ipcMain.handle('studio:ping', () => engine.request('ping', {}));
  ipcMain.handle('studio:new-project', async (_event, name: string) => {
    const pick = await dialog.showOpenDialog({properties: ['openDirectory', 'createDirectory'], title: 'Choose where to create the project'});
    if (pick.canceled || !pick.filePaths[0]) return null;
    const root = path.join(pick.filePaths[0], sanitizeFolderName(name));
    return engine.request('project.create', {path: root, name});
  });
  ipcMain.handle('studio:open-project', async () => {
    const pick = await dialog.showOpenDialog({properties: ['openDirectory'], title: 'Open Auto-Editor PRO project'});
    if (pick.canceled || !pick.filePaths[0]) return null;
    return engine.request('project.open', {path: pick.filePaths[0]});
  });
  ipcMain.handle('studio:open-project-at', (_event, projectPath: string) => engine.request('project.open', {path: projectPath}));
  ipcMain.handle('studio:update-project', (_event, projectPath: string, choices: Record<string, unknown>) => engine.request('project.update', {path: projectPath, choices}));
  ipcMain.handle('studio:import-media', async (_event, projectPath: string) => {
    const pick = await dialog.showOpenDialog({
      properties: ['openFile', 'multiSelections'],
      title: 'Add photos and videos',
      filters: [{name: 'Media', extensions: ['mp4', 'mov', 'mkv', 'webm', 'm4v', 'avi', 'mts', 'm2ts', 'jpg', 'jpeg', 'png', 'webp', 'tif', 'tiff', 'bmp']}],
    });
    if (pick.canceled || pick.filePaths.length === 0) return null;
    const result = await engine.request<{project: unknown}>('project.import_media', {path: projectPath, files: pick.filePaths});
    return result.project;
  });
  ipcMain.handle('studio:import-logo', async (_event, projectPath: string) => {
    const pick = await dialog.showOpenDialog({properties: ['openFile'], title: 'Choose transparent PNG logo', filters: [{name: 'PNG logo', extensions: ['png']}]});
    if (pick.canceled || !pick.filePaths[0]) return null;
    return engine.request('project.import_logo', {path: projectPath, file: pick.filePaths[0]});
  });
  ipcMain.handle('studio:import-voice', async (_event, projectPath: string) => {
    const pick = await dialog.showOpenDialog({properties: ['openFile'], title: 'Choose narration audio', filters: [{name: 'Audio', extensions: ['mp3','wav','m4a','aac','flac','ogg']}]});
    if (pick.canceled || !pick.filePaths[0]) return null;
    return engine.request('project.import_voice', {path: projectPath, file: pick.filePaths[0]});
  });
  ipcMain.handle('studio:remove-media', (_event, projectPath: string, mediaId: string) => engine.request('project.remove_media', {path: projectPath, media_id: mediaId}));
  ipcMain.handle('studio:start-job', (_event, projectPath: string, options: Record<string, unknown>) => engine.request('job.start', {path: projectPath, ...options}));
  ipcMain.handle('studio:get-preview', async (_event, projectPath: string) => {
    const value = await engine.request<{props: Record<string, unknown>; asset_root: string}>('project.preview', {path: projectPath});
    const token = Buffer.from(value.asset_root, 'utf8').toString('base64url');
    return {...value.props, assetBase: `studio-file://asset/${token}/`};
  });
  ipcMain.handle('studio:cancel-job', (_event, jobId: string) => engine.request('job.cancel', {job_id: jobId}));
  ipcMain.handle('studio:get-job', (_event, jobId: string) => engine.request('job.status', {job_id: jobId}));
  ipcMain.handle('studio:credential-status', () => secrets.status());
  ipcMain.handle('studio:save-credentials', async (_event, values: Record<string, string>) => {
    const saved = secrets.save(values);
    engine.setEnvironment(secrets.toEnvironment(saved));
    await engine.restart();
    return secrets.status(saved);
  });
  ipcMain.handle('studio:reveal-path', async (_event, target: string) => {
    if (!target) return;
    const existing = fs.existsSync(target) ? target : path.dirname(target);
    shell.showItemInFolder(existing);
  });
  ipcMain.handle('studio:file-url', (_event, target: string) => pathToFileURL(target).toString());

  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  void engine?.stop();
});
