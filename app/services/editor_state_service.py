"""
`editor_state` — the client editor's full state for a post or reel (trim,
crop, zoom, rotation, filters, effects, text layers, stickers, media position
and shape ...). The server never interprets it: whatever JSON object the
editor sends is stored verbatim and returned verbatim, so the editor can add
fields without a backend change.

Same contract as story drafts (story_routes._parse_editor_state):
  * must be a JSON object (arrays/strings/numbers are a 400)
  * `null` / empty / omitted = no editor state
  * size-capped (413) so one request can't park a huge document in the DB;
    media itself goes through the file upload, never through editor_state
  * NaN / Infinity are refused (not valid JSON; MySQL's JSON column would 500)
"""

import json
from typing import Any

from fastapi import HTTPException, status

# Same ceiling as story drafts.
MAX_EDITOR_STATE_BYTES = 2 * 1024 * 1024

EDITOR_STATE_FORM_DESCRIPTION = (
    "The editor's full state as a JSON-encoded object (a string in this multipart "
    "form): trim, crop, zoom, rotation, filters, effects, text, stickers, media "
    "position/shape, etc. Stored verbatim and returned as `editor_state`; the "
    "server never interprets it. Omit for none. Max 2 MB."
)


def _reject_json_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"editor_state must be at most {MAX_EDITOR_STATE_BYTES} bytes",
    )


def parse_form_value(raw: str | None) -> dict[str, Any] | None:
    """For multipart endpoints, where the object arrives as a JSON string."""
    if raw is None or not raw.strip():
        return None
    if len(raw.encode("utf-8")) > MAX_EDITOR_STATE_BYTES:
        raise _too_large()
    try:
        parsed = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="editor_state must be valid JSON"
        )
    if parsed is None:
        return None
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="editor_state must be a JSON object"
        )
    return parsed


def check_object(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """For JSON-body endpoints, where pydantic already parsed it to a dict:
    enforce the size cap and reject NaN/Infinity. Returns the value untouched."""
    if value is None:
        return None
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="editor_state must be valid JSON"
        )
    if len(encoded.encode("utf-8")) > MAX_EDITOR_STATE_BYTES:
        raise _too_large()
    return value
