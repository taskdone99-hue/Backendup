"""
Tests for sharing a post into a chat (POST /api/chat/conversations/{id}/messages
with `shared_post_id`) — the Instagram-style share card instead of pasting a
post URL as text. Mirrors test_chat_reel_share.py; posts and reels share the
same underlying message/preview machinery in chat_routes.py.
"""

import pytest

from app import models
from app.routers import chat_routes


@pytest.fixture()
def convo(client):
    """Creates a conversation as `creator` with the given other users."""
    def _make(creator, *others):
        client.login(creator)
        r = client.post(
            "/api/chat/conversations", json={"participant_ids": [u.id for u in others]}
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]
    return _make


def _send(client, cid, **body):
    return client.post(f"/api/chat/conversations/{cid}/messages", json=body)


def _messages(client, cid):
    """Messages excluding the auto intro DM every new 1:1 conversation starts
    with (its created_at ties with the first real message, so index-based
    lookups on the raw list would be ambiguous), newest id first."""
    items = client.get(f"/api/chat/conversations/{cid}/messages").json()["items"]
    return sorted((m for m in items if not m["is_auto_message"]), key=lambda m: -m["id"])


def test_share_post_with_note(client, make_user, make_post, convo):
    alice, bob, creator = make_user("alice"), make_user("bob"), make_user("creator")
    post = make_post(creator, media_url="/static/p.jpg", caption="sunset")
    cid = convo(alice, bob)

    r = _send(client, cid, shared_post_id=post.id, content="  you'll love this  ")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["content"] == "you'll love this"
    assert body["shared_post_id"] == post.id
    assert body["shared_reel_id"] is None
    card = body["shared_post"]
    assert card["is_available"] is True and card["post_id"] == post.id
    assert card["media_url"] == "/static/p.jpg"
    assert card["caption"] == "sunset"
    assert card["user"]["username"] == "creator"


def test_share_post_without_note_shows_in_history_and_inbox(client, make_user, make_post, convo, db):
    from datetime import datetime, timedelta, timezone

    alice, bob = make_user("alice"), make_user("bob")
    post = make_post(bob)
    cid = convo(alice, bob)
    # created_at has 1s resolution, so the auto intro DM would tie with the
    # message below when picking the inbox's "last message"; backdate it.
    db.query(models.Message).filter(models.Message.conversation_id == cid).update(
        {"created_at": datetime.now(timezone.utc) - timedelta(minutes=5)}
    )
    db.commit()

    r = _send(client, cid, shared_post_id=post.id)
    assert r.status_code == 201 and r.json()["content"] is None

    shared = [m for m in _messages(client, cid) if m["shared_post"]]
    assert len(shared) == 1 and shared[0]["shared_post"]["is_available"]

    last = client.get("/api/chat/conversations").json()["items"][0]["last_message"]
    assert last["shared_post"] is not None and last["shared_post"]["post_id"] == post.id

    # not a media message: keeps out of the media gallery
    assert client.get(f"/api/chat/conversations/{cid}/media").json()["total"] == 0


def test_cannot_share_both_reel_and_post(client, make_user, make_reel, make_post, convo):
    alice, bob = make_user("alice"), make_user("bob")
    reel = make_reel(bob)
    post = make_post(bob)
    cid = convo(alice, bob)
    r = _send(client, cid, shared_reel_id=reel.id, shared_post_id=post.id)
    assert r.status_code in (400, 422)


def test_unknown_post_is_404(client, make_user, convo):
    alice, bob = make_user("alice"), make_user("bob")
    cid = convo(alice, bob)
    assert _send(client, cid, shared_post_id=9999).status_code == 404


def test_cannot_share_post_you_cant_view(client, make_user, make_post, convo, db):
    alice, bob = make_user("alice"), make_user("bob")
    private_owner = make_user("priv", is_private=True)
    blocker = make_user("blocker")
    private_post = make_post(private_owner)
    blocked_post = make_post(blocker)
    db.add(models.UserBlock(blocker_id=blocker.id, blocked_id=alice.id))
    db.commit()
    cid = convo(alice, bob)

    assert _send(client, cid, shared_post_id=private_post.id).status_code == 403
    assert _send(client, cid, shared_post_id=blocked_post.id).status_code == 404
    assert _messages(client, cid) == []  # neither attempt left a message behind


def test_non_participant_cannot_share(client, make_user, make_post, convo):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    post = make_post(bob)
    cid = convo(alice, bob)
    client.login(eve)
    assert _send(client, cid, shared_post_id=post.id).status_code == 403


def test_private_post_preview_is_hidden_from_recipient_who_cant_view_it(
    client, make_user, make_post, follow, convo
):
    sender, recipient = make_user("sender"), make_user("recipient")
    private_owner = make_user("priv", is_private=True)
    follow(sender, private_owner)  # sender may view it, recipient may not
    post = make_post(private_owner, caption="secret")
    cid = convo(sender, recipient)

    sent = _send(client, cid, shared_post_id=post.id).json()
    assert sent["shared_post"]["is_available"] is True

    client.login(recipient)
    card = _messages(client, cid)[0]["shared_post"]
    assert card == {
        "post_id": post.id, "is_available": False, "media_url": None,
        "media_type": None, "caption": None, "user": None,
    }

    # once they follow the owner, the same message resolves
    follow(recipient, private_owner)
    card = _messages(client, cid)[0]["shared_post"]
    assert card["is_available"] is True and card["caption"] == "secret"


def test_deleted_post_leaves_message_as_unavailable_card(client, make_user, make_post, convo, db):
    alice, bob, owner = make_user("alice"), make_user("bob"), make_user("owner")
    post = make_post(owner)
    cid = convo(alice, bob)
    _send(client, cid, shared_post_id=post.id, content="look")

    # deleted straight from the DB, same effect as DELETE /api/posts/{id}
    db.delete(db.query(models.Post).filter(models.Post.id == post.id).first())
    db.commit()

    client.login(bob)
    msg = _messages(client, cid)[0]
    assert msg["content"] == "look" and msg["shared_post_id"] == post.id
    assert msg["shared_post"]["is_available"] is False


def test_unsent_share_hides_the_post(client, make_user, make_post, convo):
    alice, bob = make_user("alice"), make_user("bob")
    post = make_post(bob)
    cid = convo(alice, bob)
    mid = _send(client, cid, shared_post_id=post.id).json()["id"]

    assert client.delete(f"/api/chat/messages/{mid}").status_code == 200
    msg = next(m for m in _messages(client, cid) if m["id"] == mid)
    assert msg["shared_post"] is None and msg["shared_post_id"] is None
    assert msg["is_deleted"] is True


def test_reply_quote_flags_post_share(client, make_user, make_post, convo):
    alice, bob = make_user("alice"), make_user("bob")
    post = make_post(bob)
    cid = convo(alice, bob)
    mid = _send(client, cid, shared_post_id=post.id).json()["id"]

    client.login(bob)
    reply = _send(client, cid, content="lol", reply_to_message_id=mid).json()
    assert reply["reply_to"]["is_post_share"] is True
    assert reply["reply_to"]["is_reel_share"] is False


def test_socket_payloads_and_notification_preview(client, make_user, make_post, follow, convo, monkeypatch, db):
    """Group chat: one recipient can see the private post, one can't — each
    must get their own socket payload. Offline recipients also get a push
    preview of "Sent a post"."""
    sender, can_see, cannot_see = make_user("sender"), make_user("cansee"), make_user("cantsee")
    owner = make_user("priv", is_private=True)
    follow(sender, owner)
    follow(can_see, owner)
    post = make_post(owner)
    cid = convo(sender, can_see, cannot_see)

    sent = []

    async def fake_send(user_ids, payload, exclude=None):
        sent.append((list(user_ids), payload))

    monkeypatch.setattr(chat_routes.manager, "send_to_users", fake_send)

    pushes = []

    async def fake_notify(db_, **kw):
        pushes.append((kw["user_id"], kw["push_body"]))

    monkeypatch.setattr(chat_routes, "notify_user", fake_notify)

    assert _send(client, cid, shared_post_id=post.id).status_code == 201

    by_user = {ids[0]: p["message"]["shared_post"] for ids, p in sent if p["type"] == "message"}
    assert set(by_user) == {can_see.id, cannot_see.id}
    assert by_user[can_see.id]["is_available"] is True
    assert by_user[cannot_see.id]["is_available"] is False
    assert by_user[cannot_see.id]["media_url"] is None

    assert {(uid, body) for uid, body in pushes} == {
        (can_see.id, "Sent a post"), (cannot_see.id, "Sent a post"),
    }
