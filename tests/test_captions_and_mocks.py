import wave
from pathlib import Path

from autoeditor.pipeline.captions import transcript_to_captions
from autoeditor.pipeline.voice import LineTiming
from autoeditor.providers.mock import MockTranscriber, MockTTS, MockVision
from autoeditor.schemas import SCENE_ANALYSIS_SCHEMA, validate


def test_mock_tts_duration_matches_words(tmp_path: Path) -> None:
    tts = MockTTS(words_per_second=2.0)
    out = tts.synthesize("one two three four", tmp_path / "line_001.mp3")
    assert out.suffix == ".wav"
    with wave.open(str(out)) as wf:
        seconds = wf.getnframes() / wf.getframerate()
    assert abs(seconds - 2.0) < 0.05


def test_mock_transcriber_and_captions_mapping() -> None:
    script = {
        "lines": [
            {"id": 1, "narration": "Apple made thinness the point.", "emphasis_words": ["thinness"]},
            {"id": 2, "narration": "Then it got lighter.", "emphasis_words": []},
        ]
    }
    timings = [LineTiming(1, 0.0, 2.0, 2.0, "a"), LineTiming(2, 2.1, 3.6, 1.5, "b")]
    hints = [{"line_id": t.line_id, "start": t.start, "end": t.end, "text": script["lines"][i]["narration"]} for i, t in enumerate(timings)]
    transcript = MockTranscriber().transcribe(Path("voice.mp3"), language="en", line_hints=hints)
    captions = transcript_to_captions(transcript, script, timings, voice_duration=3.6, max_words_per_caption=3)
    validate(captions, "captions")
    assert captions["words"][0]["line_id"] == 1
    assert any(w["emphasis"] for w in captions["words"] if w["text"] == "thinness")
    assert all(len(seg["words"]) <= 3 for seg in captions["segments"])
    assert max(w["end"] for w in captions["words"]) <= 3.6
    assert captions["segments"][-1]["words"][-1]["line_id"] == 2


def test_mock_vision_is_deterministic(tmp_path: Path) -> None:
    from PIL import Image

    frame = tmp_path / "scene_001_50.jpg"
    Image.new("RGB", (64, 64), (200, 30, 30)).save(frame)
    vision = MockVision()
    a = vision.analyze_images(system="", user='{"scene_id": 4}', images=[frame], output_schema=SCENE_ANALYSIS_SCHEMA, max_tokens=10).data
    b = vision.analyze_images(system="", user='{"scene_id": 4}', images=[frame], output_schema=SCENE_ANALYSIS_SCHEMA, max_tokens=10).data
    assert a == b
    validate(a, "scene_analysis")
    assert a["scene_id"] == 4
    assert "red" in a["colors"] or "orange" in a["colors"]
