"""
Creator-to-creator collaboration requests
(POST/GET /api/creator-collaborations and the accept/reject/cancel actions).

Covers the rules in app/services/collaboration_service.py: who may be sent a
request (public vs private vs blocked), one open request per pair, the
notification fan-out on create, and the accept/reject lifecycle.
"""

from app import models

ENDPOINT = "/api/creator-collaborations"


def _create(client, partner_id, **extra):
    payload = {"partner_user_id": partner_id, "message": "want to collab?"}
    payload.update(extra)
    return client.post(ENDPOINT, json=payload)


def _error(response):
    """The app replaces FastAPI's {"detail": ...} envelope with {"message": ...}
    (see the exception handlers in app/main.py)."""
    body = response.json()
    return body.get("message", body.get("detail", ""))


def _notifications(db, user_id, notif_type):
    return (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == user_id,
            models.Notification.type == notif_type,
        )
        .all()
    )


# ---------------------------------------------------------------- public


def test_public_partner_needs_no_follow(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)

    response = _create(client, bob.id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["requester"]["id"] == alice.id
    assert body["partner"]["id"] == bob.id


def test_create_notifies_the_partner(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)

    request_id = _create(client, bob.id).json()["id"]

    notifications = _notifications(
        db, bob.id, models.NotificationType.collaboration_request
    )
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.actor_id == alice.id
    assert notification.target_type == "collab_request"
    assert notification.target_id == request_id
    assert alice.username in notification.message
    # target_type is a String(20) column — anything longer is silently
    # truncated on MySQL, so the value has to stay short.
    assert len(notification.target_type) <= 20


def test_requester_is_not_notified_of_their_own_request(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)

    _create(client, bob.id)

    assert _notifications(db, alice.id, models.NotificationType.collaboration_request) == []


# --------------------------------------------------------------- private


def test_private_partner_rejects_non_follower(client, db, make_user):
    alice = make_user("alice")
    priv = make_user("priv", is_private=True)
    client.login(alice)

    response = _create(client, priv.id)

    assert response.status_code == 403
    assert "private" in _error(response).lower()


def test_private_partner_rejects_pending_follow_request(client, db, make_user):
    """A follow request that hasn't been approved yet is not access."""
    alice = make_user("alice")
    priv = make_user("priv", is_private=True)
    db.add(models.FollowRequest(requester_id=alice.id, target_id=priv.id))
    db.commit()
    client.login(alice)

    assert _create(client, priv.id).status_code == 403


def test_private_partner_accepts_approved_follower(client, db, make_user, follow):
    alice = make_user("alice")
    priv = make_user("priv", is_private=True)
    follow(alice, priv)
    client.login(alice)

    response = _create(client, priv.id)

    assert response.status_code == 201, response.text
    assert len(_notifications(db, priv.id, models.NotificationType.collaboration_request)) == 1


def test_following_the_wrong_way_round_does_not_grant_access(client, db, make_user, follow):
    """The private user following the requester doesn't let the requester in."""
    alice = make_user("alice")
    priv = make_user("priv", is_private=True)
    follow(priv, alice)
    client.login(alice)

    assert _create(client, priv.id).status_code == 403


# ------------------------------------------------------- self / blocked


def test_cannot_request_yourself(client, db, make_user):
    alice = make_user("alice")
    client.login(alice)

    response = _create(client, alice.id)

    assert response.status_code == 400
    assert "yourself" in _error(response).lower()


def test_unknown_partner_returns_404(client, db, make_user):
    alice = make_user("alice")
    client.login(alice)

    assert _create(client, 999_999).status_code == 404


def test_inactive_partner_returns_404(client, db, make_user):
    alice = make_user("alice")
    gone = make_user("gone", is_active=False)
    client.login(alice)

    assert _create(client, gone.id).status_code == 404


def test_blocked_partner_is_indistinguishable_from_missing(client, db, make_user):
    """404, not 403 — a 403 here would confirm the block exists."""
    alice, blocker = make_user("alice"), make_user("blocker")
    db.add(models.UserBlock(blocker_id=blocker.id, blocked_id=alice.id))
    db.commit()
    client.login(alice)

    response = _create(client, blocker.id)

    assert response.status_code == 404
    assert _error(response) == "Partner user not found"


def test_blocking_works_in_either_direction(client, db, make_user):
    alice, other = make_user("alice"), make_user("other")
    db.add(models.UserBlock(blocker_id=alice.id, blocked_id=other.id))
    db.commit()
    client.login(alice)

    assert _create(client, other.id).status_code == 404


# ------------------------------------------------------------- duplicates


def test_duplicate_request_rejected(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    assert _create(client, bob.id).status_code == 201

    response = _create(client, bob.id)

    assert response.status_code == 409
    assert "pending" in _error(response).lower()


def test_reverse_duplicate_rejected(client, db, make_user):
    """If bob already invited alice, alice can't open a competing request."""
    alice, bob = make_user("alice"), make_user("bob")
    client.login(bob)
    assert _create(client, alice.id).status_code == 201

    client.login(alice)
    response = _create(client, bob.id)

    assert response.status_code == 409
    assert "respond to that one" in _error(response).lower()


def test_only_one_notification_survives_a_duplicate_attempt(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    _create(client, bob.id)
    _create(client, bob.id)

    assert len(_notifications(db, bob.id, models.NotificationType.collaboration_request)) == 1


def test_rejected_request_does_not_block_a_new_one(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/reject")

    client.login(alice)
    assert _create(client, bob.id).status_code == 201


def test_cancelled_request_does_not_block_a_new_one(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.post(f"{ENDPOINT}/{request_id}/cancel")

    assert _create(client, bob.id).status_code == 201


# ------------------------------------------------------------ reel-scoped


def test_reel_scoped_request_requires_owning_the_reel(client, db, make_user, make_reel):
    alice, bob = make_user("alice"), make_user("bob")
    bobs_reel = make_reel(bob)
    client.login(alice)

    response = _create(client, bob.id, reel_id=bobs_reel.id)

    assert response.status_code == 403
    assert "own" in _error(response).lower()


def test_missing_reel_returns_404(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)

    assert _create(client, bob.id, reel_id=999_999).status_code == 404


def test_general_and_reel_scoped_requests_coexist(client, db, make_user, make_reel):
    alice, bob = make_user("alice"), make_user("bob")
    reel = make_reel(alice)
    client.login(alice)

    assert _create(client, bob.id, reel_id=reel.id).status_code == 201
    assert _create(client, bob.id).status_code == 201
    # ...but a second request at the same scope is still a duplicate.
    assert _create(client, bob.id, reel_id=reel.id).status_code == 409


def test_cannot_re_invite_an_existing_reel_collaborator(client, db, make_user, make_reel):
    alice, bob = make_user("alice"), make_user("bob")
    reel = make_reel(alice)
    db.add(models.ReelCollaborator(reel_id=reel.id, user_id=bob.id))
    db.commit()
    client.login(alice)

    response = _create(client, bob.id, reel_id=reel.id)

    assert response.status_code == 409
    assert "already a collaborator" in _error(response).lower()


# ---------------------------------------------------------------- accept


def test_partner_can_accept(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    response = client.post(f"{ENDPOINT}/{request_id}/accept")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert body["responded_at"] is not None


def test_accept_notifies_the_requester(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/accept")

    notifications = _notifications(
        db, alice.id, models.NotificationType.collaboration_accepted
    )
    assert len(notifications) == 1
    assert notifications[0].actor_id == bob.id
    assert notifications[0].target_id == request_id


def test_accept_tags_the_partner_on_the_reel(client, db, make_user, make_reel):
    alice, bob = make_user("alice"), make_user("bob")
    reel = make_reel(alice)
    client.login(alice)
    request_id = _create(client, bob.id, reel_id=reel.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/accept")

    tagged = (
        db.query(models.ReelCollaborator)
        .filter(
            models.ReelCollaborator.reel_id == reel.id,
            models.ReelCollaborator.user_id == bob.id,
        )
        .count()
    )
    assert tagged == 1


def test_accept_credits_the_proposed_payout(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id, proposed_amount_cents=5000).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/accept")

    earning = (
        db.query(models.CreatorEarning)
        .filter(
            models.CreatorEarning.user_id == bob.id,
            models.CreatorEarning.source_type
            == models.EarningSourceType.creator_collaboration,
        )
        .one()
    )
    assert earning.amount_cents == 5000


def test_requester_cannot_accept_their_own_request(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    response = client.post(f"{ENDPOINT}/{request_id}/accept")

    assert response.status_code == 403


def test_outsider_cannot_accept(client, db, make_user):
    alice, bob, carol = make_user("alice"), make_user("bob"), make_user("carol")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(carol)
    assert client.post(f"{ENDPOINT}/{request_id}/accept").status_code == 403


def test_accept_is_not_replayable(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/accept")
    response = client.post(f"{ENDPOINT}/{request_id}/accept")

    assert response.status_code == 409
    assert "already accepted" in _error(response).lower()


def test_accepting_a_missing_request_returns_404(client, db, make_user):
    client.login(make_user("alice"))

    assert client.post(f"{ENDPOINT}/999999/accept").status_code == 404


# ---------------------------------------------------------------- reject


def test_partner_can_reject(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    response = client.post(f"{ENDPOINT}/{request_id}/reject")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert response.json()["responded_at"] is not None


def test_reject_notifies_the_requester(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/reject")

    assert len(_notifications(db, alice.id, models.NotificationType.collaboration_rejected)) == 1


def test_reject_does_not_tag_a_collaborator(client, db, make_user, make_reel):
    alice, bob = make_user("alice"), make_user("bob")
    reel = make_reel(alice)
    client.login(alice)
    request_id = _create(client, bob.id, reel_id=reel.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/reject")

    assert db.query(models.ReelCollaborator).count() == 0


def test_requester_cannot_reject_their_own_request(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    assert client.post(f"{ENDPOINT}/{request_id}/reject").status_code == 403


def test_reject_after_accept_conflicts(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(bob)
    client.post(f"{ENDPOINT}/{request_id}/accept")

    assert client.post(f"{ENDPOINT}/{request_id}/reject").status_code == 409


# ------------------------------------------------------------ list / read


def test_listing_splits_incoming_and_outgoing(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)
    _create(client, bob.id)

    outgoing = client.get(ENDPOINT, params={"direction": "outgoing"}).json()
    incoming = client.get(ENDPOINT, params={"direction": "incoming"}).json()
    assert outgoing["total"] == 1
    assert incoming["total"] == 0

    client.login(bob)
    assert client.get(ENDPOINT, params={"direction": "incoming"}).json()["total"] == 1


def test_outsider_cannot_read_a_request(client, db, make_user):
    alice, bob, carol = make_user("alice"), make_user("bob"), make_user("carol")
    client.login(alice)
    request_id = _create(client, bob.id).json()["id"]

    client.login(carol)
    assert client.get(f"{ENDPOINT}/{request_id}").status_code == 403
