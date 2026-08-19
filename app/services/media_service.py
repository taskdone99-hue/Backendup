"""
Media storage for avatars, posts, reels, and story attachments.

By default this saves files to local disk under app/static/<subfolder>/ and
serves them back via the /static mount in main.py — good enough to run and
test the API with no external account needed. To switch to S3 (recommended
for production/RDS deployments), swap the body of `save_upload_file` for an
S3 `put_object` call and return the resulting object URL/CDN URL instead —
callers only care about getting a URL back, so nothing else needs to change.
"""

import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger("media_service")

STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"

# Keep uploads modest — these are avatars/posts/reels/stories, not raw video masters.
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # 10 MB
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _public_url(relative_path: str) -> str:
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/static/{relative_path}"
    return f"/static/{relative_path}"


def save_upload_file(
    file: UploadFile,
    subfolder: str,
    *,
    allow_video: bool = False,
) -> tuple[str, str]:
    """
    Validates and saves an uploaded file. Returns (public_url, kind) where
    kind is "image" or "video". Raises HTTPException(400) on anything invalid.
    """
    content_type = (file.content_type or "").lower()

    if content_type in ALLOWED_IMAGE_TYPES:
        kind = "image"
        max_bytes = MAX_IMAGE_BYTES
    elif allow_video and content_type in ALLOWED_VIDEO_TYPES:
        kind = "video"
        max_bytes = MAX_VIDEO_BYTES
    else:
        allowed = ALLOWED_IMAGE_TYPES | (ALLOWED_VIDEO_TYPES if allow_video else set())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type '{content_type}'. Allowed: {', '.join(sorted(allowed))}",
        )

    ext = os.path.splitext(file.filename or "")[1].lower() or (
        ".mp4" if kind == "video" else ".jpg"
    )
    filename = f"{uuid.uuid4().hex}{ext}"

    target_dir = STATIC_ROOT / subfolder
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    size = 0
    write_started = time.perf_counter()
    with open(target_path, "wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                out.close()
                target_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File too large — max {max_bytes // (1024 * 1024)}MB",
                )
            out.write(chunk)
    write_seconds = time.perf_counter() - write_started

    if size == 0:
        target_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    # By the time we get here, the ASGI server has already fully received
    # the upload from the client (FastAPI parses the whole multipart body
    # into `file` before this function is ever called) — so write_seconds
    # below is purely local disk I/O, not client upload time. If a slow
    # upload keeps showing up in reports, compare this number against the
    # client-observed request duration: a big gap between them points at
    # network transfer (client<->server) or reverse-proxy buffering, not
    # anything happening in this function.
    if size > 20 * 1024 * 1024 or write_seconds > 2:
        logger.info(
            "save_upload_file: %s (%.1f MB) written to disk in %.2fs (%s)",
            filename, size / (1024 * 1024), write_seconds, subfolder,
        )

    return _public_url(f"{subfolder}/{filename}"), kind


def delete_media_file(public_url: str) -> None:
    """Best-effort delete of a previously saved file, given the URL save_upload_file returned."""
    marker = "/static/"
    idx = public_url.find(marker)
    if idx == -1:
        return
    relative = public_url[idx + len(marker):]
    path = STATIC_ROOT / relative
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
