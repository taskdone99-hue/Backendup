"""
Tests for advanced Stories: mentions, polls, and questions.
(Likes/replies/viewers already existed and aren't re-tested here.)

Same harness pattern as the other new test files.
"""

import os
import sys
from pathlib import Path

os.environ["SECRET_KEY"] = "test-secret-key-for-story-extras-tests"
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
from app.routers.story_routes import router as stories_router

Base.metadata.create_all(bind=test_engine)

test_app = FastAPI()
test_app.include_router(stories_router)


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


def _fake_image(name: str = "story.jpg") -> tuple:
    return (name, b"\xff\xd8\xff\xe0fakejpegbytes", "image/jpeg")


@pytest.fixture(scope="module")
def users():
    db = TestSessionLocal()
    a = models.User(username="story_a", full_name="A", is_active=True)
    b = models.User(username="story_b", full_name="B", is_active=True)
    c = models.User(username="story_c", full_name="C", is_active=True)
    db.add_all([a, b, c])
    db.commit()
    for u in (a, b, c):
        db.refresh(u)
    ids = (a.id, b.id, c.id)
    db.close()
    return ids


def test_create_story_with_mention(users):
    a_id, b_id, _ = users
    resp = client.post(
        "/api/stories", headers=_auth_headers(a_id),
        data={"caption": "with a friend", "mention_user_ids": str(b_id)},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["mentions"]) == 1
    assert body["mentions"][0]["user"]["id"] == b_id


def test_create_story_mention_unknown_user_404(users):
    a_id, _, _ = users
    resp = client.post(
        "/api/stories", headers=_auth_headers(a_id),
        data={"mention_user_ids": "999999"},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 404


def test_create_story_with_poll_and_vote(users):
    a_id, b_id, c_id = users
    created = client.post(
        "/api/stories", headers=_auth_headers(a_id),
        data={
            "caption": "which one?",
            "poll_question": "Cats or dogs?",
            "poll_option_1": "Cats",
            "poll_option_2": "Dogs",
        },
        files={"file": _fake_image()},
    ).json()
    assert created["poll"]["question"] == "Cats or dogs?"
    assert len(created["poll"]["options"]) == 2
    story_id = created["id"]
    option_a = created["poll"]["options"][0]["id"]
    option_b = created["poll"]["options"][1]["id"]

    vote1 = client.post(
        f"/api/stories/{story_id}/poll/vote", headers=_auth_headers(b_id), json={"option_id": option_a}
    )
    assert vote1.status_code == 200
    assert vote1.json()["total_votes"] == 1

    vote2 = client.post(
        f"/api/stories/{story_id}/poll/vote", headers=_auth_headers(c_id), json={"option_id": option_b}
    )
    assert vote2.json()["total_votes"] == 2

    # b changes their mind
    vote1_changed = client.post(
        f"/api/stories/{story_id}/poll/vote", headers=_auth_headers(b_id), json={"option_id": option_b}
    )
    body = vote1_changed.json()
    assert body["total_votes"] == 2  # still 2 voters total, not 3
    assert body["my_vote_option_id"] == option_b


def test_create_story_poll_requires_two_options(users):
    a_id, _, _ = users
    resp = client.post(
        "/api/stories", headers=_auth_headers(a_id),
        data={"poll_question": "Only one option?", "poll_option_1": "Yes"},
        files={"file": _fake_image()},
    )
    assert resp.status_code == 400


def test_vote_on_story_without_poll_404(users):
    a_id, b_id, _ = users
    created = client.post(
        "/api/stories", headers=_auth_headers(a_id), data={"caption": "no poll here"},
        files={"file": _fake_image()},
    ).json()
    resp = client.post(
        f"/api/stories/{created['id']}/poll/vote", headers=_auth_headers(b_id), json={"option_id": 1}
    )
    assert resp.status_code == 404


def test_create_story_with_question_and_respond(users):
    a_id, b_id, c_id = users
    created = client.post(
        "/api/stories", headers=_auth_headers(a_id),
        data={"question_prompt": "Ask me anything!"},
        files={"file": _fake_image()},
    ).json()
    assert created["question"]["prompt"] == "Ask me anything!"
    story_id = created["id"]

    r1 = client.post(
        f"/api/stories/{story_id}/question/respond", headers=_auth_headers(b_id),
        json={"response_text": "What's your favorite food?"},
    )
    assert r1.status_code == 201
    assert r1.json()["user"]["id"] == b_id

    r2 = client.post(
        f"/api/stories/{story_id}/question/respond", headers=_auth_headers(c_id),
        json={"response_text": "Hi there!"},
    )
    assert r2.status_code == 201

    # only the story owner can list responses
    owner_view = client.get(f"/api/stories/{story_id}/question/responses", headers=_auth_headers(a_id))
    assert owner_view.status_code == 200
    assert len(owner_view.json()["items"]) == 2

    stranger_view = client.get(f"/api/stories/{story_id}/question/responses", headers=_auth_headers(b_id))
    assert stranger_view.status_code == 403
