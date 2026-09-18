# Auto-Editor PRO

Automated YouTube Shorts. Two modes share one renderer:

```
normal mode:        topic/facts ─┐
                                 ├─> script.json ─> TTS ─> captions.json ─> media ─> timeline.json ─> Remotion ─> final.mp4
footage-only mode:  inbox/<job>/ ┘   (script is written AGAINST the footage that exists)
```

Python/AI prepares everything; Remotion (`remotion/`) owns the visual timeline,
captions, animations, zooms, transitions, overlays, music, SFX and the final
render. See [docs/FOOTAGE_ONLY.md](docs/FOOTAGE_ONLY.md) for the footage-only
pipeline in detail.

## 1. Installation

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # core + dev tools
pip install faster-whisper                 # optional: real word-level captions
cd remotion && npm install && cd ..        # Remotion renderer
cp .env.example .env                       # fill in keys, then export them
```

## 2. Dependencies

| Dependency | Used for | Required? |
|---|---|---|
| Python 3.11+ | pipeline | yes |
| FFmpeg + ffprobe (on PATH) | probing, normalization, frames, audio concat, QC, loudness | yes |
| Node 18+ / npm | Remotion render | yes (unless `--skip-render`) |
| Chrome / Chromium | Remotion frame capture (downloaded by Remotion, or set `REMOTION_BROWSER`) | yes for render |
| `anthropic` | vision analysis + script writing (Claude) | yes unless `--mock` |
| `scenedetect` + `opencv-python-headless` | scene detection (FFmpeg fallback exists) | recommended |
| `faster-whisper` | captions with word timestamps | optional (mock otherwise) |
| ElevenLabs account | narration | optional (mock otherwise) |
| `google-api-python-client` | YouTube upload | optional, off by default |

## 3. Environment variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude vision + LLM (or run `ant auth login`) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` | ElevenLabs TTS |
| `PEXELS_API_KEY` | stock footage for normal mode (optional) |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | upload (only with `--upload`) |
| `REMOTION_BROWSER` | path to a Chrome/Chromium binary for Remotion |
| `AUTOEDITOR_MOCK=1` | force offline mock providers (same as `--mock`) |

Keys are read only by the provider modules and are redacted from logs. They are
never stored in config files.

## 4. Folder structure

```
run.py                     CLI entry point
config/default.yaml        every default (thresholds, models, audio, QC)
autoeditor/
  cli.py                   argument parsing, mode dispatch
  config.py, cache.py, schemas.py, logging_utils.py
  media/                   ffprobe + ffmpeg wrappers
  providers/               Anthropic, ElevenLabs, faster-whisper, mocks, factory
  pipeline/                shared stages: voice, captions, render, qc, metadata, credits,
                           audio_assets, state, job paths, upload, normal-mode script/media/timeline
  footage_only/            discovery, normalize, scenes, vision, inventory, shot_plan, timeline, runner
remotion/                  Remotion project (src/Short.tsx, components/, types.ts, themes.ts)
inbox/<job>/               footage-only input (never modified)
work/<job>/                intermediates: normalized/, frames/, audio/, *.json, job_state.json, job.log
output/<job>/              final.mp4, qc.json, metadata.json, credits.txt, thumbnail_base.jpg
cache/<job>/               vision + LLM response cache (content-hash keyed)
review/<job>/              copies of outputs that failed QC
assets/music, assets/sfx   optional music bed / SFX (license sidecars carried into credits)
assets/media               optional local footage library for normal mode
tests/                     pytest suite (mock providers, synthetic clips)
docs/FOOTAGE_ONLY.md       footage-only mode reference
```

## 5. Footage-only example

```bash
mkdir -p inbox/iphone_air
cp ~/Downloads/*.mp4 inbox/iphone_air/
echo "why the iPhone Air is so thin" > inbox/iphone_air/topic.txt   # optional

export ANTHROPIC_API_KEY=...  ELEVENLABS_API_KEY=...  ELEVENLABS_VOICE_ID=...
python run.py --footage-only --inbox ./inbox
# -> output/iphone_air/final.mp4, qc.json, metadata.json, credits.txt, thumbnail_base.jpg
```

Useful flags: `--job NAME`, `--skip-vision`, `--skip-voice`, `--skip-render`,
`--force-reanalyze`, `--dry-run`, `--max-duration 50`, `--min-scene-duration 1.0`,
`--no-upload`, `--mock`, `--theme bold`, `--config my.yaml`.

Plumbing check with no API cost:

```bash
python run.py --footage-only --inbox ./inbox --mock
```

## 6. Normal mode example

```bash
python run.py --topic "Why the iPhone Air is so thin" --facts facts.txt
python run.py --topic-file topic.txt --job my_short --skip-render
```

`facts.txt` is one verified fact per line; without it every numeric/date/price
claim is added to `facts_to_verify`. Media is resolved from `assets/media/`
(matched by file name keywords), then Pexels (if `PEXELS_API_KEY` is set), then a
flagged gradient placeholder so the render is never black.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `ffprobe is not installed` | install FFmpeg and make sure `ffmpeg`/`ffprobe` are on PATH |
| `No Anthropic credentials found` | export `ANTHROPIC_API_KEY` or use `--mock` / `--skip-vision` |
| `ELEVENLABS_API_KEY is not set` | export it, or `--skip-voice` (placeholder tone) or `--mock` |
| `faster-whisper is not installed` | `pip install faster-whisper` or set `captions.provider: mock` |
| `Remotion dependencies missing` | `cd remotion && npm install` |
| `Failed to launch the browser process` / "Old Headless mode has been removed" | set `REMOTION_BROWSER` to a `chrome-headless-shell` binary, or let Remotion download one (`npx remotion browser ensure`) |
| Render is slow | set `render.concurrency` in config; footage is already normalized so decoding is cheap |
| `QC FAILED` | read `output/<job>/qc.json`; the copy in `review/<job>/` is for a human, nothing is uploaded |
| Every scene rejected | lower `vision.min_quality_score` / `vision.min_visual_interest_score`, check for watermarks |
| Script too short | footage is thin; add clips or lower `script.target_min_seconds` |
| Job stuck after crash | just re-run the same command; see "recovering failed jobs" |

## 8. API cost considerations

* Vision: one Claude request per detected scene (2 frames by default,
  `vision.max_frames_per_scene`). `media.max_scenes_per_source` caps requests
  per clip. Responses are cached by content hash, so re-runs and identical
  footage cost nothing.
* Script + metadata: one request each (retries only on invalid JSON or banned
  phrases, bounded by `llm.max_json_retries`).
* Models default to `claude-opus-5`; change `vision.model` / `llm.model` in a
  config override to trade quality for cost. `vision.effort: medium` keeps
  per-frame analysis cheap.
* TTS: one ElevenLabs request per narration line, cached by text hash.
* `--skip-vision`, `--skip-voice`, `--dry-run` and `--mock` make no paid calls.

## 9. How caching works

`autoeditor/cache.py` keys every expensive result by *content*, never by file
name:

* normalized clips: source file hash sidecar (`*.meta.json`) → skip re-encode
* scenes: `sha256(source hash | start | end)` → stable `content_hash`
* vision: `scene content_hash + frame hashes + model + prompt version` in
  `cache/<job>/vision/`
* shot plan / script / metadata: hash of the full prompt in `cache/<job>/llm/`
* TTS lines: text hash sidecar next to `audio/line_NNN.*`

`--force-reanalyze` clears the job's vision and LLM caches and resets the state
to re-run analysis and later stages.

## 10. Recovering failed jobs

Every job writes `work/<job>/job_state.json` with the completed stages
(`discovered, normalized, analyzed, scripted, voiced, captioned,
timeline_ready, rendered, qc_passed, complete`), warnings and errors, plus a
full log in `work/<job>/job.log` (secrets redacted). Re-running the same command
resumes after the last completed stage. To redo from a given stage, remove it and
later stages from `stages` in `job_state.json`. Deleting `work/<job>/` starts
over while `cache/<job>/` still prevents paying for vision again.

## Development

```bash
python -m pytest            # all tests use mock providers; synthetic clips need ffmpeg
ruff check autoeditor tests && ruff format --check autoeditor tests
mypy
cd remotion && npx tsc --noEmit && npx remotion studio src/index.ts   # preview composition
```

Data contracts (script.json, captions.json, timeline.json, scenes.json, qc.json,
metadata.json) are defined once in `autoeditor/schemas.py` and mirrored in
`remotion/src/types.ts`.
