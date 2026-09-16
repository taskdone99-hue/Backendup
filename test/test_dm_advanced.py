"""
Tests for advanced DM: media messages (image/video/voice), reply-to-message,
message requests (accept/decline), and conversation muting.

(Typing status, unsend, and group chat already existed and aren't
re-tested here.)

Same harness pattern as the other new test files.
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-dm-tests"
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.database as database
import app.auth as auth

auth.SECRET_KEY = os.environ["SECRET_KEY"]

test_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
database.engine = test_engine
database.SessionLocal = TestSessionLocal

from app import models
from app.database import Base, get_db
from app.auth import create_access_token
from app.routers.chat_routes import router as chat_router
from app.routers.privacy_routes import router as privacy_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(chat_router)
test_app.include_router(privacy_router)


@test_app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    msg = first["msg"]
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix):]
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"message": msg})


@test_app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code, content={"message": exc.detail},
        headers=getattr(exc, "headers", None),
    )


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


test_app.dependency_overrides[get_db] = override_get_db
client = TestClient(test_app)


def _auth_headers(user_id: int) -> dict:
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


def _fake_image(name: str = "pic.jpg") -> tuple:
    return (name, b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")


def _fake_audio(name: str = "voice.m4a") -> tuple:
    return (name, b"fake voice note bytes", "audio/mp4")


def _make_user(username: str) -> int:
    db = TestSessionLocal()
    u = models.User(username=username, full_name=username, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    uid = u.id
    db.close()
    return uid


def _follow(follower_id: int, following_id: int) -> None:
    db = TestSessionLocal()
    db.add(models.Follow(follower_id=follower_id, following_id=following_id))
    db.commit()
    db.close()


# ---- media messages ----

def test_send_image_message():
    a = _make_user("dm_a1")
    b = _make_user("dm_b1")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    resp = client.post(
        f"/api/chat/conversations/{conv['id']}/media",
        headers=_auth_headers(a),
        data={"caption": "check this out"},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_type"] == "image"
    assert body["media_url"]
    assert body["content"] == "check this out"


def test_send_voice_note_message():
    a = _make_user("dm_a2")
    b = _make_user("dm_b2")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    resp = client.post(
        f"/api/chat/conversations/{conv['id']}/media",
        headers=_auth_headers(a),
        files={"file": _fake_audio()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_type"] == "audio"
    assert body["content"] is None


# ---- reply-to-message ----

def test_reply_to_message():
    a = _make_user("dm_a3")
    b = _make_user("dm_b3")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    original = client.post(
        f"/api/chat/conversations/{conv['id']}/messages",
        headers=_auth_headers(a), json={"content": "original message"},
    ).json()

    reply = client.post(
        f"/api/chat/conversations/{conv['id']}/messages",
        headers=_auth_headers(b), json={"content": "replying to that", "reply_to_message_id": original["id"]},
    )
    assert reply.status_code == 201
    body = reply.json()
    assert body["reply_to_message_id"] == original["id"]
    assert body["reply_to"]["content"] == "original message"
    assert body["reply_to"]["sender_id"] == a


def test_reply_to_message_from_other_conversation_rejected():
    a = _make_user("dm_a4")
    b = _make_user("dm_b4")
    c = _make_user("dm_c4")
    conv1 = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()
    conv2 = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [c]}
    ).json()
    msg_in_conv1 = client.post(
        f"/api/chat/conversations/{conv1['id']}/messages",
        headers=_auth_headers(a), json={"content": "hi b"},
    ).json()

    resp = client.post(
        f"/api/chat/conversations/{conv2['id']}/messages",
        headers=_auth_headers(a), json={"content": "hi c", "reply_to_message_id": msg_in_conv1["id"]},
    )
    assert resp.status_code == 404


# ---- message requests ----

def test_new_conversation_from_non_follower_is_a_request():
    a = _make_user("dm_a5")
    b = _make_user("dm_b5")
    # a messages b, but b doesn't follow a -> pending for b
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    # sender sees it as a normal, accepted conversation
    a_inbox = client.get("/api/chat/conversations", headers=_auth_headers(a)).json()
    assert any(c["id"] == conv["id"] for c in a_inbox["items"])

    # recipient does NOT see it in the main inbox...
    b_inbox = client.get("/api/chat/conversations", headers=_auth_headers(b)).json()
    assert all(c["id"] != conv["id"] for c in b_inbox["items"])

    # ...but does see it in requests
    b_requests = client.get("/api/chat/requests", headers=_auth_headers(b)).json()
    assert any(c["id"] == conv["id"] for c in b_requests["items"])


def test_conversation_from_a_follower_is_not_a_request():
    a = _make_user("dm_a6")
    b = _make_user("dm_b6")
    _follow(b, a)  # b already follows a
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    b_inbox = client.get("/api/chat/conversations", headers=_auth_headers(b)).json()
    assert any(c["id"] == conv["id"] for c in b_inbox["items"])
    b_requests = client.get("/api/chat/requests", headers=_auth_headers(b)).json()
    assert all(c["id"] != conv["id"] for c in b_requests["items"])


def test_accept_message_request():
    a = _make_user("dm_a7")
    b = _make_user("dm_b7")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    resp = client.post(f"/api/chat/conversations/{conv['id']}/accept", headers=_auth_headers(b))
    assert resp.status_code == 200

    b_inbox = client.get("/api/chat/conversations", headers=_auth_headers(b)).json()
    assert any(c["id"] == conv["id"] for c in b_inbox["items"])
    b_requests = client.get("/api/chat/requests", headers=_auth_headers(b)).json()
    assert all(c["id"] != conv["id"] for c in b_requests["items"])


def test_decline_message_request_deletes_conversation():
    a = _make_user("dm_a8")
    b = _make_user("dm_b8")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    resp = client.post(f"/api/chat/conversations/{conv['id']}/decline", headers=_auth_headers(b))
    assert resp.status_code == 200

    # gone for both sides
    a_inbox = client.get("/api/chat/conversations", headers=_auth_headers(a)).json()
    assert all(c["id"] != conv["id"] for c in a_inbox["items"])

    send_attempt = client.post(
        f"/api/chat/conversations/{conv['id']}/messages",
        headers=_auth_headers(a), json={"content": "still there?"},
    )
    assert send_attempt.status_code == 404


def test_group_conversation_is_never_a_request():
    a = _make_user("dm_a9")
    b = _make_user("dm_b9")
    c = _make_user("dm_c9")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a),
        json={"participant_ids": [b, c], "title": "Group chat"},
    ).json()
    assert conv["is_group"] is True

    for uid in (b, c):
        inbox = client.get("/api/chat/conversations", headers=_auth_headers(uid)).json()
        assert any(item["id"] == conv["id"] for item in inbox["items"])
        requests = client.get("/api/chat/requests", headers=_auth_headers(uid)).json()
        assert all(item["id"] != conv["id"] for item in requests["items"])


# ---- block prevents messaging ----

def test_blocked_user_cannot_send_message():
    a = _make_user("dm_a10")
    b = _make_user("dm_b10")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()
    client.post(f"/api/privacy/block/{b}", headers=_auth_headers(a))

    resp = client.post(
        f"/api/chat/conversations/{conv['id']}/messages",
        headers=_auth_headers(b), json={"content": "can you see this?"},
    )
    assert resp.status_code == 403


# ---- conversation mute ----

def test_mute_and_unmute_conversation():
    a = _make_user("dm_a11")
    b = _make_user("dm_b11")
    conv = client.post(
        "/api/chat/conversations", headers=_auth_headers(a), json={"participant_ids": [b]}
    ).json()

    mute_resp = client.post(f"/api/chat/conversations/{conv['id']}/mute", headers=_auth_headers(a))
    assert mute_resp.status_code == 200

    convo_view = client.get("/api/chat/conversations", headers=_auth_headers(a)).json()
    this_conv = next(c for c in convo_view["items"] if c["id"] == conv["id"])
    assert this_conv["is_muted"] is True

    unmute_resp = client.delete(f"/api/chat/conversations/{conv['id']}/mute", headers=_auth_headers(a))
    assert unmute_resp.status_code == 200
    convo_view_after = client.get("/api/chat/conversations", headers=_auth_headers(a)).json()
    this_conv_after = next(c for c in convo_view_after["items"] if c["id"] == conv["id"])
    assert this_conv_after["is_muted"] is False
