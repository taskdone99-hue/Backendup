"""Create sticker tables on an existing database. Run: python -m app.add_sticker_tables"""
from app.database import engine
from app import models

TABLES = [models.Sticker.__table__, models.StickerFavorite.__table__, models.StickerUsage.__table__]
models.Base.metadata.create_all(bind=engine, tables=TABLES, checkfirst=True)
print("Sticker tables are ready.")
