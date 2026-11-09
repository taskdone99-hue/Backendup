from app import models


def test_get_returns_defaults_without_creating_row(client, make_user, db):
    user = make_user()
    client.login(user)

    response = client.get("/v1/me/privacy-settings")

    assert response.status_code == 200
    assert response.json() == {
        "visibility": "public",
        "showActivityStatus": True,
        "showFollowerList": True,
        "showOnlineStatus": False,
        "accountSuggestions": True,
    }
    assert db.query(models.UserPrivacySettings).filter_by(user_id=user.id).count() == 0


def test_patch_updates_all_fields_and_existing_visibility(client, make_user, db):
    user = make_user()
    client.login(user)

    response = client.patch("/v1/me/privacy-settings", json={
        "visibility": "private",
        "showActivityStatus": False,
        "showFollowerList": False,
        "showOnlineStatus": True,
        "accountSuggestions": False,
    })

    assert response.status_code == 200
    assert response.json() == {
        "visibility": "private",
        "showActivityStatus": False,
        "showFollowerList": False,
        "showOnlineStatus": True,
        "accountSuggestions": False,
    }
    db.refresh(user)
    assert user.is_private is True  # existing profile/feed privacy enforcement stays canonical
    assert db.query(models.PrivacySettingsAudit).filter_by(user_id=user.id).count() == 1


def test_patch_is_partial_and_returns_complete_canonical_settings(client, make_user):
    user = make_user(is_private=True)
    client.login(user)

    response = client.patch("/v1/me/privacy-settings", json={"showOnlineStatus": True})

    assert response.status_code == 200
    assert response.json() == {
        "visibility": "private",
        "showActivityStatus": True,
        "showFollowerList": True,
        "showOnlineStatus": True,
        "accountSuggestions": True,
    }


def test_patch_rejects_empty_null_and_unknown_fields(client, make_user):
    client.login(make_user())
    assert client.patch("/v1/me/privacy-settings", json={}).status_code == 400
    assert client.patch("/v1/me/privacy-settings", json={"showOnlineStatus": None}).status_code == 422
    assert client.patch("/v1/me/privacy-settings", json={"unexpected": True}).status_code == 400
    assert client.patch("/v1/me/privacy-settings", json={"visibility": "friends"}).status_code == 400


def test_patch_noop_does_not_create_audit_event(client, make_user, db):
    user = make_user()
    client.login(user)

    response = client.patch("/v1/me/privacy-settings", json={"visibility": "public"})

    assert response.status_code == 200
    assert db.query(models.PrivacySettingsAudit).filter_by(user_id=user.id).count() == 0


def test_endpoints_require_authentication(client):
    assert client.get("/v1/me/privacy-settings").status_code == 401
    assert client.patch("/v1/me/privacy-settings", json={"visibility": "private"}).status_code == 401
