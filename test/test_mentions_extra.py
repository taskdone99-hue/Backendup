"""
Additional mention coverage using the shared conftest fixtures (client/db/
make_user), filling gaps left by test_instagram_gaps.py (post/reel/comment
mentions) and test_story_extras.py (story mentions via a standalone harness
that doesn't assert on notifications or the StoryMention table).

- post *comments* (as opposed to reel comments) mentioning a user
- replies to a comment mentioning a user
- story mentions: StoryMention row + notification created
- story self-mention: no row, no notification
"""

from app import models


def _make_post(client, caption="hello world"):
    resp = client.post(
        "/api/posts",
        data={"caption": caption},
        files={"file": ("p.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------- comment / reply mentions


def test_post_comment_mention_creates_notification(client, db, make_user):
    owner, commenter, mentioned = make_user("pcm1owner"), make_user("pcm1"), make_user("pcm1target")

    client.login(owner)
    post_id = _make_post(client)

    client.login(commenter)
    resp = client.post(
        f"/api/posts/{post_id}/comments", json={"content": f"nice @{mentioned.username}"}
    )
    assert resp.status_code == 201, resp.text

    notifs = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == mentioned.id,
            models.Notification.type == models.NotificationType.mention,
        )
        .all()
    )
    assert len(notifs) == 1

    mention_rows = db.query(models.Mention).filter(
        models.Mention.target_type == models.MentionTargetType.comment,
        models.Mention.user_id == mentioned.id,
    ).all()
    assert len(mention_rows) == 1


def test_comment_reply_mention_creates_notification(client, db, make_user):
    owner, commenter, replier, mentioned = (
        make_user("crm1owner"),
        make_user("crm1"),
        make_user("crm1replier"),
        make_user("crm1target"),
    )

    client.login(owner)
    post_id = _make_post(client)

    client.login(commenter)
    comment_id = client.post(
        f"/api/posts/{post_id}/comments", json={"content": "top level"}
    ).json()["id"]

    client.login(replier)
    resp = client.post(
        f"/api/comments/{comment_id}/reply", json={"content": f"totally agree @{mentioned.username}"}
    )
    assert resp.status_code == 201, resp.text

    notifs = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == mentioned.id,
            models.Notification.type == models.NotificationType.mention,
        )
        .all()
    )
    assert len(notifs) == 1


# ---------------------------------------------------------------- story mentions


def test_story_mention_creates_row_and_notification(client, db, make_user):
    author, mentioned = make_user("sm1"), make_user("sm1target")
    client.login(author)

    resp = client.post(
        "/api/stories",
        data={"caption": "at the beach", "mention_user_ids": str(mentioned.id)},
        files={"file": ("s.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["mentions"]) == 1
    assert body["mentions"][0]["user"]["id"] == mentioned.id
    story_id = body["id"]

    row = db.query(models.StoryMention).filter(
        models.StoryMention.story_id == story_id, models.StoryMention.user_id == mentioned.id
    ).first()
    assert row is not None

    notifs = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == mentioned.id,
            models.Notification.type == models.NotificationType.mention,
            models.Notification.target_type == "story",
        )
        .all()
    )
    assert len(notifs) == 1


def test_story_self_mention_not_notified(client, db, make_user):
    author = make_user("sm2")
    client.login(author)

    resp = client.post(
        "/api/stories",
        data={"mention_user_ids": str(author.id)},
        files={"file": ("s.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mentions"] == []

    row = db.query(models.StoryMention).filter(models.StoryMention.user_id == author.id).first()
    assert row is None

    notifs = db.query(models.Notification).filter(models.Notification.user_id == author.id).all()
    assert len(notifs) == 0


def test_story_mention_unknown_user_404(client, make_user):
    author = make_user("sm3")
    client.login(author)

    resp = client.post(
        "/api/stories",
        data={"mention_user_ids": "999999"},
        files={"file": ("s.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert resp.status_code == 404