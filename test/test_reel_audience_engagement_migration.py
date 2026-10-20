from sqlalchemy import create_engine, text

from app.add_reel_audience_engagement_columns import migrate


def test_migration_preserves_legacy_age_values_and_defaults_audience():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE reels (id INTEGER PRIMARY KEY, audience VARCHAR(20))"))
        conn.execute(text("INSERT INTO reels (id, audience) VALUES (1, 'below_18'), (2, '18_plus'), (3, NULL), (4, 'followers')"))
        conn.execute(text("CREATE TABLE reel_drafts (id INTEGER PRIMARY KEY, audience VARCHAR(20))"))
        conn.execute(text("INSERT INTO reel_drafts (id, audience) VALUES (1, '18_plus')"))

    changes = migrate(engine)
    with engine.connect() as conn:
        reels = conn.execute(text("SELECT id, audience, age_restriction, remixing_enabled, downloads_enabled FROM reels ORDER BY id")).all()
        drafts = conn.execute(text("SELECT audience, age_restriction FROM reel_drafts")).one()

    assert reels == [
        (1, "everyone", "below_18", 1, 1),
        (2, "everyone", "18_plus", 1, 1),
        (3, "everyone", None, 1, 1),
        (4, "followers", None, 1, 1),
    ]
    assert drafts == ("everyone", "18_plus")
    assert changes
    assert migrate(engine) == []
