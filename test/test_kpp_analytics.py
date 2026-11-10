from datetime import datetime, timedelta, timezone

from app import models


def _make_eligible_creator(db, user):
    now = datetime.now(timezone.utc)
    user.created_at = now - timedelta(days=5)
    user.is_email_verified = True
    user.is_phone_verified = True
    user.phone_number = user.phone_number or "+919999999999"
    # Create enough followers using separate valid user rows.
    for index in range(500):
        follower = models.User(username=f"kppf{user.id}_{index}", email=f"kppf{user.id}_{index}@example.test")
        db.add(follower)
        db.flush()
        db.add(models.Follow(follower_id=follower.id, following_id=user.id))
    reel = models.Reel(user_id=user.id, video_url="/static/reels/kpp.mp4", created_at=now - timedelta(days=2))
    db.add(reel)
    db.flush()
    for offset in (2, 1, 0):
        if offset == 2:
            continue
        db.add(models.Reel(user_id=user.id, video_url=f"/static/reels/kpp{offset}.mp4", created_at=now - timedelta(days=offset)))
    viewer = models.User(username=f"kppviewer{user.id}", email=f"kppviewer{user.id}@example.test")
    db.add(viewer)
    db.flush()
    db.add(models.WatchSession(user_id=viewer.id, reel_id=reel.id, started_at=now-timedelta(hours=501),
                               ended_at=now-timedelta(hours=500), watch_seconds=500*3600,
                               active_owner_id=None, is_valid=True))
    db.commit()


def test_kpp_endpoints_reject_ineligible_and_show_upload_rule(client, db, make_user):
    user = make_user("kpp_ineligible")
    client.login(user)
    response = client.get("/api/v1/kpp/eligibility")
    assert response.status_code == 200
    data = response.json()
    assert data["eligible"] is False
    upload = next(item for item in data["requirements"] if item["key"] == "recent_uploads")
    assert upload["required"] == 3
    assert upload["period"] == "last_3_utc_calendar_days"
    submit = client.post("/api/v1/kpp/applications")
    assert submit.status_code == 409
    assert client.get("/api/v1/kpp/overview").status_code == 200
    assert client.get("/api/v1/kpp/earning-methods").status_code == 200


def test_kpp_application_revalidates_and_prevents_duplicate(client, db, make_user):
    user = make_user("kpp_eligible")
    _make_eligible_creator(db, user)
    db.refresh(user)
    client.login(user)
    eligibility = client.get("/api/v1/kpp/eligibility").json()
    assert eligibility["eligible"] is True
    submitted = client.post("/api/v1/kpp/applications")
    assert submitted.status_code == 201
    assert submitted.json()["application"]["status"] == "submitted"
    duplicate = client.post("/api/v1/kpp/applications")
    assert duplicate.status_code == 409
    current = client.get("/api/v1/kpp/applications/current")
    assert current.status_code == 200
    assert current.json()["application"]["status"] == "submitted"


def test_analytics_dashboard_returns_metrics_and_revenue_unavailable(client, db, make_user, make_reel):
    creator = make_user("analytics_creator")
    viewer = make_user("analytics_viewer")
    reel = make_reel(creator)
    now = datetime.now(timezone.utc)
    db.add_all([
        models.ReelInsightEvent(reel_id=reel.id, viewer_id=viewer.id, event_type="view", audience_type="non_followers",
                                watched_seconds=30, created_at=now - timedelta(hours=2)),
        models.Follow(follower_id=viewer.id, following_id=creator.id, created_at=now - timedelta(days=1)),
    ])
    db.commit()
    client.login(creator)
    response = client.get("/api/v1/analytics/dashboard?period=30d")
    assert response.status_code == 200
    data = response.json()
    assert data["metrics"]["total_views"]["value"] == 1
    assert data["metrics"]["unique_views"]["value"] == 1
    assert data["metrics"]["followers"]["value"] == 1
    assert data["metrics"]["estimated_revenue"]["value"] is None
    assert data["realtime_views"]["window_hours"] == 48
    assert len(data["realtime_views"]["timeline"]) >= 48
