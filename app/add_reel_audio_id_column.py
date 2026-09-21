"""
One-off migration for existing databases: adds the `audio_id` column to
`reels`, linking a reel to the sound it uses (models.Audio). Nullable and
additive — every pre-existing reel just has no audio_id until it's
attached via reel creation, the audio-remix flow, or backfilled.

Run once:
    python -m app.add_reel_audio_id_column
"""

import os

import pymysql
from dotenv import load_dotenv

load_dotenv()

conn = pymysql.connect(
    host=os.getenv("DB_HOST"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
    port=int(os.getenv("DB_PORT", "3306")),
)
cur = conn.cursor()

try:
    cur.execute(
        "ALTER TABLE reels ADD COLUMN audio_id INT NULL, "
        "ADD INDEX ix_reels_audio_id (audio_id), "
        "ADD CONSTRAINT fk_reels_audio FOREIGN KEY (audio_id) REFERENCES audio_tracks(id) ON DELETE SET NULL"
    )
    conn.commit()
    print("reels.audio_id added")
except pymysql.err.OperationalError as e:
    print("reels.audio_id:", e)
except pymysql.err.InternalError as e:
    print("reels.audio_id:", e)

conn.close()
print("Done")
