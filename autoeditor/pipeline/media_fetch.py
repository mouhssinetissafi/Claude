"""Normal mode media resolution: one asset per narration line.

Resolution order for each line's ``media_query``:
  1. explicit ``media_path`` on the line (local file)
  2. local library ``assets/media/`` matched by keywords in the file name
  3. Pexels video search when ``PEXELS_API_KEY`` is set (portrait, license carried)
  4. a generated gradient placeholder image (flagged in credits) so the render is never black

Videos are normalized to the Shorts frame; images are used as-is with Ken Burns.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger
from autoeditor.media import ffmpeg as ff
from autoeditor.media.ffprobe import ProbeError, probe
from autoeditor.pipeline.credits import SourceCredit, find_license
from autoeditor.pipeline.job import JobPaths, read_json, write_json

log = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class MediaItem:
    line_id: int
    src: str  # relative to work dir
    type: str  # video | image
    duration: float | None
    origin: str  # local | pexels | placeholder | explicit
    credit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) > 2}


def _library_match(query: str, library: list[Path], used: set[Path]) -> Path | None:
    q = _tokens(query)
    best: tuple[int, Path] | None = None
    for f in library:
        if f in used:
            continue
        score = len(q & _tokens(f.stem.replace("_", " ").replace("-", " ")))
        if score and (best is None or score > best[0]):
            best = (score, f)
    return best[1] if best else None


def _pexels_search(query: str, api_key: str) -> dict[str, Any] | None:
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode({"query": query, "orientation": "portrait", "per_page": 5})
    req = urllib.request.Request(url, headers={"Authorization": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    videos = data.get("videos") or []
    if not videos:
        return None
    video = videos[0]
    files = sorted(video.get("video_files", []), key=lambda f: -(f.get("height") or 0))
    file_url = next((f["link"] for f in files if (f.get("height") or 0) <= 1920), files[0]["link"] if files else None)
    if not file_url:
        return None
    return {"url": file_url, "page": video.get("url", ""), "author": (video.get("user") or {}).get("name", ""), "id": video.get("id")}


def _download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "auto-editor-pro/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, dst.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)


def _placeholder_image(dst: Path, seed: int, width: int, height: int) -> Path:
    from PIL import Image  # type: ignore

    top = ((seed * 37) % 200 + 20, (seed * 91) % 200 + 20, (seed * 53) % 200 + 20)
    bottom = ((seed * 17) % 120 + 10, (seed * 29) % 120 + 10, (seed * 71) % 120 + 10)
    img = Image.new("RGB", (width, height))
    px = img.load()
    if px is None:  # pragma: no cover - Pillow always returns an accessor for new images
        raise RuntimeError("could not access placeholder image pixels")
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(width):
            px[x, y] = color
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, quality=90)
    return dst


def resolve_media(script: dict[str, Any], paths: JobPaths, cfg: Config) -> list[MediaItem]:
    library_dir = cfg.assets_dir / "media"
    library = sorted(p for p in library_dir.rglob("*") if p.is_file() and p.suffix.lower() in _VIDEO_EXT | _IMAGE_EXT) if library_dir.exists() else []
    used: set[Path] = set()
    pexels_key = os.environ.get("PEXELS_API_KEY", "").strip()
    video = cfg.section("video")
    items: list[MediaItem] = []
    for line in script["lines"]:
        line_id = int(line["id"])
        query = str(line.get("media_query") or script.get("topic") or "")
        source: Path | None = None
        origin = "placeholder"
        credit = SourceCredit(file="", license="GENERATED_PLACEHOLDER", source="auto-editor-pro", notes="gradient placeholder; replace with licensed footage")
        explicit = line.get("media_path")
        if explicit and Path(explicit).exists():
            source, origin = Path(explicit), "explicit"
            credit = find_license(source)
        if source is None and library:
            match = _library_match(query, library, used)
            if match is not None:
                source, origin = match, "local"
                credit = find_license(match)
        if source is None and pexels_key:
            try:
                hit = _pexels_search(query, pexels_key)
            except (OSError, ValueError) as exc:
                log.warning("Pexels search failed for '%s': %s", query, exc)
                hit = None
            if hit:
                dst = paths.media_dir / f"pexels_{hit['id']}.mp4"
                if not dst.exists():
                    _download(hit["url"], dst)
                source, origin = dst, "pexels"
                credit = SourceCredit(file=dst.name, source="Pexels", author=hit["author"], license="Pexels License", url=hit["page"], kind="video")

        if source is None:
            dst = paths.media_dir / f"placeholder_{line_id:03d}.jpg"
            _placeholder_image(dst, line_id, int(video["width"]), int(video["height"]))
            credit.file = dst.name
            credit.kind = "image"
            items.append(MediaItem(line_id=line_id, src=paths.rel(dst), type="image", duration=None, origin=origin, credit=credit.to_dict()))
            log.warning("line %03d: no media found for '%s'; using placeholder", line_id, query)
            continue

        used.add(source)
        if source.suffix.lower() in _IMAGE_EXT:
            dst = paths.media_dir / f"line_{line_id:03d}{source.suffix.lower()}"
            if not dst.exists():
                dst.write_bytes(source.read_bytes())
            credit.file = credit.file or source.name
            credit.kind = "image"
            items.append(MediaItem(line_id=line_id, src=paths.rel(dst), type="image", duration=None, origin=origin, credit=credit.to_dict()))
            continue
        dst = paths.media_dir / f"line_{line_id:03d}.mp4"
        if not dst.exists():
            ff.normalize_video(
                source,
                dst,
                width=int(video["width"]),
                height=int(video["height"]),
                fps=int(video["fps"]),
                crf=int(video["crf"]),
                preset=str(video.get("preset", "medium")),
                pix_fmt=str(video["pix_fmt"]),
                codec=str(video["codec"]),
            )
        try:
            duration = probe(dst).duration
        except ProbeError as exc:
            raise RuntimeError(f"normalized media for line {line_id} unreadable: {exc}") from exc
        credit.file = credit.file or source.name
        items.append(MediaItem(line_id=line_id, src=paths.rel(dst), type="video", duration=duration, origin=origin, credit=credit.to_dict()))
    write_json(paths.media_manifest_json, {"version": 1, "job": paths.name, "items": [i.to_dict() for i in items]})
    return items


def load_media(paths: JobPaths) -> list[MediaItem]:
    return [MediaItem(**item) for item in read_json(paths.media_manifest_json)["items"]]
