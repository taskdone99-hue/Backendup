"""
Brand (paid-partnership) collaboration offers
(POST/GET /api/brand-collaborations and the accept/reject actions).

Regression coverage for a bug where create/accept/reject never notified
anyone — app/services/brand_collaboration_service.py was missing the
notify_user() fan-out that the equivalent creator-to-creator flow
(collaboration_service.py, see test_creator_collaboration.py) already had.
"""

from app import models

ENDPOINT = "/api/brand-collaborations"


def _create(client, creator_id, **extra):
    payload = {
        "creator_user_id": creator_id,
        "brand_name": "Acme Co",
        "campaign_title": "Summer launch",
        "offer_amount_cents": 50000,
        "currency": "USD",
    }
    payload.update(extra)
    return client.post(ENDPOINT, json=payload)


def _notifications(db, user_id, notif_type):
    return (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == user_id,
            models.Notification.type == notif_type,
        )
        .all()
    )


def test_create_notifies_the_creator(client, db, make_user):
    brand_rep, creator = make_user("brandrep"), make_user("creator")
    client.login(brand_rep)

    response = _create(client, creator.id)
    assert response.status_code == 201, response.text
    offer_id = response.json()["id"]

    notifications = _notifications(
        db, creator.id, models.NotificationType.brand_collaboration_offer
    )
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.actor_id == brand_rep.id
    assert notification.target_type == "brand_collab_offer"
    assert notification.target_id == offer_id
    assert brand_rep.username in notification.message
    # target_type is a String(20) column — anything longer is silently
    # truncated on MySQL, so the value has to stay short.
    assert len(notification.target_type) <= 20


def test_brand_rep_is_not_notified_of_their_own_offer(client, db, make_user):
    brand_rep, creator = make_user("brandrep2"), make_user("creator2")
    client.login(brand_rep)

    _create(client, creator.id)

    assert _notifications(db, brand_rep.id, models.NotificationType.brand_collaboration_offer) == []


def test_accept_notifies_the_brand_rep(client, db, make_user):
    brand_rep, creator = make_user("brandrep3"), make_user("creator3")
    client.login(brand_rep)
    offer_id = _create(client, creator.id).json()["id"]

    client.login(creator)
    response = client.post(f"{ENDPOINT}/{offer_id}/accept")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"

    notifications = _notifications(
        db, brand_rep.id, models.NotificationType.brand_collaboration_accepted
    )
    assert len(notifications) == 1
    assert notifications[0].actor_id == creator.id
    assert notifications[0].target_id == offer_id


def test_reject_notifies_the_brand_rep(client, db, make_user):
    brand_rep, creator = make_user("brandrep4"), make_user("creator4")
    client.login(brand_rep)
    offer_id = _create(client, creator.id).json()["id"]

    client.login(creator)
    response = client.post(f"{ENDPOINT}/{offer_id}/reject")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    notifications = _notifications(
        db, brand_rep.id, models.NotificationType.brand_collaboration_rejected
    )
    assert len(notifications) == 1
    assert notifications[0].actor_id == creator.id


def test_cannot_offer_yourself(client, db, make_user):
    brand_rep = make_user("brandrep5")
    client.login(brand_rep)

    response = _create(client, brand_rep.id)
    assert response.status_code == 400


def test_only_targeted_creator_can_accept(client, db, make_user):
    brand_rep, creator, stranger = make_user("brandrep6"), make_user("creator6"), make_user("stranger6")
    client.login(brand_rep)
    offer_id = _create(client, creator.id).json()["id"]

    client.login(stranger)
    response = client.post(f"{ENDPOINT}/{offer_id}/accept")
    assert response.status_code == 403
