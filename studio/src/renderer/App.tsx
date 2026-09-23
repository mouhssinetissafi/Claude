import React, {useEffect, useMemo, useState} from 'react';
import {Player} from '@remotion/player';
import {Short} from '../../../remotion/src/Short';
import type {ShortProps} from '../../../remotion/src/types';
import type {CredentialStatus, StudioChoices, StudioEvent, StudioJob, StudioProject} from '../shared/types';

const defaultCredentials: CredentialStatus = {anthropic: false, elevenlabs: false, elevenlabs_voice: false};

const formatBytes = (bytes: number): string => {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const paidSteps = (choices: StudioChoices): string[] => {
  if (choices.mock_mode) return [];
  const steps: string[] = [];
  if (choices.script_mode === 'ai') steps.push('Anthropic · script');
  if (choices.scene_mode === 'ai') steps.push('Anthropic · visual/scene decisions');
  if (choices.voice_mode === 'ai') steps.push('ElevenLabs · narration');
  return steps;
};

export const App: React.FC = () => {
  const [project, setProject] = useState<StudioProject | null>(null);
  const [job, setJob] = useState<StudioJob | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [credentials, setCredentials] = useState<CredentialStatus>(defaultCredentials);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [anthropicKey, setAnthropicKey] = useState('');
  const [elevenKey, setElevenKey] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [finalUrl, setFinalUrl] = useState<string>('');
  const [previewProps, setPreviewProps] = useState<ShortProps | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>('');
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState('My First Short');
  const [newProjectError, setNewProjectError] = useState('');

  useEffect(() => {
    void window.studio.credentialStatus().then(setCredentials).catch(() => undefined);
    const unsubscribe = window.studio.onEvent((event: StudioEvent) => {
      if (event.event === 'job.log') {
        setLogs((current) => [...current.slice(-399), event.params.line]);
      } else if (event.event === 'job.status') {
        setJob(event.params.job);
        if (event.params.job.status === 'prepared') {
          void window.studio.getPreview(event.params.job.project_root).then((value) => setPreviewProps(value as ShortProps)).catch((e) => setError(e.message));
        }
        if (event.params.job.final_path) {
          void window.studio.fileUrl(event.params.job.final_path).then(setFinalUrl);
        }
      }
    });
    return unsubscribe;
  }, []);

  const costs = useMemo(() => project ? paidSteps(project.choices) : [], [project]);

  const run = async (fn: () => Promise<void>): Promise<void> => {
    setBusy(true);
    setError('');
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };

  // Electron does not implement window.prompt() (it throws "prompt() is not supported."),
  // so the project name is asked for in an in-app dialog instead.
  const newProject = (): void => {
    setNewProjectError('');
    setNewProjectOpen(true);
  };

  const createProject = async (): Promise<void> => {
    const name = newProjectName.trim();
    if (!name) { setNewProjectError('Enter a project name.'); return; }
    setBusy(true);
    setNewProjectError('');
    try {
      const created = await window.studio.newProject(name);
      if (created) { setNewProjectOpen(false); setProject(created); setJob(null); setLogs([]); setFinalUrl(''); setPreviewProps(null); }
    } catch (e) {
      const reason = (e instanceof Error ? e.message : String(e)).replace(/^Error invoking remote method '[^']*': (Error: )?/, '');
      setNewProjectError(`Could not create the project. ${reason}`);
    } finally {
      setBusy(false);
    }
  };

  const openProject = (): void => {
    void run(async () => {
      const opened = await window.studio.openProject();
      if (opened) { setProject(opened); setJob(null); setLogs([]); setFinalUrl(''); setPreviewProps(null); }
    });
  };

  const updateChoices = (patch: Partial<StudioChoices>): void => {
    if (!project) return;
    const optimistic = {...project, choices: {...project.choices, ...patch}};
    setProject(optimistic);
    window.clearTimeout((updateChoices as unknown as {timer?: number}).timer);
    (updateChoices as unknown as {timer?: number}).timer = window.setTimeout(() => {
      void window.studio.updateProject(project.root, patch).then(setProject).catch((e) => setError(e.message));
    }, 350);
  };

  const importMedia = (): void => {
    if (!project) return;
    void run(async () => {
      const updated = await window.studio.importMedia(project.root);
      if (updated) setProject(updated);
    });
  };

  const prepareEdit = (): void => {
    if (!project) return;
    void run(async () => {
      const saved = await window.studio.updateProject(project.root, project.choices);
      setProject(saved);
      setLogs([]);
      setFinalUrl('');
      setPreviewProps(null);
      const started = await window.studio.startJob(project.root, {mock: project.choices.mock_mode, prepare_only: true});
      setJob(started);
    });
  };

  const exportVideo = (): void => {
    if (!project) return;
    void run(async () => {
      const saved = await window.studio.updateProject(project.root, project.choices);
      setProject(saved);
      setLogs([]);
      setFinalUrl('');
      const started = await window.studio.startJob(project.root, {mock: project.choices.mock_mode, prepare_only: false});
      setJob(started);
    });
  };

  const saveSecrets = (): void => {
    void run(async () => {
      const status = await window.studio.saveCredentials({anthropic: anthropicKey || undefined, elevenlabs: elevenKey || undefined, elevenlabsVoice: voiceId || undefined});
      setCredentials(status);
      setAnthropicKey(''); setElevenKey(''); setVoiceId(''); setSettingsOpen(false);
    });
  };

  if (!project) {
    return (
      <div className="home-shell">
        <div className="brand-mark">AE</div>
        <h1>Auto-Editor PRO Studio</h1>
        <p className="lead">Drop media. Choose what AI should do. Keep full control of the edit.</p>
        <div className="home-actions">
          <button className="primary" onClick={newProject} disabled={busy}>New project</button>
          <button onClick={openProject} disabled={busy}>Open project</button>
        </div>
        <button className="settings-link" onClick={() => setSettingsOpen(true)}>API settings</button>
        {error ? <div className="error-banner">{error}</div> : null}
        {settingsOpen ? <CredentialsModal {...{credentials, anthropicKey, setAnthropicKey, elevenKey, setElevenKey, voiceId, setVoiceId, saveSecrets, close: () => setSettingsOpen(false)}} /> : null}
        {newProjectOpen ? <NewProjectModal name={newProjectName} setName={setNewProjectName} error={newProjectError} busy={busy} create={() => void createProject()} close={() => setNewProjectOpen(false)} /> : null}
      </div>
    );
  }

  const running = job?.status === 'running' || job?.status === 'queued' || job?.status === 'cancelling';
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark small">AE</span><div><strong>Auto-Editor PRO</strong><span>{project.name}</span></div></div>
        <div className="top-actions">
          <button onClick={openProject}>Open</button>
          <button onClick={() => setSettingsOpen(true)}>API settings</button>
          {previewProps ? <button onClick={exportVideo} disabled={running}>Export MP4</button> : null}
          <button className="primary" onClick={prepareEdit} disabled={running || project.media.length === 0}>Generate Edit</button>
        </div>
      </header>

      <main className="workspace">
        <aside className="media-panel panel">
          <div className="panel-title"><h2>Media</h2><span>{project.media.length}</span></div>
          <button className="dropzone" onClick={importMedia}>+ Add photos / videos</button>
          <div className="media-list">
            {project.media.map((item) => (
              <div className="media-item" key={item.id}>
                <div className={`media-icon ${item.kind}`}>{item.kind === 'image' ? 'IMG' : 'VID'}</div>
                <div className="media-meta"><strong>{item.file_name}</strong><span>{formatBytes(item.size_bytes)}</span></div>
                <button className="icon-button" title="Remove" onClick={() => void window.studio.removeMedia(project.root, item.id).then(setProject)}>×</button>
              </div>
            ))}
          </div>
        </aside>

        <section className="preview-column">
          <div className="preview panel">
            {finalUrl ? <video src={finalUrl} controls playsInline /> : previewProps ? (
              <div className="player-wrap">
                <Player
                  component={Short}
                  inputProps={previewProps}
                  durationInFrames={Math.max(1, Math.ceil(previewProps.timeline.duration * previewProps.timeline.fps))}
                  compositionWidth={previewProps.timeline.width}
                  compositionHeight={previewProps.timeline.height}
                  fps={previewProps.timeline.fps}
                  controls
                  style={{width: '100%', height: '100%'}}
                />
              </div>
            ) : <div className="preview-placeholder"><div className="phone-outline"><span>9:16</span></div><p>Generate an edit to preview it here.</p></div>}
          </div>
          <div className="timeline-placeholder panel">
            <div className="track"><span>Visuals</span><div className="track-fill" /></div>
            <div className="track"><span>Captions</span><div className="track-fill short" /></div>
            <div className="track"><span>Voice</span><div className="track-fill voice" /></div>
            <small>Full timeline editing is the next Studio phase.</small>
          </div>
        </section>

        <aside className="inspector panel">
          <div className="panel-title"><h2>Project</h2><span>autosaved</span></div>
          <label>Topic <textarea value={project.choices.topic} onChange={(e) => updateChoices({topic: e.target.value})} placeholder="What is this Short about?" /></label>

          <div className="control-group">
            <span className="control-label">Script</span>
            <Segmented value={project.choices.script_mode} options={[['ai','AI writes'],['manual','My script']]} onChange={(value) => updateChoices({script_mode: value as 'ai'|'manual'})} />
            {project.choices.script_mode === 'manual' ? <textarea className="script-box" value={project.choices.script_text} onChange={(e) => updateChoices({script_text: e.target.value})} placeholder="Paste your final script here…" /> : null}
          </div>

          <div className="control-group">
            <span className="control-label">Scene decisions</span>
            <div className="feature-row"><strong>AI chooses the first cut</strong><span>Editable timeline is next</span></div>
          </div>

          <div className="control-group">
            <span className="control-label">Voice</span>
            <Segmented value={project.choices.voice_mode} options={[['ai','ElevenLabs'],['imported','My audio'],['placeholder','Free test']]} onChange={(value) => updateChoices({voice_mode: value as StudioChoices['voice_mode']})} />
            {project.choices.voice_mode === 'imported' ? <button onClick={() => void window.studio.importVoice(project.root).then((updated) => updated && setProject(updated))}>{project.choices.voice_file ? 'Replace narration audio' : 'Choose narration audio'}</button> : null}
          </div>

          <div className="control-group">
            <span className="control-label">Branding</span>
            <div className="row"><label className="inline"><input type="checkbox" checked={project.choices.watermark_enabled} onChange={(e) => updateChoices({watermark_enabled: e.target.checked})} /> Watermark/logo</label></div>
            <button onClick={() => void window.studio.importLogo(project.root).then((updated) => updated && setProject(updated))}>{project.logo_relative_path ? 'Replace logo PNG' : 'Choose logo PNG'}</button>
            {project.choices.watermark_enabled ? <>
              <label>Position
                <select value={project.choices.watermark_position} onChange={(e) => updateChoices({watermark_position: e.target.value as StudioChoices['watermark_position']})}>
                  <option value="top-right">Top right</option><option value="top-left">Top left</option><option value="bottom-right">Bottom right</option><option value="bottom-left">Bottom left</option>
                </select>
              </label>
              <label>Opacity · {Math.round(project.choices.watermark_opacity * 100)}%<input type="range" min="0.05" max="1" step="0.05" value={project.choices.watermark_opacity} onChange={(e) => updateChoices({watermark_opacity: Number(e.target.value)})} /></label>
              <label>Size · {Math.round(project.choices.watermark_width_fraction * 100)}%<input type="range" min="0.04" max="0.30" step="0.01" value={project.choices.watermark_width_fraction} onChange={(e) => updateChoices({watermark_width_fraction: Number(e.target.value)})} /></label>
            </> : null}
          </div>
          <div className="row"><label className="inline"><input type="checkbox" checked={project.choices.mock_mode} onChange={(e) => updateChoices({mock_mode: e.target.checked})} /> Free test mode (no APIs)</label></div>

          <div className="cost-card">
            <div><strong>Paid AI for this run</strong><span>{project.choices.mock_mode ? '€0 test' : costs.length ? `${costs.length} enabled step${costs.length > 1 ? 's' : ''}` : 'none'}</span></div>
            {project.choices.mock_mode ? <p>Mock mode calls no paid provider.</p> : costs.length ? <ul>{costs.map((item) => <li key={item}>{item}</li>)}</ul> : <p>All selected steps are local/manual.</p>}
            {!project.choices.mock_mode && project.choices.script_mode === 'ai' && !credentials.anthropic ? <p className="warning">Anthropic key missing.</p> : null}
            {!project.choices.mock_mode && project.choices.voice_mode === 'ai' && (!credentials.elevenlabs || !credentials.elevenlabs_voice) ? <p className="warning">ElevenLabs key/voice missing.</p> : null}
          </div>

          {job ? <JobCard job={job} logs={logs} onCancel={() => void window.studio.cancelJob(job.id).then(setJob)} onReveal={() => job.final_path && void window.studio.revealPath(job.final_path)} /> : null}
        </aside>
      </main>
      {error ? <div className="error-toast">{error}</div> : null}
      {settingsOpen ? <CredentialsModal {...{credentials, anthropicKey, setAnthropicKey, elevenKey, setElevenKey, voiceId, setVoiceId, saveSecrets, close: () => setSettingsOpen(false)}} /> : null}
    </div>
  );
};

const Segmented: React.FC<{value: string; options: [string,string][]; onChange: (value: string) => void}> = ({value, options, onChange}) => (
  <div className="segmented">{options.map(([id,label]) => <button key={id} className={value === id ? 'active' : ''} onClick={() => onChange(id)}>{label}</button>)}</div>
);

const formatElapsed = (seconds: number): string => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;

const JobCard: React.FC<{job: StudioJob; logs: string[]; onCancel: () => void; onReveal: () => void}> = ({job, logs, onCancel, onReveal}) => {
  const active = ['running', 'queued', 'cancelling'].includes(job.status);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  const total = job.stage_total ?? 0;
  const index = job.stage_index ?? 0;
  const elapsed = job.started_at ? Math.max(0, ((active ? now / 1000 : job.ended_at ?? now / 1000) - job.started_at)) : 0;
  const percent = ['prepared', 'complete'].includes(job.status) ? 100 : total > 0 ? Math.round((Math.max(0, index - 1) / total) * 100) : 0;
  return (
    <div className="job-card">
      <div className="job-head"><strong>{job.status.replace('_',' ')}</strong><span>{job.started_at ? formatElapsed(elapsed) : ''}</span></div>
      {active ? (
        <div className="job-stage">{index > 0 && total > 0 ? `Step ${index} of ${total} · ` : ''}{job.stage_label || 'Starting'}</div>
      ) : job.message ? (
        <div className={`job-message ${['failed', 'needs_review'].includes(job.status) ? 'problem' : ''}`}>{job.message}</div>
      ) : null}
      <div className="job-progress"><i className={active && index === 0 ? 'waiting' : ''} style={{width: `${active && index === 0 ? 100 : percent}%`}} /></div>
      <div className="log-box">{logs.slice(-10).map((line,i) => <div key={`${i}-${line}`}>{line}</div>)}</div>
      <div className="job-actions">
        {['running','queued'].includes(job.status) ? <button onClick={onCancel}>Cancel</button> : null}
        {job.status === 'cancelling' ? <button disabled>Cancelling…</button> : null}
        {job.final_path ? <button className="primary" onClick={onReveal}>Show final video</button> : null}
      </div>
    </div>
  );
};

const NewProjectModal: React.FC<{name: string; setName: (v: string) => void; error: string; busy: boolean; create: () => void; close: () => void}> = (p) => (
  <div className="modal-backdrop" onMouseDown={p.busy ? undefined : p.close}>
    <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
      <div className="modal-head"><h2>New project</h2><button className="icon-button" onClick={p.close} disabled={p.busy}>×</button></div>
      <p>Next you choose the folder where the project will be created.</p>
      <label>Project name <input autoFocus value={p.name} onChange={(e) => p.setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !p.busy) p.create(); }} /></label>
      {p.error ? <div className="error-banner" role="alert">{p.error}</div> : null}
      <div className="modal-actions"><button onClick={p.close} disabled={p.busy}>Cancel</button><button className="primary" onClick={p.create} disabled={p.busy}>{p.busy ? 'Creating…' : 'Create project'}</button></div>
    </div>
  </div>
);

interface CredentialsProps {
  credentials: CredentialStatus;
  anthropicKey: string; setAnthropicKey: (v:string)=>void;
  elevenKey: string; setElevenKey: (v:string)=>void;
  voiceId: string; setVoiceId: (v:string)=>void;
  saveSecrets: ()=>void; close: ()=>void;
}
const CredentialsModal: React.FC<CredentialsProps> = (p) => (
  <div className="modal-backdrop" onMouseDown={p.close}>
    <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
      <div className="modal-head"><h2>API settings</h2><button className="icon-button" onClick={p.close}>×</button></div>
      <p>Keys are encrypted with Windows secure storage and are never written to a project.</p>
      <label>Anthropic API key <input type="password" placeholder={p.credentials.anthropic ? 'Saved — enter only to replace' : 'sk-ant-…'} value={p.anthropicKey} onChange={(e) => p.setAnthropicKey(e.target.value)} /></label>
      <label>ElevenLabs API key <input type="password" placeholder={p.credentials.elevenlabs ? 'Saved — enter only to replace' : 'API key'} value={p.elevenKey} onChange={(e) => p.setElevenKey(e.target.value)} /></label>
      <label>ElevenLabs voice ID <input placeholder={p.credentials.elevenlabs_voice ? 'Saved — enter only to replace' : 'Voice ID'} value={p.voiceId} onChange={(e) => p.setVoiceId(e.target.value)} /></label>
      <div className="modal-actions"><button onClick={p.close}>Cancel</button><button className="primary" onClick={p.saveSecrets}>Save securely</button></div>
    </div>
  </div>
);
