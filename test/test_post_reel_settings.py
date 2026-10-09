"""
Posts & Reels — remaining requirements:

1. GLOBAL user settings `hide_like_count` / `hide_comments`
     GET/PUT /api/users/me/engagement-settings
   applied to every post and reel the user owns (existing and future), on top
   of the existing per-reel switches (which must keep working unchanged).

2. `editor_state` — the client editor's complete state (trim, crop, zoom,
   rotation, filters, effects, text, stickers, media position/shape...) stored
   as opaque JSON on Post and Reel; accepted on create (multipart JSON string)
   and update (JSON body), returned in every post/reel response.

Plus the migration and OpenAPI documentation. File uploads are faked.
"""

import json

import pytest
from sqlalchemy import create_engine, text

from app import add_post_reel_settings_columns as migration
from app import models
from app.routers import content_routes, video_routes
from app.services import editor_state_service

EDITOR_STATE = {
    "version": 2,
    "trim": {"start": 1.25, "end": 9.5},
    "crop": {"x": 0.1, "y": 0.05, "width": 0.8, "height": 0.9, "aspect": "4:5"},
    "zoom": 1.35,
    "rotation": -7.5,
    "filters": [{"name": "warm", "intensity": 0.6}, {"name": "grain", "intensity": 0.2}],
    "effects": {"blur": 0, "vignette": None, "glow": False},
    "texts": [{"id": "t1", "text": "नमस्ते 👋", "x": 0.5, "y": 0.2, "color": "#fff", "bold": True}],
    "stickers": [{"id": "s1", "kind": "emoji", "value": "🔥", "x": 0.7, "y": 0.6, "scale": 1.5}],
    "media": {"shape": "rounded_rect", "cornerRadius": 24.5, "position": {"x": 0.5, "y": 0.5}},
    "someFutureEditorFeature": {"nested": [1, 2, {"deep": True}], "empty_list": [], "empty_obj": {}},
}


# ------------------------------------------------------------------ helpers

@pytest.fixture()
def fake_uploads(monkeypatch):
    """No files are written; records every attempted save."""
    saved = []

    def fake_save(f, folder, allow_video=False):
        saved.append(folder)
        return (f"/static/{folder}/fake.mp4", "video") if folder == "reels" else (f"/static/{folder}/fake.jpg", "image")

    for module in (content_routes, video_routes):
        monkeypatch.setattr(module, "save_upload_file", fake_save)
    monkeypatch.setattr(content_routes, "get_video_duration", lambda url: 10.0)
    monkeypatch.setattr(content_routes, "generate_video_thumbnail", lambda url: None)
    monkeypatch.setattr(video_routes, "generate_video_thumbnail", lambda url: None)
    return saved


def _settings(client, **body):
    return client.put("/api/users/me/engagement-settings", json=body)


def _like(db, user, target_type, target_id):
    db.add(models.Like(user_id=user.id, target_type=target_type, target_id=target_id))
    db.commit()


def _comment(db, user, **target):
    c = models.Comment(user_id=user.id, content="nice", **target)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _create_post(client, **data):
    return client.post("/api/posts", files={"file": ("a.jpg", b"x", "image/jpeg")}, data=data)


def _create_reel(client, **data):
    return client.post("/api/reels", files={"file": ("a.mp4", b"x", "video/mp4")}, data=data)


# ============================================================ 1. global settings API

def test_defaults_are_off(client, make_user):
    client.login(make_user("u"))
    r = client.get("/api/users/me/engagement-settings")
    assert r.status_code == 200
    assert r.json() == {"hide_like_count": False, "hide_comments": False}


def test_put_saves_and_get_returns_them(client, make_user, db):
    u = make_user("u")
    client.login(u)
    r = _settings(client, hide_like_count=True, hide_comments=True)
    assert r.status_code == 200
    assert r.json() == {"hide_like_count": True, "hide_comments": True}
    assert client.get("/api/users/me/engagement-settings").json() == {"hide_like_count": True, "hide_comments": True}
    db.refresh(u)
    assert (u.hide_like_count, u.hide_comments) == (True, True)


def test_put_one_field_keeps_the_other(client, make_user):
    client.login(make_user("u"))
    _settings(client, hide_like_count=True, hide_comments=True)
    assert _settings(client, hide_comments=False).json() == {"hide_like_count": True, "hide_comments": False}
    assert _settings(client, hide_like_count=False).json() == {"hide_like_count": False, "hide_comments": False}


@pytest.mark.parametrize("body", [{}, {"hide_like_count": None}, {"hide_comments": "maybe"}, {"hide_like_count": []}])
def test_put_rejects_invalid_bodies(client, make_user, body):
    client.login(make_user("u"))
    assert client.put("/api/users/me/engagement-settings", json=body).status_code == 400


def test_settings_require_authentication(client):
    client.login(None)
    assert client.get("/api/users/me/engagement-settings").status_code == 401
    assert client.put("/api/users/me/engagement-settings", json={"hide_comments": True}).status_code == 401


def test_settings_are_per_user(client, make_user):
    a, b = make_user("a"), make_user("b")
    client.login(a)
    _settings(client, hide_like_count=True)
    client.login(b)
    assert client.get("/api/users/me/engagement-settings").json()["hide_like_count"] is False


def test_existing_user_routes_unaffected_by_me_route(client, make_user):
    u = make_user("u")
    client.login(u)
    assert client.get(f"/api/users/{u.id}").status_code == 200
    assert client.get(f"/api/users/{u.id}/stats").status_code == 200


# ============================================================ global hide_like_count: posts

def test_hide_like_count_hides_it_on_all_posts_everywhere(client, make_user, make_post, follow, db):
    owner, fan = make_user("owner"), make_user("fan")
    follow(fan, owner)
    p1, p2 = make_post(owner), make_post(owner)
    for p in (p1, p2):
        _like(db, fan, models.LikeTargetType.post, p.id)
    client.login(owner)
    _settings(client, hide_like_count=True)

    client.login(fan)
    for p in (p1, p2):
        d = client.get(f"/api/posts/{p.id}").json()
        assert d["likes_count"] is None and d["like_count_hidden"] is True
        assert d["is_liked"] is True  # the viewer's own heart still shows
    feed = client.get("/api/posts/feed").json()["items"]
    assert len(feed) == 2 and all(i["likes_count"] is None for i in feed)
    profile = client.get(f"/api/users/{owner.id}/posts").json()["items"]
    assert profile and all(i["likes_count"] is None for i in profile)

    client.login(None)  # anonymous too
    assert client.get(f"/api/posts/{p1.id}").json()["likes_count"] is None

    client.login(owner)  # the owner always sees the real number
    d = client.get(f"/api/posts/{p1.id}").json()
    assert d["likes_count"] == 1 and d["like_count_hidden"] is True


def test_hide_like_count_applies_to_posts_created_afterwards(client, make_user, fake_uploads, db):
    owner, fan = make_user("owner"), make_user("fan")
    client.login(owner)
    _settings(client, hide_like_count=True)
    new_id = _create_post(client).json()["id"]
    _like(db, fan, models.LikeTargetType.post, new_id)
    client.login(fan)
    assert client.get(f"/api/posts/{new_id}").json()["likes_count"] is None


def test_hide_like_count_hides_likers_and_like_response_for_posts(client, make_user, make_post, db):
    owner, fan = make_user("owner"), make_user("fan")
    post = make_post(owner)
    client.login(owner)
    _settings(client, hide_like_count=True)

    client.login(fan)
    r = client.post("/api/likes", json={"target_type": "post", "target_id": post.id})
    assert r.status_code == 201 and r.json()["likes_count"] is None
    assert client.get(f"/api/posts/{post.id}/likes").status_code == 403
    client.login(None)
    assert client.get(f"/api/posts/{post.id}/likes").status_code == 403
    client.login(owner)
    likes = client.get(f"/api/posts/{post.id}/likes")
    assert likes.status_code == 200 and likes.json()["total"] == 1


def test_turning_hide_like_count_off_restores_counts(client, make_user, make_post, db):
    owner, fan = make_user("owner"), make_user("fan")
    post = make_post(owner)
    _like(db, fan, models.LikeTargetType.post, post.id)
    client.login(owner)
    _settings(client, hide_like_count=True)
    client.login(fan)
    assert client.get(f"/api/posts/{post.id}").json()["likes_count"] is None
    client.login(owner)
    _settings(client, hide_like_count=False)
    client.login(fan)
    d = client.get(f"/api/posts/{post.id}").json()
    assert d["likes_count"] == 1 and d["like_count_hidden"] is False
    assert client.get(f"/api/posts/{post.id}/likes").status_code == 200


def test_someone_elses_setting_does_not_touch_my_posts(client, make_user, make_post, db):
    a, b, fan = make_user("a"), make_user("b"), make_user("fan")
    post_b = make_post(b)
    _like(db, fan, models.LikeTargetType.post, post_b.id)
    client.login(a)
    _settings(client, hide_like_count=True, hide_comments=True)
    client.login(fan)
    d = client.get(f"/api/posts/{post_b.id}").json()
    assert d["likes_count"] == 1 and d["like_count_hidden"] is False and d["comments_hidden"] is False


def test_post_flags_default_to_visible(client, make_user, make_post):
    post = make_post(make_user("o"))
    d = client.get(f"/api/posts/{post.id}").json()
    assert (d["like_count_hidden"], d["comments_hidden"]) == (False, False)
    assert d["likes_count"] == 0


# ============================================================ global hide_comments: posts

def test_hide_comments_hides_threads_on_all_posts(client, make_user, make_post, db):
    owner, fan = make_user("owner"), make_user("fan")
    p1, p2 = make_post(owner), make_post(owner)
    top = _comment(db, fan, post_id=p1.id)
    _comment(db, owner, post_id=p1.id, parent_id=top.id)
    _comment(db, fan, post_id=p2.id)
    client.login(owner)
    _settings(client, hide_comments=True)

    for viewer in (fan, None):
        client.login(viewer)
        assert client.get(f"/api/posts/{p1.id}/comments").status_code == 403
        assert client.get(f"/api/posts/{p2.id}/comments").status_code == 403
        assert client.get(f"/api/comments/{top.id}/replies").status_code == 403
        d = client.get(f"/api/posts/{p1.id}").json()
        assert d["comments_hidden"] is True and d["comments_count"] == 2  # count still reported

    client.login(owner)
    assert client.get(f"/api/posts/{p1.id}/comments").json()["total"] == 1
    assert client.get(f"/api/comments/{top.id}/replies").json()["total"] == 1

    _settings(client, hide_comments=False)
    client.login(fan)
    assert client.get(f"/api/posts/{p1.id}/comments").status_code == 200


def test_hide_comments_still_allows_posting_comments(client, make_user, make_post):
    owner, fan = make_user("owner"), make_user("fan")
    post = make_post(owner)
    client.login(owner)
    _settings(client, hide_comments=True)
    client.login(fan)
    assert client.post(f"/api/posts/{post.id}/comments", json={"content": "hi"}).status_code == 201


def test_likes_and_comments_switches_are_independent(client, make_user, make_post, db):
    owner, fan = make_user("owner"), make_user("fan")
    post = make_post(owner)
    _like(db, fan, models.LikeTargetType.post, post.id)
    client.login(owner)
    _settings(client, hide_comments=True)
    client.login(fan)
    assert client.get(f"/api/posts/{post.id}").json()["likes_count"] == 1
    assert client.get(f"/api/posts/{post.id}/likes").status_code == 200
    client.login(owner)
    _settings(client, hide_comments=False, hide_like_count=True)
    client.login(fan)
    assert client.get(f"/api/posts/{post.id}/comments").status_code == 200


# ============================================================ global settings: reels + per-reel coexistence

def test_global_hide_like_count_applies_to_existing_reels(client, make_user, make_reel, follow, db):
    owner, fan = make_user("owner"), make_user("fan")
    r1, r2 = make_reel(owner), make_reel(owner)
    for r in (r1, r2):
        _like(db, fan, models.LikeTargetType.reel, r.id)
    client.login(owner)
    _settings(client, hide_like_count=True)

    client.login(fan)
    for r in (r1, r2):
        d = client.get(f"/api/reels/{r.id}").json()
        assert d["likes_count"] is None and d["like_count_hidden"] is True
        assert d["hide_like_count"] is False  # the per-reel switch itself is untouched
        assert client.get(f"/api/reels/{r.id}/likes").status_code == 403
    assert all(i["likes_count"] is None for i in client.get("/api/reels/feed").json()["items"])
    assert all(i["likes_count"] is None for i in client.get(f"/api/users/{owner.id}/reels").json()["items"])
    liked = client.post("/api/likes", json={"target_type": "reel", "target_id": r1.id})
    assert liked.json()["likes_count"] is None

    client.login(owner)
    assert client.get(f"/api/reels/{r1.id}").json()["likes_count"] == 1
    assert client.get(f"/api/reels/{r1.id}/likes").status_code == 200


def test_global_hide_comments_applies_to_existing_reels(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    r1, r2 = make_reel(owner), make_reel(owner)
    top = _comment(db, fan, reel_id=r1.id)
    client.login(owner)
    _settings(client, hide_comments=True)
    client.login(fan)
    for r in (r1, r2):
        assert client.get(f"/api/reels/{r.id}/comments").status_code == 403
        d = client.get(f"/api/reels/{r.id}").json()
        assert d["comments_hidden"] is True and d["hide_comments"] is False
    assert client.get(f"/api/comments/{top.id}/replies").status_code == 403
    client.login(owner)
    assert client.get(f"/api/reels/{r1.id}/comments").status_code == 200


def test_per_reel_settings_keep_working_when_global_is_off(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel, other = make_reel(owner), make_reel(owner)
    _like(db, fan, models.LikeTargetType.reel, reel.id)
    _comment(db, fan, reel_id=reel.id)
    client.login(owner)
    client.put(f"/api/videos/{reel.id}/engagement-settings",
               json={"hide_like_count": True, "hide_comments": True, "comments_enabled": False})
    client.login(fan)
    d = client.get(f"/api/reels/{reel.id}").json()
    assert d["likes_count"] is None and d["like_count_hidden"] is True and d["comments_hidden"] is True
    assert (d["hide_like_count"], d["hide_comments"], d["comments_enabled"]) == (True, True, False)
    assert client.get(f"/api/reels/{reel.id}/comments").status_code == 403
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "x"}).status_code == 403
    # the owner's *other* reel is unaffected
    assert client.get(f"/api/reels/{other.id}").json()["like_count_hidden"] is False
    assert client.get(f"/api/reels/{other.id}/comments").status_code == 200


def test_per_reel_and_global_combine_with_or(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    _like(db, fan, models.LikeTargetType.reel, reel.id)
    client.login(owner)
    client.put(f"/api/videos/{reel.id}/engagement-settings", json={"hide_like_count": True})
    _settings(client, hide_like_count=True)

    client.login(owner)  # turn the GLOBAL one off: the per-reel one still hides
    _settings(client, hide_like_count=False)
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] is None

    client.login(owner)  # now per-reel off, global on: still hidden
    client.put(f"/api/videos/{reel.id}/engagement-settings", json={"hide_like_count": False})
    _settings(client, hide_like_count=True)
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] is None

    client.login(owner)  # both off: visible
    _settings(client, hide_like_count=False)
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] == 1


def test_reel_flags_default_to_visible(client, make_user, make_reel):
    d = client.get(f"/api/reels/{make_reel(make_user('o')).id}").json()
    assert (d["like_count_hidden"], d["comments_hidden"]) == (False, False)


def test_global_settings_do_not_change_comments_enabled(client, make_user, make_reel):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(owner)
    _settings(client, hide_comments=True, hide_like_count=True)
    assert client.get(f"/api/reels/{reel.id}").json()["comments_enabled"] is True
    client.login(fan)
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "ok"}).status_code == 201


# ============================================================ 2. editor_state: posts

def test_create_post_stores_and_returns_full_editor_state(client, make_user, fake_uploads, db):
    owner, fan = make_user("owner"), make_user("fan")
    client.login(owner)
    r = _create_post(client, caption="hi", editor_state=json.dumps(EDITOR_STATE))
    assert r.status_code == 201, r.text
    assert r.json()["editor_state"] == EDITOR_STATE  # nothing dropped, reordered values intact
    pid = r.json()["id"]
    assert db.get(models.Post, pid).editor_state == EDITOR_STATE
    for viewer in (owner, fan, None):
        client.login(viewer)
        assert client.get(f"/api/posts/{pid}").json()["editor_state"] == EDITOR_STATE
    client.login(owner)
    assert client.get(f"/api/users/{owner.id}/posts").json()["items"][0]["editor_state"] == EDITOR_STATE


def test_create_post_without_editor_state_is_null(client, make_user, fake_uploads):
    client.login(make_user("u"))
    for kwargs in ({}, {"editor_state": ""}, {"editor_state": "null"}, {"editor_state": "   "}):
        r = _create_post(client, **kwargs)
        assert r.status_code == 201 and r.json()["editor_state"] is None, kwargs


@pytest.mark.parametrize("raw", ["{not json", "[1,2]", '"text"', "42", "true", '{"a": NaN}', '{"a": Infinity}'])
def test_create_post_rejects_bad_editor_state_before_saving_the_file(client, make_user, fake_uploads, db, raw):
    client.login(make_user("u"))
    r = _create_post(client, editor_state=raw)
    assert r.status_code == 400
    assert fake_uploads == []  # nothing was written
    assert db.query(models.Post).count() == 0


def test_create_post_editor_state_size_cap(client, make_user, fake_uploads, monkeypatch):
    client.login(make_user("u"))
    monkeypatch.setattr(editor_state_service, "MAX_EDITOR_STATE_BYTES", 200)
    r = _create_post(client, editor_state=json.dumps({"blob": "x" * 500}))
    assert r.status_code == 413 and fake_uploads == []
    assert _create_post(client, editor_state=json.dumps({"ok": 1})).status_code == 201


def test_update_post_replaces_clears_and_preserves_editor_state(client, make_user, make_post, db):
    owner = make_user("owner")
    post = make_post(owner, editor_state={"zoom": 1, "old": True})
    client.login(owner)

    r = client.put(f"/api/posts/{post.id}", json={"editor_state": EDITOR_STATE})
    assert r.status_code == 200 and r.json()["editor_state"] == EDITOR_STATE

    # other edits leave it alone
    r = client.put(f"/api/posts/{post.id}", json={"caption": "new caption"})
    assert r.json()["caption"] == "new caption" and r.json()["editor_state"] == EDITOR_STATE

    # full replacement, not a merge
    r = client.put(f"/api/posts/{post.id}", json={"editor_state": {"only": "this"}})
    assert r.json()["editor_state"] == {"only": "this"}

    # null clears
    r = client.put(f"/api/posts/{post.id}", json={"editor_state": None})
    assert r.json()["editor_state"] is None
    db.refresh(post)
    assert post.editor_state is None


def test_update_post_editor_state_validation(client, make_user, make_post, monkeypatch):
    owner = make_user("owner")
    post = make_post(owner, editor_state={"keep": 1})
    client.login(owner)
    for bad in ([1, 2], "string", 5, True):
        assert client.put(f"/api/posts/{post.id}", json={"editor_state": bad}).status_code == 400, bad
    nan = client.put(f"/api/posts/{post.id}", content='{"editor_state": {"a": NaN}}',
                     headers={"Content-Type": "application/json"})
    assert nan.status_code == 400
    monkeypatch.setattr(editor_state_service, "MAX_EDITOR_STATE_BYTES", 100)
    big = client.put(f"/api/posts/{post.id}", json={"editor_state": {"blob": "x" * 300}})
    assert big.status_code == 413
    assert client.get(f"/api/posts/{post.id}").json()["editor_state"] == {"keep": 1}  # untouched


def test_update_post_editor_state_owner_only(client, make_user, make_post, db):
    owner, other = make_user("owner"), make_user("other")
    post = make_post(owner, editor_state={"mine": 1})
    client.login(other)
    assert client.put(f"/api/posts/{post.id}", json={"editor_state": {"hacked": 1}}).status_code == 403
    db.refresh(post)
    assert post.editor_state == {"mine": 1}
    client.login(None)
    assert client.put(f"/api/posts/{post.id}", json={"editor_state": {}}).status_code == 401


def test_post_editor_state_empty_object_roundtrips(client, make_user, make_post):
    owner = make_user("owner")
    post = make_post(owner)
    client.login(owner)
    r = client.put(f"/api/posts/{post.id}", json={"editor_state": {}})
    assert r.json()["editor_state"] == {}


# ============================================================ 2. editor_state: reels / videos

def test_create_reel_stores_and_returns_editor_state(client, make_user, fake_uploads, db):
    owner, fan = make_user("owner"), make_user("fan")
    client.login(owner)
    r = _create_reel(client, caption="clip", editor_state=json.dumps(EDITOR_STATE))
    assert r.status_code == 201, r.text
    assert r.json()["editor_state"] == EDITOR_STATE
    rid = r.json()["id"]
    assert db.get(models.Reel, rid).editor_state == EDITOR_STATE
    client.login(fan)
    assert client.get(f"/api/reels/{rid}").json()["editor_state"] == EDITOR_STATE
    assert client.get("/api/reels/feed").json()["items"][0]["editor_state"] == EDITOR_STATE


def test_create_reel_without_editor_state_is_null(client, make_user, fake_uploads):
    client.login(make_user("u"))
    r = _create_reel(client)
    assert r.status_code == 201 and r.json()["editor_state"] is None


@pytest.mark.parametrize("raw", ["{bad", "[1]", '"s"', '{"a": NaN}'])
def test_create_reel_rejects_bad_editor_state_before_saving(client, make_user, fake_uploads, db, raw):
    client.login(make_user("u"))
    assert _create_reel(client, editor_state=raw).status_code == 400
    assert fake_uploads == [] and db.query(models.Reel).count() == 0


def test_video_upload_accepts_editor_state(client, make_user, fake_uploads):
    client.login(make_user("u"))
    r = client.post("/api/videos/upload", files={"file": ("a.mp4", b"x", "video/mp4")},
                    data={"title": "T", "editor_state": json.dumps(EDITOR_STATE)})
    assert r.status_code == 201 and r.json()["editor_state"] == EDITOR_STATE
    bad = client.post("/api/videos/upload", files={"file": ("a.mp4", b"x", "video/mp4")},
                      data={"editor_state": "[1]"})
    assert bad.status_code == 400


def test_video_metadata_update_replaces_clears_and_preserves(client, make_user, make_reel, db):
    owner = make_user("owner")
    reel = make_reel(owner, editor_state={"old": 1})
    client.login(owner)
    r = client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": EDITOR_STATE})
    assert r.status_code == 200 and r.json()["editor_state"] == EDITOR_STATE

    r = client.put(f"/api/videos/{reel.id}/metadata", json={"title": "New title"})
    assert r.json()["title"] == "New title" and r.json()["editor_state"] == EDITOR_STATE

    assert client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": {"x": 1}}).json()["editor_state"] == {"x": 1}
    assert client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": None}).json()["editor_state"] is None
    db.refresh(reel)
    assert reel.editor_state is None


def test_video_metadata_editor_state_validation_and_owner_only(client, make_user, make_reel, monkeypatch):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner, editor_state={"keep": 1})
    client.login(owner)
    assert client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": [1]}).status_code == 400
    monkeypatch.setattr(editor_state_service, "MAX_EDITOR_STATE_BYTES", 100)
    assert client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": {"b": "x" * 300}}).status_code == 413
    client.login(other)
    assert client.put(f"/api/videos/{reel.id}/metadata", json={"editor_state": {"h": 1}}).status_code == 403
    client.login(owner)
    assert client.get(f"/api/reels/{reel.id}").json()["editor_state"] == {"keep": 1}


def test_other_reel_settings_do_not_wipe_editor_state(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner, editor_state=EDITOR_STATE)
    client.login(owner)
    assert client.put(f"/api/videos/{reel.id}/language", json={"language": "English"}).json()["video"]["editor_state"] == EDITOR_STATE
    assert client.put(f"/api/videos/{reel.id}/visibility", json={"visibility": "public"}).json()["video"]["editor_state"] == EDITOR_STATE
    assert client.get(f"/api/reels/{reel.id}").json()["editor_state"] == EDITOR_STATE


def test_editor_state_default_null_for_existing_records(client, make_user, make_post, make_reel):
    u = make_user("u")
    assert client.get(f"/api/posts/{make_post(u).id}").json()["editor_state"] is None
    assert client.get(f"/api/reels/{make_reel(u).id}").json()["editor_state"] is None


# ============================================================ regression + docs

def test_existing_post_and_reel_creation_still_works(client, make_user, fake_uploads):
    client.login(make_user("u"))
    p = _create_post(client, caption="plain", ai_generated="true")
    assert p.status_code == 201 and p.json()["caption"] == "plain" and p.json()["ai_generated"] is True
    r = _create_reel(client, caption="plain reel")
    assert r.status_code == 201 and r.json()["caption"] == "plain reel"
    assert r.json()["visibility"] == "public" and r.json()["comments_enabled"] is True


def test_swagger_documents_everything(client):
    spec = client.get("/openapi.json").json()
    paths, schemas_ = spec["paths"], spec["components"]["schemas"]
    path = paths["/api/users/me/engagement-settings"]
    assert path["get"]["summary"] and path["put"]["summary"] and path["put"]["description"]
    assert "400" in path["put"]["responses"]
    for model in ("PostDetailOut", "ReelDetailOut"):
        props = schemas_[model]["properties"]
        assert {"editor_state", "like_count_hidden", "comments_hidden"} <= props.keys(), model
        assert props["editor_state"]["description"]
    assert "editor_state" in schemas_["PostUpdate"]["properties"]
    assert "editor_state" in schemas_["VideoMetadataUpdate"]["properties"]
    for form in ("Body_create_post_api_posts_post", "Body_create_reel_api_reels_post", "Body_upload_video_api_videos_upload_post"):
        assert "editor_state" in schemas_[form]["properties"], form
    assert {"hide_like_count", "hide_comments"} <= schemas_["UserEngagementSettingsOut"]["properties"].keys()


# ============================================================ migration

def _legacy_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(30) NOT NULL)"))
        conn.execute(text("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, media_url VARCHAR(500))"))
        conn.execute(text("CREATE TABLE reels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, video_url VARCHAR(500))"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'old')"))
        conn.execute(text("INSERT INTO posts (id, user_id, media_url) VALUES (1, 1, '/a.jpg')"))
        conn.execute(text("INSERT INTO reels (id, user_id, video_url) VALUES (1, 1, '/a.mp4')"))
    return engine


def test_migration_dry_run_changes_nothing():
    engine = _legacy_engine()
    statements = migration.migrate(engine, dry_run=True)
    assert len(statements) == 4
    assert migration.plan(engine) == statements  # still pending


def test_migration_adds_columns_and_existing_rows_get_safe_defaults():
    engine = _legacy_engine()
    migration.migrate(engine)
    with engine.connect() as conn:
        user = conn.execute(text("SELECT username, hide_like_count, hide_comments FROM users")).one()
        post = conn.execute(text("SELECT media_url, editor_state FROM posts")).one()
        reel = conn.execute(text("SELECT video_url, editor_state FROM reels")).one()
    assert tuple(user) == ("old", 0, 0)
    assert tuple(post) == ("/a.jpg", None) and tuple(reel) == ("/a.mp4", None)


def test_migration_is_idempotent_and_skips_existing_columns():
    engine = _legacy_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE posts ADD COLUMN editor_state JSON NULL"))
    assert not any("posts" in s for s in migration.plan(engine))
    migration.migrate(engine)
    assert migration.migrate(engine) == []


def test_migration_skips_tables_that_do_not_exist_yet():
    engine = create_engine("sqlite://")
    assert migration.plan(engine) == []


def test_migration_stays_in_step_with_the_models():
    for table, columns in migration.NEW_COLUMNS.items():
        model_columns = {c.name for c in models.Base.metadata.tables[table].columns}
        assert set(columns) <= model_columns, table
    assert set(migration.NEW_COLUMNS["users"]) == {"hide_like_count", "hide_comments"}
