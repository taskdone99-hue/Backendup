"""
Tests for StoryDraft.editor_state - the story editor's full state (background,
media shape, text, positions, effects, stickers, filters, ...) saved with a
draft so the editor can be reopened exactly as it was left.

Covers: create, update (PATCH), get, list, publish, the OpenAPI/Swagger
schema, and the database migration for existing databases.

Media uploads are faked (no files are written under app/static): what these
tests care about is the JSON blob and the draft lifecycle around it.

Run with:
    pytest test/test_story_draft_editor_state.py -v
"""

import json

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import add_story_draft_editor_state_column as migration
from app import models
from app.database import Base
from app.main import app
from app.routers import story_routes

# A deliberately awkward editor payload: nested objects/arrays, unicode and
# emoji, floats, ints, booleans, explicit nulls, an empty list, an empty
# object, and keys this backend has never heard of. Everything must round-trip
# byte-for-byte equal, because the server must not interpret or "clean" it.
FULL_EDITOR_STATE = {
    "version": 3,
    "canvas": {"width": 1080, "height": 1920, "aspect": 0.5625},
    "background": {"type": "gradient", "colors": ["#833ab4", "#fd1d1d", "#fcb045"], "angle": 135},
    "media": {
        "shape": "rounded_rect",
        "cornerRadius": 24.5,
        "x": 0.5,
        "y": 0.45,
        "scale": 0.9,
        "rotation": -7.25,
        "flipX": False,
        "crop": None,
    },
    "texts": [
        {
            "id": "t1",
            "text": "Hello, नमस्ते 👋",
            "x": 0.5,
            "y": 0.18,
            "font": "classic",
            "size": 32,
            "color": "#ffffff",
            "bold": True,
            "alignment": "center",
        },
        {"id": "t2", "text": "second layer\nwith a newline", "x": 0.2, "y": 0.8},
    ],
    "stickers": [
        {"id": "s1", "type": "emoji", "value": "🔥", "x": 0.8, "y": 0.7, "scale": 1.2},
        {"id": "s2", "type": "location", "label": "Hyderabad", "x": 0.3, "y": 0.9},
    ],
    "filter": {"name": "warm", "intensity": 0.6},
    "effects": [],
    "drawing": {"strokes": [], "meta": {}},
    "futureField": {"anything": ["the", "editor", {"adds": 1}]},
}


# --------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def fake_media(monkeypatch):
    """Replaces disk-touching media helpers in the stories router. Records
    what was saved and deleted so tests can assert on file cleanup."""

    class Media:
        saved: list[str] = []
        deleted: list[str] = []

    Media.saved, Media.deleted = [], []

    def _save(file, subfolder, *, allow_video=False, allow_audio=False):
        kind = "video" if (file.content_type or "").startswith("video/") else "image"
        url = f"/static/{subfolder}/fake-{len(Media.saved) + 1}.{'mp4' if kind == 'video' else 'jpg'}"
        Media.saved.append(url)
        return url, kind

    monkeypatch.setattr(story_routes, "save_upload_file", _save)
    monkeypatch.setattr(story_routes, "delete_media_file", lambda url: Media.deleted.append(url))
    return Media


IMAGE = {"file": ("d.jpg", b"fake image bytes", "image/jpeg")}
VIDEO = {"file": ("d.mp4", b"fake video bytes", "video/mp4")}


def _create(client, editor_state=None, **data):
    if editor_state is not None:
        data["editor_state"] = (
            editor_state if isinstance(editor_state, str) else json.dumps(editor_state)
        )
    return client.post("/api/stories/drafts", data=data, files=IMAGE)


def _stored_editor_state(db, draft_id):
    """What the database itself holds for the column (raw SQL, so a JSON
    `null` literal and a real SQL NULL can't be confused)."""
    db.expire_all()
    return db.execute(
        text("SELECT editor_state FROM story_drafts WHERE id = :id"), {"id": draft_id}
    ).scalar_one()


def _insert_legacy_draft(db, owner, media_url="/static/stories/legacy.jpg"):
    """A draft exactly as it exists in a database from before this feature:
    the editor_state column is simply never written."""
    result = db.execute(
        text(
            "INSERT INTO story_drafts (user_id, media_url, media_type, close_friends_only) "
            "VALUES (:uid, :url, 'image', 0)"
        ),
        {"uid": owner.id, "url": media_url},
    )
    db.commit()
    return result.lastrowid


# ---------------------------------------------------------------- create

def test_create_draft_stores_and_returns_the_complete_editor_state(client, db, make_user):
    client.login(make_user("ava"))

    resp = _create(client, FULL_EDITOR_STATE, caption="with editor state")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["editor_state"] == FULL_EDITOR_STATE
    assert body["caption"] == "with editor state"  # the rest of the draft is unaffected
    assert json.loads(_stored_editor_state(db, body["id"])) == FULL_EDITOR_STATE


def test_editor_state_is_not_reshaped_or_pruned(client, make_user):
    """Explicit nulls, empty containers and unknown keys are data too."""
    client.login(make_user("bo"))
    state = {"a": None, "b": [], "c": {}, "d": [None, 0, False, ""], "unknown_key": {"x": [1, 2]}}

    body = _create(client, state).json()

    assert body["editor_state"] == state
    assert list(body["editor_state"]) == list(state)  # key order preserved as sent


def test_create_without_editor_state_returns_null(client, db, make_user):
    client.login(make_user("cy"))

    resp = _create(client)

    assert resp.status_code == 201, resp.text
    assert "editor_state" in resp.json()  # always present in the response, never omitted
    assert resp.json()["editor_state"] is None
    assert _stored_editor_state(db, resp.json()["id"]) is None  # SQL NULL, not a JSON 'null' literal


@pytest.mark.parametrize("raw", ["", "   ", "null"])
def test_create_with_blank_or_null_editor_state_is_stored_as_null(client, db, make_user, raw):
    client.login(make_user("di"))

    resp = _create(client, raw)

    assert resp.status_code == 201, resp.text
    assert resp.json()["editor_state"] is None
    assert _stored_editor_state(db, resp.json()["id"]) is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[1, 2, 3]",  # valid JSON, but the editor state must be an object
        '"just a string"',
        "42",
        "true",
        '{"x": NaN}',  # not valid JSON; MySQL's JSON column would reject it
        '{"x": Infinity}',
    ],
)
def test_create_rejects_editor_state_that_is_not_a_json_object(client, make_user, fake_media, raw):
    client.login(make_user("ed"))

    resp = _create(client, raw)

    assert resp.status_code == 400, resp.text
    assert "editor_state" in resp.json()["message"]  # the app's {"message": ...} error shape
    assert fake_media.saved == [], "a rejected draft must not leave an uploaded file behind"
    assert client.get("/api/stories/drafts").json()["total"] == 0


def test_create_rejects_oversized_editor_state(client, make_user, fake_media, monkeypatch):
    monkeypatch.setattr(story_routes, "MAX_EDITOR_STATE_BYTES", 100)
    client.login(make_user("fay"))

    resp = _create(client, {"blob": "x" * 500})

    assert resp.status_code == 413, resp.text
    assert fake_media.saved == []


def test_editor_state_at_the_size_limit_is_accepted(client, make_user):
    client.login(make_user("gus"))
    state = {"blob": "x" * 1000}

    assert _create(client, state).status_code == 201


# ------------------------------------------------------------- get / list

def test_get_draft_returns_editor_state(client, make_user):
    client.login(make_user("hal"))
    draft_id = _create(client, FULL_EDITOR_STATE).json()["id"]

    resp = client.get(f"/api/stories/drafts/{draft_id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["editor_state"] == FULL_EDITOR_STATE


def test_list_drafts_returns_editor_state_for_each_draft(client, make_user):
    client.login(make_user("ivy"))
    with_state = _create(client, FULL_EDITOR_STATE).json()["id"]
    without_state = _create(client).json()["id"]

    resp = client.get("/api/stories/drafts")

    assert resp.status_code == 200, resp.text
    by_id = {item["id"]: item for item in resp.json()["items"]}
    assert by_id[with_state]["editor_state"] == FULL_EDITOR_STATE
    assert by_id[without_state]["editor_state"] is None


def test_existing_drafts_without_editor_state_keep_working(client, db, make_user):
    """A row from before the column existed (editor_state never written) must
    list, fetch, update and publish like any other draft."""
    owner = make_user("jon")
    legacy_id = _insert_legacy_draft(db, owner)
    client.login(owner)

    fetched = client.get(f"/api/stories/drafts/{legacy_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["editor_state"] is None
    assert fetched.json()["media_url"] == "/static/stories/legacy.jpg"

    listed = client.get("/api/stories/drafts")
    assert listed.status_code == 200, listed.text
    assert [(i["id"], i["editor_state"]) for i in listed.json()["items"]] == [(legacy_id, None)]

    # An edit that doesn't mention editor_state leaves it null...
    patched = client.patch(f"/api/stories/drafts/{legacy_id}", data={"caption": "still fine"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["editor_state"] is None

    # ...and it can be published.
    published = client.post(f"/api/stories/drafts/{legacy_id}/publish")
    assert published.status_code == 201, published.text
    assert published.json()["caption"] == "still fine"


def test_drafts_of_other_users_never_leak_editor_state(client, make_user):
    owner, other = make_user("kai"), make_user("lea")
    client.login(owner)
    draft_id = _create(client, FULL_EDITOR_STATE).json()["id"]

    client.login(other)
    assert client.get(f"/api/stories/drafts/{draft_id}").status_code == 404
    assert client.get("/api/stories/drafts").json()["items"] == []


# ---------------------------------------------------------------- update

def test_update_sets_editor_state_on_a_draft_that_had_none(client, make_user):
    client.login(make_user("max"))
    draft_id = _create(client).json()["id"]

    resp = client.patch(
        f"/api/stories/drafts/{draft_id}", data={"editor_state": json.dumps(FULL_EDITOR_STATE)}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["editor_state"] == FULL_EDITOR_STATE
    # ...and it is really persisted, not just echoed back:
    assert client.get(f"/api/stories/drafts/{draft_id}").json()["editor_state"] == FULL_EDITOR_STATE


def test_update_replaces_editor_state_as_a_whole_without_merging(client, make_user):
    client.login(make_user("nia"))
    draft_id = _create(client, {"keep_me": 1, "texts": [{"text": "old"}]}).json()["id"]
    replacement = {"texts": [{"text": "new"}]}

    resp = client.patch(f"/api/stories/drafts/{draft_id}", data={"editor_state": json.dumps(replacement)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["editor_state"] == replacement  # "keep_me" is gone: replaced, not merged


@pytest.mark.parametrize("data", [{}, {"editor_state": ""}, {"editor_state": "  "}])
def test_update_that_omits_or_blanks_editor_state_leaves_it_untouched(client, make_user, data):
    """Swagger/multipart clients send untouched optional fields as "" - that
    must never wipe a saved editor state."""
    client.login(make_user("oli"))
    draft_id = _create(client, FULL_EDITOR_STATE, caption="before").json()["id"]

    resp = client.patch(f"/api/stories/drafts/{draft_id}", data={**data, "caption": "after"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["caption"] == "after"
    assert resp.json()["editor_state"] == FULL_EDITOR_STATE
    assert client.get(f"/api/stories/drafts/{draft_id}").json()["editor_state"] == FULL_EDITOR_STATE


def test_update_with_json_null_clears_editor_state(client, db, make_user):
    client.login(make_user("pam"))
    draft_id = _create(client, FULL_EDITOR_STATE).json()["id"]

    resp = client.patch(f"/api/stories/drafts/{draft_id}", data={"editor_state": "null"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["editor_state"] is None
    assert client.get(f"/api/stories/drafts/{draft_id}").json()["editor_state"] is None
    assert _stored_editor_state(db, draft_id) is None  # SQL NULL, not a JSON 'null' literal


@pytest.mark.parametrize("raw", ["{broken", "[1]", "7", '{"x": NaN}'])
def test_update_with_invalid_editor_state_is_rejected_and_changes_nothing(client, make_user, fake_media, raw):
    client.login(make_user("quin"))
    draft_id = _create(client, FULL_EDITOR_STATE, caption="original").json()["id"]
    saved_before = list(fake_media.saved)

    resp = client.patch(
        f"/api/stories/drafts/{draft_id}",
        data={"editor_state": raw, "caption": "should not apply"},
        files={"file": ("n.jpg", b"new bytes", "image/jpeg")},
    )

    assert resp.status_code == 400, resp.text
    after = client.get(f"/api/stories/drafts/{draft_id}").json()
    assert after["editor_state"] == FULL_EDITOR_STATE
    assert after["caption"] == "original"
    assert fake_media.saved == saved_before, "the replacement upload must not be written when validation fails"


def test_update_can_change_other_fields_without_touching_editor_state(client, make_user):
    client.login(make_user("ray"))
    draft_id = _create(client, FULL_EDITOR_STATE, caption="old").json()["id"]

    resp = client.patch(
        f"/api/stories/drafts/{draft_id}", data={"caption": "new", "close_friends_only": "true"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["caption"], body["close_friends_only"]) == ("new", True)
    assert body["editor_state"] == FULL_EDITOR_STATE


def test_update_can_clear_the_caption(client, make_user):
    client.login(make_user("sol"))
    draft_id = _create(client, caption="to be removed").json()["id"]

    resp = client.patch(f"/api/stories/drafts/{draft_id}", data={"caption": ""})

    assert resp.status_code == 200, resp.text
    assert resp.json()["caption"] is None


def test_update_with_no_fields_is_a_harmless_noop(client, make_user):
    client.login(make_user("tess"))
    created = _create(client, FULL_EDITOR_STATE, caption="same").json()

    resp = client.patch(f"/api/stories/drafts/{created['id']}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == created


def test_update_replaces_media_and_keeps_editor_state(client, make_user, fake_media):
    """The editor re-renders the image after an edit: the file changes, the
    editor state is saved alongside it, and the superseded file is deleted."""
    client.login(make_user("uma"))
    created = _create(client, {"v": 1}).json()
    old_url = created["media_url"]

    resp = client.patch(
        f"/api/stories/drafts/{created['id']}",
        data={"editor_state": json.dumps({"v": 2})},
        files={"file": ("clip.mp4", b"new video bytes", "video/mp4")},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["media_url"] != old_url
    assert body["media_type"] == "video"
    assert body["editor_state"] == {"v": 2}
    assert fake_media.deleted == [old_url]


def test_update_without_a_file_keeps_the_existing_media(client, make_user, fake_media):
    client.login(make_user("vic"))
    created = _create(client, {"v": 1}).json()

    resp = client.patch(f"/api/stories/drafts/{created['id']}", data={"editor_state": '{"v": 2}'})

    assert resp.json()["media_url"] == created["media_url"]
    assert fake_media.deleted == []


def test_update_failing_to_save_cleans_up_the_new_upload_and_keeps_the_old_one(
    client, db, make_user, fake_media, monkeypatch
):
    client.login(make_user("wes"))
    created = _create(client, {"v": 1}).json()
    new_url = "/static/stories/fake-2.jpg"

    def _boom():
        raise RuntimeError("database went away")

    monkeypatch.setattr(db, "commit", _boom)
    with pytest.raises(RuntimeError):
        client.patch(
            f"/api/stories/drafts/{created['id']}",
            data={"editor_state": '{"v": 2}'},
            files={"file": ("n.jpg", b"new bytes", "image/jpeg")},
        )
    monkeypatch.undo()

    assert fake_media.deleted == [new_url], "only the orphaned NEW file may be deleted"
    after = client.get(f"/api/stories/drafts/{created['id']}").json()
    assert after["media_url"] == created["media_url"]
    assert after["editor_state"] == {"v": 1}


def test_update_can_set_a_location(client, make_user):
    client.login(make_user("xan"))
    draft_id = _create(client, {"v": 1}).json()["id"]

    resp = client.patch(
        f"/api/stories/drafts/{draft_id}",
        data={"location_name": "Charminar", "location_latitude": "17.3616", "location_longitude": "78.4747"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["location"]["name"] == "Charminar"
    assert resp.json()["editor_state"] == {"v": 1}


def test_update_with_an_unknown_location_id_is_404_and_changes_nothing(client, make_user):
    client.login(make_user("yara"))
    draft_id = _create(client, {"v": 1}, caption="orig").json()["id"]

    resp = client.patch(
        f"/api/stories/drafts/{draft_id}", data={"location_id": "99999", "caption": "changed"}
    )

    assert resp.status_code == 404, resp.text
    assert client.get(f"/api/stories/drafts/{draft_id}").json()["caption"] == "orig"


def test_cannot_update_someone_elses_draft(client, make_user):
    owner, other = make_user("zed"), make_user("abe")
    client.login(owner)
    draft_id = _create(client, {"v": 1}).json()["id"]

    client.login(other)
    resp = client.patch(f"/api/stories/drafts/{draft_id}", data={"editor_state": '{"v": 99}'})
    assert resp.status_code == 404

    client.login(owner)
    assert client.get(f"/api/stories/drafts/{draft_id}").json()["editor_state"] == {"v": 1}


def test_update_unknown_draft_is_404(client, make_user):
    client.login(make_user("bea"))
    assert client.patch("/api/stories/drafts/12345", data={"caption": "x"}).status_code == 404


# --------------------------------------------------------------- publish

def test_publish_draft_with_editor_state(client, db, make_user, fake_media):
    owner = make_user("cal")
    client.login(owner)
    created = _create(client, FULL_EDITOR_STATE, caption="going live", close_friends_only="true").json()

    resp = client.post(f"/api/stories/drafts/{created['id']}/publish")

    assert resp.status_code == 201, resp.text
    story = resp.json()
    assert story["caption"] == "going live"
    assert story["media_url"] == created["media_url"]
    assert story["close_friends_only"] is True

    # The draft is consumed (editor_state goes with it - a published Story is
    # the flattened media and has no editor state of its own)...
    assert client.get(f"/api/stories/drafts/{created['id']}").status_code == 404
    assert client.get("/api/stories/drafts").json()["total"] == 0
    assert db.query(models.StoryDraft).count() == 0
    # ...and the story is live.
    assert [s["id"] for s in client.get("/api/stories/mine").json()["items"]] == [story["id"]]
    # Publishing converts the draft; it must not delete the media the story now uses.
    assert fake_media.deleted == []


def test_publish_draft_without_editor_state(client, make_user):
    client.login(make_user("dee"))
    draft_id = _create(client, caption="plain").json()["id"]

    resp = client.post(f"/api/stories/drafts/{draft_id}/publish")

    assert resp.status_code == 201, resp.text
    assert resp.json()["caption"] == "plain"


def test_publish_uses_the_media_and_fields_from_the_latest_update(client, make_user):
    client.login(make_user("eli"))
    draft_id = _create(client, {"v": 1}, caption="v1").json()["id"]
    updated = client.patch(
        f"/api/stories/drafts/{draft_id}",
        data={"editor_state": '{"v": 2}', "caption": "v2"},
        files={"file": ("n.jpg", b"re-rendered", "image/jpeg")},
    ).json()

    resp = client.post(f"/api/stories/drafts/{draft_id}/publish")

    assert resp.status_code == 201, resp.text
    assert resp.json()["caption"] == "v2"
    assert resp.json()["media_url"] == updated["media_url"]


def test_publish_still_cannot_be_done_twice(client, make_user):
    client.login(make_user("fox"))
    draft_id = _create(client, FULL_EDITOR_STATE).json()["id"]
    assert client.post(f"/api/stories/drafts/{draft_id}/publish").status_code == 201
    assert client.post(f"/api/stories/drafts/{draft_id}/publish").status_code == 404


def test_delete_draft_with_editor_state(client, make_user, fake_media):
    client.login(make_user("gia"))
    created = _create(client, FULL_EDITOR_STATE).json()

    assert client.delete(f"/api/stories/drafts/{created['id']}").status_code == 200
    assert client.get(f"/api/stories/drafts/{created['id']}").status_code == 404
    assert fake_media.deleted == [created["media_url"]]


# ----------------------------------------------------------- Swagger / OpenAPI

def _schema():
    return app.openapi()


def _resolve(spec, node):
    while "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].split("/")[-1]]
    return node


def test_openapi_documents_editor_state_on_the_draft_response():
    spec = _schema()
    prop = spec["components"]["schemas"]["StoryDraftOut"]["properties"]["editor_state"]

    assert "editor_state" not in spec["components"]["schemas"]["StoryDraftOut"].get("required", [])
    assert prop["description"].startswith("Opaque JSON object")
    kinds = {branch.get("type") for branch in prop["anyOf"]}
    assert kinds == {"object", "null"}  # an object, or null
    assert prop["examples"] and "background" in prop["examples"][0]


def test_openapi_documents_editor_state_on_create_and_update_bodies():
    spec = _schema()
    create_op = spec["paths"]["/api/stories/drafts"]["post"]
    update_op = spec["paths"]["/api/stories/drafts/{draft_id}"]["patch"]

    for op in (create_op, update_op):
        body = _resolve(spec, op["requestBody"]["content"]["multipart/form-data"]["schema"])
        field = body["properties"]["editor_state"]
        assert "JSON-encoded object" in field["description"]
        assert "editor_state" not in body.get("required", [])  # optional everywhere

    assert "REPLACES" in _resolve(
        spec, update_op["requestBody"]["content"]["multipart/form-data"]["schema"]
    )["properties"]["editor_state"]["description"]


def test_openapi_lists_the_draft_update_endpoint_and_its_response():
    spec = _schema()
    update_op = spec["paths"]["/api/stories/drafts/{draft_id}"]["patch"]

    assert update_op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/StoryDraftOut"
    )
    list_item = spec["components"]["schemas"]["PaginatedStoryDraftsResponse"]["properties"]["items"]
    assert list_item["items"]["$ref"].endswith("/StoryDraftOut")  # list responses carry it too


# ------------------------------------------------------------- migration
#
# The normal tests above build a *fresh* SQLite schema from the models, so
# model and database always agree. These build the *legacy* schema instead -
# the shape production's story_drafts table has today.

ORIGINAL_COLUMNS = {
    "id", "user_id", "media_url", "media_type", "caption", "location_name",
    "location_latitude", "location_longitude", "location_id", "close_friends_only",
    "created_at", "updated_at",
}
MODEL_COLUMNS = {c.name for c in models.StoryDraft.__table__.columns}

LEGACY_DDL = """
CREATE TABLE story_drafts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    media_url VARCHAR(500) NOT NULL,
    media_type VARCHAR(5) NOT NULL,
    caption VARCHAR(280),
    location_name VARCHAR(150),
    location_latitude FLOAT,
    location_longitude FLOAT,
    location_id INTEGER REFERENCES locations(id),
    close_friends_only BOOLEAN NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def _new_engine():
    return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)


def _engine_with_legacy_story_drafts_table():
    engine = _new_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE story_drafts"))
        conn.execute(text(LEGACY_DDL))
    return engine


def _columns(engine):
    return {c["name"] for c in inspect(engine).get_columns("story_drafts")}


def test_every_column_added_after_creation_is_covered_by_the_migration():
    """Fails if someone adds a column to models.StoryDraft without adding it
    to app/add_story_draft_editor_state_column.py."""
    assert MODEL_COLUMNS - ORIGINAL_COLUMNS == set(migration.STORY_DRAFT_COLUMNS), (
        "models.StoryDraft and add_story_draft_editor_state_column.STORY_DRAFT_COLUMNS disagree. "
        "If you added a column to models.StoryDraft, add it to the migration too "
        "(existing databases won't get it from create_all())."
    )


def test_editor_state_column_is_nullable_json_with_no_default():
    column = models.StoryDraft.__table__.c.editor_state

    assert column.nullable is True
    assert column.default is None and column.server_default is None
    assert column.type.__class__.__name__ == "JSON"
    assert migration.STORY_DRAFT_COLUMNS["editor_state"] == "JSON NULL"


def test_migration_adds_the_column_to_a_legacy_table():
    engine = _engine_with_legacy_story_drafts_table()
    assert _columns(engine) == ORIGINAL_COLUMNS

    statements = migration.migrate(engine)

    assert statements == ["ALTER TABLE story_drafts ADD COLUMN editor_state JSON NULL"]
    assert _columns(engine) == MODEL_COLUMNS


def test_migration_is_idempotent():
    engine = _engine_with_legacy_story_drafts_table()
    migration.migrate(engine)

    assert migration.migrate(engine) == []
    assert migration.migrate(engine) == []
    assert _columns(engine) == MODEL_COLUMNS


def test_migration_is_a_noop_on_an_up_to_date_schema():
    engine = _new_engine()
    Base.metadata.create_all(bind=engine)  # fresh install: model == database

    assert migration.migrate(engine) == []


def test_migration_does_nothing_if_the_table_does_not_exist_yet():
    assert migration.migrate(_new_engine()) == []  # create_all() at startup will build it


def test_dry_run_reports_the_statement_but_changes_nothing():
    engine = _engine_with_legacy_story_drafts_table()

    statements = migration.migrate(engine, dry_run=True)

    assert statements == ["ALTER TABLE story_drafts ADD COLUMN editor_state JSON NULL"]
    assert _columns(engine) == ORIGINAL_COLUMNS


def test_existing_drafts_survive_the_migration_and_read_back_as_null():
    engine = _engine_with_legacy_story_drafts_table()
    session = sessionmaker(bind=engine)()
    try:
        user = models.User(username="old_user", email="old@example.test", hashed_password="x", is_active=True)
        session.add(user)
        session.commit()
        # A draft written before the column existed (raw SQL: the legacy table has no editor_state).
        session.execute(
            text(
                "INSERT INTO story_drafts (user_id, media_url, media_type, caption, close_friends_only) "
                "VALUES (:uid, '/static/stories/old.jpg', 'image', 'written long ago', 0)"
            ),
            {"uid": user.id},
        )
        session.commit()

        migration.migrate(engine)

        draft = session.query(models.StoryDraft).one()
        assert (draft.media_url, draft.caption) == ("/static/stories/old.jpg", "written long ago")
        assert draft.editor_state is None
        # and the migrated column accepts and returns a real editor state:
        draft.editor_state = FULL_EDITOR_STATE
        session.commit()
        session.expire_all()
        assert session.query(models.StoryDraft).one().editor_state == FULL_EDITOR_STATE
    finally:
        session.close()


def test_drafts_fail_on_the_unmigrated_schema_then_work_after_migration():
    """Documents why the migration must run before the new code is deployed."""
    engine = _engine_with_legacy_story_drafts_table()
    session = sessionmaker(bind=engine)()
    try:
        with pytest.raises(OperationalError, match="editor_state"):
            session.query(models.StoryDraft).all()
        session.rollback()

        migration.migrate(engine)

        assert session.query(models.StoryDraft).all() == []
    finally:
        session.close()
