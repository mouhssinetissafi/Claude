"""Phase 13: credits.txt with carried-forward license information.

License data is read from sidecar files next to the footage or from a
job-level manifest. Nothing is ever invented: a clip without license data is
marked ``LICENSE_UNKNOWN`` and a warning is raised.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from autoeditor.logging_utils import get_logger
from autoeditor.pipeline.job import JobPaths

log = get_logger(__name__)

LICENSE_UNKNOWN = "LICENSE_UNKNOWN"
_JOB_MANIFESTS = ("credits.json", "sources.json", "licenses.json")


@dataclass
class SourceCredit:
    file: str
    source: str = ""
    author: str = ""
    license: str = LICENSE_UNKNOWN
    url: str = ""
    notes: str = ""
    kind: str = "video"  # video | image | music | sfx
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def unknown(self) -> bool:
        return not self.license or self.license.upper() == LICENSE_UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("Could not parse license file %s", path)
        return None
    return data if isinstance(data, dict) else None


def _from_mapping(file_name: str, data: dict[str, Any], kind: str) -> SourceCredit:
    known = {"source", "author", "license", "url", "notes", "file", "kind"}
    return SourceCredit(
        file=file_name,
        source=str(data.get("source") or data.get("provider") or ""),
        author=str(data.get("author") or data.get("creator") or data.get("photographer") or ""),
        license=str(data.get("license") or data.get("licence") or LICENSE_UNKNOWN),
        url=str(data.get("url") or data.get("link") or ""),
        notes=str(data.get("notes") or ""),
        kind=kind,
        extra={k: v for k, v in data.items() if k not in known},
    )


def find_license(clip: Path, *, kind: str = "video") -> SourceCredit:
    """Look for sidecar license info next to ``clip``.

    Accepted, in order: ``<name>.license.json``, ``<stem>.license.json``,
    ``<stem>.json``, ``<name>.license.txt`` / ``<stem>.license.txt`` (free text
    stored as notes with license marked unknown unless a `License:` line exists),
    then a job-level ``credits.json`` / ``sources.json`` / ``licenses.json``
    keyed by file name (or with a ``default`` entry).
    """
    folder = clip.parent
    candidates = [
        folder / f"{clip.name}.license.json",
        folder / f"{clip.stem}.license.json",
        folder / f"{clip.stem}.json",
    ]
    for cand in candidates:
        if cand.exists():
            data = _load_json(cand)
            if data is not None:
                return _from_mapping(clip.name, data, kind)
    for cand in (folder / f"{clip.name}.license.txt", folder / f"{clip.stem}.license.txt"):
        if cand.exists():
            text = cand.read_text(encoding="utf-8", errors="replace").strip()
            license_line = next((ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.lower().startswith("license")), "")
            return SourceCredit(file=clip.name, license=license_line or LICENSE_UNKNOWN, notes=text[:500], kind=kind)
    for manifest_name in _JOB_MANIFESTS:
        manifest = folder / manifest_name
        if manifest.exists():
            data = _load_json(manifest)
            if not data:
                continue
            entry = data.get(clip.name) or data.get(clip.stem) or data.get("default")
            if isinstance(entry, dict):
                return _from_mapping(clip.name, entry, kind)
    return SourceCredit(file=clip.name, kind=kind)


def render_credits(job: str, title: str, credits: list[SourceCredit]) -> tuple[str, list[str]]:
    warnings: list[str] = []
    lines = [f"CREDITS for '{title}' (job: {job})", "=" * 60, ""]
    for kind, heading in (("video", "FOOTAGE"), ("image", "IMAGES"), ("music", "MUSIC"), ("sfx", "SOUND EFFECTS")):
        group = [c for c in credits if c.kind == kind]
        if not group:
            continue
        lines.append(heading)
        lines.append("-" * len(heading))
        for c in group:
            lines.append(f"* {c.file}")
            lines.append(f"    license: {c.license or LICENSE_UNKNOWN}")
            if c.source:
                lines.append(f"    source:  {c.source}")
            if c.author:
                lines.append(f"    author:  {c.author}")
            if c.url:
                lines.append(f"    url:     {c.url}")
            if c.notes:
                lines.append(f"    notes:   {c.notes.splitlines()[0][:200]}")
            if c.unknown:
                warnings.append(f"{c.file}: license unknown - verify rights before publishing")
        lines.append("")
    if warnings:
        lines.append("WARNINGS")
        lines.append("--------")
        lines.extend(f"! {w}" for w in warnings)
        lines.append("")
    lines.append("Generated by Auto-Editor PRO. Licenses are carried forward from source metadata; none were inferred.")
    return "\n".join(lines) + "\n", warnings


def write_credits(paths: JobPaths, title: str, credits: list[SourceCredit]) -> list[str]:
    text, warnings = render_credits(paths.name, title, credits)
    paths.output.mkdir(parents=True, exist_ok=True)
    paths.credits_txt.write_text(text, encoding="utf-8")
    for w in warnings:
        log.warning("credits: %s", w)
    return warnings
