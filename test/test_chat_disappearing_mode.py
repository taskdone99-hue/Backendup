"""Tests for disappearing/regular chat modes and bulk message deletion."""
import pytest
from app import models


@pytest.fixture()
def chat_pair(client, make_user):
    alice, bob = make_user("mode_alice"), make_user("mode_bob")
    client.login(alice)
    response = client.post("/api/chat/conversations", json={"participant_ids": [bob.id]})
    assert response.status_code == 201, response.text
    return alice, bob, response.json()["id"]


def _send(client, cid, text):
    return client.post(f"/api/chat/conversations/{cid}/messages", json={"content": text})


def test_new_chat_defaults_to_disappearing(client, chat_pair):
    alice, _, cid = chat_pair
    client.login(alice)
    response = client.get(f"/api/chat/conversations/{cid}/mode")
    assert response.status_code == 200
    assert response.json()["mode"] == "disappearing"


def test_leave_clears_messages_in_disappearing_chat(client, db, chat_pair):
    alice, bob, cid = chat_pair
    client.login(alice)
    sent = _send(client, cid, "temporary")
    assert sent.status_code in (200, 201), sent.text
    message_id = sent.json()["id"]
    response = client.post(f"/api/chat/conversations/{cid}/leave")
    assert response.status_code == 200, response.text
    assert response.json()["messages_cleared"] is True
    assert db.query(models.Message).filter_by(id=message_id).count() == 0


def test_regular_chat_preserves_messages_after_leave(client, db, chat_pair):
    alice, _, cid = chat_pair
    client.login(alice)
    mode = client.put(f"/api/chat/conversations/{cid}/mode", json={"mode": "regular"})
    assert mode.status_code == 200, mode.text
    sent = _send(client, cid, "keep this")
    assert sent.status_code in (200, 201), sent.text
    message_id = sent.json()["id"]
    response = client.post(f"/api/chat/conversations/{cid}/leave")
    assert response.status_code == 200, response.text
    assert response.json()["messages_cleared"] is False
    assert db.query(models.Message).filter_by(id=message_id).count() == 1


def test_bulk_delete_only_deletes_callers_messages(client, db, chat_pair):
    alice, bob, cid = chat_pair
    client.login(alice)
    first = _send(client, cid, "one")
    second = _send(client, cid, "two")
    ids = [first.json()["id"], second.json()["id"]]
    response = client.post(
        "/api/chat/messages/bulk-delete",
        json={"chat_id": cid, "message_ids": ids},
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_count"] == 2
    assert db.query(models.Message).filter(models.Message.id.in_(ids), models.Message.is_deleted.is_(True)).count() == 2


def test_bulk_delete_rejects_messages_from_another_chat(client, chat_pair, make_user):
    alice, _, cid = chat_pair
    other = make_user("not_in_chat")
    client.login(alice)
    sent = _send(client, cid, "mine")
    client.login(other)
    response = client.post(
        "/api/chat/messages/bulk-delete",
        json={"chat_id": cid, "message_ids": [sent.json()["id"]]},
    )
    assert response.status_code == 403
