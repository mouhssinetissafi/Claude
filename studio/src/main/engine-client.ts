import {ChildProcessWithoutNullStreams, spawn} from 'node:child_process';
import path from 'node:path';
import readline from 'node:readline';
import fs from 'node:fs';
import {app} from 'electron';

interface RpcResponse {
  id?: number | string | null;
  result?: unknown;
  error?: {type: string; message: string};
  event?: string;
  params?: unknown;
}

const findFile = (root: string, filename: string, maxDepth = 8): string | null => {
  if (!fs.existsSync(root)) return null;
  const walk = (dir: string, depth: number): string | null => {
    if (depth > maxDepth) return null;
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(dir, {withFileTypes: true}); } catch { return null; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isFile() && entry.name.toLowerCase() === filename.toLowerCase()) return full;
    }
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const found = walk(path.join(dir, entry.name), depth + 1);
      if (found) return found;
    }
    return null;
  };
  return walk(root, 0);
};

export class EngineClient {
  private child: ChildProcessWithoutNullStreams | null = null;
  private nextId = 1;
  private pending = new Map<number, {resolve: (value: unknown) => void; reject: (error: Error) => void}>();
  private listeners = new Set<(event: RpcResponse) => void>();
  private env: NodeJS.ProcessEnv = {};

  constructor(env?: NodeJS.ProcessEnv) {
    this.env = {...env};
  }

  setEnvironment(env: NodeJS.ProcessEnv): void {
    this.env = {...env};
  }

  onEvent(listener: (event: RpcResponse) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async restart(): Promise<void> {
    await this.stop();
    this.start();
    await this.request('ping', {});
  }

  start(): void {
    if (this.child && !this.child.killed) return;
    const repoRoot = path.resolve(app.getAppPath(), '..');
    const packagedEngine = path.join(process.resourcesPath, 'engine', 'autoeditor-engine.exe');
    const command = app.isPackaged ? packagedEngine : (process.env.AUTOEDITOR_PYTHON || 'python');
    const args = app.isPackaged ? ['--studio-service'] : ['-m', 'autoeditor.studio.service'];
    const remotionDir = app.isPackaged ? path.join(process.resourcesPath, 'remotion') : path.join(repoRoot, 'remotion');

    const runtimeEnv: NodeJS.ProcessEnv = {...process.env, ...this.env, PYTHONUNBUFFERED: '1', AUTOEDITOR_REMOTION_DIR: remotionDir};
    if (app.isPackaged) {
      const nodeDir = path.join(process.resourcesPath, 'node');
      const toolsDir = path.join(process.resourcesPath, 'tools');
      runtimeEnv.PATH = [nodeDir, toolsDir, process.env.PATH || ''].filter(Boolean).join(path.delimiter);
      const ffmpeg = path.join(toolsDir, 'ffmpeg.exe');
      const ffprobe = path.join(toolsDir, 'ffprobe.exe');
      const npx = path.join(nodeDir, 'npx.cmd');
      if (fs.existsSync(ffmpeg)) runtimeEnv.AUTOEDITOR_FFMPEG = ffmpeg;
      if (fs.existsSync(ffprobe)) runtimeEnv.AUTOEDITOR_FFPROBE = ffprobe;
      if (fs.existsSync(npx)) runtimeEnv.AUTOEDITOR_NPX = npx;
      const browserRoot = path.join(remotionDir, 'node_modules', '.remotion', 'chrome-headless-shell');
      const browser = findFile(browserRoot, 'chrome-headless-shell.exe');
      if (browser) runtimeEnv.REMOTION_BROWSER = browser;
    }

    this.child = spawn(command, args, {
      cwd: app.isPackaged ? process.resourcesPath : repoRoot,
      env: runtimeEnv,
      windowsHide: true,
    });
    const lines = readline.createInterface({input: this.child.stdout});
    lines.on('line', (line) => this.handleLine(line));
    this.child.stderr.on('data', (chunk) => {
      const text = chunk.toString().trim();
      if (text) this.emit({event: 'engine.stderr', params: {line: text}});
    });
    this.child.on('exit', (code) => {
      for (const item of this.pending.values()) item.reject(new Error(`Engine service exited (${code ?? 'unknown'})`));
      this.pending.clear();
      this.child = null;
    });
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.child = null;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        try { child.kill(); } catch {}
        resolve();
      }, 2500);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
      try { child.kill(); } catch { clearTimeout(timer); resolve(); }
    });
  }

  request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    this.start();
    const child = this.child;
    if (!child) return Promise.reject(new Error('Engine service is not running'));
    const id = this.nextId++;
    const payload = JSON.stringify({id, method, params});
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, {resolve: resolve as (value: unknown) => void, reject});
      child.stdin.write(payload + '\n');
    });
  }

  private handleLine(line: string): void {
    let value: RpcResponse;
    try { value = JSON.parse(line) as RpcResponse; } catch { return; }
    if (value.event) {
      this.emit(value);
      return;
    }
    if (typeof value.id === 'number') {
      const pending = this.pending.get(value.id);
      if (!pending) return;
      this.pending.delete(value.id);
      if (value.error) pending.reject(new Error(value.error.message));
      else pending.resolve(value.result);
    }
  }

  private emit(event: RpcResponse): void {
    for (const listener of this.listeners) listener(event);
  }
}
