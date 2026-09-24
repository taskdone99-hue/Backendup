"""
Tests for the user-tag features: tagged feed, tag notifications, block /
privacy / permission checks when tagging, tag controls (approve manually,
who can tag me, hide from profile), story tags, and tag search.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import models


# ----------------------------------------------------------------- helpers

@pytest.fixture()
def make_post(db):
    def _make(owner, caption="a post"):
        post = models.Post(user_id=owner.id, media_url="/static/posts/x.jpg", caption=caption)
        db.add(post)
        db.commit()
        db.refresh(post)
        return post
    return _make


@pytest.fixture()
def make_story(db):
    def _make(owner, **kw):
        story = models.Story(
            user_id=owner.id, media_url="/static/stories/x.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24), **kw,
        )
        db.add(story)
        db.commit()
        db.refresh(story)
        return story
    return _make


@pytest.fixture()
def block(db):
    def _block(blocker, blocked):
        db.add(models.UserBlock(blocker_id=blocker.id, blocked_id=blocked.id))
        db.commit()
    return _block


@pytest.fixture()
def set_tag_settings(db):
    def _set(user, **kw):
        db.add(models.UserTagSettings(user_id=user.id, **kw))
        db.commit()
    return _set


def notifs(db, user, notif_type=None):
    q = db.query(models.Notification).filter(models.Notification.user_id == user.id)
    if notif_type is not None:
        q = q.filter(models.Notification.type == notif_type)
    return q.all()


def tag_body(*user_ids, x=None, y=None):
    return {"tags": [{"user_id": u.id, "x_position": x, "y_position": y} for u in user_ids]}


def tag_count(db, model):
    return db.query(model).count()


# ------------------------------------------------- tagging + notifications

def test_tag_on_post_notifies_the_tagged_user(client, db, make_user, make_post):
    owner, friend = make_user("owner"), make_user("friend")
    post = make_post(owner)
    client.login(owner)

    r = client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend, x=0.2, y=0.4))
    assert r.status_code == 201, r.text
    assert r.json()["tags"][0]["is_approved"] is True

    n = notifs(db, friend, models.NotificationType.tag)
    assert len(n) == 1
    assert n[0].message == "owner tagged you in a post"
    assert (n[0].target_type, n[0].target_id, n[0].actor_id) == ("post", post.id, owner.id)


def test_retagging_same_user_does_not_notify_twice(client, db, make_user, make_post):
    owner, friend = make_user("owner"), make_user("friend")
    post = make_post(owner)
    client.login(owner)
    client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend))
    client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend))
    assert len(notifs(db, friend, models.NotificationType.tag)) == 1
    assert tag_count(db, models.PostTag) == 1


def test_tagging_yourself_is_allowed_and_silent(client, db, make_user, make_post):
    owner = make_user("owner")
    post = make_post(owner)
    client.login(owner)
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(owner)).status_code == 201
    assert notifs(db, owner) == []


def test_tag_notification_respects_mentions_preference(client, db, make_user, make_post):
    owner, friend = make_user("owner"), make_user("friend")
    db.add(models.NotificationPreference(user_id=friend.id, mentions_enabled=False))
    db.commit()
    post = make_post(owner)
    client.login(owner)
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend)).status_code == 201
    assert notifs(db, friend) == []


def test_reel_tag_notifies(client, db, make_user, make_reel):
    owner, friend = make_user("owner"), make_user("friend")
    reel = make_reel(owner)
    client.login(owner)
    assert client.post(f"/api/reels/{reel.id}/tags", json=tag_body(friend)).status_code == 201
    n = notifs(db, friend, models.NotificationType.tag)
    assert n[0].message == "owner tagged you in a reel" and n[0].target_type == "reel"


def test_post_update_tag_user_ids_notifies_only_new_tags(client, db, make_user, make_post):
    owner, a, b = make_user("owner"), make_user("a"), make_user("b")
    post = make_post(owner)
    client.login(owner)

    assert client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [a.id]}).status_code == 200
    assert client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [a.id, b.id]}).status_code == 200
    assert len(notifs(db, a, models.NotificationType.tag)) == 1
    assert len(notifs(db, b, models.NotificationType.tag)) == 1

    # full replace: dropping a removes the tag
    client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [b.id]})
    assert {t.user_id for t in db.query(models.PostTag).all()} == {b.id}


# ------------------------------------------------- block / privacy checks

@pytest.mark.parametrize("who_blocks", ["tagger", "tagged"])
def test_cannot_tag_a_user_with_a_block_either_way(
    client, db, make_user, make_post, block, who_blocks
):
    owner, friend = make_user("owner"), make_user("friend")
    post = make_post(owner)
    if who_blocks == "tagger":
        block(owner, friend)
    else:
        block(friend, owner)
    client.login(owner)

    r = client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend))
    assert r.status_code == 403
    assert tag_count(db, models.PostTag) == 0
    assert notifs(db, friend) == []


def test_tagging_is_all_or_nothing(client, db, make_user, make_post, block):
    owner, ok, blocked = make_user("owner"), make_user("ok"), make_user("blocked")
    block(blocked, owner)
    post = make_post(owner)
    client.login(owner)
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(ok, blocked)).status_code == 403
    assert tag_count(db, models.PostTag) == 0


def test_block_and_permission_denials_are_indistinguishable(
    client, make_user, make_post, block, set_tag_settings
):
    owner, blocker, picky = make_user("owner"), make_user("blocker"), make_user("picky")
    block(blocker, owner)
    set_tag_settings(picky, allow_tags_from=models.TagPermission.no_one)
    post = make_post(owner)
    client.login(owner)
    a = client.post(f"/api/posts/{post.id}/tags", json=tag_body(blocker))
    b = client.post(f"/api/posts/{post.id}/tags", json=tag_body(picky))
    assert a.status_code == b.status_code == 403
    assert a.json() == b.json()


def test_unknown_user_is_404(client, make_user, make_post):
    owner = make_user("owner")
    post = make_post(owner)
    client.login(owner)
    r = client.post(f"/api/posts/{post.id}/tags", json={"tags": [{"user_id": 9999}]})
    assert r.status_code == 404


def test_cannot_tag_someone_who_cant_see_a_private_account_post(
    client, db, make_user, make_post, follow
):
    owner = make_user("owner", is_private=True)
    outsider, follower = make_user("outsider"), make_user("follower")
    follow(follower, owner)
    post = make_post(owner)
    client.login(owner)

    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(outsider)).status_code == 403
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(follower)).status_code == 201


def test_cannot_tag_non_close_friend_on_close_friends_story(
    client, db, make_user, make_story
):
    owner, friend, other = make_user("owner"), make_user("friend"), make_user("other")
    db.add(models.CloseFriend(owner_id=owner.id, friend_id=friend.id))
    db.commit()
    story = make_story(owner, visibility=models.StoryVisibility.close_friends)
    client.login(owner)
    assert client.post(f"/api/stories/{story.id}/tags", json=tag_body(other)).status_code == 403
    assert client.post(f"/api/stories/{story.id}/tags", json=tag_body(friend)).status_code == 201


def test_only_owner_can_tag(client, make_user, make_post, make_reel, make_story):
    owner, other, friend = make_user("owner"), make_user("other"), make_user("friend")
    post, reel, story = make_post(owner), make_reel(owner), make_story(owner)
    client.login(other)
    for path in (f"/api/posts/{post.id}/tags", f"/api/reels/{reel.id}/tags",
                 f"/api/stories/{story.id}/tags"):
        assert client.post(path, json=tag_body(friend)).status_code == 403, path


def test_post_update_with_blocked_tag_is_rejected(client, db, make_user, make_post, block):
    owner, friend = make_user("owner"), make_user("friend")
    block(friend, owner)
    post = make_post(owner)
    client.login(owner)
    assert client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [friend.id]}).status_code == 403
    assert tag_count(db, models.PostTag) == 0


def test_existing_tag_survives_later_block_on_unrelated_edit(
    client, db, make_user, make_post, block
):
    """Re-sending an unchanged tag list must not start failing just because
    the tagged user blocked the owner after being tagged."""
    owner, friend = make_user("owner"), make_user("friend")
    post = make_post(owner)
    client.login(owner)
    client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [friend.id]})
    block(friend, owner)
    assert client.put(f"/api/posts/{post.id}", json={"tag_user_ids": [friend.id]}).status_code == 200


# ------------------------------------------------------------ tag controls

def test_settings_default_update_and_validation(client, make_user):
    me = make_user("me")
    client.login(me)
    assert client.get("/api/tags/settings").json() == {
        "approve_tags_manually": False, "allow_tags_from": "everyone",
    }

    r = client.put("/api/tags/settings", json={"approve_tags_manually": True})
    assert r.json() == {"approve_tags_manually": True, "allow_tags_from": "everyone"}
    r = client.put("/api/tags/settings", json={"allow_tags_from": "following"})
    assert r.json() == {"approve_tags_manually": True, "allow_tags_from": "following"}
    assert client.get("/api/tags/settings").json()["allow_tags_from"] == "following"

    assert client.put("/api/tags/settings", json={}).status_code in (400, 422)
    assert client.put("/api/tags/settings", json={"allow_tags_from": "bots"}).status_code in (400, 422)


def test_settings_require_login(client):
    assert client.get("/api/tags/settings").status_code == 401


def test_allow_tags_from_following_only(client, make_user, make_post, follow, set_tag_settings):
    owner, friend = make_user("owner"), make_user("friend")
    set_tag_settings(friend, allow_tags_from=models.TagPermission.following)
    post = make_post(owner)
    client.login(owner)

    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend)).status_code == 403
    follow(friend, owner)  # friend follows the tagger
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend)).status_code == 201


def test_allow_tags_from_no_one(client, make_user, make_post, follow, set_tag_settings):
    owner, friend = make_user("owner"), make_user("friend")
    set_tag_settings(friend, allow_tags_from=models.TagPermission.no_one)
    follow(friend, owner)
    post = make_post(owner)
    client.login(owner)
    assert client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend)).status_code == 403


def test_manual_approval_flow_on_post(client, db, make_user, make_post, set_tag_settings):
    owner, friend, viewer = make_user("owner"), make_user("friend"), make_user("viewer")
    set_tag_settings(friend, approve_tags_manually=True)
    post = make_post(owner)

    client.login(owner)
    r = client.post(f"/api/posts/{post.id}/tags", json=tag_body(friend))
    assert r.status_code == 201
    assert r.json()["tags"][0]["is_approved"] is False  # owner sees it, flagged
    assert len(notifs(db, friend, models.NotificationType.tag_request)) == 1
    assert notifs(db, friend, models.NotificationType.tag) == []

    # not on the post yet: detail shows no tags, third parties can't see it
    assert client.get(f"/api/posts/{post.id}").json()["tags_count"] == 0
    client.login(viewer)
    assert client.get(f"/api/posts/{post.id}/tags").json()["tags"] == []
    client.login(friend)  # the tagged user can see their own pending tag
    assert len(client.get(f"/api/posts/{post.id}/tags").json()["tags"]) == 1

    pending = client.get("/api/tags/pending").json()
    assert pending["total"] == 1
    item = pending["items"][0]
    assert (item["content_type"], item["content_id"]) == ("post", post.id)
    assert item["owner"]["username"] == "owner" and item["preview_url"] == "/static/posts/x.jpg"

    assert client.post(f"/api/tags/post/{post.id}/approve").status_code == 200
    assert client.post(f"/api/tags/post/{post.id}/approve").status_code == 200  # idempotent
    assert client.get("/api/tags/pending").json()["total"] == 0
    assert client.get(f"/api/posts/{post.id}").json()["tags_count"] == 1
    client.login(viewer)
    assert len(client.get(f"/api/posts/{post.id}/tags").json()["tags"]) == 1


def test_decline_pending_tag_removes_it(client, db, make_user, make_reel, set_tag_settings):
    owner, friend = make_user("owner"), make_user("friend")
    set_tag_settings(friend, approve_tags_manually=True)
    reel = make_reel(owner)
    client.login(owner)
    client.post(f"/api/reels/{reel.id}/tags", json=tag_body(friend))

    client.login(friend)
    assert client.post(f"/api/tags/reel/{reel.id}/decline").status_code == 200
    assert tag_count(db, models.ReelTag) == 0
    assert client.get("/api/tags/pending").json()["total"] == 0
    assert client.post(f"/api/tags/reel/{reel.id}/decline").status_code == 404


def test_owner_tagging_themselves_skips_approval(client, make_user, make_post, set_tag_settings):
    owner = make_user("owner")
    set_tag_settings(owner, approve_tags_manually=True)
    post = make_post(owner)
    client.login(owner)
    r = client.post(f"/api/posts/{post.id}/tags", json=tag_body(owner))
    assert r.json()["tags"][0]["is_approved"] is True


def test_tag_action_endpoints_404_without_a_tag_and_reject_bad_type(client, make_user, make_post):
    me = make_user("me")
    post = make_post(make_user("other"))
    client.login(me)
    for action in ("approve", "decline", "hide"):
        assert client.post(f"/api/tags/post/{post.id}/{action}").status_code == 404
    assert client.delete(f"/api/tags/post/{post.id}/hide").status_code == 404
    assert client.post(f"/api/tags/comment/{post.id}/approve").status_code in (400, 422)


# ---------------------------------------------------------------- stories

def test_story_tags_full_flow(client, db, make_user, make_story, set_tag_settings):
    owner, friend, careful = make_user("owner"), make_user("friend"), make_user("careful")
    set_tag_settings(careful, approve_tags_manually=True)
    story = make_story(owner)

    client.login(owner)
    r = client.post(f"/api/stories/{story.id}/tags", json=tag_body(friend, x=0.5, y=0.5))
    assert r.status_code == 201
    client.post(f"/api/stories/{story.id}/tags", json=tag_body(careful))

    n = notifs(db, friend, models.NotificationType.tag)
    assert n[0].message == "owner tagged you in a story" and n[0].target_type == "story"
    assert len(notifs(db, careful, models.NotificationType.tag_request)) == 1

    # StoryOut carries approved tags only
    from app.routers.story_routes import _to_story_out
    db.refresh(story)
    out = _to_story_out(story, owner.id)
    assert out.tags_count == 1 and out.tags[0].user.username == "friend"
    assert out.tags[0].x_position == 0.5

    tags = client.get(f"/api/stories/{story.id}/tags").json()["tags"]
    assert [t["user"]["username"] for t in tags] == ["friend", "careful"]  # owner sees pending too

    # tagged user can untag themselves; a stranger cannot
    stranger = make_user("stranger")
    client.login(stranger)
    assert client.delete(f"/api/stories/{story.id}/tags/{friend.id}").status_code == 403
    client.login(friend)
    assert client.delete(f"/api/stories/{story.id}/tags/{friend.id}").status_code == 200
    assert client.delete(f"/api/stories/{story.id}/tags/{friend.id}").status_code == 404


def test_expired_story_cannot_be_tagged_and_pending_list_skips_it(
    client, db, make_user, make_story, set_tag_settings
):
    owner, friend = make_user("owner"), make_user("friend")
    set_tag_settings(friend, approve_tags_manually=True)
    story = make_story(owner)
    client.login(owner)
    client.post(f"/api/stories/{story.id}/tags", json=tag_body(friend))
    story.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    assert client.post(f"/api/stories/{story.id}/tags", json=tag_body(friend)).status_code == 404
    client.login(friend)
    assert client.get("/api/tags/pending").json()["total"] == 0


def test_deleting_a_story_removes_its_tags(client, db, make_user, make_story):
    owner, friend = make_user("owner"), make_user("friend")
    story = make_story(owner)
    client.login(owner)
    client.post(f"/api/stories/{story.id}/tags", json=tag_body(friend))
    db.delete(story)
    db.commit()
    assert tag_count(db, models.StoryTag) == 0


# ------------------------------------------------------------ tagged feed

def _tag(db, model, fk, content, user, when=None, **kw):
    t = model(**{fk: content.id}, user_id=user.id, **kw)
    if when:
        t.tagged_at = when
    db.add(t)
    db.commit()
    return t


def test_tagged_feed_mixes_posts_and_reels_newest_first(
    client, db, make_user, make_post, make_reel
):
    me, a = make_user("me"), make_user("a")
    p1, p2, r1 = make_post(a, "old"), make_post(a, "new"), make_reel(a)
    now = datetime.now(timezone.utc)
    _tag(db, models.PostTag, "post_id", p1, me, now - timedelta(days=3))
    _tag(db, models.ReelTag, "reel_id", r1, me, now - timedelta(days=2))
    _tag(db, models.PostTag, "post_id", p2, me, now - timedelta(days=1))

    client.login(me)
    body = client.get(f"/api/users/{me.id}/tagged").json()
    assert body["total"] == 3
    assert [i["content_type"] for i in body["items"]] == ["post", "reel", "post"]
    assert body["items"][0]["post"]["caption"] == "new" and body["items"][0]["reel"] is None
    assert body["items"][1]["reel"]["id"] == r1.id

    page = client.get(f"/api/users/{me.id}/tagged", params={"limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and [i["content_type"] for i in page["items"]] == ["reel"]

    only_reels = client.get(f"/api/users/{me.id}/tagged", params={"content_type": "reel"}).json()
    assert only_reels["total"] == 1


def test_tagged_feed_excludes_pending_hidden_and_own_content(
    client, db, make_user, make_post
):
    me, a = make_user("me"), make_user("a")
    pending, hidden, mine, ok = make_post(a), make_post(a), make_post(me), make_post(a)
    _tag(db, models.PostTag, "post_id", pending, me, is_approved=False)
    _tag(db, models.PostTag, "post_id", hidden, me, hidden_from_profile=True)
    _tag(db, models.PostTag, "post_id", mine, me)
    _tag(db, models.PostTag, "post_id", ok, me)

    client.login(a)  # someone else looking at my profile
    ids = [i["post"]["id"] for i in client.get(f"/api/users/{me.id}/tagged").json()["items"]]
    assert ids == [ok.id]

    client.login(me)
    ids = [i["post"]["id"] for i in client.get(f"/api/users/{me.id}/tagged").json()["items"]]
    assert ids == [ok.id]
    with_hidden = client.get(f"/api/users/{me.id}/tagged", params={"include_hidden": True}).json()
    assert {i["post"]["id"] for i in with_hidden["items"]} == {ok.id, hidden.id}

    # include_hidden is ignored for other people's views
    client.login(a)
    other = client.get(f"/api/users/{me.id}/tagged", params={"include_hidden": True}).json()
    assert [i["post"]["id"] for i in other["items"]] == [ok.id]


def test_hide_and_unhide_from_profile(client, db, make_user, make_post):
    me, a, viewer = make_user("me"), make_user("a"), make_user("viewer")
    post = make_post(a)
    _tag(db, models.PostTag, "post_id", post, me)

    client.login(me)
    assert client.post(f"/api/tags/post/{post.id}/hide").status_code == 200
    client.login(viewer)
    assert client.get(f"/api/users/{me.id}/tagged").json()["total"] == 0
    # the tag itself is still on the post
    assert len(client.get(f"/api/posts/{post.id}/tags").json()["tags"]) == 1

    client.login(me)
    assert client.delete(f"/api/tags/post/{post.id}/hide").status_code == 200
    client.login(viewer)
    assert client.get(f"/api/users/{me.id}/tagged").json()["total"] == 1


def test_tagged_feed_respects_author_privacy(client, db, make_user, make_post, follow):
    me, viewer = make_user("me"), make_user("viewer")
    private_author = make_user("priv", is_private=True)
    public_author = make_user("pub")
    hidden_from_viewer, visible = make_post(private_author), make_post(public_author)
    _tag(db, models.PostTag, "post_id", hidden_from_viewer, me)
    _tag(db, models.PostTag, "post_id", visible, me)

    client.login(viewer)
    body = client.get(f"/api/users/{me.id}/tagged").json()
    assert body["total"] == 1 and body["items"][0]["post"]["id"] == visible.id

    follow(viewer, private_author)
    assert client.get(f"/api/users/{me.id}/tagged").json()["total"] == 2


def test_tagged_feed_private_profile_and_blocks(client, db, make_user, make_post, follow, block):
    private_me = make_user("me", is_private=True)
    outsider, follower, blocked = make_user("out"), make_user("fol"), make_user("blk")
    follow(follower, private_me)
    block(private_me, blocked)

    client.login(outsider)
    assert client.get(f"/api/users/{private_me.id}/tagged").status_code == 403
    client.login(follower)
    assert client.get(f"/api/users/{private_me.id}/tagged").status_code == 200
    client.login(blocked)
    assert client.get(f"/api/users/{private_me.id}/tagged").status_code == 404
    assert client.get("/api/users/9999/tagged").status_code == 404


def test_tagged_feed_hides_content_of_blocked_authors(client, db, make_user, make_post, block):
    me, viewer, author = make_user("me"), make_user("viewer"), make_user("author")
    _tag(db, models.PostTag, "post_id", make_post(author), me)
    block(viewer, author)
    client.login(viewer)
    assert client.get(f"/api/users/{me.id}/tagged").json()["total"] == 0


# ------------------------------------------------------------- tag search

def test_search_matches_names_and_puts_followed_first(client, make_user, follow):
    me = make_user("me")
    aaron, zed, abby = make_user("aaron_z"), make_user("zed_a"), make_user("abby")
    follow(me, zed)
    client.login(me)

    body = client.get("/api/tags/search", params={"q": "a"}).json()
    names = [u["username"] for u in body["items"]]
    assert names[0] == "zed_a"  # followed first
    assert set(names) == {"aaron_z", "zed_a", "abby"} and "me" not in names
    assert body["items"][0]["is_following"] is True and body["items"][1]["is_following"] is False

    assert client.get("/api/tags/search", params={"q": "zzzz"}).json()["total"] == 0
    page = client.get("/api/tags/search", params={"q": "a", "limit": 1, "offset": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_search_excludes_blocked_inactive_and_people_who_wont_be_taggable(
    client, db, make_user, block, follow, set_tag_settings
):
    me = make_user("me")
    ok = make_user("tag_ok")
    blocked_me, i_blocked = make_user("tag_blockedme"), make_user("tag_iblocked")
    inactive = make_user("tag_inactive", is_active=False)
    no_one = make_user("tag_noone")
    following_only, following_only_ok = make_user("tag_fol"), make_user("tag_fol_ok")
    block(blocked_me, me)
    block(me, i_blocked)
    set_tag_settings(no_one, allow_tags_from=models.TagPermission.no_one)
    set_tag_settings(following_only, allow_tags_from=models.TagPermission.following)
    set_tag_settings(following_only_ok, allow_tags_from=models.TagPermission.following)
    follow(following_only_ok, me)

    client.login(me)
    names = {u["username"] for u in client.get("/api/tags/search", params={"q": "tag_"}).json()["items"]}
    assert names == {"tag_ok", "tag_fol_ok"}


def test_search_with_content_filters_to_people_who_can_see_it(
    client, db, make_user, make_post, follow
):
    me = make_user("me", is_private=True)
    follower, outsider = make_user("s_follower"), make_user("s_outsider")
    follow(follower, me)
    post = make_post(me)
    client.login(me)

    plain = {u["username"] for u in client.get("/api/tags/search", params={"q": "s_"}).json()["items"]}
    assert plain == {"s_follower", "s_outsider"}
    scoped = client.get(
        "/api/tags/search", params={"q": "s_", "content_type": "post", "content_id": post.id}
    ).json()
    assert [u["username"] for u in scoped["items"]] == ["s_follower"]


def test_search_content_params_validation(client, make_user, make_post):
    me, other = make_user("me"), make_user("other")
    theirs = make_post(other)
    client.login(me)
    assert client.get("/api/tags/search", params={"q": "x", "content_type": "post"}).status_code == 400
    assert client.get(
        "/api/tags/search", params={"q": "x", "content_type": "post", "content_id": 999}
    ).status_code == 404
    assert client.get(
        "/api/tags/search", params={"q": "x", "content_type": "post", "content_id": theirs.id}
    ).status_code == 403
    assert client.get("/api/tags/search", params={"q": ""}).status_code in (400, 422)


def test_search_requires_login(client):
    assert client.get("/api/tags/search", params={"q": "a"}).status_code == 401


# -------------------------------------------- get-tags privacy (was open)

def test_get_tags_of_private_account_content_needs_access(
    client, db, make_user, make_post, follow
):
    owner = make_user("owner", is_private=True)
    friend, outsider = make_user("friend"), make_user("outsider")
    follow(friend, owner)
    post = make_post(owner)
    _tag(db, models.PostTag, "post_id", post, friend)

    client.login(outsider)
    assert client.get(f"/api/posts/{post.id}/tags").status_code == 403
    client.login(friend)
    assert client.get(f"/api/posts/{post.id}/tags").status_code == 200


# ------------------------------------------- tag people at reel upload

@pytest.fixture()
def upload_reel(client, monkeypatch):
    """POST /api/reels with the file save + ffmpeg helpers stubbed out."""
    from app.routers import content_routes

    monkeypatch.setattr(content_routes, "save_upload_file", lambda f, folder, allow_video=False: ("/static/reels/t.mp4", "video"))
    monkeypatch.setattr(content_routes, "get_video_duration", lambda url: 5.0)
    monkeypatch.setattr(content_routes, "generate_video_thumbnail", lambda url: None)

    def _upload(**data):
        return client.post(
            "/api/reels", data=data, files={"file": ("clip.mp4", b"video", "video/mp4")}
        )
    return _upload


def test_create_reel_with_tags_tags_and_notifies(client, db, make_user, upload_reel):
    owner, a, b = make_user("owner"), make_user("a"), make_user("b")
    client.login(owner)

    r = upload_reel(caption="hi", tag_user_ids=f"{a.id}, {b.id}")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tags_count"] == 2
    assert {t["username"] for t in body["tags"]} == {"a", "b"}
    assert len(notifs(db, a, models.NotificationType.tag)) == 1
    assert notifs(db, b, models.NotificationType.tag)[0].target_id == body["id"]


def test_create_reel_without_tags_unchanged(client, make_user, upload_reel):
    client.login(make_user("owner"))
    r = upload_reel(caption="plain")
    assert r.status_code == 201 and r.json()["tags_count"] == 0


def test_create_reel_with_unTaggable_user_creates_nothing(
    client, db, make_user, block, upload_reel
):
    owner, ok, blocker = make_user("owner"), make_user("ok"), make_user("blocker")
    block(blocker, owner)
    client.login(owner)

    r = upload_reel(tag_user_ids=f"{ok.id},{blocker.id}")
    assert r.status_code == 403
    assert db.query(models.Reel).count() == 0
    assert tag_count(db, models.ReelTag) == 0
    assert notifs(db, ok) == []


def test_create_reel_tag_validation_errors(client, db, make_user, upload_reel):
    client.login(make_user("owner"))
    assert upload_reel(tag_user_ids="abc").status_code == 400
    assert upload_reel(tag_user_ids="9999").status_code == 404
    assert db.query(models.Reel).count() == 0


def test_create_reel_respects_manual_approval(client, db, make_user, set_tag_settings, upload_reel):
    owner, careful = make_user("owner"), make_user("careful")
    set_tag_settings(careful, approve_tags_manually=True)
    client.login(owner)

    r = upload_reel(tag_user_ids=str(careful.id))
    assert r.status_code == 201
    assert r.json()["tags_count"] == 0  # pending, not on the reel yet
    assert len(notifs(db, careful, models.NotificationType.tag_request)) == 1
    client.login(careful)
    assert client.get("/api/tags/pending").json()["total"] == 1
