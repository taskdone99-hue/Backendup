"""
DELETE /api/reels/{reel_id} — the Home reel delete.

The regression these cover: a reel that any other table still pointed at
couldn't be deleted at all. The delete handler cleared some of those
references but not all of them, so the final DELETE hit a foreign key
violation and surfaced as a 500. The tests below delete a reel with every
kind of related record attached and assert both the 200 and the state of
each related table afterwards.

Note these only mean anything with foreign keys actually enforced — see the
PRAGMA in conftest.py.
"""

from datetime import datetime, timezone

import pytest

from app import models


def _error(response):
    body = response.json()
    return body.get("message", body.get("detail", ""))


def _delete(client, reel_id):
    return client.delete(f"/api/reels/{reel_id}")


# ------------------------------------------------------------ permissions


def test_owner_can_delete(client, db, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)

    response = _delete(client, reel.id)

    assert response.status_code == 200, response.text
    assert db.query(models.Reel).filter(models.Reel.id == reel.id).first() is None


def test_non_owner_gets_403(client, db, make_user, make_reel):
    owner, stranger = make_user("owner"), make_user("stranger")
    reel = make_reel(owner)
    client.login(stranger)

    response = _delete(client, reel.id)

    assert response.status_code == 403
    assert "your own" in _error(response).lower()
    # ...and the reel is untouched.
    assert db.query(models.Reel).filter(models.Reel.id == reel.id).first() is not None


def test_tagged_collaborator_cannot_delete(client, db, make_user, make_reel):
    """Being a co-creator on a reel is not ownership of it."""
    owner, collaborator = make_user("owner"), make_user("collab")
    reel = make_reel(owner)
    db.add(models.ReelCollaborator(reel_id=reel.id, user_id=collaborator.id))
    db.commit()
    client.login(collaborator)

    assert _delete(client, reel.id).status_code == 403


def test_missing_reel_returns_404(client, db, make_user):
    client.login(make_user("owner"))

    response = _delete(client, 999_999)

    assert response.status_code == 404
    assert "not found" in _error(response).lower()


def test_double_delete_returns_404(client, db, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200
    assert _delete(client, reel.id).status_code == 404


def test_missing_reel_404s_before_the_ownership_check(client, db, make_user):
    """A stranger asking for a reel that doesn't exist gets 404, not 403."""
    client.login(make_user("stranger"))

    assert _delete(client, 999_999).status_code == 404


def test_anonymous_cannot_delete(client, db, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)

    response = _delete(client, reel.id)

    assert response.status_code == 401
    assert db.query(models.Reel).filter(models.Reel.id == reel.id).first() is not None


# ------------------------------------------------------ the 500 regression


def test_delete_reel_referenced_by_a_collaboration_request(
    client, db, make_user, make_reel
):
    """The reference that was 500ing: CreatorCollaborationRequest.reel_id is a
    real foreign key into reels, and nothing was clearing it."""
    owner, partner = make_user("owner"), make_user("partner")
    reel = make_reel(owner)
    request = models.CreatorCollaborationRequest(
        requester_id=owner.id,
        partner_id=partner.id,
        reel_id=reel.id,
        status=models.CollaborationStatus.pending,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    client.login(owner)

    response = _delete(client, reel.id)

    assert response.status_code == 200, response.text
    db.expire_all()
    # The request survives with its reel reference cleared, and is cancelled
    # because the reel it proposed work on no longer exists.
    refreshed = db.query(models.CreatorCollaborationRequest).get(request.id)
    assert refreshed is not None
    assert refreshed.reel_id is None
    assert refreshed.status == models.CollaborationStatus.cancelled
    assert refreshed.responded_at is not None


def test_accepted_collaboration_request_keeps_its_status(client, db, make_user, make_reel):
    """An accepted request may already have paid out a CreatorEarning, so the
    audit trail is preserved — only the reel pointer is cleared."""
    owner, partner = make_user("owner"), make_user("partner")
    reel = make_reel(owner)
    request = models.CreatorCollaborationRequest(
        requester_id=owner.id,
        partner_id=partner.id,
        reel_id=reel.id,
        status=models.CollaborationStatus.accepted,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200

    db.expire_all()
    refreshed = db.query(models.CreatorCollaborationRequest).get(request.id)
    assert refreshed.status == models.CollaborationStatus.accepted
    assert refreshed.reel_id is None


def test_delete_reel_with_every_related_record(client, db, make_user, make_reel):
    """The full gauntlet: one reel carrying every kind of related row at once."""
    owner, viewer = make_user("owner"), make_user("viewer")
    reel = make_reel(owner)

    comment = models.Comment(user_id=viewer.id, reel_id=reel.id, content="nice")
    db.add(comment)
    db.commit()
    db.refresh(comment)

    db.add_all(
        [
            models.Comment(
                user_id=owner.id, reel_id=reel.id, content="thanks", parent_id=comment.id
            ),
            models.Like(
                user_id=viewer.id,
                target_type=models.LikeTargetType.reel,
                target_id=reel.id,
            ),
            models.Like(
                user_id=owner.id,
                target_type=models.LikeTargetType.comment,
                target_id=comment.id,
            ),
            models.ReelCollaborator(reel_id=reel.id, user_id=viewer.id),
            models.ReelRevenueShare(reel_id=reel.id, user_id=viewer.id, percentage=20),
            models.WatchSession(
                user_id=viewer.id, reel_id=reel.id, started_at=datetime.now(timezone.utc)
            ),
            models.SavedItem(
                user_id=viewer.id,
                target_type=models.SavedItemType.reel,
                target_id=reel.id,
            ),
            models.CreatorCollaborationRequest(
                requester_id=owner.id,
                partner_id=viewer.id,
                reel_id=reel.id,
                status=models.CollaborationStatus.pending,
            ),
        ]
    )
    db.commit()
    client.login(owner)

    response = _delete(client, reel.id)

    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.query(models.Reel).filter(models.Reel.id == reel.id).count() == 0
    assert db.query(models.Comment).filter(models.Comment.reel_id == reel.id).count() == 0
    assert db.query(models.Like).count() == 0
    assert db.query(models.ReelCollaborator).count() == 0
    assert db.query(models.ReelRevenueShare).count() == 0
    assert db.query(models.WatchSession).count() == 0
    assert db.query(models.SavedItem).count() == 0


def test_delete_reel_in_a_series(client, db, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(owner)
    series = models.Series(user_id=owner.id, title="My series")
    db.add(series)
    db.commit()
    db.refresh(series)
    db.add(models.SeriesReel(series_id=series.id, reel_id=reel.id, position=1))
    db.commit()
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200

    db.expire_all()
    assert db.query(models.SeriesReel).count() == 0
    # The series itself outlives the reel.
    assert db.query(models.Series).filter(models.Series.id == series.id).count() == 1


def test_audio_survives_its_source_reel(client, db, make_user, make_reel):
    """A bookmarked sound shouldn't vanish because the reel it came from did."""
    owner = make_user("owner")
    reel = make_reel(owner)
    audio = models.Audio(title="a sound", audio_url="/static/audio/a.m4a", source_reel_id=reel.id)
    db.add(audio)
    db.commit()
    db.refresh(audio)
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200

    db.expire_all()
    refreshed = db.query(models.Audio).get(audio.id)
    assert refreshed is not None
    assert refreshed.source_reel_id is None


def test_saved_collection_entries_are_cleaned_up(client, db, make_user, make_reel):
    """A bookmark in a folder shouldn't survive as an unrenderable blank."""
    owner, viewer = make_user("owner"), make_user("viewer")
    reel = make_reel(owner)
    saved = models.SavedItem(
        user_id=viewer.id, target_type=models.SavedItemType.reel, target_id=reel.id
    )
    collection = models.SavedCollection(user_id=viewer.id, name="Later")
    db.add_all([saved, collection])
    db.commit()
    db.refresh(saved)
    db.refresh(collection)
    db.add(
        models.SavedCollectionItem(collection_id=collection.id, saved_item_id=saved.id)
    )
    db.commit()
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200

    db.expire_all()
    assert db.query(models.SavedItem).count() == 0
    assert db.query(models.SavedCollectionItem).count() == 0
    # The folder itself stays.
    assert db.query(models.SavedCollection).count() == 1


def test_other_reels_are_untouched(client, db, make_user, make_reel):
    """Cleanup is scoped to the reel being deleted, not the whole table."""
    owner, viewer = make_user("owner"), make_user("viewer")
    doomed, keeper = make_reel(owner), make_reel(owner)
    db.add_all(
        [
            models.Like(
                user_id=viewer.id,
                target_type=models.LikeTargetType.reel,
                target_id=keeper.id,
            ),
            models.WatchSession(
                user_id=viewer.id, reel_id=keeper.id, started_at=datetime.now(timezone.utc)
            ),
            models.SavedItem(
                user_id=viewer.id,
                target_type=models.SavedItemType.reel,
                target_id=keeper.id,
            ),
            models.Comment(user_id=viewer.id, reel_id=keeper.id, content="keep me"),
        ]
    )
    db.commit()
    client.login(owner)

    assert _delete(client, doomed.id).status_code == 200

    db.expire_all()
    assert db.query(models.Reel).filter(models.Reel.id == keeper.id).count() == 1
    assert db.query(models.Like).count() == 1
    assert db.query(models.WatchSession).count() == 1
    assert db.query(models.SavedItem).count() == 1
    assert db.query(models.Comment).count() == 1


def test_failed_delete_leaves_the_reel_intact(client, db, make_user, make_reel, monkeypatch):
    """If the final DELETE still fails, the cleanup already done must not be
    committed — otherwise the reel survives with its likes and comments
    stripped off it."""
    from sqlalchemy.exc import IntegrityError

    from app.routers import content_routes

    owner, viewer = make_user("owner"), make_user("viewer")
    reel = make_reel(owner)
    db.add(
        models.Like(
            user_id=viewer.id,
            target_type=models.LikeTargetType.reel,
            target_id=reel.id,
        )
    )
    db.commit()
    client.login(owner)

    real_commit = db.commit

    def _boom():
        raise IntegrityError("simulated", None, Exception("FK still referenced"))

    monkeypatch.setattr(db, "commit", _boom)
    response = _delete(client, reel.id)
    monkeypatch.setattr(db, "commit", real_commit)

    assert response.status_code == 409
    db.expire_all()
    assert db.query(models.Reel).filter(models.Reel.id == reel.id).count() == 1
    assert db.query(models.Like).count() == 1


def test_media_files_are_removed(client, db, make_user, make_reel, monkeypatch):
    owner = make_user("owner")
    reel = make_reel(owner, video_url="/static/reels/x.mp4", thumbnail_url="/static/t.jpg")
    removed = []

    from app.routers import content_routes

    monkeypatch.setattr(content_routes, "delete_media_file", removed.append)
    client.login(owner)

    assert _delete(client, reel.id).status_code == 200
    assert removed == ["/static/reels/x.mp4", "/static/t.jpg"]


def test_media_files_are_kept_when_the_delete_fails(
    client, db, make_user, make_reel, monkeypatch
):
    """Unlinking the video before the row is gone would leave a reel that
    still lists but can't play."""
    from sqlalchemy.exc import IntegrityError

    from app.routers import content_routes

    owner = make_user("owner")
    reel = make_reel(owner)
    removed = []
    monkeypatch.setattr(content_routes, "delete_media_file", removed.append)
    monkeypatch.setattr(
        db, "commit", lambda: (_ for _ in ()).throw(IntegrityError("x", None, Exception()))
    )
    client.login(owner)

    assert _delete(client, reel.id).status_code == 409
    assert removed == []
