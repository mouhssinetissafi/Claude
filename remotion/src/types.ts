/**
 * TypeScript mirror of the JSON contracts produced by the Python side.
 * Keep in sync with autoeditor/schemas.py (timeline, captions, script).
 */

export type SegmentType = 'video' | 'image';
export type SegmentEffect = 'none' | 'kenburns' | 'zoom_in' | 'zoom_out';
export type SegmentTransition = 'cut' | 'fade';

export interface TimelineSegment {
  /** Path relative to the job asset base (e.g. "normalized/01_clip.mp4"). */
  src: string;
  type: SegmentType;
  /** Timeline seconds. */
  start: number;
  end: number;
  /** Source seconds inside the media file (video only). */
  source_start?: number;
  source_end?: number;
  scene_id?: number | null;
  effect?: SegmentEffect;
  transition?: SegmentTransition;
  /** Diagnostic: how this segment was chosen ("still_frame", "loop", ...). */
  fallback?: string | null;
}

export interface TimelineLine {
  line_id: number;
  start: number;
  end: number;
  overlay_text?: string | null;
  emphasis_words?: string[];
  segments: TimelineSegment[];
}

export interface TimelineMusic {
  src: string;
  volume: number;
  ducking?: boolean;
  ducking_volume?: number;
}

export interface TimelineSfx {
  src: string;
  at: number;
  volume?: number;
}

export type WatermarkPosition = 'top-right' | 'top-left' | 'bottom-right' | 'bottom-left';

/** Optional logo watermark (assets/branding/logo.png), placed inside the safe zones. */
export interface TimelineWatermark {
  src: string;
  position: WatermarkPosition;
  /** Fraction of frame width reserved for the logo box. */
  width_fraction: number;
  /** Fraction of frame height reserved for the logo box. */
  max_height_fraction: number;
  opacity?: number;
  margin?: number;
}

export interface Timeline {
  version: number;
  fps: number;
  width: number;
  height: number;
  duration: number;
  voice: string;
  music?: TimelineMusic | null;
  sfx?: TimelineSfx[];
  watermark?: TimelineWatermark | null;
  lines: TimelineLine[];
}

export interface CaptionWord {
  text: string;
  start: number;
  end: number;
  line_id?: number | null;
  emphasis?: boolean;
}

export interface CaptionSegment {
  text: string;
  start: number;
  end: number;
  words: CaptionWord[];
}

export interface Captions {
  version: number;
  language: string;
  duration: number;
  words: CaptionWord[];
  segments: CaptionSegment[];
}

export interface ScriptLine {
  id: number;
  narration: string;
  overlay_text?: string | null;
  emphasis_words?: string[];
}

export interface ScriptSummary {
  title: string;
  lines: ScriptLine[];
}

/**
 * Input props of the "Short" composition (written by autoeditor/pipeline/render.py).
 * Declared as a type alias (not an interface) so it satisfies Remotion's
 * Record<string, unknown> constraint on composition props.
 */
export type ShortProps = {
  jobId: string;
  /** Prefix under remotion/public, e.g. "jobs/iphone_air/". */
  assetBase: string;
  theme: string;
  timeline: Timeline;
  captions: Captions;
  script: ScriptSummary;
};
