from datetime import datetime, timezone

from app import models


def test_reel_insights_event_and_overview(client, db, make_user, make_reel, follow):
    owner = make_user("insights_owner")
    viewer = make_user("insights_viewer")
    reel = make_reel(owner, duration_seconds=20)
    follow(viewer, owner)

    client.login(viewer)
    response = client.post(
        f"/api/reels/{reel.id}/insights/events",
        json={"event_type": "view", "source": "feed", "client_event_id": "view-1"},
    )
    assert response.status_code == 201
    assert response.json()["audience"] == "followers"

    duplicate = client.post(
        f"/api/reels/{reel.id}/insights/events",
        json={"event_type": "view", "source": "feed", "client_event_id": "view-1"},
    )
    assert duplicate.status_code == 201
    assert duplicate.json() == {"recorded": False, "duplicate": True}

    client.login(owner)
    overview = client.get(f"/api/reels/{reel.id}/insights")
    assert overview.status_code == 200
    assert overview.json()["views"] == 1
    assert overview.json()["reach"] == 1
    assert overview.json()["engagement"]["reposts"] is None

    timeline = client.get(f"/api/reels/{reel.id}/insights/timeline?audience=followers&days=7")
    assert timeline.status_code == 200
    assert sum(point["views"] for point in timeline.json()["data"]) == 1

    sources = client.get(f"/api/reels/{reel.id}/insights/sources")
    assert sources.status_code == 200
    assert sources.json()["available"] is True
    assert next(x for x in sources.json()["data"] if x["source"] == "feed")["views"] == 1


def test_reel_insights_owner_cannot_record_own_views(client, make_user, make_reel):
    owner = make_user("insights_self_owner")
    reel = make_reel(owner)
    client.login(owner)
    response = client.post(
        f"/api/reels/{reel.id}/insights/events",
        json={"event_type": "view", "source": "profile"},
    )
    assert response.status_code == 400


def test_reel_insights_private_to_owner(client, make_user, make_reel):
    owner = make_user("insights_private_owner")
    other = make_user("insights_private_other")
    reel = make_reel(owner)
    client.login(other)
    response = client.get(f"/api/reels/{reel.id}/insights")
    assert response.status_code == 403


def test_retention_unavailable_until_progress_events(client, db, make_user, make_reel):
    owner = make_user("insights_retention_owner")
    reel = make_reel(owner)
    client.login(owner)
    response = client.get(f"/api/reels/{reel.id}/insights/retention")
    assert response.status_code == 200
    assert response.json() == {"reel_id": reel.id, "available": False, "data": []}
