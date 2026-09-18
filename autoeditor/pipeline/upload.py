"""Optional YouTube upload.

Uploading is never automatic: it requires ``upload.enabled: true`` in config,
the ``--upload`` flag, a passed QC, and credentials in the environment. The
Google client libraries are optional; without them this module explains what
is missing instead of failing silently.

Environment variables used:
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from autoeditor.config import Config
from autoeditor.logging_utils import get_logger

log = get_logger(__name__)


class UploadNotConfiguredError(RuntimeError):
    pass


def upload_allowed(cfg: Config, *, requested: bool, qc_passed: bool, approved: bool = False) -> tuple[bool, str]:
    """Every condition must hold: explicit request, config, QC, human approval, credentials."""
    if not requested:
        return False, "upload not requested (--upload not given)"
    if not bool(cfg.get("upload.enabled", False)):
        return False, "upload.enabled is false in config"
    if not qc_passed:
        return False, "QC did not pass; output moved to review/"
    if bool(cfg.get("upload.require_approval", True)) and not approved:
        return False, "human approval missing: review output/<job>/REVIEW.md and create output/<job>/APPROVED"
    missing = [v for v in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN") if not os.environ.get(v)]
    if missing:
        return False, f"missing environment variables: {', '.join(missing)}"
    return True, "ok"


def upload_to_youtube(video: Path, metadata: dict[str, Any], *, privacy: str = "private") -> str:
    """Upload ``video`` and return the YouTube video id. Requires google-api-python-client."""
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as exc:
        raise UploadNotConfiguredError(
            "google-api-python-client and google-auth are not installed; pip install google-api-python-client google-auth google-auth-oauthlib"
        ) from exc
    creds = Credentials(
        None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata.get("tags", []),
            "categoryId": "28",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            log.info("Upload progress: %d%%", int(status.progress() * 100))
    video_id = str(response["id"])
    log.info("Uploaded as %s (privacy=%s)", video_id, privacy)
    return video_id
