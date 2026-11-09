from app import models


def test_get_defaults_to_digital_creator_without_writing_row(client, make_user, db):
    user = make_user()
    client.login(user)

    response = client.get("/v1/me/content-category")

    assert response.status_code == 200
    assert response.json() == {
        "category": "digital_creator",
        "customCategory": None,
        "updatedAt": None,
    }
    assert db.query(models.UserContentCategory).filter_by(user_id=user.id).count() == 0


def test_category_catalogue_returns_all_supported_options(client):
    response = client.get("/v1/content-categories")

    assert response.status_code == 200
    data = response.json()
    assert [item["id"] for item in data] == [
        "artist", "blogger", "digital_creator", "education", "entertainment",
        "fashion", "food_lifestyle", "gaming", "health_fitness", "music",
        "photography", "technology", "travel", "business", "other",
    ]
    assert data[-1] == {"id": "other", "label": "Other", "allowsCustom": True}


def test_put_saves_standard_category_and_audits_change(client, make_user, db):
    user = make_user()
    client.login(user)

    response = client.put("/v1/me/content-category", json={
        "category": "digital_creator", "customCategory": None,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "digital_creator"
    assert body["customCategory"] is None
    assert body["updatedAt"] is not None
    assert db.query(models.UserContentCategory).filter_by(user_id=user.id).count() == 1
    assert db.query(models.ContentCategoryAudit).filter_by(user_id=user.id).count() == 1


def test_put_other_requires_trimmed_nonempty_custom_category(client, make_user):
    client.login(make_user())

    response = client.put("/v1/me/content-category", json={
        "category": "other", "customCategory": "  My niche  ",
    })
    assert response.status_code == 200
    assert response.json()["customCategory"] == "My niche"

    assert client.put("/v1/me/content-category", json={
        "category": "other", "customCategory": "   ",
    }).status_code == 400
    assert client.put("/v1/me/content-category", json={
        "category": "other",
    }).status_code == 400


def test_put_rejects_unsupported_id_long_custom_and_custom_for_normal_category(client, make_user):
    client.login(make_user())

    assert client.put("/v1/me/content-category", json={
        "category": "influencer", "customCategory": None,
    }).status_code == 400
    assert client.put("/v1/me/content-category", json={
        "category": "other", "customCategory": "x" * 61,
    }).status_code == 400
    assert client.put("/v1/me/content-category", json={
        "category": "artist", "customCategory": "my niche",
    }).status_code == 400


def test_put_replaces_previous_value_and_get_returns_canonical_state(client, make_user, db):
    user = make_user()
    client.login(user)

    first = client.put("/v1/me/content-category", json={
        "category": "other", "customCategory": "My niche",
    })
    assert first.status_code == 200

    second = client.put("/v1/me/content-category", json={
        "category": "food_lifestyle", "customCategory": None,
    })
    assert second.status_code == 200
    assert second.json()["category"] == "food_lifestyle"
    assert second.json()["customCategory"] is None
    assert db.query(models.UserContentCategory).filter_by(user_id=user.id).count() == 1
    assert db.query(models.ContentCategoryAudit).filter_by(user_id=user.id).count() == 2

    fetched = client.get("/v1/me/content-category")
    assert fetched.json()["category"] == "food_lifestyle"
    assert fetched.json()["customCategory"] is None


def test_me_endpoints_require_authentication(client):
    assert client.get("/v1/me/content-category").status_code == 401
    assert client.put("/v1/me/content-category", json={
        "category": "artist", "customCategory": None,
    }).status_code == 401
