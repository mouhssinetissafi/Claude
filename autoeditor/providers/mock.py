"""Deterministic offline providers for tests, dry runs and CI.

No network calls, no paid APIs. Outputs are derived from the inputs (image
statistics, text hashes, word counts) so repeated runs are stable, while the
variation profile in the prompt changes which lines are used so two mock jobs
do not produce identical scripts.
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

# Enough varied material for a 60-second script plus expansions (about 10 words a line).
_FOOTAGE_OPENERS = [
    "{subject} looks simple at first. The details tell a different story.",
    "Most people see {subject} and move on. The footage rewards a second look.",
    "Start with one small detail on {subject}. It explains almost everything else.",
    "Here is {subject} with nothing hidden. Watch what the camera lingers on.",
    "The first thing you notice about {subject} is not the most important thing.",
    "Take {subject} apart with your eyes. Every part is doing a job.",
]
_FOOTAGE_MIDDLE = [
    "Start with the shape. Every line is doing a job and none of them is decoration.",
    "Look at how the light moves across it. Nothing here is accidental.",
    "Then the close-ups. This is where the design earns its keep.",
    "Each choice trades one thing for another, and the trade shows on screen.",
    "Notice the edges. They decide how the whole thing feels in the hand.",
    "The wide shot tells you where it lives. The tight shot tells you why.",
    "Color does quiet work here. It separates the parts you touch from the parts you see.",
    "Motion gives it away. Watch how little the frame has to move to show the point.",
    "Compare the front to the back. One is built for looking, one is built for holding.",
    "There is a reason the camera keeps returning to the same corner.",
    "Small parts carry the most weight, and the footage keeps proving it.",
    "The texture changes from surface to surface, and that is not a coincidence.",
    "Put two angles side by side and the pattern becomes obvious.",
    "The quiet moments in the footage matter as much as the busy ones.",
    "Even the background is chosen. It never competes with the subject.",
    "Step back and the whole layout starts to make sense.",
]
_FOOTAGE_ENDINGS = [
    "Put it all together and the pattern is clear. That is the whole idea, and it is right there on screen.",
    "So the next time it looks simple, remember what the details were doing.",
    "Which brings it back to that first small detail. It was the point all along.",
    "That is what the footage was showing the whole time.",
    "Look once more at the opening shot. It reads differently now.",
]
_NORMAL_OPENERS = [
    "{topic} starts with one decision.",
    "Most explanations of {topic} skip the part that matters.",
    "There is a simple way to think about {topic}, and it is not the usual one.",
    "{topic} looks like one problem. It is really three.",
    "The interesting part of {topic} is not the headline.",
]
_NORMAL_MIDDLE = [
    "That decision shapes everything that follows, and the effects stack up fast.",
    "Here is the part most people skip, because it looks boring from the outside.",
    "The trade-off is real, and it shows the moment you look closely.",
    "One constraint sets the tone. Every other choice has to live with it.",
    "The cost is not where people expect it. It is spread across small places.",
    "Then a second factor arrives and changes the math again.",
    "It helps to separate what was chosen from what was forced.",
    "Look at the sequence, not the snapshot. The order explains the outcome.",
    "The details that seem cosmetic usually carry the real weight.",
    "Nothing here happens in isolation, which is why the simple version misleads.",
    "Put the pieces in order and the shape of the answer appears.",
    "The last piece is timing, and timing is what most summaries leave out.",
    "There is always a version of this that sounds easier. It is not the real one.",
    "Once you see the structure, the rest of the story is hard to unsee.",
]
_NORMAL_ENDINGS = [
    "Which brings us back to the start, and the one decision that set it all in motion.",
    "That is the short version. The long version is the same story with more receipts.",
    "So the next time it comes up, watch for that first decision.",
    "It is a small idea with a long reach, and it was there from the first line.",
]


def _seed(*parts: Any) -> int:
    payload = json.dumps([str(p) for p in parts], sort_keys=True).encode()
    return int(hashlib.sha256(payload).hexdigest()[:12], 16)


def _pick(pool: list[str], seed: int, offset: int = 0) -> str:
    return pool[(seed + offset) % len(pool)]


def _rotate(items: list[str], by: int) -> list[str]:
    if not items:
        return []
    k = by % len(items)
    return items[k:] + items[:k]


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


def _seconds(text: str, wps: float) -> float:
    return len(_WORD_RE.findall(text)) / wps


def _variation_offsets(payload: dict[str, Any]) -> tuple[int, int]:
    """(rotation, attempt) from the prompt's variation profile; both 0 when absent."""
    variation = payload.get("variation") or {}
    rotation = int(variation.get("opener_rotation", 0)) + _seed(variation.get("hook_style", ""), variation.get("ending", "")) % 7
    return rotation, int(variation.get("attempt", 0))


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

    # -- shared -------------------------------------------------------------
    @staticmethod
    def _compose(
        openers: list[str], middle: list[str], endings: list[str], *, target: float, wps: float, rotation: int, attempt: int, fmt: dict[str, str]
    ) -> list[str]:
        """Pick opener + enough middle lines + ending to reach ``target`` seconds."""
        opener = _rotate(openers, rotation + attempt)[0].format(**fmt)
        ending = _rotate(endings, rotation // 2 + attempt)[0].format(**fmt)
        body = _rotate(middle, rotation * 3 + attempt * 5)
        chosen = [opener]
        elapsed = _seconds(opener, wps) + _seconds(ending, wps)
        for text in body:
            if elapsed >= target:
                break
            chosen.append(text.format(**fmt))
            elapsed += _seconds(text, wps)
        chosen.append(ending)
        return chosen

    @staticmethod
    def _expand(existing: list[dict[str, Any]], pool: list[str], *, add_seconds: float, wps: float, rotation: int, fmt: dict[str, str]) -> list[str]:
        """Insert unused pool lines before the ending until ``add_seconds`` is covered."""
        texts = [str(ln.get("narration", "")) for ln in existing]
        used = set(texts)
        extra: list[str] = []
        added = 0.0
        for text in _rotate(pool, rotation * 7 + 3):
            formatted = text.format(**fmt)
            if formatted in used:
                continue
            extra.append(formatted)
            added += _seconds(formatted, wps)
            if added >= add_seconds:
                break
        if len(texts) >= 2:
            return texts[:-1] + extra + texts[-1:]
        return texts + extra

    # -- footage-only ----------------------------------------------------
    def _shot_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        inventory = payload["inventory"]
        topic = (payload.get("topic") or "").strip()
        scenes = list(inventory.get("usable_scenes", []))
        ranked = sorted(scenes, key=lambda s: (-float(s.get("visual_interest_score", 0)), s["scene_id"]))
        strongest = [s["scene_id"] for s in inventory.get("strongest_scenes", [])] or [s["scene_id"] for s in ranked[:3]]
        wps = float(payload.get("words_per_second", 2.6))
        target = float(payload.get("target_seconds", 45))
        usable_total = float(inventory.get("total_usable_seconds", target))
        rotation, attempt = _variation_offsets(payload)
        subject = topic or _dominant_subject(inventory) or "this footage"
        fmt = {"subject": subject}

        expand = payload.get("expand")
        if expand:
            texts = self._expand(
                expand["existing_script"]["lines"], _FOOTAGE_MIDDLE, add_seconds=float(expand["add_seconds"]), wps=wps, rotation=rotation, fmt=fmt
            )
        else:
            texts = self._compose(_FOOTAGE_OPENERS, _FOOTAGE_MIDDLE, _FOOTAGE_ENDINGS, target=target, wps=wps, rotation=rotation, attempt=attempt, fmt=fmt)

        # Scene assignment: rotate the opener among the strongest scenes, then cycle the rest.
        opener = payload.get("preferred_opener_scene_id")
        if opener not in strongest:
            opener = strongest[rotation % len(strongest)] if strongest else None
        order = ([opener] if opener is not None else []) + [s["scene_id"] for s in ranked if s["scene_id"] != opener]
        lines: list[dict[str, Any]] = []
        cursor = 0
        for idx, text in enumerate(texts, start=1):
            dur = _seconds(text, wps)
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
                    "overlay_text": emphasis[0].upper() if emphasis and idx % 3 == 1 else None,
                    "emphasis_words": emphasis,
                }
            )
        facts = [f"Verify any specific claims about {topic}: dates, prices, specs are not visible in footage."] if topic else []
        title_variants = [
            f"{subject}: what the footage shows",
            f"A closer look at {subject}",
            f"{subject}, detail by detail",
            f"What {subject} is really doing",
        ]
        return {
            "title": _rotate(title_variants, rotation + attempt)[0][:90].strip(),
            "description": f"A short look at {subject}, built only from the available footage ({usable_total:.0f}s of source material).",
            "facts_to_verify": facts,
            "lines": lines,
        }

    # -- normal mode -------------------------------------------------------
    def _normal_script(self, payload: dict[str, Any]) -> dict[str, Any]:
        topic = (payload.get("topic") or "an interesting subject").strip()
        facts = payload.get("facts") or []
        wps = float(payload.get("words_per_second", 2.6))
        rng = payload.get("target_range_seconds") or [50, 60]
        target = (float(rng[0]) + float(rng[1])) / 2
        rotation, attempt = _variation_offsets(payload)
        fmt = {"topic": topic}
        expand = payload.get("expand")
        if expand:
            texts = self._expand(
                expand["existing_script"]["lines"], _NORMAL_MIDDLE, add_seconds=float(expand["add_seconds"]), wps=wps, rotation=rotation, fmt=fmt
            )
        else:
            texts = self._compose(_NORMAL_OPENERS, _NORMAL_MIDDLE, _NORMAL_ENDINGS, target=target, wps=wps, rotation=rotation, attempt=attempt, fmt=fmt)
        queries = ["overview", "detail", "people", "contrast", "process", "closing", "texture", "scale", "motion", "context"]
        lines = []
        for i, text in enumerate(texts, start=1):
            lines.append(
                {
                    "id": i,
                    "narration": text,
                    "media_query": f"{topic} {queries[(i - 1) % len(queries)]}",
                    "overlay_text": None if i % 3 else text.split()[0].strip(",.").upper(),
                    "emphasis_words": [text.split()[-1].strip(".,")],
                }
            )
        return {
            "title": _rotate([f"{topic}: the short version", f"{topic}, explained in one minute", f"The part of {topic} people skip"], rotation + attempt)[0][
                :90
            ],
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
