"""
Saved tab: bookmarking posts/reels/audio/series, filterable by category
(All / Posts / Reels / Audio / Series), plus user-created Collections for
organizing them.

Backed by models.SavedItem (a single table keyed by target_type/target_id —
see models.SavedItemType), which replaced the old posts-only SavedPost
table. POST/DELETE /api/posts/:id/save and /api/reels/:id/save also write
to this same table (see content_routes._save_target/_unsave_target), so
everything stays in sync regardless of which endpoint was used to save.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user

router = APIRouter(prefix="/api/saved", tags=["saved"])


def _get_post_or_404(db: Session, post_id: int) -> models.Post:
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return post


def _get_reel_or_404(db: Session, reel_id: int) -> models.Reel:
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reel not found")
    return reel


def _get_audio_or_404(db: Session, audio_id: int) -> models.Audio:
    audio = db.query(models.Audio).filter(models.Audio.id == audio_id).first()
    if audio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio not found")
    return audio


def _get_series_or_404(db: Session, series_id: int) -> models.Series:
    series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if series is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Series not found")
    return series


def _target_exists(db: Session, target_type: models.SavedItemType, target_id: int) -> bool:
    model = {
        models.SavedItemType.post: models.Post,
        models.SavedItemType.reel: models.Reel,
        models.SavedItemType.audio: models.Audio,
        models.SavedItemType.series: models.Series,
    }[target_type]
    return db.query(model.id).filter(model.id == target_id).first() is not None


def _to_series_out(db: Session, series: models.Series) -> schemas.SeriesOut:
    out = schemas.SeriesOut.model_validate(series)
    out.reels_count = (
        db.query(models.SeriesReel).filter(models.SeriesReel.series_id == series.id).count()
    )
    return out


def _to_saved_item_out(
    db: Session, item: models.SavedItem, viewer_id: int
) -> schemas.SavedItemOut | None:
    """None if the underlying post/reel/etc has since been deleted (an
    orphaned SavedItem row) — caller filters those out."""
    # Imported here (not at module scope) to avoid a circular import with
    # content_routes, which itself imports from this module's siblings.
    from app.routers.content_routes import _to_post_detail, _to_reel_detail

    out = schemas.SavedItemOut(type=item.target_type, saved_at=item.created_at)

    if item.target_type == models.SavedItemType.post:
        post = db.query(models.Post).filter(models.Post.id == item.target_id).first()
        if post is None:
            return None
        out.post = _to_post_detail(db, post, viewer_id)
    elif item.target_type == models.SavedItemType.reel:
        reel = db.query(models.Reel).filter(models.Reel.id == item.target_id).first()
        if reel is None:
            return None
        out.reel = _to_reel_detail(db, reel, viewer_id)
    elif item.target_type == models.SavedItemType.audio:
        audio = db.query(models.Audio).filter(models.Audio.id == item.target_id).first()
        if audio is None:
            return None
        out.audio = schemas.AudioOut.model_validate(audio)
    elif item.target_type == models.SavedItemType.series:
        series = db.query(models.Series).filter(models.Series.id == item.target_id).first()
        if series is None:
            return None
        out.series = _to_series_out(db, series)

    return out


# ---- Save / unsave any target type ----

@router.post("", response_model=schemas.MessageResponse, status_code=status.HTTP_201_CREATED)
def save_item(
    payload: schemas.SaveItemRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not _target_exists(db, payload.target_type, payload.target_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{payload.target_type.value.capitalize()} not found",
        )

    existing = (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == current_user.id,
            models.SavedItem.target_type == payload.target_type,
            models.SavedItem.target_id == payload.target_id,
        )
        .first()
    )
    if existing is None:
        db.add(
            models.SavedItem(
                user_id=current_user.id,
                target_type=payload.target_type,
                target_id=payload.target_id,
            )
        )
        db.commit()

    return schemas.MessageResponse(message=f"{payload.target_type.value.capitalize()} saved")


@router.delete("", response_model=schemas.MessageResponse)
def remove_saved_item(
    target_type: models.SavedItemType = Query(...),
    target_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    existing = (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == current_user.id,
            models.SavedItem.target_type == target_type,
            models.SavedItem.target_id == target_id,
        )
        .first()
    )
    if existing is not None:
        db.delete(existing)
        db.commit()

    return schemas.MessageResponse(message=f"{target_type.value.capitalize()} unsaved")


# NOTE: /collections must be registered before nothing conflicts here since
# this router has no other "/{...}" catch-all — but keep collections routes
# grouped below for clarity regardless.

@router.get("", response_model=schemas.PaginatedSavedResponse)
def get_saved_items(
    category: str = Query(
        "all",
        pattern="^(all|posts|reels|audio|series)$",
        description="all | posts | reels | audio | series",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """The Saved tab. `category=all` (default) mixes every saved type
    together, newest-saved first; pass posts/reels/audio/series to filter
    to just one. Collections are a separate view — see GET /collections."""
    query = db.query(models.SavedItem).filter(models.SavedItem.user_id == current_user.id)

    category_map = {
        "posts": models.SavedItemType.post,
        "reels": models.SavedItemType.reel,
        "audio": models.SavedItemType.audio,
        "series": models.SavedItemType.series,
    }
    if category != "all":
        query = query.filter(models.SavedItem.target_type == category_map[category])

    total = query.count()
    rows = query.order_by(models.SavedItem.created_at.desc()).offset(offset).limit(limit).all()

    items = []
    for row in rows:
        out = _to_saved_item_out(db, row, current_user.id)
        if out is not None:
            items.append(out)

    return schemas.PaginatedSavedResponse(total=total, limit=limit, offset=offset, items=items)


# ---- Collections ----

@router.get("/collections", response_model=schemas.SavedCollectionsResponse)
def get_collections(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    collections = (
        db.query(models.SavedCollection)
        .filter(models.SavedCollection.user_id == current_user.id)
        .order_by(models.SavedCollection.created_at.desc())
        .all()
    )
    items = []
    for c in collections:
        out = schemas.SavedCollectionOut.model_validate(c)
        out.items_count = (
            db.query(models.SavedCollectionItem)
            .filter(models.SavedCollectionItem.collection_id == c.id)
            .count()
        )
        items.append(out)
    return schemas.SavedCollectionsResponse(items=items)


@router.post(
    "/collections", response_model=schemas.SavedCollectionOut, status_code=status.HTTP_201_CREATED
)
def create_collection(
    payload: schemas.SavedCollectionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    collection = models.SavedCollection(
        user_id=current_user.id, name=payload.name, cover_url=payload.cover_url
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    out = schemas.SavedCollectionOut.model_validate(collection)
    out.items_count = 0
    return out


@router.delete("/collections/{collection_id}", response_model=schemas.MessageResponse)
def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    collection = (
        db.query(models.SavedCollection)
        .filter(models.SavedCollection.id == collection_id)
        .first()
    )
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own collections"
        )
    db.delete(collection)  # cascades saved_collection_items
    db.commit()
    return schemas.MessageResponse(message="Collection deleted")


@router.get("/collections/{collection_id}", response_model=schemas.PaginatedSavedResponse)
def get_collection_items(
    collection_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    collection = (
        db.query(models.SavedCollection)
        .filter(models.SavedCollection.id == collection_id)
        .first()
    )
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own collections"
        )

    query = (
        db.query(models.SavedItem)
        .join(
            models.SavedCollectionItem,
            models.SavedCollectionItem.saved_item_id == models.SavedItem.id,
        )
        .filter(models.SavedCollectionItem.collection_id == collection_id)
    )
    total = query.count()
    rows = (
        query.order_by(models.SavedCollectionItem.added_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = []
    for row in rows:
        out = _to_saved_item_out(db, row, current_user.id)
        if out is not None:
            items.append(out)

    return schemas.PaginatedSavedResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/collections/{collection_id}/items", response_model=schemas.MessageResponse)
def add_to_collection(
    collection_id: int,
    payload: schemas.AddToCollectionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Adds a target to a collection, saving it first if it wasn't already."""
    collection = (
        db.query(models.SavedCollection)
        .filter(models.SavedCollection.id == collection_id)
        .first()
    )
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own collections"
        )
    if not _target_exists(db, payload.target_type, payload.target_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{payload.target_type.value.capitalize()} not found",
        )

    saved_item = (
        db.query(models.SavedItem)
        .filter(
            models.SavedItem.user_id == current_user.id,
            models.SavedItem.target_type == payload.target_type,
            models.SavedItem.target_id == payload.target_id,
        )
        .first()
    )
    if saved_item is None:
        saved_item = models.SavedItem(
            user_id=current_user.id,
            target_type=payload.target_type,
            target_id=payload.target_id,
        )
        db.add(saved_item)
        db.flush()

    existing_link = (
        db.query(models.SavedCollectionItem)
        .filter(
            models.SavedCollectionItem.collection_id == collection_id,
            models.SavedCollectionItem.saved_item_id == saved_item.id,
        )
        .first()
    )
    if existing_link is None:
        db.add(
            models.SavedCollectionItem(collection_id=collection_id, saved_item_id=saved_item.id)
        )
    db.commit()
    return schemas.MessageResponse(message="Added to collection")


@router.delete(
    "/collections/{collection_id}/items/{saved_item_id}", response_model=schemas.MessageResponse
)
def remove_from_collection(
    collection_id: int,
    saved_item_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    collection = (
        db.query(models.SavedCollection)
        .filter(models.SavedCollection.id == collection_id)
        .first()
    )
    if collection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    if collection.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own collections"
        )

    link = (
        db.query(models.SavedCollectionItem)
        .filter(
            models.SavedCollectionItem.collection_id == collection_id,
            models.SavedCollectionItem.saved_item_id == saved_item_id,
        )
        .first()
    )
    if link is not None:
        db.delete(link)
        db.commit()
    return schemas.MessageResponse(message="Removed from collection")
