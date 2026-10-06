"""
Phase 1 of the video/reel upload flow — the upload-page settings:

  PUT    /api/videos/{id}/language
  PUT    /api/videos/{id}/ai-content
  PUT    /api/videos/{id}/engagement-settings
  PUT    /api/videos/{id}/audience
  PUT    /api/videos/{id}/visibility
  PUT    /api/videos/{id}/schedule
  DELETE /api/videos/{id}/schedule

plus how those settings are enforced by the pre-existing reel / comment /
like / feed / profile / share APIs, the migration, and regression checks that
upload, thumbnails and the reel APIs still behave as before.

Runs on conftest.py's in-memory SQLite harness with auth stubbed
(`client.login(user)`; not logged in => 401).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from app import models
from app.add_video_upload_settings_columns import migrate
from app.routers import content_routes, video_routes
from app.services import video_settings_service as vss

SETTING_ENDPOINTS = {
    "language": {"language": "English"},
    "ai-content": {"ai_generated": True},
    "engagement-settings": {"comments_enabled": False},
    "audience": {"audience": "18_plus"},
    "visibility": {"visibility": "private"},
    "schedule": {"schedule_enabled": True, "scheduled_at": "2999-01-01T00:00:00"},
}


def _future(**delta) -> datetime:
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0)


def _naive(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


def _comment(db, reel, user, content="nice", parent=None):
    c = models.Comment(
        reel_id=reel.id, user_id=user.id, content=content,
        parent_id=parent.id if parent else None,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _set(client, video_id, endpoint, body):
    return client.put(f"/api/videos/{video_id}/{endpoint}", json=body)


# =========================================================== language

def test_language_update_saves_and_confirms(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner, caption="cap", title="T")
    client.login(owner)

    r = _set(client, reel.id, "language", {"language": "English"})

    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "Language updated"
    assert body["video_id"] == reel.id
    assert body["language"] == "English"
    # the full video comes back too, with existing data untouched
    assert body["video"]["language"] == "English"
    assert body["video"]["caption"] == "cap"
    assert body["video"]["title"] == "T"
    assert body["video"]["video_url"] == reel.video_url


@pytest.mark.parametrize("sent,stored", [
    ("english", "English"), ("EN", "English"), ("  hindi ", "Hindi"),
    ("te", "Telugu"), ("Telugu", "Telugu"),
])
def test_language_is_normalised_to_canonical_name(client, make_user, make_reel, sent, stored):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    assert _set(client, reel.id, "language", {"language": sent}).json()["language"] == stored


def test_language_persists_and_is_returned_by_reel_detail(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "language", {"language": "Tamil"})
    assert client.get(f"/api/reels/{reel.id}").json()["language"] == "Tamil"


def test_language_can_be_changed_again(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "language", {"language": "English"})
    assert _set(client, reel.id, "language", {"language": "Hindi"}).json()["language"] == "Hindi"


@pytest.mark.parametrize("bad", ["", "   ", "Klingon", "xx", "English!!"])
def test_language_rejects_empty_or_unknown(client, make_user, make_reel, db, bad):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "language", {"language": "English"})

    r = _set(client, reel.id, "language", {"language": bad})

    assert r.status_code == 400
    assert "message" in r.json()
    db.refresh(reel)
    assert reel.language == "English"  # a rejected request changes nothing


def test_language_missing_or_null_field_rejected(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    assert _set(client, reel.id, "language", {}).status_code == 400
    assert _set(client, reel.id, "language", {"language": None}).status_code == 400


def test_language_other_user_forbidden(client, make_user, make_reel, db):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    client.login(other)
    r = _set(client, reel.id, "language", {"language": "English"})
    assert r.status_code == 403
    db.refresh(reel)
    assert reel.language is None


def test_language_unknown_video_404(client, make_user):
    client.login(make_user("u"))
    assert _set(client, 9999, "language", {"language": "English"}).status_code == 404


def test_supported_language_list_contains_core_languages():
    langs = vss.supported_languages()
    for expected in ("English", "Hindi", "Telugu", "Tamil", "Kannada", "Malayalam"):
        assert expected in langs


# =========================================================== AI content

def test_ai_content_enable_then_disable(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)

    on = _set(client, reel.id, "ai-content", {"ai_generated": True})
    assert on.status_code == 200
    assert on.json()["ai_generated"] is True
    assert on.json()["video"]["ai_generated"] is True
    assert client.get(f"/api/reels/{reel.id}").json()["ai_generated"] is True

    off = _set(client, reel.id, "ai-content", {"ai_generated": False})
    assert off.json()["ai_generated"] is False
    assert client.get(f"/api/reels/{reel.id}").json()["ai_generated"] is False


def test_ai_content_defaults_false_for_existing_records(client, make_user, make_reel):
    reel = make_reel(make_user("owner"))
    assert client.get(f"/api/reels/{reel.id}").json()["ai_generated"] is False


def test_ai_content_requires_boolean(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    assert _set(client, reel.id, "ai-content", {}).status_code == 400
    assert _set(client, reel.id, "ai-content", {"ai_generated": "maybe"}).status_code == 400


def test_ai_content_other_user_forbidden(client, make_user, make_reel, db):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    client.login(other)
    assert _set(client, reel.id, "ai-content", {"ai_generated": True}).status_code == 403
    db.refresh(reel)
    assert reel.ai_generated is False


# =========================================================== engagement settings

def test_engagement_settings_save_all_three(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)

    r = _set(client, reel.id, "engagement-settings", {
        "comments_enabled": False, "hide_like_count": True, "hide_comments": True,
    })

    assert r.status_code == 200
    body = r.json()
    assert (body["comments_enabled"], body["hide_like_count"], body["hide_comments"]) == (False, True, True)
    video = client.get(f"/api/reels/{reel.id}").json()
    assert (video["comments_enabled"], video["hide_like_count"], video["hide_comments"]) == (False, True, True)


def test_engagement_defaults_for_existing_records(client, make_user, make_reel):
    reel = make_reel(make_user("owner"))
    video = client.get(f"/api/reels/{reel.id}").json()
    assert (video["comments_enabled"], video["hide_like_count"], video["hide_comments"]) == (True, False, False)


def test_engagement_partial_update_keeps_other_switches(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "engagement-settings",
         {"comments_enabled": False, "hide_like_count": True, "hide_comments": True})

    r = _set(client, reel.id, "engagement-settings", {"hide_like_count": False})

    body = r.json()
    assert (body["comments_enabled"], body["hide_like_count"], body["hide_comments"]) == (False, False, True)


def test_engagement_rejects_empty_body_and_nulls(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    assert _set(client, reel.id, "engagement-settings", {}).status_code == 400
    assert _set(client, reel.id, "engagement-settings", {"hide_comments": None}).status_code == 400
    assert _set(client, reel.id, "engagement-settings", {"comments_enabled": "perhaps"}).status_code == 400


def test_engagement_other_user_forbidden(client, make_user, make_reel, db):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    client.login(other)
    r = _set(client, reel.id, "engagement-settings", {"comments_enabled": False})
    assert r.status_code == 403
    db.refresh(reel)
    assert reel.comments_enabled is True


def test_comments_on_allows_new_comment_and_reply(client, make_user, make_reel):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(fan)
    c = client.post(f"/api/reels/{reel.id}/comments", json={"content": "hi"})
    assert c.status_code == 201
    r = client.post(f"/api/comments/{c.json()['id']}/reply", json={"content": "yo"})
    assert r.status_code == 201


def test_comments_off_blocks_new_comments_and_replies_for_everyone(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    existing = _comment(db, reel, fan, "before")
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"comments_enabled": False})

    # nobody — fan or owner — can add a new comment or reply
    for who in (fan, owner):
        client.login(who)
        assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "x"}).status_code == 403
        assert client.post(f"/api/comments/{existing.id}/reply", json={"content": "x"}).status_code == 403

    msg = client.post(f"/api/reels/{reel.id}/comments", json={"content": "x"}).json()["message"]
    assert "turned off" in msg
    # existing comments are kept and still readable
    got = client.get(f"/api/reels/{reel.id}/comments")
    assert got.status_code == 200 and got.json()["total"] == 1

    # switching back on restores commenting
    _set(client, reel.id, "engagement-settings", {"comments_enabled": True})
    client.login(fan)
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "back"}).status_code == 201


def test_comments_off_on_a_reel_does_not_affect_post_comments(client, make_user, make_reel, make_post):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    post = make_post(owner)
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"comments_enabled": False})
    client.login(fan)
    assert client.post(f"/api/posts/{post.id}/comments", json={"content": "ok"}).status_code == 201


def test_hide_like_count_hides_count_from_everyone_but_owner(client, make_user, make_reel, db):
    owner, fan, other = make_user("owner"), make_user("fan"), make_user("other")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_like_count": True})

    client.login(fan)
    liked = client.post("/api/likes", json={"target_type": "reel", "target_id": reel.id})
    assert liked.status_code == 201
    assert liked.json()["likes_count"] is None  # not echoed back to the liker either

    # another viewer, the liker, and an anonymous visitor: count hidden
    for viewer in (other, fan, None):
        client.login(viewer)
        video = client.get(f"/api/reels/{reel.id}").json()
        assert video["likes_count"] is None
        assert video["hide_like_count"] is True
    # the liker still sees their own heart
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["is_liked"] is True

    # who-liked list is owner-only
    assert client.get(f"/api/reels/{reel.id}/likes").status_code == 403
    client.login(owner)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] == 1
    likes = client.get(f"/api/reels/{reel.id}/likes")
    assert likes.status_code == 200 and likes.json()["total"] == 1


def test_hide_like_count_off_shows_count_again(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    db.add(models.Like(user_id=fan.id, target_type=models.LikeTargetType.reel, target_id=reel.id))
    db.commit()
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_like_count": True})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] is None
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_like_count": False})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").json()["likes_count"] == 1
    assert client.get(f"/api/reels/{reel.id}/likes").status_code == 200


def test_hide_like_count_applies_in_feeds(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    db.add(models.Like(user_id=fan.id, target_type=models.LikeTargetType.reel, target_id=reel.id))
    db.commit()
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_like_count": True})
    client.login(fan)
    item = client.get("/api/reels/feed").json()["items"][0]
    assert item["id"] == reel.id and item["likes_count"] is None


def test_like_count_of_other_reels_and_posts_unaffected(client, make_user, make_reel, make_post):
    owner, fan = make_user("owner"), make_user("fan")
    hidden, visible = make_reel(owner), make_reel(owner)
    post = make_post(owner)
    client.login(owner)
    _set(client, hidden.id, "engagement-settings", {"hide_like_count": True})
    client.login(fan)
    assert client.post("/api/likes", json={"target_type": "reel", "target_id": visible.id}).json()["likes_count"] == 1
    assert client.post("/api/likes", json={"target_type": "post", "target_id": post.id}).json()["likes_count"] == 1


def test_hide_comments_hides_thread_from_everyone_but_owner(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    top = _comment(db, reel, fan, "first")
    _comment(db, reel, owner, "thanks", parent=top)
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_comments": True})

    for viewer in (fan, None):
        client.login(viewer)
        assert client.get(f"/api/reels/{reel.id}/comments").status_code == 403
        assert client.get(f"/api/comments/{top.id}/replies").status_code == 403
        # the count itself is still reported on the reel
        assert client.get(f"/api/reels/{reel.id}").json()["comments_count"] >= 1

    client.login(owner)
    assert client.get(f"/api/reels/{reel.id}/comments").json()["total"] == 1
    assert client.get(f"/api/comments/{top.id}/replies").json()["total"] == 1

    # turning it back off restores public reading
    _set(client, reel.id, "engagement-settings", {"hide_comments": False})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}/comments").status_code == 200


def test_hide_comments_still_lets_people_comment(client, make_user, make_reel):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "engagement-settings", {"hide_comments": True})
    client.login(fan)
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "hi"}).status_code == 201


# =========================================================== audience

@pytest.mark.parametrize("value", ["18_plus", "below_18"])
def test_audience_saves_allowed_values(client, make_user, make_reel, value):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    r = _set(client, reel.id, "audience", {"audience": value})
    assert r.status_code == 200
    assert r.json()["audience"] == value
    assert r.json()["video"]["audience"] == value
    assert client.get(f"/api/reels/{reel.id}").json()["audience"] == value


@pytest.mark.parametrize("bad", ["18+", "adult", "everyone", "", "ALL", 18, None])
def test_audience_rejects_anything_else(client, make_user, make_reel, db, bad):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "audience", {"audience": "below_18"})
    r = _set(client, reel.id, "audience", {"audience": bad})
    assert r.status_code == 400
    db.refresh(reel)
    assert reel.audience == "below_18"


def test_audience_unset_for_existing_records(client, make_user, make_reel):
    reel = make_reel(make_user("owner"))
    assert client.get(f"/api/reels/{reel.id}").json()["audience"] is None


def test_audience_other_user_forbidden(client, make_user, make_reel):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    client.login(other)
    assert _set(client, reel.id, "audience", {"audience": "18_plus"}).status_code == 403


# =========================================================== visibility: setting

@pytest.mark.parametrize("value", ["public", "private", "members"])
def test_visibility_saves_allowed_values(client, make_user, make_reel, value):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    r = _set(client, reel.id, "visibility", {"visibility": value})
    assert r.status_code == 200
    assert r.json()["message"] == "Visibility updated"
    assert r.json()["visibility"] == value
    assert r.json()["video"]["visibility"] == value
    assert client.get(f"/api/reels/{reel.id}").json()["visibility"] == value  # owner can always open it


@pytest.mark.parametrize("bad", ["friends", "Public ", "", "only_members", 1, None])
def test_visibility_rejects_invalid_values(client, make_user, make_reel, db, bad):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    r = _set(client, reel.id, "visibility", {"visibility": bad})
    assert r.status_code == 400
    db.refresh(reel)
    assert reel.visibility == "public"


def test_visibility_defaults_public_for_existing_records(client, make_user, make_reel):
    reel = make_reel(make_user("owner"))
    assert client.get(f"/api/reels/{reel.id}").json()["visibility"] == "public"


def test_visibility_other_user_forbidden(client, make_user, make_reel, db):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    client.login(other)
    assert _set(client, reel.id, "visibility", {"visibility": "private"}).status_code == 403
    db.refresh(reel)
    assert reel.visibility == "public"


# =========================================================== visibility: access control

@pytest.mark.parametrize("visibility", ["private", "members"])
def test_restricted_reel_is_hidden_everywhere_from_other_users(
    client, make_user, make_reel, follow, db, visibility
):
    owner, fan, anon_probe = make_user("owner"), make_user("fan"), None
    follow(fan, owner)
    reel = make_reel(owner, caption="secret-caption")
    plain = make_reel(owner, caption="plain")  # a public one for contrast
    _comment(db, reel, owner, "c")
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": visibility})

    for viewer in (fan, anon_probe):
        client.login(viewer)
        rid = reel.id
        assert client.get(f"/api/reels/{rid}").status_code == 404
        assert client.get(f"/api/reels/{rid}/comments").status_code == 404
        assert client.get(f"/api/reels/{rid}/likes").status_code == 404
        assert client.get(f"/api/reels/{rid}/remixes").status_code == 404
        assert client.get(f"/api/reels/{rid}/tags").status_code == 404
        # public landing page renders the generic page, never the reel
        page = client.get(f"/r/{rid}").text
        assert "secret-caption" not in page and "test.mp4" not in page

        feed_ids = [i["id"] for i in client.get("/api/reels/feed").json()["items"]]
        assert rid not in feed_ids and plain.id in feed_ids
        profile_ids = [i["id"] for i in client.get(f"/api/users/{owner.id}/reels").json()["items"]]
        assert rid not in profile_ids and plain.id in profile_ids
        assert client.get(f"/api/users/{owner.id}").json()["reels_count"] == 1

    client.login(fan)
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "x"}).status_code == 404
    assert client.post("/api/likes", json={"target_type": "reel", "target_id": reel.id}).status_code == 404
    assert client.post(f"/api/reels/{reel.id}/save").status_code == 404
    assert client.get(f"/api/share/reels/{reel.id}/link").status_code == 404
    assert client.post("/api/watch/start", json={"reel_id": reel.id}).status_code == 404
    assert client.get("/api/reels/home").json()["total"] == 1  # only the public one

    # the owner still has everything
    client.login(owner)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200
    assert client.get(f"/api/reels/{reel.id}/comments").status_code == 200
    assert client.get(f"/api/users/{owner.id}").json()["reels_count"] == 2
    own = [i["id"] for i in client.get(f"/api/users/{owner.id}/reels").json()["items"]]
    assert reel.id in own
    assert client.get("/api/reels/home").json()["total"] == 2


def test_public_reel_stays_accessible_and_switching_back_restores_access(client, make_user, make_reel):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200

    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "private"})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404

    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "public"})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200
    client.login(None)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200


def test_credited_collaborator_can_still_open_a_private_reel(client, make_user, make_reel, db):
    owner, collab, fan = make_user("owner"), make_user("collab"), make_user("fan")
    reel = make_reel(owner)
    db.add(models.ReelCollaborator(reel_id=reel.id, user_id=collab.id))
    db.commit()
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "private"})
    client.login(collab)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404
    # ...but being a collaborator doesn't grant the right to change settings
    client.login(collab)
    assert _set(client, reel.id, "visibility", {"visibility": "public"}).status_code == 403


def test_members_visibility_fails_closed_without_a_membership_system(client, make_user, make_reel, db):
    """A platform-wide subscription (/api/membership) is not 'member of this
    creator', so even an active subscriber must not unlock members-only."""
    owner, subscriber = make_user("owner"), make_user("sub")
    plan = models.MembershipPlan(name="Pro", price_amount=100)
    db.add(plan)
    db.commit()
    db.add(models.UserMembership(user_id=subscriber.id, plan_id=plan.id, status=models.MembershipStatus.active))
    db.commit()
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "members"})
    client.login(subscriber)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404


def test_private_account_rule_still_applies_to_public_reels(client, make_user, make_reel):
    owner, stranger = make_user("priv", is_private=True), make_user("stranger")
    reel = make_reel(owner)
    client.login(stranger)
    assert client.get(f"/api/reels/{reel.id}").status_code == 403  # unchanged behaviour


def test_trending_excludes_private_reels(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    shown, hidden = make_reel(owner), make_reel(owner)
    for r in (shown, hidden):
        db.add(models.Like(user_id=fan.id, target_type=models.LikeTargetType.reel, target_id=r.id))
    db.commit()
    client.login(owner)
    _set(client, hidden.id, "visibility", {"visibility": "private"})
    client.login(fan)
    ids = [i["id"] for i in client.get("/api/reels/trending").json()["items"]]
    assert shown.id in ids and hidden.id not in ids


def test_audio_and_location_lists_exclude_private_reels(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    audio = models.Audio(title="sound", audio_url="/static/a.mp3")
    place = models.Location(name="Hyderabad")
    db.add_all([audio, place])
    db.commit()
    shown = make_reel(owner, audio_id=audio.id, location_id=place.id)
    hidden = make_reel(owner, audio_id=audio.id, location_id=place.id)
    client.login(owner)
    _set(client, hidden.id, "visibility", {"visibility": "private"})
    client.login(fan)
    a = [i["id"] for i in client.get(f"/api/audio/{audio.id}/reels").json()["items"]]
    l = [i["id"] for i in client.get(f"/api/locations/{place.id}/reels").json()["items"]]
    assert a == [shown.id] and l == [shown.id]


def test_tagged_feed_excludes_private_reels(client, make_user, make_reel, db):
    owner, tagged, fan = make_user("owner"), make_user("tagged"), make_user("fan")
    shown, hidden = make_reel(owner), make_reel(owner)
    for r in (shown, hidden):
        db.add(models.ReelTag(reel_id=r.id, user_id=tagged.id))
    db.commit()
    client.login(owner)
    _set(client, hidden.id, "visibility", {"visibility": "private"})
    client.login(fan)
    items = client.get(f"/api/users/{tagged.id}/tagged").json()["items"]
    assert [i["reel"]["id"] for i in items] == [shown.id]


def test_saved_reel_disappears_when_made_private(client, make_user, make_reel):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(fan)
    assert client.post(f"/api/reels/{reel.id}/save").status_code == 200
    assert client.get(f"/api/users/{fan.id}/saved/reels").json()["total"] == 1
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "private"})
    client.login(fan)
    assert client.get(f"/api/users/{fan.id}/saved/reels").json()["items"] == []
    assert client.get("/api/saved").json()["items"] == []


def test_private_reel_preview_in_chat_is_unavailable(client, make_user, make_reel, db):
    from app.routers.chat_routes import _build_shared_reel_out

    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    assert _build_shared_reel_out(db, reel.id, fan.id).is_available is True
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "private"})
    assert _build_shared_reel_out(db, reel.id, fan.id).is_available is False
    assert _build_shared_reel_out(db, reel.id, owner.id).is_available is True


# =========================================================== schedule

def test_schedule_enable_saves_and_returns_utc(client, make_user, make_reel, db):
    owner = make_user("owner")
    reel = make_reel(owner)
    when = _future(days=3)
    client.login(owner)

    r = _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(when)})

    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "Schedule updated"
    assert body["schedule_enabled"] is True
    assert datetime.fromisoformat(body["scheduled_at"]) == when
    assert body["video"]["schedule_enabled"] is True
    db.refresh(reel)
    assert reel.schedule_enabled is True and reel.scheduled_at == when.replace(tzinfo=None)
    detail = client.get(f"/api/reels/{reel.id}").json()  # owner sees it
    assert detail["schedule_enabled"] is True
    assert datetime.fromisoformat(detail["scheduled_at"]) == when


def test_schedule_timezone_offset_is_converted_to_utc(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    when = _future(days=2)
    ist = timezone(timedelta(hours=5, minutes=30))
    r = _set(client, reel.id, "schedule",
             {"schedule_enabled": True, "scheduled_at": when.astimezone(ist).isoformat()})
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["scheduled_at"]) == when
    assert r.json()["scheduled_at"].endswith("Z")  # explicit UTC marker


def test_schedule_can_be_updated(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(_future(days=1))})
    later = _future(days=9)
    r = _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(later)})
    assert r.status_code == 200
    assert datetime.fromisoformat(r.json()["scheduled_at"]) == later


def test_schedule_disable_with_put(client, make_user, make_reel, db):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(_future(days=1))})
    r = _set(client, reel.id, "schedule", {"schedule_enabled": False})
    assert r.status_code == 200
    assert r.json()["schedule_enabled"] is False and r.json()["scheduled_at"] is None
    db.refresh(reel)
    assert reel.schedule_enabled is False and reel.scheduled_at is None


def test_schedule_delete_clears_it_and_is_idempotent(client, make_user, make_reel, db):
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(_future(days=1))})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404

    client.login(owner)
    first = client.delete(f"/api/videos/{reel.id}/schedule")
    assert first.status_code == 200
    assert first.json()["schedule_enabled"] is False and first.json()["scheduled_at"] is None
    assert client.delete(f"/api/videos/{reel.id}/schedule").status_code == 200  # again: fine

    client.login(fan)  # cancelling the schedule made it live right away
    assert client.get(f"/api/reels/{reel.id}").status_code == 200


@pytest.mark.parametrize("body", [
    {"schedule_enabled": True},
    {"schedule_enabled": True, "scheduled_at": None},
    {"schedule_enabled": True, "scheduled_at": "not-a-date"},
    {"schedule_enabled": True, "scheduled_at": "2026-13-45T99:00:00"},
    {"schedule_enabled": True, "scheduled_at": ""},
    {"scheduled_at": "2999-01-01T00:00:00"},
    {"schedule_enabled": "sometimes", "scheduled_at": "2999-01-01T00:00:00"},
])
def test_schedule_invalid_input_rejected(client, make_user, make_reel, db, body):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    r = _set(client, reel.id, "schedule", body)
    assert r.status_code == 400
    assert "message" in r.json()
    db.refresh(reel)
    assert reel.schedule_enabled is False and reel.scheduled_at is None


def test_schedule_in_the_past_rejected(client, make_user, make_reel, db):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    past = _naive(datetime.now(timezone.utc) - timedelta(minutes=5))
    r = _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": past})
    assert r.status_code == 400
    assert "future" in r.json()["message"]
    db.refresh(reel)
    assert reel.schedule_enabled is False


def test_schedule_out_of_range_rejected_not_500(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    r = _set(client, reel.id, "schedule",
             {"schedule_enabled": True, "scheduled_at": "9999-12-31T23:59:59-05:00"})
    assert r.status_code == 400


def test_schedule_other_user_forbidden_and_anonymous_unauthorized(client, make_user, make_reel, db):
    owner, other = make_user("owner"), make_user("other")
    reel = make_reel(owner)
    body = {"schedule_enabled": True, "scheduled_at": _naive(_future(days=1))}
    client.login(other)
    assert _set(client, reel.id, "schedule", body).status_code == 403
    assert client.delete(f"/api/videos/{reel.id}/schedule").status_code == 403
    client.login(None)
    assert _set(client, reel.id, "schedule", body).status_code == 401
    assert client.delete(f"/api/videos/{reel.id}/schedule").status_code == 401
    db.refresh(reel)
    assert reel.schedule_enabled is False


def test_scheduled_reel_is_not_public_before_its_time(client, make_user, make_reel, follow, db):
    owner, fan = make_user("owner"), make_user("fan")
    follow(fan, owner)
    reel = make_reel(owner, caption="later-caption")
    client.login(owner)
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(_future(days=2))})

    for viewer in (fan, None):
        client.login(viewer)
        rid = reel.id
        assert client.get(f"/api/reels/{rid}").status_code == 404
        assert client.get(f"/api/reels/{rid}/comments").status_code == 404
        assert client.get(f"/api/reels/{rid}/likes").status_code == 404
        assert "later-caption" not in client.get(f"/r/{rid}").text
        assert client.get("/api/reels/feed").json()["total"] == 0
        assert client.get(f"/api/users/{owner.id}/reels").json()["total"] == 0
        assert client.get(f"/api/users/{owner.id}").json()["reels_count"] == 0
    client.login(fan)
    assert client.get("/api/reels/home").json()["total"] == 0
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "x"}).status_code == 404
    assert client.post("/api/likes", json={"target_type": "reel", "target_id": reel.id}).status_code == 404

    client.login(owner)  # the owner can still see and manage it
    assert client.get(f"/api/reels/{reel.id}").status_code == 200
    assert client.get("/api/reels/home").json()["total"] == 1


def test_scheduled_reel_goes_live_when_its_time_arrives(client, make_user, make_reel, follow, monkeypatch):
    owner, fan = make_user("owner"), make_user("fan")
    follow(fan, owner)
    reel = make_reel(owner)
    when = _future(days=2)
    client.login(owner)
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(when)})
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404

    # move the clock to just past the schedule — no worker, no restart needed
    monkeypatch.setattr(vss, "utcnow", lambda: when + timedelta(seconds=1))

    assert client.get(f"/api/reels/{reel.id}").status_code == 200
    assert [i["id"] for i in client.get("/api/reels/feed").json()["items"]] == [reel.id]
    assert client.get("/api/reels/home").json()["total"] == 1
    assert client.get(f"/api/users/{owner.id}/reels").json()["total"] == 1
    assert client.post(f"/api/reels/{reel.id}/comments", json={"content": "first"}).status_code == 201


def test_scheduled_reel_goes_live_according_to_its_visibility(client, make_user, make_reel, monkeypatch):
    """At the scheduled time it becomes visible *per its visibility setting*:
    a private scheduled reel stays hidden from everyone but the owner."""
    owner, fan = make_user("owner"), make_user("fan")
    reel = make_reel(owner)
    when = _future(days=1)
    client.login(owner)
    _set(client, reel.id, "visibility", {"visibility": "private"})
    _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(when)})
    monkeypatch.setattr(vss, "utcnow", lambda: when + timedelta(hours=1))
    client.login(fan)
    assert client.get(f"/api/reels/{reel.id}").status_code == 404
    client.login(owner)
    assert client.get(f"/api/reels/{reel.id}").status_code == 200


def test_schedule_already_fired_cannot_be_rearmed(client, make_user, make_reel, db):
    owner = make_user("owner")
    reel = make_reel(owner, schedule_enabled=True, scheduled_at=datetime.utcnow() - timedelta(days=1))
    client.login(owner)
    r = _set(client, reel.id, "schedule", {"schedule_enabled": True, "scheduled_at": _naive(_future(days=1))})
    assert r.status_code == 409
    # ...but clearing the (finished) schedule is still fine
    assert client.delete(f"/api/videos/{reel.id}/schedule").status_code == 200


# =========================================================== response shape / defaults

NEW_FIELDS = {
    "language", "ai_generated", "comments_enabled", "hide_like_count", "hide_comments",
    "audience", "visibility", "schedule_enabled", "scheduled_at",
}


def test_existing_records_expose_documented_defaults(client, make_user, make_reel):
    reel = make_reel(make_user("owner"))
    body = client.get(f"/api/reels/{reel.id}").json()
    assert NEW_FIELDS <= body.keys()
    assert body["language"] is None
    assert body["ai_generated"] is False
    assert body["comments_enabled"] is True
    assert body["hide_like_count"] is False
    assert body["hide_comments"] is False
    assert body["audience"] is None
    assert body["visibility"] == "public"
    assert body["schedule_enabled"] is False
    assert body["scheduled_at"] is None


def test_new_fields_appear_on_every_reel_listing(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    for url in ("/api/reels/feed", "/api/reels/home", f"/api/users/{owner.id}/reels"):
        item = client.get(url).json()["items"][0]
        assert item["id"] == reel.id and NEW_FIELDS <= item.keys(), url


def test_all_settings_can_be_combined_without_clobbering_each_other(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    when = _future(days=4)
    for endpoint, body in {
        "language": {"language": "Telugu"},
        "ai-content": {"ai_generated": True},
        "engagement-settings": {"comments_enabled": False, "hide_like_count": True, "hide_comments": True},
        "audience": {"audience": "below_18"},
        "visibility": {"visibility": "members"},
        "schedule": {"schedule_enabled": True, "scheduled_at": _naive(when)},
    }.items():
        assert _set(client, reel.id, endpoint, body).status_code == 200

    video = client.get(f"/api/reels/{reel.id}").json()
    assert video["language"] == "Telugu"
    assert video["ai_generated"] is True
    assert (video["comments_enabled"], video["hide_like_count"], video["hide_comments"]) == (False, True, True)
    assert video["audience"] == "below_18"
    assert video["visibility"] == "members"
    assert video["schedule_enabled"] is True
    assert datetime.fromisoformat(video["scheduled_at"]) == when


@pytest.mark.parametrize("endpoint", list(SETTING_ENDPOINTS))
def test_every_setting_endpoint_requires_authentication(client, make_user, make_reel, endpoint):
    reel = make_reel(make_user("owner"))
    client.login(None)
    assert _set(client, reel.id, endpoint, SETTING_ENDPOINTS[endpoint]).status_code == 401


@pytest.mark.parametrize("endpoint", list(SETTING_ENDPOINTS))
def test_every_setting_endpoint_is_owner_only(client, make_user, make_reel, endpoint):
    reel = make_reel(make_user("owner"))
    client.login(make_user("intruder"))
    assert _set(client, reel.id, endpoint, SETTING_ENDPOINTS[endpoint]).status_code == 403


@pytest.mark.parametrize("endpoint", list(SETTING_ENDPOINTS))
def test_every_setting_endpoint_404s_for_unknown_video(client, make_user, endpoint):
    client.login(make_user("u"))
    assert _set(client, 424242, endpoint, SETTING_ENDPOINTS[endpoint]).status_code == 404


@pytest.mark.parametrize("endpoint", list(SETTING_ENDPOINTS))
def test_non_integer_video_id_rejected(client, make_user, endpoint):
    client.login(make_user("u"))
    assert client.put(f"/api/videos/abc/{endpoint}", json=SETTING_ENDPOINTS[endpoint]).status_code in (400, 422)


def test_swagger_documents_the_new_endpoints(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for endpoint in SETTING_ENDPOINTS:
        op = paths[f"/api/videos/{{video_id}}/{endpoint}"]["put"]
        assert op["summary"] and op["responses"]["200"]
    assert "delete" in paths["/api/videos/{video_id}/schedule"]
    assert "ReelDetailOut" in spec["components"]["schemas"]
    assert NEW_FIELDS <= spec["components"]["schemas"]["ReelDetailOut"]["properties"].keys()


# =========================================================== regression

def test_video_upload_still_works_and_gets_defaults(client, make_user, monkeypatch):
    owner = make_user("owner")
    monkeypatch.setattr(video_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/reels/u.mp4", "video"))
    monkeypatch.setattr(video_routes, "generate_video_thumbnail", lambda url: "/static/thumbnails/u.jpg")
    client.login(owner)

    r = client.post(
        "/api/videos/upload",
        files={"file": ("a.mp4", b"fake", "video/mp4")},
        data={"title": "My title", "caption": "My caption"},
    )

    assert r.status_code == 201
    body = r.json()
    assert body["video_url"] == "/static/reels/u.mp4"
    assert body["thumbnail_url"] == "/static/thumbnails/u.jpg"
    assert body["title"] == "My title" and body["caption"] == "My caption"
    assert body["visibility"] == "public" and body["comments_enabled"] is True
    assert body["schedule_enabled"] is False and body["language"] is None


def test_upload_then_configure_then_other_users_see_it_only_when_allowed(client, make_user, monkeypatch):
    """The full upload-page flow: upload, then save each setting."""
    owner, fan = make_user("owner"), make_user("fan")
    monkeypatch.setattr(video_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/reels/u.mp4", "video"))
    monkeypatch.setattr(video_routes, "generate_video_thumbnail", lambda url: None)
    client.login(owner)
    vid = client.post("/api/videos/upload", files={"file": ("a.mp4", b"x", "video/mp4")}).json()["id"]
    assert _set(client, vid, "language", {"language": "Hindi"}).status_code == 200
    assert _set(client, vid, "visibility", {"visibility": "private"}).status_code == 200
    client.login(fan)
    assert client.get(f"/api/reels/{vid}").status_code == 404
    client.login(owner)
    assert _set(client, vid, "visibility", {"visibility": "public"}).status_code == 200
    client.login(fan)
    assert client.get(f"/api/reels/{vid}").json()["language"] == "Hindi"


def test_video_upload_rejects_non_video_still(client, make_user, monkeypatch):
    monkeypatch.setattr(video_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/x.jpg", "image"))
    client.login(make_user("owner"))
    r = client.post("/api/videos/upload", files={"file": ("a.jpg", b"x", "image/jpeg")})
    assert r.status_code == 400


def test_thumbnail_upload_still_works(client, make_user, make_reel, monkeypatch, db):
    owner = make_user("owner")
    reel = make_reel(owner)
    monkeypatch.setattr(video_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/thumbnails/t.jpg", "image"))
    client.login(owner)
    r = client.post(f"/api/videos/{reel.id}/thumbnail", files={"file": ("t.jpg", b"x", "image/jpeg")})
    assert r.status_code == 200
    assert r.json() == {"message": "Thumbnail updated", "thumbnail_url": "/static/thumbnails/t.jpg"}
    db.refresh(reel)
    assert reel.thumbnail_url == "/static/thumbnails/t.jpg"
    # thumbnail ownership rule unchanged
    client.login(make_user("other"))
    assert client.post(f"/api/videos/{reel.id}/thumbnail", files={"file": ("t.jpg", b"x", "image/jpeg")}).status_code == 403


def test_metadata_update_still_works_and_keeps_new_settings(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)
    _set(client, reel.id, "language", {"language": "English"})
    r = client.put(f"/api/videos/{reel.id}/metadata", json={"title": "New", "description": "Desc"})
    assert r.status_code == 200
    assert r.json()["title"] == "New" and r.json()["caption"] == "Desc"
    assert r.json()["language"] == "English"


def test_reels_api_still_works(client, make_user, monkeypatch):
    owner, fan = make_user("owner"), make_user("fan")
    monkeypatch.setattr(content_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/reels/r.mp4", "video"))
    monkeypatch.setattr(content_routes, "get_video_duration", lambda url: 12.5)
    monkeypatch.setattr(content_routes, "generate_video_thumbnail", lambda url: "/static/thumbnails/r.jpg")
    client.login(owner)
    created = client.post("/api/reels", files={"file": ("r.mp4", b"x", "video/mp4")}, data={"caption": "hello"})
    assert created.status_code == 201
    rid = created.json()["id"]
    assert created.json()["visibility"] == "public"

    client.login(fan)
    assert client.get(f"/api/reels/{rid}").json()["caption"] == "hello"
    assert client.get("/api/reels/feed").json()["total"] == 1
    assert client.post(f"/api/reels/{rid}/comments", json={"content": "nice"}).status_code == 201
    liked = client.post("/api/likes", json={"target_type": "reel", "target_id": rid})
    assert liked.status_code == 201 and liked.json()["likes_count"] == 1
    assert client.get(f"/api/reels/{rid}").json()["likes_count"] == 1
    assert client.get(f"/api/reels/{rid}/likes").json()["total"] == 1
    assert client.post(f"/api/reels/{rid}/save").status_code == 200
    assert client.get(f"/api/share/reels/{rid}/link").status_code == 200

    client.login(owner)
    assert client.delete(f"/api/reels/{rid}").status_code == 200
    assert client.get(f"/api/reels/{rid}").status_code == 404


# =========================================================== migration

def _legacy_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE reels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "caption TEXT, video_url VARCHAR(500) NOT NULL)"
        ))
        conn.execute(text("INSERT INTO reels (id, user_id, caption, video_url) VALUES (1, 7, 'old', '/a.mp4')"))
    return engine


def test_migration_adds_columns_with_safe_defaults_for_existing_rows():
    engine = _legacy_engine()
    done = migrate(engine)

    assert any("visibility" in line for line in done)
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT caption, language, ai_generated, comments_enabled, hide_like_count, hide_comments, "
            "audience, visibility, schedule_enabled, scheduled_at FROM reels WHERE id = 1"
        )).one()
    assert tuple(row) == ("old", None, 0, 1, 0, 0, None, "public", 0, None)


def test_migration_is_idempotent():
    engine = _legacy_engine()
    migrate(engine)
    assert migrate(engine) == []


def test_migration_skips_columns_that_already_exist():
    engine = _legacy_engine()
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE reels ADD COLUMN language VARCHAR(50) NULL"))
    done = migrate(engine)
    assert not any("language" in line for line in done)
    assert any("visibility" in line for line in done)
