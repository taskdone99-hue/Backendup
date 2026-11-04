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


def test_reel_audience_aggregates_age_gender_and_hides_viewer_identities(
    client, db, make_user, make_reel
):
    from datetime import date

    owner = make_user("audience_owner")
    viewer = make_user("audience_viewer")
    viewer.date_of_birth = date(2000, 1, 1)
    viewer.gender = models.Gender.female
    db.commit()
    reel = make_reel(owner)
    db.add(models.ReelInsightEvent(
        reel_id=reel.id, viewer_id=viewer.id, event_type="view",
        audience_type="non_followers", source="feed",
    ))
    db.commit()

    client.login(owner)
    summary = client.get(f"/api/reels/{reel.id}/audience")
    assert summary.status_code == 200
    assert summary.json()["unique_viewers"] == 1
    assert summary.json()["categories"]["age"]["available"] is True
    assert summary.json()["categories"]["gender"]["available"] is True
    assert summary.json()["categories"]["country"]["available"] is False

    age = client.get(f"/api/reels/{reel.id}/audience/age")
    assert age.status_code == 200
    assert age.json()["available"] is True
    assert sum(item["viewers"] for item in age.json()["data"]) == 1

    gender = client.get(f"/api/reels/{reel.id}/audience/gender")
    assert gender.status_code == 200
    assert gender.json()["available"] is True
    assert next(item for item in gender.json()["data"] if item["gender"] == "female")["percentage"] == 100.0

    countries = client.get(f"/api/reels/{reel.id}/audience/countries")
    assert countries.status_code == 200
    assert countries.json()["available"] is False
    assert countries.json()["reason"] == "viewer_country_not_collected"

    viewers = client.get(f"/api/reels/{reel.id}/audience/viewers")
    assert viewers.status_code == 200
    assert viewers.json()["available"] is False
    assert "viewer" not in viewers.json() or "data" not in viewers.json()


def test_reel_insights_period_filters_views_and_event_watch_time(
    client, db, make_user, make_reel
):
    from datetime import timedelta

    owner = make_user("insights_period_owner")
    viewer = make_user("insights_period_viewer")
    reel = make_reel(owner)
    now = datetime.now(timezone.utc)

    db.add_all([
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="view",
            audience_type="non_followers", watched_seconds=30,
            created_at=now,
        ),
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="view",
            audience_type="non_followers", watched_seconds=10,
            created_at=now - timedelta(hours=13),
        ),
    ])
    db.commit()

    client.login(owner)
    response = client.get(f"/api/reels/{reel.id}/insights?period=12h")
    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == "12h"
    assert payload["views"] == 1
    assert payload["reach"] == 1
    assert payload["total_watch_time_seconds"] == 30
    assert payload["average_watch_time_seconds"] == 30
    assert payload["watch_time_events"] == 1

    lifetime = client.get(f"/api/reels/{reel.id}/insights?period=lifetime")
    assert lifetime.status_code == 200
    assert lifetime.json()["views"] == 2
    assert lifetime.json()["total_watch_time_seconds"] == 40


def test_reel_insights_timeline_returns_views_and_watch_time_for_period(
    client, db, make_user, make_reel
):
    from datetime import timedelta

    owner = make_user("insights_timeline_owner")
    viewer = make_user("insights_timeline_viewer")
    reel = make_reel(owner)
    now = datetime.now(timezone.utc)

    db.add_all([
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="view",
            audience_type="non_followers", watched_seconds=30,
            created_at=now,
        ),
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="view",
            audience_type="non_followers", watched_seconds=10,
            created_at=now - timedelta(days=5),
        ),
    ])
    db.commit()

    client.login(owner)
    response = client.get(f"/api/reels/{reel.id}/insights/timeline?period=7d")
    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == "7d"
    assert payload["total_views"] == 2
    assert payload["total_watch_time_seconds"] == 40
    assert sum(point["views"] for point in payload["data"]) == 2
    assert sum(point["watch_time_seconds"] for point in payload["data"]) == 40

    realtime = client.get(f"/api/reels/{reel.id}/insights/timeline?period=12h")
    assert realtime.status_code == 200
    assert realtime.json()["total_views"] == 1
    assert realtime.json()["total_watch_time_seconds"] == 30


def test_retention_includes_progress_percent_on_view_events(
    client, db, make_user, make_reel
):
    owner = make_user("insights_view_progress_owner")
    viewer = make_user("insights_view_progress_viewer")
    reel = make_reel(owner)
    db.add(models.ReelInsightEvent(
        reel_id=reel.id, viewer_id=viewer.id, event_type="view",
        audience_type="non_followers", progress_percent=75,
        watched_seconds=30,
    ))
    db.commit()

    client.login(owner)
    response = client.get(f"/api/reels/{reel.id}/insights/retention")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["data"] == [{
        "progress_percent": 75,
        "retained_viewers": 1,
        "retention_percent": 100.0,
    }]
