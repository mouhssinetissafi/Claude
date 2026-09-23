from __future__ import annotations

import io
from pathlib import Path

from autoeditor.studio.service import StudioService


def test_ping() -> None:
    service = StudioService(output=io.StringIO())
    response = service.handle({"id": 1, "method": "ping", "params": {}})
    assert response["id"] == 1
    assert response["result"]["ok"] is True


def test_project_rpc_round_trip(tmp_path: Path) -> None:
    service = StudioService(output=io.StringIO())
    root = tmp_path / "project"
    created = service.handle({"id": 1, "method": "project.create", "params": {"path": str(root), "name": "Demo"}})
    assert created["result"]["name"] == "Demo"
    opened = service.handle({"id": 2, "method": "project.open", "params": {"path": str(root)}})
    assert opened["result"]["project_id"] == "demo"


def test_unknown_method_returns_protocol_error() -> None:
    service = StudioService(output=io.StringIO())
    response = service.handle({"id": "x", "method": "does.not.exist", "params": {}})
    assert response["id"] == "x"
    assert response["error"]["type"] == "KeyError"


def test_credentials_status(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    service = StudioService(output=io.StringIO())
    result = service.dispatch("credentials.status", {})
    assert result == {"anthropic": True, "elevenlabs": False, "elevenlabs_voice": True}
