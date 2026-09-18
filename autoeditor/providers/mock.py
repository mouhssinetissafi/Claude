"""Deterministic offline providers for tests, dry runs and CI.

No network calls, no paid APIs. Outputs are derived from the inputs (image
statistics, text hashes, word counts) so repeated runs are stable.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import wave
from pathlib import Path
from typing import Any

from autoeditor.providers.base import LLMResult, Transcript, TranscriptSegment, TranscriptWord

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

_SUBJECT_POOL = ["smartphone", "hands", "city street", "workspace", "product on table", "person walking", "landscape", "close-up detail"]
_ENV_POOL = ["studio", "indoor office", "urban exterior", "kitchen", "outdoors daylight", "dark stage"]
_SHOT_POOL = ["close-up", "medium shot", "wide shot", "macro", "over-the-shoulder"]
_CAM_POOL = ["static", "slow pan", "handheld", "push in", "orbit"]
_MOOD_POOL = ["calm", "energetic", "sleek", "moody", "bright"]


def _seed(*parts: Any) -> int:
    payload = json.dumps([str(p) for p in parts], sort_keys=True).encode()
    return int(hashlib.sha256(payload).hexdigest()[:12], 16)


def _pick(pool: list[str], seed: int, offset: int = 0) -> str:
    return pool[(seed + offset) % len(pool)]


def _image_stats(path: Path) -> tuple[float, list[str]]:
    """Return (brightness 0..1, dominant color names) using Pillow if available."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:  # pragma: no cover
        return 0.5, ["gray"]
    try:
        with Image.open(path) as im:
            small = im.convert("RGB").resize((16, 16))
            pixels = list(small.getdata())
    except OSError:
        return 0.5, ["gray"]
    if not pixels:
        return 0.5, ["gray"]
    r = sum(p[0] for p in pixels) / len(pixels)
    g = sum(p[1] for p in pixels) / len(pixels)
    b = sum(p[2] for p in pixels) / len(pixels)
    brightness = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    colors: list[str] = []
    if max(r, g, b) - min(r, g, b) < 20:
        colors.append("gray" if 60 < brightness * 255 < 200 else ("white" if brightness > 0.5 else "black"))
    else:
        if r >= g and r >= b:
            colors.append("red" if g < 120 else "orange")
        elif g >= r and g >= b:
            colors.append("green")
        else:
            colors.append("blue")
    return brightness, colors


class MockLLM:
    """Writes a plausible, rule-abiding script from the inventory or topic."""

    name = "mock"

    def complete_json(self, *, system: str, user: str, output_schema: dict[str, Any], max_tokens: int) -> LLMResult:
        payload = _extract_payload(user)
        if "metadata_request" in payload:
            return LLMResult(data=self._metadata(payload), model="mock")
        if "inventory" in payload:
            return LLMResult(data=self._shot_plan(payload), model="mock")
        return LLMResult(data=self._normal_script(payload), model="mock")

    # -- footage-only ----------------------------------------------------
    def _shot_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        inventory = payload["inventory"]
        topic = (payload.get("topic") or "").strip()
        scenes = [s for s in inventory.get("usable_scenes", [])]
        ranked = sorted(scenes, key=lambda s: (-float(s.get("visual_interest_score", 0)), s["scene_id"]))
        strongest = [s["scene_id"] for s in inventory.get("strongest_scenes", [])] or [s["scene_id"] for s in ranked[:3]]
        target = float(payload.get("target_seconds", 45))
        usable_total = float(inventory.get("total_usable_seconds", target))
        target = max(20.0, min(target, usable_total * 0.95 if usable_total > 0 else target))
        wps = float(payload.get("words_per_second", 2.6))

        subject = topic or _dominant_subject(inventory) or "this footage"
        templates = [
            f"{subject} looks simple. The details tell a different story.",
            "Start with the shape. Every line is doing a job.",
            "Look at how the light moves across it. Nothing here is accidental.",
            "Then the close-ups. This is where the design earns its keep.",
            "Each choice trades one thing for another.",
            "Put it all together and the pattern is clear.",
            "That is the whole idea, and it is right there on screen.",
        ]
        lines: list[dict[str, Any]] = []
        elapsed = 0.0
        order = list(strongest) + [s["scene_id"] for s in ranked if s["scene_id"] not in strongest]
        cursor = 0
        for idx, text in enumerate(templates, start=1):
            words = len(_WORD_RE.findall(text))
            dur = words / wps
            if elapsed + dur > target and idx > 3:
                break
            n_scenes = 1 if dur < 2.2 else (2 if dur < 4.5 else 3)
            picked: list[int] = []
            for _ in range(n_scenes):
                if not order:
                    break
                picked.append(order[cursor % len(order)])
                cursor += 1
            emphasis = [w for w in _WORD_RE.findall(text) if len(w) > 6][:1]
            lines.append(
                {
                    "id": idx,
                    "narration": text,
                    "scene_ids": picked,
                    "overlay_text": emphasis[0].upper() if emphasis and idx % 2 == 1 else None,
                    "emphasis_words": emphasis,
                }
            )
            elapsed += dur
        facts = [f"Verify any specific claims about {topic}: dates, prices, specs are not visible in footage."] if topic else []
        return {
            "title": (f"{subject}: what the footage shows"[:90]).strip(),
            "description": f"A short look at {subject}, built only from the available footage.",
            "facts_to_verify": facts,
            "lines": lines,
        }

    # -- normal mode -------------------------------------------------------
    def _normal_script(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = (payload.get("topic") or "an interesting subject").strip()
        facts = payload.get("facts") or []
        base = [
            f"{topic} starts with one decision.",
            "That decision shapes everything that follows.",
            "Here is the part most people skip.",
            "The trade-off is real, and it shows.",
            "Which brings us back to the start.",
        ]
        lines = []
        for i, text in enumerate(base, start=1):
            lines.append(
                {
                    "id": i,
                    "narration": text,
                    "media_query": f"{topic} {['overview', 'detail', 'people', 'contrast', 'closing'][i - 1]}",
                    "overlay_text": None if i % 2 else text.split()[0].upper(),
                    "emphasis_words": [text.split()[-1].strip(".")],
                }
            )
        return {
            "title": f"{topic}: the short version"[:90],
            "description": f"A quick explainer about {topic}.",
            "facts_to_verify": [] if facts else [f"No sources were supplied for {topic}; verify all factual statements."],
            "lines": lines,
        }

    def _metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "Untitled short")
        topic = str(payload.get("topic") or title)
        words = [w.lower() for w in _WORD_RE.findall(topic) if len(w) > 3][:6]
        return {
            "title": title[:100],
            "description": str(payload.get("description") or "")[:4000],
            "hashtags": ["#shorts"] + [f"#{w}" for w in words[:3]],
            "tags": words,
        }


class MockVision:
    """Scores frames from simple image statistics; never calls a network."""

    name = "mock"

    def analyze_images(self, *, system: str, user: str, images: list[Path], output_schema: dict[str, Any], max_tokens: int) -> LLMResult:
        payload = _extract_payload(user)
        scene_id = int(payload.get("scene_id", 1))
        seed = _seed(scene_id, *[p.name for p in images])
        brightness, colors = _image_stats(images[0]) if images else (0.5, ["gray"])
        # Very dark or blown-out frames score lower.
        exposure_penalty = abs(brightness - 0.5) * 80
        quality = max(5.0, min(98.0, 88.0 - exposure_penalty + (seed % 7)))
        interest = max(5.0, min(97.0, 55.0 + (seed % 41) - exposure_penalty / 2))
        subject = _pick(_SUBJECT_POOL, seed)
        data = {
            "scene_id": scene_id,
            "description": f"{_pick(_SHOT_POOL, seed, 1).capitalize()} of a {subject} in a {_pick(_ENV_POOL, seed, 2)}.",
            "subjects": [subject],
            "objects": [subject.split()[-1]],
            "environment": _pick(_ENV_POOL, seed, 2),
            "shot_type": _pick(_SHOT_POOL, seed, 1),
            "camera_motion": _pick(_CAM_POOL, seed, 3),
            "subject_motion": "slow" if seed % 2 else "static",
            "mood": _pick(_MOOD_POOL, seed, 4),
            "colors": colors,
            "quality_score": round(quality, 1),
            "visual_interest_score": round(interest, 1),
            "watermark_detected": False,
            "text_detected": False,
            "text_content": "",
            "product_or_brand": [],
            "possible_topics": [subject, _pick(_MOOD_POOL, seed, 4) + " product video"],
            "safe_to_use": True,
        }
        return LLMResult(data=data, model="mock")


class MockTTS:
    """Writes a quiet tone WAV whose length matches the word count."""

    name = "mock"
    output_extension = ".wav"

    def __init__(self, words_per_second: float = 2.6, sample_rate: int = 22050) -> None:
        self.words_per_second = words_per_second
        self.sample_rate = sample_rate

    def duration_for(self, text: str) -> float:
        words = len(_WORD_RE.findall(text))
        return round(max(0.6, words / self.words_per_second), 3)

    def synthesize(self, text: str, dst: Path) -> Path:
        dst = dst.with_suffix(self.output_extension)
        dst.parent.mkdir(parents=True, exist_ok=True)
        duration = self.duration_for(text)
        n = int(duration * self.sample_rate)
        seed = _seed(text)
        freq = 180 + (seed % 120)
        frames = bytearray()
        for i in range(n):
            t = i / self.sample_rate
            # Word-like amplitude envelope so silencedetect sees "speech".
            envelope = 0.55 + 0.45 * math.sin(2 * math.pi * 3.0 * t)
            sample = int(9000 * envelope * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", sample)
        with wave.open(str(dst), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(frames))
        return dst


class MockTranscriber:
    """Places each line's words evenly across that line's timing."""

    name = "mock"

    def transcribe(self, audio: Path, *, language: str, line_hints: list[dict[str, Any]] | None = None) -> Transcript:
        if not line_hints:
            raise ValueError("MockTranscriber requires line_hints (script text + timing)")
        segments: list[TranscriptSegment] = []
        total_end = 0.0
        for hint in line_hints:
            words = _WORD_RE.findall(str(hint.get("text", "")))
            start = float(hint["start"])
            end = float(hint["end"])
            total_end = max(total_end, end)
            if not words:
                continue
            slot = (end - start) / len(words)
            tw = [TranscriptWord(text=w, start=round(start + i * slot, 3), end=round(start + (i + 1) * slot - 0.02, 3)) for i, w in enumerate(words)]
            segments.append(TranscriptSegment(text=" ".join(words), start=start, end=end, words=tw))
        return Transcript(language=language, duration=round(total_end, 3), segments=segments)


# --------------------------------------------------------------------------- #
def _extract_payload(user: str) -> dict[str, Any]:
    """Prompts embed their data as a JSON block; mock providers read it back."""
    start = user.find("{")
    end = user.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        parsed = json.loads(user[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dominant_subject(inventory: dict[str, Any]) -> str | None:
    subjects = inventory.get("recurring_subjects") or []
    if subjects:
        first = subjects[0]
        return str(first.get("subject") if isinstance(first, dict) else first)
    return None
