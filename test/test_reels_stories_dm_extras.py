"""
Smoke tests for the second batch of additions:
  Reels: audio details / reels-using-audio / audio trending, reel remixes list
  Stories: Close Friends visibility, archive, drafts
"""
from datetime import datetime, timedelta, timezone

from app import models


# ---------------------------------------------------------------- audio

def test_reel_can_be_created_with_existing_audio(client, db, make_user, make_reel):
    owner = make_user("amy")
    audio = models.Audio(title="Cool Beat", artist="DJ Test", audio_url="/static/audio/1.mp3")
    db.add(audio)
    db.commit()
    db.refresh(audio)

    client.login(owner)
    resp = client.post(
        "/api/reels",
        data={"caption": "test", "audio_id": str(audio.id)},
        files={"file": ("clip.mp4", b"fake video bytes", "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["audio"]["id"] == audio.id


def test_audio_details_and_reels_using_it(client, db, make_user, make_reel):
    owner = make_user("bea")
    audio = models.Audio(title="Song A", artist="Artist", audio_url="/static/audio/2.mp3")
    db.add(audio)
    db.commit()
    db.refresh(audio)

    reel = make_reel(owner, audio_id=audio.id)

    detail = client.get(f"/api/audio/{audio.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["reels_count"] == 1

    using = client.get(f"/api/audio/{audio.id}/reels")
    assert using.status_code == 200, using.text
    ids = [r["id"] for r in using.json()["items"]]
    assert reel.id in ids


def test_audio_trending_ranks_by_usage(client, db, make_user, make_reel):
    owner = make_user("cleo")
    popular = models.Audio(title="Popular", audio_url="/static/audio/3.mp3")
    rare = models.Audio(title="Rare", audio_url="/static/audio/4.mp3")
    db.add_all([popular, rare])
    db.commit()
    db.refresh(popular)
    db.refresh(rare)

    make_reel(owner, audio_id=popular.id)
    make_reel(owner, audio_id=popular.id)
    make_reel(owner, audio_id=rare.id)

    resp = client.get("/api/audio/trending")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items[0]["id"] == popular.id
    assert items[0]["reels_count"] == 2


def test_remix_backfills_audio_and_lists_under_remixes(client, db, make_user, make_reel):
    owner = make_user("dan")
    remixer = make_user("eve")
    original = make_reel(owner)
    assert original.audio_id is None

    client.login(remixer)
    resp = client.post(
        f"/api/reels/{original.id}/audio-remix",
        data={"caption": "my remix"},
        files={"file": ("remix.mp4", b"fake video bytes", "video/mp4")},
    )
    assert resp.status_code == 201, resp.text
    remix_body = resp.json()
    assert remix_body["audio"] is not None

    db.refresh(original)
    assert original.audio_id == remix_body["audio"]["id"]

    remixes = client.get(f"/api/reels/{original.id}/remixes")
    assert remixes.status_code == 200, remixes.text
    ids = [r["id"] for r in remixes.json()["items"]]
    assert remix_body["id"] in ids


# ---------------------------------------------------------------- close friends

def test_close_friends_add_list_remove(client, make_user):
    owner, friend = make_user("frank"), make_user("gwen")
    client.login(owner)

    add = client.post(f"/api/privacy/close-friends/{friend.id}")
    assert add.status_code == 200, add.text
    assert add.json()["is_close_friend"] is True

    listed = client.get("/api/privacy/close-friends")
    assert listed.status_code == 200, listed.text
    assert any(u["id"] == friend.id for u in listed.json()["items"])

    remove = client.delete(f"/api/privacy/close-friends/{friend.id}")
    assert remove.status_code == 200, remove.text
    assert remove.json()["is_close_friend"] is False

    listed_after = client.get("/api/privacy/close-friends")
    assert not any(u["id"] == friend.id for u in listed_after.json()["items"])


def test_close_friends_only_story_hidden_from_non_close_friend(client, db, make_user):
    owner, close_friend, stranger = make_user("hank"), make_user("iris"), make_user("jack")

    client.login(owner)
    client.post(f"/api/privacy/close-friends/{close_friend.id}")

    created = client.post(
        "/api/stories",
        data={"close_friends_only": "true"},
        files={"file": ("s.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert created.status_code == 201, created.text
    story_id = created.json()["id"]
    assert created.json()["close_friends_only"] is True

    client.login(stranger)
    hidden = client.get(f"/api/stories/{story_id}")
    assert hidden.status_code == 404

    client.login(close_friend)
    visible = client.get(f"/api/stories/{story_id}")
    assert visible.status_code == 200, visible.text


def test_close_friends_only_story_filtered_from_feed(client, db, make_user, follow):
    owner, close_friend, follower = make_user("kate"), make_user("liam"), make_user("mona")
    follow(close_friend, owner)
    follow(follower, owner)

    client.login(owner)
    client.post(f"/api/privacy/close-friends/{close_friend.id}")
    client.post(
        "/api/stories",
        data={"close_friends_only": "true"},
        files={"file": ("s.jpg", b"fake image bytes", "image/jpeg")},
    )

    client.login(follower)
    feed = client.get("/api/stories/feed")
    assert feed.status_code == 200, feed.text
    assert feed.json()["items"] == []

    client.login(close_friend)
    feed2 = client.get("/api/stories/feed")
    assert len(feed2.json()["items"]) == 1


# ---------------------------------------------------------------- archive

def test_story_archive_shows_expired_own_stories(client, db, make_user):
    owner = make_user("nora")
    expired = models.Story(
        user_id=owner.id,
        media_url="/static/stories/x.jpg",
        media_type=models.MediaType.image,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(expired)
    db.commit()

    client.login(owner)
    archive = client.get("/api/stories/archive")
    assert archive.status_code == 200, archive.text
    assert archive.json()["total"] == 1

    # Expired story should NOT show up in /mine (active-only)
    mine = client.get("/api/stories/mine")
    assert mine.json()["items"] == []


# ---------------------------------------------------------------- drafts

def test_story_draft_lifecycle(client, make_user):
    owner = make_user("omar")
    client.login(owner)

    created = client.post(
        "/api/stories/drafts",
        data={"caption": "draft caption"},
        files={"file": ("d.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    listed = client.get("/api/stories/drafts")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1

    fetched = client.get(f"/api/stories/drafts/{draft_id}")
    assert fetched.status_code == 200, fetched.text

    published = client.post(f"/api/stories/drafts/{draft_id}/publish")
    assert published.status_code == 201, published.text
    assert published.json()["caption"] == "draft caption"

    gone = client.get(f"/api/stories/drafts/{draft_id}")
    assert gone.status_code == 404

    mine = client.get("/api/stories/mine")
    assert len(mine.json()["items"]) == 1


def test_story_draft_delete(client, make_user):
    owner = make_user("pia")
    client.login(owner)
    created = client.post(
        "/api/stories/drafts",
        data={},
        files={"file": ("d.jpg", b"fake image bytes", "image/jpeg")},
    )
    draft_id = created.json()["id"]

    deleted = client.delete(f"/api/stories/drafts/{draft_id}")
    assert deleted.status_code == 200, deleted.text

    gone = client.get(f"/api/stories/drafts/{draft_id}")
    assert gone.status_code == 404


def test_other_users_cannot_see_your_drafts(client, make_user):
    owner, other = make_user("quinn"), make_user("ruth")
    client.login(owner)
    created = client.post(
        "/api/stories/drafts",
        data={},
        files={"file": ("d.jpg", b"fake image bytes", "image/jpeg")},
    )
    draft_id = created.json()["id"]

    client.login(other)
    resp = client.get(f"/api/stories/drafts/{draft_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------- DM: gallery + item 13 verification

def _open_conversation(client, other_user_id):
    resp = client.post("/api/chat/conversations", json={"participant_ids": [other_user_id]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_conversation_media_gallery_filters_by_type(client, make_user):
    alice, bob = make_user("sam"), make_user("tina")
    client.login(alice)
    conv_id = _open_conversation(client, bob.id)

    text_msg = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "hey"})
    assert text_msg.status_code == 201, text_msg.text

    photo = client.post(
        f"/api/chat/conversations/{conv_id}/media",
        data={"caption": "look at this"},
        files={"file": ("pic.jpg", b"fake image bytes", "image/jpeg")},
    )
    assert photo.status_code == 201, photo.text

    voice = client.post(
        f"/api/chat/conversations/{conv_id}/media",
        files={"file": ("note.mp3", b"fake audio bytes", "audio/mpeg")},
    )
    assert voice.status_code == 201, voice.text

    gallery = client.get(f"/api/chat/conversations/{conv_id}/media")
    assert gallery.status_code == 200, gallery.text
    assert gallery.json()["total"] == 2  # photo + voice note, not the text message
    media_types = {m["media_type"] for m in gallery.json()["items"]}
    assert media_types == {"image", "audio"}

    only_audio = client.get(f"/api/chat/conversations/{conv_id}/media", params={"media_type": "audio"})
    assert only_audio.status_code == 200, only_audio.text
    assert only_audio.json()["total"] == 1
    assert only_audio.json()["items"][0]["media_type"] == "audio"


def test_voice_note_sent_as_audio_media_type(client, make_user):
    alice, bob = make_user("uma"), make_user("vik")
    client.login(alice)
    conv_id = _open_conversation(client, bob.id)

    voice = client.post(
        f"/api/chat/conversations/{conv_id}/media",
        files={"file": ("note.mp3", b"fake audio bytes", "audio/mpeg")},
    )
    assert voice.status_code == 201, voice.text
    assert voice.json()["media_type"] == "audio"


def test_reply_read_delivered_status_flow(client, make_user):
    alice, bob = make_user("wren"), make_user("xavi")
    client.login(alice)
    conv_id = _open_conversation(client, bob.id)

    first = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"content": "hi bob"})
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    # Bob hasn't fetched anything yet -> not delivered from his side.
    assert first.json()["status"] == "sent"

    reply = client.post(
        f"/api/chat/conversations/{conv_id}/messages",
        json={"content": "reply to you", "reply_to_message_id": first_id},
    )
    assert reply.status_code == 201, reply.text
    assert reply.json()["reply_to_message_id"] == first_id
    assert reply.json()["reply_to"]["id"] == first_id

    # Bob fetches messages -> both of alice's messages become "delivered" to him.
    client.login(bob)
    fetched = client.get(f"/api/chat/conversations/{conv_id}/messages")
    assert fetched.status_code == 200, fetched.text

    client.login(alice)
    after_fetch = client.get(f"/api/chat/conversations/{conv_id}/messages")
    statuses = {m["id"]: m["status"] for m in after_fetch.json()["items"]}
    assert statuses[first_id] == "delivered"

    # Bob marks the conversation read -> alice's messages become "read".
    client.login(bob)
    mark_read = client.post(f"/api/chat/conversations/{conv_id}/read")
    assert mark_read.status_code == 200, mark_read.text

    client.login(alice)
    after_read = client.get(f"/api/chat/conversations/{conv_id}/messages")
    statuses = {m["id"]: m["status"] for m in after_read.json()["items"]}
    assert statuses[first_id] == "read"
