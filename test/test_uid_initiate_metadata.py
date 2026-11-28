from app.routers.upload_lifecycle_routes import _parse_form_list


def test_parse_tagged_people_json_ids():
    assert _parse_form_list("[1, 2, 2]", "tagged_people", numeric=True) == [1, 2]


def test_parse_tagged_people_comma_separated_ids():
    assert _parse_form_list("1, 2, 3", "tagged_people", numeric=True) == [1, 2, 3]


def test_parse_hashtags_normalizes_hash_and_case():
    assert _parse_form_list('["#Travel", "FOOD", "travel"]', "hashtags", lower=True) == [
        "travel", "food"
    ]


def test_parse_empty_optional_field():
    assert _parse_form_list(None, "hashtags", lower=True) == []
