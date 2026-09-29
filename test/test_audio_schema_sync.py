"""
Tests for the audio_tracks schema drift that caused GET /api/search to return
HTTP 500 in production:

    Unknown column 'audio_tracks.duration_seconds' in 'field list'
    (and, once that was added by hand, 'audio_tracks.created_by_user_id')

Root cause: audio_tracks was created by Base.metadata.create_all() before
models.Audio gained `duration_seconds` and `created_by_user_id`; create_all()
never ALTERs existing tables. app/add_audio_track_columns.py is the migration.

The normal test suite can't catch this class of bug — it builds a *fresh*
SQLite schema from the models, so model and database always agree. These tests
deliberately build the *legacy* schema instead.

Run with:
    pytest test/test_audio_schema_sync.py -v
"""

import os
import sys
import types
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-audio-schema-tests")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import add_audio_track_columns as migration
from app import models
from app.auth import get_current_user, get_current_user_optional
from app.database import Base, get_db
from app.routers.audio_routes import router as audio_router
from app.routers.search_routes import router as search_router

# Columns audio_tracks had when create_all() first built it in production —
# i.e. everything on models.Audio *except* the two later additions.
ORIGINAL_COLUMNS = {
    "id", "title", "artist", "audio_url", "source_post_id", "source_reel_id", "created_at",
}

LEGACY_DDL = """
CREATE TABLE audio_tracks (
    id INTEGER PRIMARY KEY,
    title VARCHAR(150) NOT NULL,
    artist VARCHAR(150),
    audio_url VARCHAR(500) NOT NULL UNIQUE,
    source_post_id INTEGER,
    source_reel_id INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


def _new_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def _engine_with_legacy_audio_table(*, extra_sql: tuple[str, ...] = ()):
    """A full schema built from the models, except audio_tracks is swapped for
    the legacy shape (optionally with extra ALTERs applied, to model a
    database someone patched by hand)."""
    engine = _new_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE audio_tracks"))
        conn.execute(text(LEGACY_DDL))
        for statement in extra_sql:
            conn.execute(text(statement))
    return engine


def _columns(engine, table="audio_tracks"):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _indexed_columns(engine):
    return {tuple(i["column_names"]) for i in inspect(engine).get_indexes("audio_tracks")}


MODEL_COLUMNS = {c.name for c in models.Audio.__table__.columns}

# What production looks like right now: the user added duration_seconds by hand.
PROD_STATE = ("ALTER TABLE audio_tracks ADD COLUMN duration_seconds INTEGER",)


# ------------------------------------------------------------ drift guard

def test_every_column_added_after_creation_is_covered_by_the_migration():
    """Fails if someone adds a column to models.Audio without adding it to
    app/add_audio_track_columns.py — i.e. exactly how this outage happened."""
    added_since_creation = MODEL_COLUMNS - ORIGINAL_COLUMNS
    assert added_since_creation == set(migration.AUDIO_TRACK_COLUMNS), (
        "models.Audio and app/add_audio_track_columns.AUDIO_TRACK_COLUMNS disagree. "
        "If you added a column to models.Audio, add it to the migration too "
        "(existing databases won't get it from create_all())."
    )


def test_original_columns_really_are_on_the_model():
    assert ORIGINAL_COLUMNS <= MODEL_COLUMNS


# ------------------------------------------------------------ the migration

def test_migration_brings_legacy_table_in_sync_with_the_model():
    engine = _engine_with_legacy_audio_table()
    assert _columns(engine) == ORIGINAL_COLUMNS

    statements = migration.migrate(engine)

    assert _columns(engine) == MODEL_COLUMNS
    assert ("created_by_user_id",) in _indexed_columns(engine)
    assert any("duration_seconds" in s for s in statements)
    assert any("created_by_user_id" in s for s in statements)


def test_migration_only_adds_what_is_missing_when_duration_seconds_was_added_by_hand():
    """The real production state at the time of this fix."""
    engine = _engine_with_legacy_audio_table(extra_sql=PROD_STATE)

    statements = migration.migrate(engine)

    assert not any("duration_seconds" in s for s in statements), (
        "must not try to re-add a column that already exists (would raise Duplicate column)"
    )
    assert any("ADD COLUMN created_by_user_id" in s for s in statements)
    assert _columns(engine) == MODEL_COLUMNS


def test_migration_is_idempotent():
    engine = _engine_with_legacy_audio_table(extra_sql=PROD_STATE)
    migration.migrate(engine)
    assert migration.migrate(engine) == []
    assert migration.migrate(engine) == []
    assert _columns(engine) == MODEL_COLUMNS


def test_migration_is_a_noop_on_an_up_to_date_schema():
    engine = _new_engine()
    Base.metadata.create_all(bind=engine)  # fresh install: model == database
    assert migration.migrate(engine) == []


def test_migration_does_nothing_if_table_does_not_exist_yet():
    engine = _new_engine()  # nothing created; create_all() at app startup will build it
    assert migration.migrate(engine) == []


def test_dry_run_reports_statements_but_changes_nothing():
    engine = _engine_with_legacy_audio_table(extra_sql=PROD_STATE)
    statements = migration.migrate(engine, dry_run=True)
    assert statements  # it knows what it would do...
    assert _columns(engine) == ORIGINAL_COLUMNS | {"duration_seconds"}  # ...but did nothing


def test_migration_does_not_duplicate_an_index_someone_already_created():
    engine = _engine_with_legacy_audio_table(
        extra_sql=(
            "ALTER TABLE audio_tracks ADD COLUMN duration_seconds INTEGER",
            "ALTER TABLE audio_tracks ADD COLUMN created_by_user_id INTEGER",
            "CREATE INDEX my_hand_made_index ON audio_tracks (created_by_user_id)",
        )
    )
    assert migration.migrate(engine) == []


def test_migration_preserves_existing_rows():
    engine = _engine_with_legacy_audio_table(extra_sql=PROD_STATE)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audio_tracks (title, artist, audio_url, duration_seconds) "
                "VALUES ('Kesariya', 'Arijit Singh', 'https://example.com/a1.mp3', 30)"
            )
        )

    migration.migrate(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT title, duration_seconds, created_by_user_id FROM audio_tracks")
        ).one()
    assert tuple(row) == ("Kesariya", 30, None)


# ------------------------------------------------- end to end, real endpoints

@pytest.fixture()
def legacy_app():
    """The real search + audio routers, running against the production-like
    legacy schema. Yields (client, engine)."""
    engine = _engine_with_legacy_audio_table(extra_sql=PROD_STATE)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    seed = SessionLocal()
    seed.add_all(
        [
            models.User(username="anjali_r", full_name="Anjali Rao", is_active=True),
            models.User(username="me", full_name="Me", is_active=True),
        ]
    )
    seed.commit()
    seed.close()

    holder = SessionLocal()  # kept open: the "logged-in user" object stays attached to it
    me = holder.query(models.User).filter_by(username="me").one()

    def _get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(search_router)
    app.include_router(audio_router)
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: me
    app.dependency_overrides[get_current_user_optional] = lambda: me

    # raise_server_exceptions=False so a DB error surfaces as the HTTP 500 a
    # real client would see, instead of an exception inside the test.
    yield TestClient(app, raise_server_exceptions=False), engine
    holder.close()


def test_search_returns_500_on_the_drifted_schema_then_200_after_migration(legacy_app):
    """Regression test for the production incident: GET /api/search?q=anjali."""
    client, engine = legacy_app

    # Pre-fix production state (duration_seconds present, created_by_user_id not).
    assert client.get("/api/search", params={"q": "anjali"}).status_code == 500

    migration.migrate(engine)

    resp = client.get("/api/search", params={"q": "anjali"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"users", "songs", "locations", "hashtags"}
    assert [u["username"] for u in body["users"]] == ["anjali_r"]


def test_each_search_type_works_after_migration(legacy_app):
    client, engine = legacy_app
    migration.migrate(engine)
    for search_type in ("all", "users", "songs", "locations", "hashtags"):
        resp = client.get("/api/search", params={"q": "anjali", "type": search_type})
        assert resp.status_code == 200, f"type={search_type}: {resp.text}"


def test_songs_search_and_audio_endpoints_work_after_migration(legacy_app):
    client, engine = legacy_app
    migration.migrate(engine)

    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    uploader = db.query(models.User).filter_by(username="anjali_r").one()
    audio = models.Audio(
        title="Anjali's Song",
        artist="Someone",
        audio_url="https://example.com/anjali.mp3",
        duration_seconds=42,
        created_by_user_id=uploader.id,  # the column that was missing in production
    )
    db.add(audio)
    db.commit()
    audio_id = audio.id
    db.close()

    songs = client.get("/api/search", params={"q": "anjali", "type": "songs"}).json()
    assert songs["total"] == 1 and songs["items"][0]["title"] == "Anjali's Song"

    assert client.get(f"/api/audio/{audio_id}").status_code == 200
    assert client.get("/api/audio/trending").status_code == 200
    assert client.get("/api/audio/saved").status_code == 200
    assert client.post(f"/api/audio/{audio_id}/save").status_code == 200

# ------------------------------------------- MySQL-only branch (the FK step)
#
# SQLite can't ALTER TABLE ... ADD CONSTRAINT, so the migration only plans the
# foreign key on MySQL — which is exactly what production runs. There's no
# MySQL server in the test environment, so the planning logic for that branch
# is exercised here against a stubbed inspector.

class _StubInspector:
    def __init__(self, columns, indexes=(), foreign_keys=()):
        self._columns, self._indexes, self._fks = columns, list(indexes), list(foreign_keys)

    def get_table_names(self):
        return ["audio_tracks"]

    def get_columns(self, table):
        return [{"name": name} for name in self._columns]

    def get_indexes(self, table):
        return self._indexes

    def get_foreign_keys(self, table):
        return self._fks


def _plan_on_mysql(monkeypatch, inspector):
    monkeypatch.setattr(migration, "inspect", lambda _engine: inspector)
    return migration.plan(types.SimpleNamespace(dialect=types.SimpleNamespace(name="mysql")))


def test_mysql_plan_for_the_production_state_adds_column_index_and_foreign_key(monkeypatch):
    inspector = _StubInspector(ORIGINAL_COLUMNS | {"duration_seconds"})  # user added it by hand

    statements = _plan_on_mysql(monkeypatch, inspector)

    assert statements == [
        "ALTER TABLE audio_tracks ADD COLUMN created_by_user_id INT NULL",
        "CREATE INDEX ix_audio_tracks_created_by_user_id ON audio_tracks (created_by_user_id)",
        "ALTER TABLE audio_tracks ADD CONSTRAINT fk_audio_tracks_created_by_user "
        "FOREIGN KEY (created_by_user_id) REFERENCES users(id)",
    ]


def test_mysql_plan_is_empty_when_columns_index_and_foreign_key_all_exist(monkeypatch):
    inspector = _StubInspector(
        MODEL_COLUMNS,
        indexes=[{"name": "ix_audio_tracks_created_by_user_id", "column_names": ["created_by_user_id"]}],
        foreign_keys=[{"name": "audio_tracks_ibfk_1", "constrained_columns": ["created_by_user_id"]}],
    )
    assert _plan_on_mysql(monkeypatch, inspector) == []


def test_mysql_plan_adds_only_the_missing_foreign_key(monkeypatch):
    inspector = _StubInspector(
        MODEL_COLUMNS,
        indexes=[{"name": "ix_audio_tracks_created_by_user_id", "column_names": ["created_by_user_id"]}],
    )
    statements = _plan_on_mysql(monkeypatch, inspector)
    assert len(statements) == 1 and "ADD CONSTRAINT" in statements[0]


def test_mysql_plan_does_not_duplicate_a_foreign_key_added_under_another_name(monkeypatch):
    inspector = _StubInspector(
        MODEL_COLUMNS,
        indexes=[{"name": "created_by_user_id", "column_names": ["created_by_user_id"]}],
        foreign_keys=[{"name": "some_hand_made_fk", "constrained_columns": ["created_by_user_id"]}],
    )
    assert _plan_on_mysql(monkeypatch, inspector) == []
