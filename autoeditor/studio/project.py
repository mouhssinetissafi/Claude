"""Versioned on-disk project format used by Auto-Editor PRO Studio.

A project is deliberately self-contained and human-inspectable::

    My Short/
      project.json
      media/                  # user-imported originals (never modified)
      output/                 # final renders / exports
      .engine/                # generated working state (safe to rebuild)

``project.json`` is the single source of truth for user choices.  API keys are
*never* stored in it; the desktop shell will keep them in the operating-system
credential store and expose them to the engine only for a generation process.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoeditor.pipeline.job import sanitize_job_name

PROJECT_VERSION = 1
PROJECT_FILE = "project.json"
MEDIA_DIR = "media"
OUTPUT_DIR = "output"
ENGINE_DIR = ".engine"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _collision_safe_destination(directory: Path, name: str, source_hash: str) -> Path:
    """Return a deterministic destination without overwriting another original."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    try:
        if _sha256(candidate) == source_hash:
            return candidate
    except OSError:
        pass
    stem = Path(name).stem
    suffix = Path(name).suffix
    short = source_hash[:8]
    candidate = directory / f"{stem}_{short}{suffix}"
    counter = 2
    while candidate.exists():
        try:
            if _sha256(candidate) == source_hash:
                return candidate
        except OSError:
            pass
        candidate = directory / f"{stem}_{short}_{counter}{suffix}"
        counter += 1
    return candidate


@dataclass
class MediaItem:
    id: str
    file_name: str
    relative_path: str
    kind: str
    size_bytes: int
    sha256: str
    imported_at: str


@dataclass
class StudioChoices:
    """User-facing choices.  Paid AI actions are opt-in per project."""

    topic: str = ""
    script_mode: str = "ai"  # ai | manual
    script_text: str = ""
    scene_mode: str = "ai"  # ai | manual (manual editor is a later UI phase)
    voice_mode: str = "ai"  # ai | imported | placeholder
    voice_file: str | None = None
    captions_mode: str = "auto"  # auto | manual
    theme: str = "default"
    watermark_enabled: bool = True
    watermark_position: str = "top-right"
    watermark_opacity: float = 0.85
    watermark_width_fraction: float = 0.14
    mock_mode: bool = False


@dataclass
class StudioProject:
    version: int
    project_id: str
    name: str
    root: str
    created_at: str
    updated_at: str
    media: list[MediaItem] = field(default_factory=list)
    choices: StudioChoices = field(default_factory=StudioChoices)
    last_job_id: str | None = None
    logo_relative_path: str | None = None

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    @property
    def project_file(self) -> Path:
        return self.root_path / PROJECT_FILE

    @property
    def media_dir(self) -> Path:
        return self.root_path / MEDIA_DIR

    @property
    def output_dir(self) -> Path:
        return self.root_path / OUTPUT_DIR

    @property
    def engine_dir(self) -> Path:
        return self.root_path / ENGINE_DIR

    def ensure_layout(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.engine_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "project_id": self.project_id,
            "name": self.name,
            "root": self.root,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "media": [asdict(item) for item in self.media],
            "choices": asdict(self.choices),
            "last_job_id": self.last_job_id,
            "logo_relative_path": self.logo_relative_path,
        }

    def save(self) -> None:
        self.updated_at = _now()
        self.ensure_layout()
        _write_json_atomic(self.project_file, self.to_dict())

    def update_choices(self, values: dict[str, Any]) -> StudioChoices:
        allowed = set(StudioChoices.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"unknown project choice(s): {', '.join(unknown)}")
        for key, value in values.items():
            setattr(self.choices, key, value)
        self._validate_choices(require_ready=False)
        self.save()
        return self.choices

    def _validate_choices(self, *, require_ready: bool = False) -> None:
        if self.choices.script_mode not in {"ai", "manual"}:
            raise ValueError("script_mode must be 'ai' or 'manual'")
        if self.choices.scene_mode not in {"ai", "manual"}:
            raise ValueError("scene_mode must be 'ai' or 'manual'")
        if self.choices.voice_mode not in {"ai", "imported", "placeholder"}:
            raise ValueError("voice_mode must be 'ai', 'imported' or 'placeholder'")
        if self.choices.captions_mode not in {"auto", "manual"}:
            raise ValueError("captions_mode must be 'auto' or 'manual'")
        if self.choices.watermark_position not in {"top-right", "top-left", "bottom-right", "bottom-left"}:
            raise ValueError("invalid watermark_position")
        if not 0.05 <= float(self.choices.watermark_opacity) <= 1.0:
            raise ValueError("watermark_opacity must be between 0.05 and 1.0")
        if not 0.04 <= float(self.choices.watermark_width_fraction) <= 0.30:
            raise ValueError("watermark_width_fraction must be between 0.04 and 0.30")
        if require_ready and self.choices.script_mode == "manual" and not self.choices.script_text.strip():
            raise ValueError("manual script mode requires script_text before generation")
        if require_ready and self.choices.voice_mode == "imported" and not self.choices.voice_file:
            raise ValueError("imported voice mode requires voice_file before generation")
        if require_ready and self.choices.voice_mode == "imported" and self.choices.script_mode != "manual":
            raise ValueError("imported voice currently requires 'My script' so narration timing can be aligned")
        if require_ready and self.choices.scene_mode == "manual":
            raise ValueError("manual scene mode will be enabled with the editable timeline; use AI scene matching for now")

    def import_media(self, sources: list[Path]) -> list[MediaItem]:
        self.ensure_layout()
        imported: list[MediaItem] = []
        by_hash = {item.sha256: item for item in self.media}
        for source in sources:
            source = source.expanduser().resolve()
            if not source.exists() or not source.is_file():
                raise FileNotFoundError(f"media file not found: {source}")
            digest = _sha256(source)
            existing = by_hash.get(digest)
            if existing:
                imported.append(existing)
                continue
            destination = _collision_safe_destination(self.media_dir, source.name, digest)
            if not destination.exists():
                shutil.copy2(source, destination)
            suffix = source.suffix.lower()
            kind = "image" if suffix in {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"} else "video"
            item = MediaItem(
                id=digest[:16],
                file_name=destination.name,
                relative_path=(Path(MEDIA_DIR) / destination.name).as_posix(),
                kind=kind,
                size_bytes=destination.stat().st_size,
                sha256=digest,
                imported_at=_now(),
            )
            self.media.append(item)
            by_hash[digest] = item
            imported.append(item)
        self.save()
        return imported

    def import_voice(self, source: Path) -> str:
        """Copy user narration into the project; never modify the original."""
        source = source.expanduser().resolve()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"voice file not found: {source}")
        allowed = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
        if source.suffix.lower() not in allowed:
            raise ValueError("unsupported voice format; use MP3, WAV, M4A, AAC, FLAC or OGG")
        audio_dir = self.root_path / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        destination = audio_dir / ("voice" + source.suffix.lower())
        shutil.copy2(source, destination)
        self.choices.voice_file = (Path("audio") / destination.name).as_posix()
        self.choices.voice_mode = "imported"
        self.save()
        return self.choices.voice_file

    def import_logo(self, source: Path) -> str:
        """Copy a PNG logo into the project so branding remains portable."""
        source = source.expanduser().resolve()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"logo file not found: {source}")
        if source.suffix.lower() != ".png":
            raise ValueError("logo must be a PNG file")
        try:
            from PIL import Image  # type: ignore
            with Image.open(source) as im:
                if (im.format or "").upper() != "PNG":
                    raise ValueError("logo must be a PNG image")
        except OSError as exc:
            raise ValueError(f"logo is not a readable PNG: {exc}") from exc
        branding = self.root_path / "branding"
        branding.mkdir(parents=True, exist_ok=True)
        destination = branding / "logo.png"
        shutil.copy2(source, destination)
        self.logo_relative_path = (Path("branding") / "logo.png").as_posix()
        self.save()
        return self.logo_relative_path

    def remove_media(self, media_id: str, *, delete_file: bool = True) -> None:
        match = next((m for m in self.media if m.id == media_id), None)
        if match is None:
            raise KeyError(f"media item not found: {media_id}")
        self.media = [m for m in self.media if m.id != media_id]
        if delete_file:
            path = self.root_path / match.relative_path
            if path.exists():
                path.unlink()
        self.save()


def create_project(path: Path, name: str | None = None) -> StudioProject:
    root = path.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_file = root / PROJECT_FILE
    if project_file.exists():
        raise FileExistsError(f"project already exists: {project_file}")
    display = (name or root.name or "Untitled Project").strip() or "Untitled Project"
    project_id = sanitize_job_name(display).lower()
    now = _now()
    project = StudioProject(
        version=PROJECT_VERSION,
        project_id=project_id,
        name=display,
        root=str(root),
        created_at=now,
        updated_at=now,
    )
    project.save()
    return project


def open_project(path: Path) -> StudioProject:
    root = path.expanduser().resolve()
    project_file = root if root.name == PROJECT_FILE else root / PROJECT_FILE
    if not project_file.exists():
        raise FileNotFoundError(f"Studio project not found: {project_file}")
    raw = json.loads(project_file.read_text(encoding="utf-8"))
    version = int(raw.get("version", 0))
    if version != PROJECT_VERSION:
        raise ValueError(f"unsupported project version {version}; expected {PROJECT_VERSION}")
    project_root = project_file.parent
    media = [MediaItem(**item) for item in raw.get("media", [])]
    choices = StudioChoices(**raw.get("choices", {}))
    project = StudioProject(
        version=version,
        project_id=str(raw["project_id"]),
        name=str(raw["name"]),
        root=str(project_root),
        created_at=str(raw["created_at"]),
        updated_at=str(raw["updated_at"]),
        media=media,
        choices=choices,
        last_job_id=raw.get("last_job_id"),
        logo_relative_path=raw.get("logo_relative_path"),
    )
    project.ensure_layout()
    project._validate_choices(require_ready=False)
    return project
