from app import models


def test_explore_all_followed_falls_back(client, make_user, db):
    """Regression: if the viewer follows every author currently eligible
    for Explore, they should still see the same visible posts an
    anonymous viewer would (minus their own), not an empty feed."""
    viewer = make_user("viewer")
    authors = [make_user(f"author{i}") for i in range(4)]
    for i, a in enumerate(authors):
        p = models.Post(user_id=a.id, caption=f"post{i}", media_url=f"/static/p{i}.jpg", media_type=models.MediaType.image)
        db.add(p)
    for a in authors:
        db.add(models.Follow(follower_id=viewer.id, following_id=a.id))
    db.commit()

    r = client.get("/api/posts/explore")
    assert r.status_code == 200
    assert r.json()["total"] == 4

    client.login(viewer)
    r = client.get("/api/posts/explore")
    assert r.status_code == 200
    assert r.json()["total"] == 4, "explore should fall back instead of returning 0 when all authors are followed"


def test_explore_fallback_still_hides_private_and_blocked(client, make_user, db):
    """The fallback path must keep enforcing privacy/visibility rules:
    private (non-followed) accounts and blocked accounts stay hidden even
    when every *visible* author happens to be followed."""
    viewer = make_user("viewer")
    followed_author = make_user("followed_author")
    private_author = make_user("private_author", is_private=True)
    blocked_author = make_user("blocked_author")

    followed_post = models.Post(user_id=followed_author.id, caption="visible", media_url="/static/a.jpg", media_type=models.MediaType.image)
    private_post = models.Post(user_id=private_author.id, caption="private", media_url="/static/b.jpg", media_type=models.MediaType.image)
    blocked_post = models.Post(user_id=blocked_author.id, caption="blocked", media_url="/static/c.jpg", media_type=models.MediaType.image)
    db.add_all([followed_post, private_post, blocked_post])
    db.add(models.Follow(follower_id=viewer.id, following_id=followed_author.id))
    db.add(models.UserBlock(blocker_id=viewer.id, blocked_id=blocked_author.id))
    db.commit()

    client.login(viewer)
    r = client.get("/api/posts/explore")
    assert r.status_code == 200
    ids = {item["id"] for item in r.json()["items"]}
    # followed_author's post is the only visible+eligible one, so the
    # fallback kicks in, but it must never surface the private or blocked
    # author's posts.
    assert followed_post.id in ids
    assert private_post.id not in ids
    assert blocked_post.id not in ids


def test_explore_excludes_own_posts_in_fallback(client, make_user, db):
    """Even in the fallback path, the viewer's own posts stay out of their
    own Explore feed."""
    viewer = make_user("viewer")
    other = make_user("other")
    own_post = models.Post(user_id=viewer.id, caption="mine", media_url="/static/mine.jpg", media_type=models.MediaType.image)
    other_post = models.Post(user_id=other.id, caption="theirs", media_url="/static/theirs.jpg", media_type=models.MediaType.image)
    db.add_all([own_post, other_post])
    db.add(models.Follow(follower_id=viewer.id, following_id=other.id))
    db.commit()

    client.login(viewer)
    r = client.get("/api/posts/explore")
    ids = {item["id"] for item in r.json()["items"]}
    assert own_post.id not in ids
    assert other_post.id in ids


def test_explore_personalization_used_when_non_followed_content_exists(client, make_user, db):
    """When there IS visible content from a non-followed account, the
    normal 'don't already follow' personalization still applies (no
    fallback needed)."""
    viewer = make_user("viewer")
    followed = make_user("followed")
    stranger = make_user("stranger")
    followed_post = models.Post(user_id=followed.id, caption="followed", media_url="/static/f.jpg", media_type=models.MediaType.image)
    stranger_post = models.Post(user_id=stranger.id, caption="stranger", media_url="/static/s.jpg", media_type=models.MediaType.image)
    db.add_all([followed_post, stranger_post])
    db.add(models.Follow(follower_id=viewer.id, following_id=followed.id))
    db.commit()

    client.login(viewer)
    r = client.get("/api/posts/explore")
    ids = {item["id"] for item in r.json()["items"]}
    assert stranger_post.id in ids
    assert followed_post.id not in ids
