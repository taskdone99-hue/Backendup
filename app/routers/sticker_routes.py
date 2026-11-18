from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import save_upload_file

router = APIRouter(prefix="/api/stickers", tags=["stickers"])


def _visible_query(db: Session, user_id: int):
    return db.query(models.Sticker).filter(
        ((models.Sticker.owner_id == user_id) & (models.Sticker.visibility == "private")) |
        ((models.Sticker.visibility == "public") & (models.Sticker.approval_status == "approved"))
    )


def _out(row: models.Sticker):
    return {
        "id": row.id, "owner_id": row.owner_id, "name": row.name,
        "sticker_url": row.sticker_url, "thumbnail_url": row.thumbnail_url,
        "mime_type": row.mime_type, "visibility": row.visibility,
        "approval_status": row.approval_status, "created_at": row.created_at,
    }


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload_sticker(
    file: UploadFile = File(...), name: str = Form(...),
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    if not name.strip():
        raise HTTPException(400, "Sticker name is required")
    url, kind = save_upload_file(file, "stickers", allow_video=False)
    if kind != "image":
        raise HTTPException(400, "Sticker must be an image")
    row = models.Sticker(owner_id=user.id, name=name.strip()[:100], sticker_url=url,
                         thumbnail_url=url, mime_type=file.content_type or "image/*",
                         visibility="private", approval_status="approved")
    db.add(row); db.commit(); db.refresh(row)
    return _out(row)


@router.get("/my")
def my_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.query(models.Sticker).filter(models.Sticker.owner_id == user.id).order_by(desc(models.Sticker.created_at)).offset(offset).limit(limit).all()
    return {"items": [_out(x) for x in rows], "limit": limit, "offset": offset}


@router.get("/public")
def public_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
                    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.query(models.Sticker).filter(models.Sticker.visibility == "public", models.Sticker.approval_status == "approved").order_by(desc(models.Sticker.created_at)).offset(offset).limit(limit).all()
    return {"items": [_out(x) for x in rows], "limit": limit, "offset": offset}


@router.get("/search")
def search_stickers(q: str = Query(..., min_length=1, max_length=100), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = _visible_query(db, user.id).filter(models.Sticker.name.ilike(f"%{q.strip()}%")).order_by(desc(models.Sticker.created_at)).limit(50).all()
    return {"items": [_out(x) for x in rows]}


@router.get("/favorites")
def favorites(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.Sticker).join(models.StickerFavorite, models.StickerFavorite.sticker_id == models.Sticker.id).filter(models.StickerFavorite.user_id == user.id).all()
    return {"items": [_out(x) for x in rows if x.visibility == "private" and x.owner_id == user.id or x.visibility == "public" and x.approval_status == "approved"]}


@router.post("/{sticker_id}/favorite")
def add_favorite(sticker_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = _visible_query(db, user.id).filter(models.Sticker.id == sticker_id).first()
    if not row: raise HTTPException(404, "Sticker not found")
    exists = db.query(models.StickerFavorite).filter_by(user_id=user.id, sticker_id=sticker_id).first()
    if not exists: db.add(models.StickerFavorite(user_id=user.id, sticker_id=sticker_id)); db.commit()
    return {"message": "Sticker added to favorites"}


@router.delete("/{sticker_id}/favorite")
def remove_favorite(sticker_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    db.query(models.StickerFavorite).filter_by(user_id=user.id, sticker_id=sticker_id).delete(); db.commit()
    return {"message": "Sticker removed from favorites"}


@router.post("/{sticker_id}/use")
def use_sticker(sticker_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = _visible_query(db, user.id).filter(models.Sticker.id == sticker_id).first()
    if not row: raise HTTPException(404, "Sticker not found")
    db.add(models.StickerUsage(user_id=user.id, sticker_id=sticker_id)); db.commit()
    return {"message": "Sticker usage recorded", "sticker": _out(row)}


@router.get("/recent")
def recent_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user), limit: int = Query(30, ge=1, le=100)):
    rows = db.query(models.Sticker).join(models.StickerUsage, models.StickerUsage.sticker_id == models.Sticker.id).filter(models.StickerUsage.user_id == user.id).order_by(desc(models.StickerUsage.used_at)).limit(limit).all()
    unique = []
    seen = set()
    for row in rows:
        if row.id not in seen and (row.owner_id == user.id and row.visibility == "private" or row.visibility == "public" and row.approval_status == "approved"):
            unique.append(_out(row)); seen.add(row.id)
    return {"items": unique}


@router.put("/{sticker_id}")
def update_sticker(sticker_id: int, name: str = Form(...), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id, owner_id=user.id).first()
    if not row: raise HTTPException(404, "Sticker not found")
    row.name = name.strip()[:100]; db.commit(); db.refresh(row)
    return _out(row)


@router.delete("/{sticker_id}")
def delete_sticker(sticker_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id, owner_id=user.id).first()
    if not row: raise HTTPException(404, "Sticker not found")
    db.delete(row); db.commit()
    return {"message": "Sticker deleted"}
