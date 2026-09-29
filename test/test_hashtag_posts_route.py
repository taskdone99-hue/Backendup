"""
Verification tests for GET /api/hashtags/{name}/posts (app/routers/hashtag_routes.py)
and for the search -> hashtag -> posts flow that GET /api/search?type=hashtags
now feeds.

Uses the shared conftest fixtures, i.e. the real app.main app (real routers and
the real {"message": ...} error handlers) on a per-test in-memory SQLite schema.

Run with:
    pytest test/test_hashtag_posts_route.py -v
"""

from datetime import datetime, timedelta

import pytest

from app import models
from app.services.hashtag_service import sync_post_hashtags

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture()
def tagged_post(db, make_post):
    """Creates a post AND persists its caption hashtags the same way the
    create-post endpoint does (hashtag_service.sync_post_hashtags)."""
    counter = {"n": 0}

    def _make(owner, caption, **kwargs):
        counter["n"] += 1
        # Explicit, distinct timestamps: CURRENT_TIMESTAMP only has 1s resolution,
        # which would make "newest first" ordering assertions flaky.
        kwargs.setdefault("created_at", BASE_TIME + timedelta(minutes=counter["n"]))
        post = make_post(owner, caption=caption, **kwargs)
        sync_post_hashtags(db, post, caption)
        db.commit()
        return post

    return _make


def _ids(resp):
    return [item["id"] for item in resp.json()["items"]]


# ------------------------------------------------------------ basic behavior

def test_lists_only_posts_carrying_the_hashtag_newest_first(client, make_user, tagged_post):
    author = make_user("anjali_r")
    older = tagged_post(author, "first #anjali")
    newer = tagged_post(author, "second #Anjali #wedding")
    tagged_post(author, "unrelated #biryani")

    resp = client.get("/api/hashtags/anjali/posts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2 and body["limit"] == 20 and body["offset"] == 0
    assert _ids(resp) == [newer.id, older.id]
    assert "anjali" in body["items"][0]["hashtags"]


@pytest.mark.parametrize("path_name", ["anjali", "ANJALI", "%23anjali", "%23Anjali"])
def test_name_is_case_and_hash_insensitive(client, make_user, tagged_post, path_name):
    post = tagged_post(make_user(), "#anjali")
    resp = client.get(f"/api/hashtags/{path_name}/posts")
    assert resp.status_code == 200
    assert _ids(resp) == [post.id]


def test_unknown_hashtag_is_404_with_message_body(client):
    resp = client.get("/api/hashtags/nosuchtag/posts")
    assert resp.status_code == 404
    assert resp.json() == {"message": "Hashtag not found"}


def test_hashtag_with_no_posts_is_an_empty_page_not_an_error(client, make_user):
    # Following a hashtag creates it even if nobody has posted under it yet.
    fan = make_user()
    client.login(fan)
    assert client.post("/api/hashtags/emptytag/follow").status_code == 200

    resp = client.get("/api/hashtags/emptytag/posts")

    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "limit": 20, "offset": 0, "items": []}


def test_pagination(client, make_user, tagged_post):
    author = make_user()
    posts = [tagged_post(author, f"post {i} #many") for i in range(3)]
    newest_first = [p.id for p in reversed(posts)]

    page1 = client.get("/api/hashtags/many/posts", params={"limit": 2, "offset": 0})
    page2 = client.get("/api/hashtags/many/posts", params={"limit": 2, "offset": 2})

    assert page1.json()["total"] == page2.json()["total"] == 3
    assert _ids(page1) + _ids(page2) == newest_first
    assert client.get("/api/hashtags/many/posts", params={"limit": 0}).status_code == 400
    assert client.get("/api/hashtags/many/posts", params={"limit": 101}).status_code == 400


# --------------------------------------------------------------- visibility

def test_private_accounts_posts_are_hidden_unless_viewer_is_author_or_follower(
    client, make_user, tagged_post, follow
):
    private_author = make_user("secret", is_private=True)
    public_author = make_user("open")
    follower = make_user("follower")
    stranger = make_user("stranger")
    follow(follower, private_author)
    private_post = tagged_post(private_author, "hidden #vis")
    public_post = tagged_post(public_author, "shown #vis")

    # anonymous
    assert _ids(client.get("/api/hashtags/vis/posts")) == [public_post.id]
    # logged-in non-follower
    client.login(stranger)
    assert _ids(client.get("/api/hashtags/vis/posts")) == [public_post.id]
    # follower sees it
    client.login(follower)
    assert set(_ids(client.get("/api/hashtags/vis/posts"))) == {private_post.id, public_post.id}
    # author sees own
    client.login(private_author)
    assert set(_ids(client.get("/api/hashtags/vis/posts"))) == {private_post.id, public_post.id}


def test_blocked_users_posts_are_hidden_in_both_directions(client, db, make_user, tagged_post):
    blocker = make_user("blocker")
    blocked = make_user("blocked")
    bystander_post = tagged_post(make_user("bystander"), "#blk")
    blocked_post = tagged_post(blocked, "#blk")
    blockers_post = tagged_post(blocker, "#blk")
    db.add(models.UserBlock(blocker_id=blocker.id, blocked_id=blocked.id))
    db.commit()

    client.login(blocker)
    assert set(_ids(client.get("/api/hashtags/blk/posts"))) == {bystander_post.id, blockers_post.id}
    client.login(blocked)
    assert set(_ids(client.get("/api/hashtags/blk/posts"))) == {bystander_post.id, blocked_post.id}


# ----------------------------------------- search -> hashtag -> posts flow

def test_search_result_hashtag_leads_to_its_posts(client, make_user, tagged_post):
    author = make_user("anjali_r")
    post = tagged_post(author, "wedding day #anjaliwedding")
    client.login(make_user("searcher"))

    found = client.get("/api/search", params={"q": "anjali", "type": "hashtags"}).json()
    assert [h["name"] for h in found["items"]] == ["anjaliwedding"]
    assert found["items"][0]["posts_count"] == 1

    # A client takes the name straight from the search hit...
    name = found["items"][0]["name"]
    posts = client.get(f"/api/hashtags/{name}/posts")
    assert posts.status_code == 200
    assert _ids(posts) == [post.id]
    # ...and the search count agrees with what the posts endpoint lists.
    assert posts.json()["total"] == found["items"][0]["posts_count"]

    # The dropdown (type=all) exposes the same hashtag next to users.
    grouped = client.get("/api/search", params={"q": "anjali"}).json()
    assert set(grouped) == {"users", "songs", "locations", "hashtags"}
    assert [h["name"] for h in grouped["hashtags"]] == ["anjaliwedding"]
    assert [u["username"] for u in grouped["users"]] == ["anjali_r"]


def test_search_accepts_hash_prefixed_query_and_matches_the_same_posts(
    client, make_user, tagged_post
):
    post = tagged_post(make_user(), "#hyderabadbiryani")
    client.login(make_user("searcher"))

    hit = client.get("/api/search", params={"q": "#hyderabad", "type": "hashtags"}).json()

    assert [h["name"] for h in hit["items"]] == ["hyderabadbiryani"]
    assert _ids(client.get(f"/api/hashtags/{hit['items'][0]['name']}/posts")) == [post.id]


def test_search_hashtags_requires_login(client):
    # Same auth rule as every other search type.
    assert client.get("/api/search", params={"q": "anjali", "type": "hashtags"}).status_code == 401
