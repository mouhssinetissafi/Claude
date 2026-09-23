export type MediaKind = 'image' | 'video';

export interface MediaItem {
  id: string;
  file_name: string;
  relative_path: string;
  kind: MediaKind;
  size_bytes: number;
  sha256: string;
  imported_at: string;
}

export interface StudioChoices {
  topic: string;
  script_mode: 'ai' | 'manual';
  script_text: string;
  scene_mode: 'ai' | 'manual';
  voice_mode: 'ai' | 'imported' | 'placeholder';
  voice_file: string | null;
  captions_mode: 'auto' | 'manual';
  theme: string;
  watermark_enabled: boolean;
  watermark_position: 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left';
  watermark_opacity: number;
  watermark_width_fraction: number;
  mock_mode: boolean;
}

export interface StudioProject {
  version: number;
  project_id: string;
  name: string;
  root: string;
  created_at: string;
  updated_at: string;
  media: MediaItem[];
  choices: StudioChoices;
  last_job_id: string | null;
  logo_relative_path: string | null;
}

export interface StudioJob {
  id: string;
  project_root: string;
  engine_job_name: string;
  status: 'queued' | 'running' | 'cancelling' | 'cancelled' | 'prepared' | 'complete' | 'needs_review' | 'failed';
  started_at: number | null;
  ended_at: number | null;
  return_code: number | null;
  message: string;
  command: string[];
  final_path: string | null;
  pid: number | null;
  prepare_only: boolean;
  /** Current engine stage key, e.g. "analyzed" (null until the engine reports its first stage). */
  stage?: string | null;
  /** Human label of what the engine is doing now, e.g. "Analyzing scenes". */
  stage_label?: string;
  /** 1-based position of the current stage (0 before the first stage) out of stage_total. */
  stage_index?: number;
  stage_total?: number;
}

export interface CredentialStatus {
  anthropic: boolean;
  elevenlabs: boolean;
  elevenlabs_voice: boolean;
}

export type StudioEvent =
  | {event: 'job.log'; params: {job_id: string; line: string}}
  | {event: 'job.status'; params: {job: StudioJob}};

export interface DesktopApi {
  ping(): Promise<{ok: boolean; service_version: number; engine_version: string}>;
  newProject(name: string): Promise<StudioProject | null>;
  openProject(): Promise<StudioProject | null>;
  openProjectAt(path: string): Promise<StudioProject>;
  updateProject(path: string, choices: Partial<StudioChoices>): Promise<StudioProject>;
  importMedia(path: string): Promise<StudioProject | null>;
  importLogo(path: string): Promise<StudioProject | null>;
  importVoice(path: string): Promise<StudioProject | null>;
  removeMedia(path: string, mediaId: string): Promise<StudioProject>;
  startJob(path: string, options?: {mock?: boolean; dry_run?: boolean; prepare_only?: boolean}): Promise<StudioJob>;
  getPreview(path: string): Promise<unknown>;
  cancelJob(jobId: string): Promise<StudioJob>;
  getJob(jobId: string): Promise<StudioJob>;
  credentialStatus(): Promise<CredentialStatus>;
  saveCredentials(values: {anthropic?: string; elevenlabs?: string; elevenlabsVoice?: string}): Promise<CredentialStatus>;
  revealPath(path: string): Promise<void>;
  fileUrl(path: string): Promise<string>;
  onEvent(listener: (event: StudioEvent) => void): () => void;
}

declare global {
  interface Window {
    studio: DesktopApi;
  }
}
