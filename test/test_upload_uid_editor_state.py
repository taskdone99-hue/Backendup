"""Stable-UID editor-state persistence for pending Reels and Videos."""

import pytest

from app import models

EDITOR_STATE = {
    "version": 1,
    "trim": {"start": 1.5, "end": 12.0},
    "crop": {"x": 0, "y": 0, "width": 1, "height": 1},
    "rotation": 0,
    "zoom": 1,
    "filters": [{"name": "warm", "intensity": 0.5}],
    "texts": [{"id": "t1", "text": "Hello"}],
    "stickers": [],
}


@pytest.mark.parametrize("content_type", ["reel", "video"])
def test_editor_state_can_be_saved_read_and_cleared_by_uid(
    client, db, make_user, make_reel, content_type
):
    owner = make_user(f"editor_{content_type}_owner")
    if content_type == "reel":
        row = make_reel(owner, video_uid="reel-editor-uid", upload_status="PENDING")
        base, uid_key = "/api/reels", "reel_uid"
    else:
        row = models.Video(
            user_id=owner.id,
            video_uid="video-editor-uid",
            upload_status="PENDING",
            upload_progress=0,
            title="Editor test",
            video_url=None,
            mime_type=None,
            duration_seconds=None,
            file_size=0,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        base, uid_key = "/api/videos/uid", "video_uid"

    client.login(owner)
    url = f"{base}/{row.video_uid}/editor-state"

    saved = client.put(url, json={"editor_state": EDITOR_STATE})
    assert saved.status_code == 200, saved.text
    assert saved.json()[uid_key] == row.video_uid
    assert saved.json()["editor_state"] == EDITOR_STATE

    read = client.get(url)
    assert read.status_code == 200, read.text
    assert read.json()["editor_state"] == EDITOR_STATE

    cleared = client.delete(url)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["editor_state"] is None
    assert client.get(url).json()["editor_state"] is None


@pytest.mark.parametrize(
    "content_type,base,uid_key",
    [
        ("reel", "/api/reels", "reel_uid"),
        ("video", "/api/videos/uid", "video_uid"),
    ],
)
def test_editor_state_rejects_non_object_payload(
    client, db, make_user, make_reel, content_type, base, uid_key
):
    owner = make_user(f"editor_invalid_{content_type}")
    if content_type == "reel":
        row = make_reel(owner, video_uid="reel-invalid-uid", upload_status="PENDING")
    else:
        row = models.Video(
            user_id=owner.id, video_uid="video-invalid-uid", upload_status="PENDING",
            upload_progress=0, title="Invalid test", video_url=None,
            mime_type=None, duration_seconds=None, file_size=0,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    client.login(owner)
    response = client.put(
        f"{base}/{row.video_uid}/editor-state",
        json={"editor_state": []},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "content_type,base",
    [("reel", "/api/reels"), ("video", "/api/videos/uid")],
)
def test_editor_state_rejects_published_upload(client, db, make_user, make_reel, content_type, base):
    owner = make_user(f"editor_published_{content_type}")
    if content_type == "reel":
        row = make_reel(owner, video_uid="reel-published-uid", upload_status="PUBLISHED")
    else:
        row = models.Video(
            user_id=owner.id, video_uid="video-published-uid", upload_status="PUBLISHED",
            upload_progress=100, title="Published test", video_url="/video.mp4",
            mime_type="video/mp4", duration_seconds=1, file_size=10,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    client.login(owner)
    response = client.put(
        f"{base}/{row.video_uid}/editor-state",
        json={"editor_state": EDITOR_STATE},
    )
    assert response.status_code == 409
