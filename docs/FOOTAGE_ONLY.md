# Footage-only mode

`--footage-only` turns raw stock/video clips into a finished YouTube Short using
the existing Auto-Editor PRO Remotion renderer. Python/AI prepares everything
Remotion needs (script, voice, captions, timeline); Remotion still owns the
visual timeline, captions, animations, zooms, transitions, overlays, music,
SFX and the final render.

```
python run.py --footage-only --inbox ./inbox
```

## Input layout

```
inbox/
  iphone_air/                 # one folder = one job
    clip01.mp4
    clip02.mov
    clip03.mp4
    topic.txt                 # optional
    clip01.license.json       # optional license sidecar
    credits.json              # optional job-level license manifest
```

* `topic.txt` present: the story is about that topic, told with the footage
  that exists. Factual claims the footage cannot prove are flagged in
  `facts_to_verify` instead of being presented as certain.
* `topic.txt` missing or empty: the most coherent story is inferred from the
  footage alone, without inventing facts.
* Original files are never modified. Sub-folders are not scanned.
* Supported extensions are listed in `config/default.yaml` (`media.supported_extensions`).

### License sidecars (Phase 13)

Checked in this order for each clip:

1. `<name>.license.json`, `<stem>.license.json`, `<stem>.json` with keys
   `source`, `author`, `license`, `url`, `notes`
2. `<name>.license.txt` / `<stem>.license.txt` (free text; a `License:` line is parsed)
3. job-level `credits.json` / `sources.json` / `licenses.json` keyed by file name
   (or a `"default"` entry)

Anything else is written as `LICENSE_UNKNOWN` in `credits.txt` with a warning.
No license is ever inferred.

## Pipeline

| Phase | Module | Output |
|---|---|---|
| 1 Discovery | `footage_only/discovery.py` | `work/<job>/discovery.json` |
| 2 Validation + normalization | `footage_only/normalize.py` | `work/<job>/normalized/*.mp4`, `media_manifest.json` |
| 3 Scene detection + frames | `footage_only/scenes.py` | `scenes.json`, `frames/scene_NNN_{25,50,75}.jpg` |
| 4 Vision analysis | `footage_only/vision.py` | `scenes.json` (with `analysis`), `cache/<job>/vision/` |
| 5 Inventory | `footage_only/inventory.py` | `inventory.json` |
| 6 Script + shot plan | `footage_only/shot_plan.py` | `script.json` |
| 7 Voice | `pipeline/voice.py` | `audio/line_NNN.mp3`, `audio/voice.mp3`, `voice_timing.json` |
| 8 Captions | `pipeline/captions.py` | `captions.json` |
| 9 Timeline | `footage_only/timeline.py` | `timeline.json` |
| 10 Remotion | `pipeline/render.py` | `work/<job>/render_raw.mp4` |
| 11 QC | `pipeline/qc.py` | `output/<job>/final.mp4`, `qc.json` (or `review/<job>/`) |
| 12 Metadata | `pipeline/metadata.py` | `metadata.json`, `thumbnail_base.jpg` |
| 13 Credits | `pipeline/credits.py` | `credits.txt` |
| 15 State | `pipeline/state.py` | `job_state.json`, `job.log` |

### Normalization (Phase 2)

Every clip is probed with ffprobe (duration, size, fps, codec, aspect ratio,
audio presence). Corrupt files, unsupported formats, clips shorter than
`media.min_clip_seconds` and very low resolutions are rejected with a reason
in `media_manifest.json`. Usable clips are encoded with:

```
ffmpeg -i in.mp4 -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30" \
  -c:v libx264 -crf 18 -pix_fmt yuv420p -an out.mp4
```

Nothing is stretched. Normalized files carry a content-hash sidecar so a
re-run skips clips that have not changed.

### Scene detection (Phase 3)

PySceneDetect (`ContentDetector`, threshold `media.scene_threshold`) with an
FFmpeg `scene` filter fallback. Scenes are logical records, not files. Scenes
shorter than `media.min_scene_seconds` are merged into neighbours; scenes longer
than `media.max_scene_seconds` are chunked so the timeline has variety. Frames
are taken at 50 %, 25 % and 75 % of each scene.

### Vision analysis (Phase 4)

Each scene's frames (up to `vision.max_frames_per_scene`) are sent to the
Anthropic vision model with a strict JSON schema (see `SCENE_ANALYSIS_SCHEMA`
in `autoeditor/schemas.py`). Responses are validated locally before use.
Scenes are rejected for low quality, low visual interest, watermarks or
`safe_to_use: false`. The model is asked not to identify people.

Cache key = content hash of the scene (source hash + bounds) + frame hashes +
model + prompt version. Unchanged footage never triggers a second API call,
even across job re-runs. `--force-reanalyze` clears the job's vision and LLM
caches. `--skip-vision` uses neutral heuristic scores at zero cost.

### Inventory (Phase 5)

`inventory.json` summarises usable scenes with descriptions, scores, durations,
per-source totals, recurring subjects, products/brands, rejected scenes and
`duplicate_groups` (near-identical scenes found by token overlap of the vision
descriptions). `strongest_scenes` is diversified across sources and
duplicate groups so one look cannot dominate.

### Script + shot plan (Phase 6)

The inventory and topic are sent to the LLM with the editorial rules
(cold open, escalation, payoff, banned phrases, no personal experience, fact
safety). The model returns narration lines **with** `scene_ids`,
`overlay_text` and `emphasis_words`. After schema validation the plan is
repaired deterministically:

* unknown scene ids are dropped, consecutive duplicates removed
* line 1 is forced to open on a `strongest_scenes` entry
* over-long scripts are trimmed to `script.hard_max_seconds`
* numeric/date/price claims are auto-added to `facts_to_verify`
* banned phrases trigger a retry with feedback, then a hard failure

No research/source module exists in this project, so `verified_sources` is
always empty and the model is told so. Unsupported claims land in
`facts_to_verify` and are appended to the description in `metadata.json` as a
review block.

### Voice, captions (Phases 7-8)

Narration is synthesized line by line (ElevenLabs when configured, mock
otherwise), leading/trailing silence trimmed, exact durations measured with
ffprobe and concatenated with a small natural gap (`tts.inter_line_gap_seconds`)
into `voice.mp3`. `voice_timing.json` holds `start`/`end`/`duration` per line.
Captions come from faster-whisper word timestamps on the final joined narration,
mapped back to lines and emphasis words, and written in the schema the Remotion
project consumes.

### Timeline (Phase 9)

For every line, slot = measured TTS duration. Assigned scenes fill the slot in
order at 1x speed. When they run short the fallback chain is:

1. visually similar unused scene (`similar_to` from the inventory)
2. a different section of the same source clip
3. tasteful reuse outside `timeline.repeat_window_seconds`
4. Ken Burns still frame of the anchor scene

Segments never exceed their source span (no stretching), the final segment is
extended through `timeline.outro_seconds`, and `check_timeline_math` asserts
the whole thing tiles with no gaps. `timeline.json` is the single contract the
renderer reads; `script.json` keeps the narration and scene assignments.

### QC (Phase 11)

After Remotion renders `render_raw.mp4`, loudness is normalized **once**
(EBU R128, `audio.loudness_target_lufs`) into `output/<job>/final.mp4`. Then:
stream presence, resolution, duration, `blackdetect`, `silencedetect`, peak
level (`volumedetect`) and caption overrun are checked. `qc.json` records each
check. On failure the deliverables are copied to `review/<job>/` and nothing is
uploaded.

## CLI flags

```
python run.py --footage-only --inbox ./inbox [--job NAME] [--skip-vision] [--skip-voice]
              [--skip-render] [--force-reanalyze] [--dry-run] [--max-duration 50]
              [--min-scene-duration 1.0] [--min-clip-duration 1.0] [--no-upload]
              [--mock] [--config my.yaml] [--theme bold] [--log-level DEBUG]
```

* `--dry-run` probes the media, prints what would happen and writes nothing.
* `--mock` uses deterministic offline providers (tests, CI, plumbing checks).
* `--skip-render` stops after `timeline.json`; re-run without it to render.
* `--upload` is required for any upload, plus `upload.enabled: true` in config,
  passing QC and YouTube credentials. There is no automatic upload.

## Resume / recovery

`work/<job>/job_state.json` records every completed stage
(`discovered → normalized → analyzed → scripted → voiced → captioned →
timeline_ready → rendered → qc_passed → complete`). Re-running the same
command resumes after the last completed stage. To redo a stage, delete it and
everything after it from `stages` in `job_state.json`, or:

* `--force-reanalyze` to redo vision + script + everything after
* delete `work/<job>/` to start over (the cache in `cache/<job>/` still avoids
  paying for vision calls again)

Errors are recorded in `job_state.json` under `errors` with the failing stage.

## Cost notes

* Vision: one request per scene (2 frames each by default). A job with 30
  scenes is 30 requests; cached forever for unchanged footage.
* Script: one request (plus retries on banned phrases or invalid JSON).
* Metadata: one small request.
* TTS: one ElevenLabs request per line, cached by text hash.
* `--skip-vision` and `--mock` cost nothing.
