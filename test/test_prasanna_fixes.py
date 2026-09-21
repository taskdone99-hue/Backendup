"""
Ad-hoc smoke tests for the three issues reported by Prasanna:
  1. No notification when a collaboration request is cancelled.
  2. GET /api/reels/{reel_id} not returning collaborator info after accept.
  3. Missing remix/share/tags APIs for reels (tags + share-link were the
     actual gaps; remix and likes already existed).
"""

from app import models


def test_cancel_notifies_the_partner(client, db, make_user):
    alice, bob = make_user("alice"), make_user("bob")
    client.login(alice)

    request_id = client.post(
        "/api/creator-collaborations",
        json={"partner_user_id": bob.id, "message": "collab?"},
    ).json()["id"]

    response = client.post(f"/api/creator-collaborations/{request_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    notifs = (
        db.query(models.Notification)
        .filter(
            models.Notification.user_id == bob.id,
            models.Notification.type == models.NotificationType.collaboration_cancelled,
        )
        .all()
    )
    assert len(notifs) == 1


def test_reel_detail_includes_collaborator_after_accept(client, db, make_user, make_reel):
    owner, collaborator = make_user("carol"), make_user("dave")
    reel = make_reel(owner)

    client.login(owner)
    request_id = client.post(
        "/api/creator-collaborations",
        json={"partner_user_id": collaborator.id, "reel_id": reel.id},
    ).json()["id"]

    client.login(collaborator)
    accept = client.post(f"/api/creator-collaborations/{request_id}/accept")
    assert accept.status_code == 200, accept.text

    detail = client.get(f"/api/reels/{reel.id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "collaborators" in body
    collab_ids = [c["user"]["id"] for c in body["collaborators"]]
    assert collaborator.id in collab_ids, body


def test_reel_tags_lifecycle(client, make_user, make_reel):
    owner, friend = make_user("erin"), make_user("frank")
    reel = make_reel(owner)
    client.login(owner)

    created = client.post(f"/api/reels/{reel.id}/tags", json={"tags": [{"user_id": friend.id}]})
    assert created.status_code == 201, created.text
    assert created.json()["tags"][0]["user"]["id"] == friend.id

    fetched = client.get(f"/api/reels/{reel.id}/tags")
    assert fetched.status_code == 200, fetched.text
    assert len(fetched.json()["tags"]) == 1

    detail = client.get(f"/api/reels/{reel.id}")
    assert detail.json()["tags_count"] == 1

    removed = client.delete(f"/api/reels/{reel.id}/tags/{friend.id}")
    assert removed.status_code == 200, removed.text

    fetched_again = client.get(f"/api/reels/{reel.id}/tags")
    assert len(fetched_again.json()["tags"]) == 0


def test_reel_tag_forbidden_for_non_owner(client, make_user, make_reel):
    owner, stranger, someone = make_user("gina"), make_user("hank"), make_user("iris")
    reel = make_reel(owner)
    client.login(stranger)

    response = client.post(f"/api/reels/{reel.id}/tags", json={"tags": [{"user_id": someone.id}]})
    assert response.status_code == 403


def test_reel_share_link(client, make_user, make_reel):
    owner = make_user("jane")
    reel = make_reel(owner)

    response = client.get(f"/api/share/reels/{reel.id}/link")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reel_id"] == reel.id
    assert str(reel.id) in body["url"]


def test_user_reels_endpoint_includes_accepted_collab_reels(client, db, make_user, make_reel):
    """anjali (2nd report): GET /api/users/{user_id}/reels only returned
    reels the user owns, not ones they're a credited collaborator on."""
    owner, collaborator = make_user("kai"), make_user("liu")
    reel = make_reel(owner)

    client.login(owner)
    request_id = client.post(
        "/api/creator-collaborations",
        json={"partner_user_id": collaborator.id, "reel_id": reel.id},
    ).json()["id"]

    client.login(collaborator)
    accept = client.post(f"/api/creator-collaborations/{request_id}/accept")
    assert accept.status_code == 200, accept.text

    # Shows on the collaborator's own reels list...
    collaborator_reels = client.get(f"/api/users/{collaborator.id}/reels")
    assert collaborator_reels.status_code == 200, collaborator_reels.text
    ids = [r["id"] for r in collaborator_reels.json()["items"]]
    assert reel.id in ids, collaborator_reels.json()

    # ...and still shows on the owner's, without duplicating it.
    owner_reels = client.get(f"/api/users/{owner.id}/reels")
    owner_ids = [r["id"] for r in owner_reels.json()["items"]]
    assert owner_ids.count(reel.id) == 1
