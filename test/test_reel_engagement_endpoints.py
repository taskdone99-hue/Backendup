from app import models


def test_engagement_screen_endpoints(client, db, make_user, make_reel):
    owner = make_user("engagement_owner")
    viewer = make_user("engagement_viewer")
    reel = make_reel(owner)

    db.add_all([
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="view",
            audience_type="non_followers", source="suggested_content",
        ),
        models.ReelInsightEvent(
            reel_id=reel.id, viewer_id=viewer.id, event_type="skip",
            audience_type="non_followers", source="suggested_content",
        ),
    ])
    db.add(models.Like(
        user_id=viewer.id, target_type=models.LikeTargetType.reel,
        target_id=reel.id,
    ))
    db.commit()

    client.login(owner)
    response = client.get(f"/api/reels/{reel.id}/insights/engagement")
    assert response.status_code == 200
    payload = response.json()
    assert payload["reach"]["total_reach"] == 1
    assert payload["reach"]["unique_accounts_reached"] == 1
    assert payload["viewing"]["viewed"] == 1
    assert payload["viewing"]["skipped"] == 1
    assert payload["reach_sources"]["data"][0]["source"] == "suggested_content"
    assert payload["interactions"]["likes"] == 1
    assert payload["interactions"]["reposts"] is None
    assert payload["engagement_rate"] == 100.0

    for suffix, expected_key in [
        ("reach", "unique_accounts_reached"),
        ("views", "viewed"),
        ("sources", "data"),
        ("interactions", "likes"),
    ]:
        result = client.get(f"/api/reels/{reel.id}/insights/{suffix}")
        assert result.status_code == 200
        assert expected_key in result.json()


def test_engagement_endpoints_are_owner_only(client, make_user, make_reel):
    owner = make_user("engagement_private_owner")
    other = make_user("engagement_private_other")
    reel = make_reel(owner)
    client.login(other)
    for suffix in ("engagement", "reach", "views", "sources", "interactions"):
        response = client.get(f"/api/reels/{reel.id}/insights/{suffix}")
        assert response.status_code == 403
