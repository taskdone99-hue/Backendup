from io import BytesIO
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.database import get_db
from app.services.media_service import STATIC_ROOT

router = APIRouter(prefix="/api/stickers", tags=["stickers"])
MAX_STICKER_BYTES = 15 * 1024 * 1024
MAX_EDGE = 512
THUMB_EDGE = 160


def _sent_in_participating_conversation(db: Session, sticker_id: int, user_id: int) -> bool:
    return db.query(models.Message.id).join(
        models.ConversationParticipant,
        models.ConversationParticipant.conversation_id == models.Message.conversation_id,
    ).filter(
        models.Message.sticker_id == sticker_id,
        models.ConversationParticipant.user_id == user_id,
    ).first() is not None


def _can_view(db: Session, row: models.Sticker, user_id: int) -> bool:
    return (
        row.owner_id == user_id
        or (row.visibility == "public" and row.approval_status == "approved")
        or _sent_in_participating_conversation(db, row.id, user_id)
    )


def _is_favorite(db: Session, sticker_id: int, user_id: int) -> bool:
    return db.query(models.StickerFavorite.id).filter_by(
        sticker_id=sticker_id, user_id=user_id
    ).first() is not None


def _out(row: models.Sticker, db: Session, user_id: int):
    return {
        "id": row.id, "owner_id": row.owner_id, "name": row.name,
        "sticker_url": f"/api/stickers/{row.id}/file",
        "thumbnail_url": f"/api/stickers/{row.id}/file?thumbnail=true",
        "mime_type": row.mime_type, "visibility": row.visibility,
        "approval_status": row.approval_status, "created_at": row.created_at,
        "is_favorite": _is_favorite(db, row.id, user_id),
    }


def _sticker_path(url: str | None) -> Path | None:
    if not url:
        return None
    marker = "/static/"
    if marker not in url:
        return None
    relative = url.split(marker, 1)[1]
    path = (STATIC_ROOT / relative).resolve()
    if STATIC_ROOT.resolve() not in path.parents:
        return None
    return path


def _read_image(file: UploadFile) -> Image.Image:
    raw = file.file.read(MAX_STICKER_BYTES + 1)
    if len(raw) > MAX_STICKER_BYTES:
        raise HTTPException(413, "Sticker image exceeds 15 MB")
    if not raw:
        raise HTTPException(400, "Empty sticker image")
    try:
        image = Image.open(BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Invalid image file")
    if image.format not in {"PNG", "WEBP", "JPEG"}:
        raise HTTPException(400, "Only PNG, WebP and JPEG images are supported")
    return image.convert("RGBA")


def _save_sticker_images(image: Image.Image) -> tuple[str, str]:
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
    folder = STATIC_ROOT / "stickers"
    folder.mkdir(parents=True, exist_ok=True)
    stem = uuid4().hex
    original_path = folder / f"{stem}.png"
    thumb_path = folder / f"{stem}_thumb.png"
    image.save(original_path, format="PNG", optimize=True)
    thumb = image.copy()
    thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
    thumb.save(thumb_path, format="PNG", optimize=True)
    return f"/static/stickers/{stem}.png", f"/static/stickers/{stem}_thumb.png"


def _visible_query(db: Session, user_id: int):
    sent_ids = db.query(models.Message.sticker_id).join(
        models.ConversationParticipant,
        models.ConversationParticipant.conversation_id == models.Message.conversation_id,
    ).filter(models.ConversationParticipant.user_id == user_id, models.Message.sticker_id.isnot(None))
    return db.query(models.Sticker).filter(or_(
        (models.Sticker.owner_id == user_id) & (models.Sticker.visibility == "private"),
        (models.Sticker.visibility == "public") & (models.Sticker.approval_status == "approved"),
        models.Sticker.id.in_(sent_ids),
    ))


@router.post("/remove-bg", status_code=status.HTTP_200_OK)
def remove_background(file: UploadFile = File(...), user: models.User = Depends(get_current_user)):
    image = _read_image(file)
    try:
        from rembg import remove
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Background removal engine is not installed on this server. Install rembg and its CPU runtime.",
        )
    try:
        result = remove(image)
        if not isinstance(result, Image.Image):
            result = Image.open(BytesIO(result)).convert("RGBA")
        else:
            result = result.convert("RGBA")
        result.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        output = BytesIO()
        result.save(output, format="PNG", optimize=True)
        return Response(content=output.getvalue(), media_type="image/png")
    except Exception as exc:
        raise HTTPException(422, f"Could not remove image background: {exc.__class__.__name__}")


@router.post("/upload", status_code=status.HTTP_201_CREATED)
def upload_sticker(
    file: UploadFile = File(...), name: str = Form(...),
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
):
    if not name.strip():
        raise HTTPException(400, "Sticker name is required")
    image = _read_image(file)
    url, thumbnail_url = _save_sticker_images(image)
    row = models.Sticker(
        owner_id=user.id, name=name.strip()[:100], sticker_url=url,
        thumbnail_url=thumbnail_url, mime_type="image/png",
        visibility="private", approval_status="approved",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row, db, user.id)


@router.get("/my")
def my_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.query(models.Sticker).filter(models.Sticker.owner_id == user.id).order_by(
        desc(models.Sticker.created_at)
    ).offset(offset).limit(limit).all()
    return {"items": [_out(x, db, user.id) for x in rows], "limit": limit, "offset": offset}


@router.get("/public")
def public_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
                    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = db.query(models.Sticker).filter(
        models.Sticker.visibility == "public",
        models.Sticker.approval_status == "approved",
    ).order_by(desc(models.Sticker.created_at)).offset(offset).limit(limit).all()
    return {"items": [_out(x, db, user.id) for x in rows], "limit": limit, "offset": offset}


@router.get("/search")
def search_stickers(q: str = Query(..., min_length=1, max_length=100),
                    db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = _visible_query(db, user.id).filter(
        models.Sticker.name.ilike(f"%{q.strip()}%")
    ).order_by(desc(models.Sticker.created_at)).limit(50).all()
    return {"items": [_out(x, db, user.id) for x in rows]}


@router.get("/favorites")
def favorites(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.Sticker).join(
        models.StickerFavorite, models.StickerFavorite.sticker_id == models.Sticker.id
    ).filter(models.StickerFavorite.user_id == user.id).all()
    return {"items": [_out(x, db, user.id) for x in rows if _can_view(db, x, user.id)]}


@router.get("/recent")
def recent_stickers(db: Session = Depends(get_db), user: models.User = Depends(get_current_user),
                    limit: int = Query(30, ge=1, le=100)):
    rows = db.query(models.Sticker).join(
        models.StickerUsage, models.StickerUsage.sticker_id == models.Sticker.id
    ).filter(models.StickerUsage.user_id == user.id).order_by(
        desc(models.StickerUsage.used_at)
    ).limit(limit).all()
    unique, seen = [], set()
    for row in rows:
        if row.id not in seen and _can_view(db, row, user.id):
            unique.append(_out(row, db, user.id))
            seen.add(row.id)
    return {"items": unique}


@router.get("/{sticker_id}/file")
def sticker_file(sticker_id: int, thumbnail: bool = Query(False),
                 db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id).first()
    if row is None or not _can_view(db, row, user.id):
        raise HTTPException(404, "Sticker not found")
    path = _sticker_path(row.thumbnail_url if thumbnail else row.sticker_url)
    if path is None or not path.is_file():
        raise HTTPException(404, "Sticker file not found")
    return FileResponse(path, media_type=row.mime_type, filename=path.name)


@router.get("/{sticker_id}")
def get_sticker(sticker_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id).first()
    if row is None or not _can_view(db, row, user.id):
        raise HTTPException(404, "Sticker not found")
    return _out(row, db, user.id)


@router.post("/{sticker_id}/favorite")
def add_favorite(sticker_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id).first()
    if row is None or not _can_view(db, row, user.id):
        raise HTTPException(404, "Sticker not found")
    exists = db.query(models.StickerFavorite).filter_by(
        user_id=user.id, sticker_id=sticker_id
    ).first()
    if not exists:
        db.add(models.StickerFavorite(user_id=user.id, sticker_id=sticker_id))
        db.commit()
    return {"message": "Sticker added to favorites", "is_favorite": True}


@router.delete("/{sticker_id}/favorite")
def remove_favorite(sticker_id: int, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id).first()
    if row is None or not _can_view(db, row, user.id):
        raise HTTPException(404, "Sticker not found")
    db.query(models.StickerFavorite).filter_by(
        user_id=user.id, sticker_id=sticker_id
    ).delete()
    db.commit()
    return {"message": "Sticker removed from favorites", "is_favorite": False}


@router.post("/{sticker_id}/use")
def use_sticker(sticker_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id).first()
    if row is None or not _can_view(db, row, user.id):
        raise HTTPException(404, "Sticker not found")
    db.add(models.StickerUsage(user_id=user.id, sticker_id=sticker_id))
    db.commit()
    return {"message": "Sticker usage recorded", "sticker": _out(row, db, user.id)}


@router.put("/{sticker_id}")
def update_sticker(sticker_id: int, name: str = Form(...),
                   db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id, owner_id=user.id).first()
    if not row:
        raise HTTPException(404, "Sticker not found")
    row.name = name.strip()[:100]
    db.commit()
    db.refresh(row)
    return _out(row, db, user.id)


@router.delete("/{sticker_id}")
def delete_sticker(sticker_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    row = db.query(models.Sticker).filter_by(id=sticker_id, owner_id=user.id).first()
    if not row:
        raise HTTPException(404, "Sticker not found")
    db.delete(row)
    db.commit()
    return {"message": "Sticker deleted"}
