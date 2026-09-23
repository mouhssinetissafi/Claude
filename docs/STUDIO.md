# Auto-Editor PRO Studio

Studio is the desktop boundary around the existing, tested Auto-Editor PRO engine.
The engine remains usable from `run.py`; the desktop app talks to a local sidecar
service instead of importing pipeline internals directly.

## Phase 1 boundary (implemented)

Run the service in development:

```bash
python -m autoeditor.studio.service
```

It reads newline-delimited JSON requests on stdin and writes responses/events on
stdout. No TCP port is opened.

Example:

```json
{"id":1,"method":"ping","params":{}}
{"id":2,"method":"project.create","params":{"path":"C:/Videos/My Short","name":"My Short"}}
{"id":3,"method":"project.import_media","params":{"path":"C:/Videos/My Short","files":["C:/Footage/a.mp4","C:/Footage/b.jpg"]}}
{"id":4,"method":"project.update","params":{"path":"C:/Videos/My Short","choices":{"topic":"Why this gadget failed","mock_mode":true}}}
{"id":5,"method":"job.start","params":{"path":"C:/Videos/My Short","mock":true}}
```

Generation is launched as a child process through the existing `run.py` entry
point. Log lines and job-state updates are emitted as asynchronous events. A job
can be cancelled by `job.cancel`, which terminates only the engine child process.

## Project format

```text
My Short/
  project.json       # versioned user choices, never API keys
  media/             # imported originals, never modified
  output/            # final renders
  .engine/           # generated/staged/cached state; rebuildable
```

Paid actions are represented explicitly in `project.json` (`script_mode`,
`voice_mode`, `scene_mode`) so the future UI can show exactly which API-backed
steps are enabled before Generate is pressed. Credentials belong in Windows
Credential Manager, not the project.

## Manual and AI modes

Studio supports both AI-assisted and manual workflows. A supplied manual script is
kept verbatim while the engine matches available visuals to it. Imported narration
can be aligned to a manual script, so ElevenLabs is optional. Free Test mode uses
mock providers and makes no paid API calls.

## Desktop shell (MVP scaffold)

`studio/` contains the Electron + React desktop shell. The first UI milestone can
create/open projects, import any number of supported photos/videos, choose which
paid AI steps are enabled, securely store API credentials through Electron's
OS-backed `safeStorage`, start/cancel a generation, stream engine logs, and play
the finished MP4.

Development prerequisites are Node.js plus the Python environment used by the
engine:

```bash
cd studio
npm install
npm run typecheck
npm run dev
```

The production Windows installer will bundle a PyInstaller engine sidecar; that
packaging step is intentionally separated from the engine/UI boundary so both
remain testable independently.

## Windows packaging

On a Windows build machine, `scripts/build_studio_windows.ps1` builds the Python
sidecar with PyInstaller, verifies its local RPC endpoint, type-checks the Electron
app, and creates an NSIS installer with electron-builder. The packaged sidecar
runs generations as child instances of itself (`--engine-cli`) so Cancel remains
reliable in the installed app.

The Windows builder now prepares a self-contained installer: it packages the
PyInstaller engine sidecar, FFmpeg/ffprobe, a private Node runtime, Remotion
dependencies and Remotion's matching Chrome Headless Shell. Normal installed-app
use therefore does not require Python, Node or FFmpeg to be installed separately.

For a local build, double-click `BUILD_INSTALLER.bat`. The finished installer is
written to `release/Auto-Editor-PRO-Studio-Setup.exe`. A GitHub Actions workflow is
also provided for building the same installer on a Windows runner.
