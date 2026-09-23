# Studio implementation status

## Implemented in this checkpoint

- Versioned portable Studio project format (`project.json`, media, output, rebuildable engine state).
- Local JSON-RPC sidecar over stdin/stdout; no network port is opened.
- Generation runs in an isolated child process with streamed logs and cancellation.
- Electron + React desktop shell scaffold with New/Open Project, media import, autosaved choices, job progress and final-video playback.
- Paid AI is opt-in per step in the project model; mock/free mode is visible in the UI.
- API credentials are encrypted with Electron `safeStorage` and kept out of project files.
- Manual script mode: user narration is locked verbatim; AI only matches visuals. Short manual scripts are flagged instead of silently rewritten or padded.
- Imported narration mode: user audio is copied into the project, converted locally, and aligned to a manual script without ElevenLabs.
- Project-specific PNG logo with on/off, corner, opacity and size controls.
- Generate/Edit split: Studio can prepare the timeline/props without encoding, then preview the exact Remotion composition and export MP4 separately.
- Windows installer pipeline: PyInstaller sidecar + Electron/NSIS, with FFmpeg/ffprobe, private Node runtime, Remotion dependencies and Chrome Headless Shell bundled by the Windows builder.

## Still to build before calling Studio 1.0 complete

- Full visual timeline editing (trim, reorder, replace, multi-track drag/drop).
- Direct caption text/timing/style editor and caption presets.
- Audio mixer UI (music/SFX library, volume automation, fades, ducking controls and waveform).
- Manual scene placement in the timeline (the current first cut is AI-matched).
- Per-scene crop/reframe/zoom/pan handles in the preview.
- Undo/redo command stack for timeline edits.
- Proxy generation and thumbnail/waveform caches for very large projects.
- GPU capability probe and hardware-encode selection/fallback.
- Exact pre-run API cost estimate based on provider/model pricing and planned calls.
- Installer code signing, auto-update and diagnostics bundle.

The existing CLI remains supported throughout development.
