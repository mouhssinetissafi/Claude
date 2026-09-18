from pathlib import Path

from autoeditor.cache import JsonCache, hash_file, hash_text, make_key


def test_make_key_is_deterministic_and_order_insensitive() -> None:
    a = make_key("vision", {"x": 1, "y": [1, 2]}, "model")
    b = make_key("vision", {"y": [1, 2], "x": 1}, "model")
    assert a == b
    assert a.startswith("vision_")


def test_make_key_changes_with_content_and_namespace() -> None:
    base = make_key("vision", "hash1", "claude-opus-5", "v1")
    assert base != make_key("vision", "hash2", "claude-opus-5", "v1")
    assert base != make_key("vision", "hash1", "claude-opus-5", "v2")
    assert base != make_key("llm", "hash1", "claude-opus-5", "v1")


def test_hash_file_tracks_content(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello" * 1000)
    h1 = hash_file(f)
    g = tmp_path / "renamed.bin"
    g.write_bytes(b"hello" * 1000)
    assert hash_file(g) == h1  # same content, different name -> same key
    f.write_bytes(b"hellp" * 1000)
    assert hash_file(f) != h1


def test_hash_file_large_fast_path(tmp_path: Path) -> None:
    f = tmp_path / "big.bin"
    f.write_bytes(b"\x01" * (20 * 1024 * 1024))
    fast = hash_file(f, fast=True)
    assert fast == hash_file(f, fast=True)
    assert fast != hash_text("")


def test_json_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    cache = JsonCache(tmp_path / "cache")
    assert cache.get("missing") is None
    cache.put("k1", {"a": 1})
    assert cache.has("k1")
    assert cache.get("k1") == {"a": 1}
    # Corrupt entry is treated as a miss, never raises.
    (tmp_path / "cache" / "k1.json").write_text("{not json", encoding="utf-8")
    assert cache.get("k1") is None
    cache.put("k2", [1, 2])
    assert cache.clear() == 2
    assert not cache.has("k2")
