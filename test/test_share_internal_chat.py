"""
Tests for POST /api/share/internal — the Instagram-style Share sheet:
Post/Reel -> Share -> select person(s)/group -> Send. This is the endpoint
that actually delivers the share as a rich preview card inside chat (never a
raw URL), reusing the same message pipeline as sharing a reel/post from
inside a thread (see test_chat_reel_share.py / test_chat_post_share.py).
"""

import pytest

from app import models
from app.routers import chat_routes


def _internal_share(client, **body):
    return client.post("/api/share/internal", json=body)


def _messages(client, cid):
    items = client.get(f"/api/chat/conversations/{cid}/messages").json()["items"]
    return sorted((m for m in items if not m["is_auto_message"]), key=lambda m: -m["id"])


def _conversations(client):
    return client.get("/api/chat/conversations").json()["items"]


@pytest.fixture()
def group_convo(client):
    """Creates a group conversation as `creator` with the given other users."""
    def _make(creator, *others):
        client.login(creator)
        r = client.post(
            "/api/chat/conversations", json={"participant_ids": [u.id for u in others]}
        )
        assert r.status_code == 201, r.text
        return r.json()["id"]
    return _make


# ------------------------------------------------------- happy paths

def test_share_post_to_new_recipient_creates_1on1_and_message(client, make_user, make_post, follow):
    sender, recipient = make_user("sender"), make_user("recipient")
    follow(recipient, sender)  # so the new 1:1 thread lands in the main inbox, not requests
    post = make_post(sender, caption="hi")

    client.login(sender)
    r = _internal_share(
        client,
        content_type="post",
        content_id=post.id,
        recipient_ids=[recipient.id],
        message="check this out",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["messages"]) == 1
    msg = body["messages"][0]
    assert msg["shared_post_id"] == post.id
    assert msg["shared_reel_id"] is None
    assert msg["content"] == "check this out"
    assert msg["shared_post"]["is_available"] is True
    assert msg["shared_post"]["post_id"] == post.id
    # never just a raw link
    assert "http" not in (msg["content"] or "")

    # engagement log row too
    assert len(body["shares"]) == 1
    assert body["shares"][0]["recipient_id"] == recipient.id
    assert body["shares"][0]["content_type"] == "post"

    # actually landed in the recipient's chat as a real message
    client.login(recipient)
    convos = _conversations(client)
    assert len(convos) == 1
    shared = [m for m in _messages(client, convos[0]["id"]) if m["shared_post"]]
    assert len(shared) == 1 and shared[0]["shared_post"]["post_id"] == post.id


def test_share_reel_reuses_existing_1on1_conversation(client, make_user, make_reel, follow):
    sender, recipient = make_user("sender"), make_user("recipient")
    follow(recipient, sender)
    reel = make_reel(sender)

    client.login(sender)
    existing = client.post(
        "/api/chat/conversations", json={"participant_ids": [recipient.id]}
    )
    cid = existing.json()["id"]

    r = _internal_share(client, content_type="reel", content_id=reel.id, recipient_ids=[recipient.id])
    assert r.status_code == 201, r.text

    client.login(recipient)
    convos = _conversations(client)
    assert len(convos) == 1 and convos[0]["id"] == cid  # no duplicate thread created


def test_share_fans_out_to_multiple_recipients(client, make_user, make_reel, follow):
    sender = make_user("sender")
    bob, carol = make_user("bob"), make_user("carol")
    follow(bob, sender)
    follow(carol, sender)
    reel = make_reel(sender)

    client.login(sender)
    r = _internal_share(
        client, content_type="reel", content_id=reel.id, recipient_ids=[bob.id, carol.id]
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["messages"]) == 2
    assert {s["recipient_id"] for s in r.json()["shares"]} == {bob.id, carol.id}

    for u in (bob, carol):
        client.login(u)
        convos = _conversations(client)
        assert len(convos) == 1
        assert any(m["shared_reel_id"] == reel.id for m in _messages(client, convos[0]["id"]))


def test_share_into_existing_group_conversation(client, make_user, make_post, group_convo):
    sender, bob, carol = make_user("sender"), make_user("bob"), make_user("carol")
    post = make_post(sender)
    cid = group_convo(sender, bob, carol)

    client.login(sender)
    r = _internal_share(client, content_type="post", content_id=post.id, conversation_ids=[cid])
    assert r.status_code == 201, r.text
    assert len(r.json()["messages"]) == 1
    assert r.json()["messages"][0]["conversation_id"] == cid
    # one Share log row per other participant reached
    assert {s["recipient_id"] for s in r.json()["shares"]} == {bob.id, carol.id}

    client.login(bob)
    assert any(m["shared_post_id"] == post.id for m in _messages(client, cid))


def test_share_can_combine_recipients_and_conversations(client, make_user, make_reel, group_convo):
    sender, bob, carol, dave = make_user("sender"), make_user("bob"), make_user("carol"), make_user("dave")
    reel = make_reel(sender)
    group_cid = group_convo(sender, bob, carol)

    client.login(sender)
    r = _internal_share(
        client,
        content_type="reel",
        content_id=reel.id,
        recipient_ids=[dave.id],
        conversation_ids=[group_cid],
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["messages"]) == 2  # one to dave's new 1:1, one to the group


# ------------------------------------------------------- validation / errors

def test_requires_a_recipient_or_conversation(client, make_user, make_post):
    sender = make_user("sender")
    post = make_post(sender)
    client.login(sender)
    r = _internal_share(client, content_type="post", content_id=post.id)
    assert r.status_code in (400, 422)


def test_unknown_content_is_404(client, make_user):
    sender, recipient = make_user("sender"), make_user("recipient")
    client.login(sender)
    r = _internal_share(
        client, content_type="post", content_id=9999, recipient_ids=[recipient.id]
    )
    assert r.status_code == 404


def test_deleted_content_is_404_and_sends_nothing(client, make_user, make_reel, db):
    sender, recipient = make_user("sender"), make_user("recipient")
    reel = make_reel(sender)
    reel_id = reel.id
    db.delete(reel)
    db.commit()

    client.login(sender)
    r = _internal_share(
        client, content_type="reel", content_id=reel_id, recipient_ids=[recipient.id]
    )
    assert r.status_code == 404

    client.login(recipient)
    assert _conversations(client) == []


def test_cannot_share_private_content_you_dont_follow(client, make_user, make_post, make_reel):
    owner = make_user("owner", is_private=True)
    sender, recipient = make_user("sender"), make_user("recipient")
    post = make_post(owner)

    client.login(sender)
    r = _internal_share(
        client, content_type="post", content_id=post.id, recipient_ids=[recipient.id]
    )
    assert r.status_code == 403

    client.login(recipient)
    assert _conversations(client) == []


def test_cannot_share_content_from_a_blocker(client, make_user, make_post, db):
    owner = make_user("owner")
    sender, recipient = make_user("sender"), make_user("recipient")
    post = make_post(owner)
    db.add(models.UserBlock(blocker_id=owner.id, blocked_id=sender.id))
    db.commit()

    client.login(sender)
    r = _internal_share(
        client, content_type="post", content_id=post.id, recipient_ids=[recipient.id]
    )
    assert r.status_code == 404  # same as GET /api/posts/{id} for a blocked viewer


def test_blocked_recipient_rejects_whole_request_atomically(client, make_user, make_reel, db):
    """One good recipient and one blocked recipient in the same call: the
    whole share is rejected, and the good recipient gets nothing — no
    partial fan-out."""
    sender = make_user("sender")
    ok_recipient, blocked_recipient = make_user("ok"), make_user("blocked")
    reel = make_reel(sender)
    db.add(models.UserBlock(blocker_id=blocked_recipient.id, blocked_id=sender.id))
    db.commit()

    client.login(sender)
    r = _internal_share(
        client,
        content_type="reel",
        content_id=reel.id,
        recipient_ids=[ok_recipient.id, blocked_recipient.id],
    )
    assert r.status_code == 403

    client.login(ok_recipient)
    assert _conversations(client) == []


def test_unknown_recipient_is_404(client, make_user, make_post):
    sender = make_user("sender")
    post = make_post(sender)
    client.login(sender)
    r = _internal_share(client, content_type="post", content_id=post.id, recipient_ids=[9999])
    assert r.status_code == 404


def test_unknown_conversation_is_404(client, make_user, make_post):
    sender = make_user("sender")
    post = make_post(sender)
    client.login(sender)
    r = _internal_share(client, content_type="post", content_id=post.id, conversation_ids=[9999])
    assert r.status_code == 404


def test_non_participant_cannot_share_into_a_conversation(client, make_user, make_post, group_convo):
    sender, bob, carol, eve = make_user("sender"), make_user("bob"), make_user("carol"), make_user("eve")
    post = make_post(eve)
    cid = group_convo(bob, carol, sender)  # sender not included

    client.login(eve)
    r = _internal_share(client, content_type="post", content_id=post.id, conversation_ids=[cid])
    assert r.status_code == 403


def test_blocked_participant_in_conversation_rejects_share(client, make_user, make_post, db):
    sender, bob = make_user("sender"), make_user("bob")
    post = make_post(sender)

    client.login(sender)
    cid = client.post("/api/chat/conversations", json={"participant_ids": [bob.id]}).json()["id"]

    db.add(models.UserBlock(blocker_id=bob.id, blocked_id=sender.id))
    db.commit()

    r = _internal_share(client, content_type="post", content_id=post.id, conversation_ids=[cid])
    assert r.status_code == 403


# ------------------------------------------------------- delivery plumbing

def test_dispatches_over_socket_and_notifies_offline_recipient(
    client, make_user, make_reel, monkeypatch
):
    sender, recipient = make_user("sender"), make_user("recipient")
    reel = make_reel(sender)

    sent = []

    async def fake_send(user_ids, payload, exclude=None):
        sent.append((list(user_ids), payload))

    monkeypatch.setattr(chat_routes.manager, "send_to_users", fake_send)

    pushes = []

    async def fake_notify(db_, **kw):
        pushes.append((kw["user_id"], kw["push_body"]))

    monkeypatch.setattr(chat_routes, "notify_user", fake_notify)

    client.login(sender)
    r = _internal_share(client, content_type="reel", content_id=reel.id, recipient_ids=[recipient.id])
    assert r.status_code == 201, r.text

    assert any(
        recipient.id in ids and p["type"] == "message" and p["message"]["shared_reel_id"] == reel.id
        for ids, p in sent
    )
    assert (recipient.id, "Sent a reel") in pushes
