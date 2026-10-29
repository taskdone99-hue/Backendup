"""Call REST APIs, WebRTC signaling WebSocket, Not Interested, and Theme preferences."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app import models
from app.auth import get_current_user, get_user_from_raw_token
from app.database import get_db, SessionLocal

router = APIRouter(tags=["Calls", "Not Interested", "Theme"])

# In-process socket registry. For multiple worker/server instances, replace with Redis Pub/Sub.
_call_sockets: dict[int, set[WebSocket]] = {}

async def _send_user(user_id: int, payload: dict) -> None:
    dead = []
    for ws in list(_call_sockets.get(user_id, set())):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _call_sockets.get(user_id, set()).discard(ws)

def _call_or_404(db: Session, call_id: int) -> models.Call:
    call = db.query(models.Call).filter(models.Call.id == call_id).first()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call

def _participant(call: models.Call, user_id: int) -> bool:
    return user_id in (call.caller_id, call.receiver_id)

def _call_json(call: models.Call) -> dict:
    return {"id": call.id, "caller_id": call.caller_id, "receiver_id": call.receiver_id,
            "call_type": call.call_type, "status": call.status.value if hasattr(call.status, "value") else call.status,
            "created_at": call.created_at.isoformat() if call.created_at else None,
            "answered_at": call.answered_at.isoformat() if call.answered_at else None,
            "ended_at": call.ended_at.isoformat() if call.ended_at else None}

class CallCreate(BaseModel):
    receiver_id: int = Field(gt=0)
    call_type: Literal["audio", "video"]

class CallAction(BaseModel):
    action: Literal["accept", "reject", "end"]

class NotInterestedIn(BaseModel):
    content_type: Literal["post", "reel"]
    content_id: int = Field(gt=0)

class ThemeIn(BaseModel):
    theme: Literal["light", "dark", "system"]

@router.post("/api/calls", status_code=status.HTTP_201_CREATED)
async def create_call(data: CallCreate, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    receiver = db.query(models.User).filter(models.User.id == data.receiver_id, models.User.is_active.is_(True), models.User.is_suspended.is_(False)).first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")
    if receiver.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot call yourself")
    active = db.query(models.Call).filter(
        ((models.Call.caller_id == user.id) | (models.Call.receiver_id == user.id)),
        models.Call.status.in_([models.CallStatus.ringing, models.CallStatus.accepted])
    ).first()
    if active:
        raise HTTPException(status_code=409, detail="You already have an active call")
    call = models.Call(caller_id=user.id, receiver_id=receiver.id, call_type=data.call_type, status=models.CallStatus.ringing)
    db.add(call); db.commit(); db.refresh(call)
    result = _call_json(call)
    await _send_user(receiver.id, {"type": "incoming_call", "call": result})
    return result

@router.get("/api/calls")
def list_calls(limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.Call).filter((models.Call.caller_id == user.id) | (models.Call.receiver_id == user.id)).order_by(models.Call.id.desc()).limit(limit).all()
    return {"calls": [_call_json(c) for c in rows]}

@router.get("/api/calls/{call_id}")
def get_call(call_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    call = _call_or_404(db, call_id)
    if not _participant(call, user.id): raise HTTPException(status_code=403, detail="Not a call participant")
    return _call_json(call)

@router.post("/api/calls/{call_id}/action")
async def call_action(call_id: int, data: CallAction, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    call = _call_or_404(db, call_id)
    if not _participant(call, user.id): raise HTTPException(status_code=403, detail="Not a call participant")
    now = datetime.now(timezone.utc)
    if data.action == "accept":
        if user.id != call.receiver_id: raise HTTPException(status_code=403, detail="Only receiver can accept")
        if call.status != models.CallStatus.ringing: raise HTTPException(status_code=409, detail="Call is no longer ringing")
        call.status = models.CallStatus.accepted; call.answered_at = now
    elif data.action == "reject":
        if user.id != call.receiver_id: raise HTTPException(status_code=403, detail="Only receiver can reject")
        if call.status != models.CallStatus.ringing: raise HTTPException(status_code=409, detail="Call is no longer ringing")
        call.status = models.CallStatus.rejected; call.ended_at = now
    else:
        if call.status in (models.CallStatus.ended, models.CallStatus.rejected, models.CallStatus.missed):
            raise HTTPException(status_code=409, detail="Call has already ended")
        call.status = models.CallStatus.ended; call.ended_at = now
    db.commit(); db.refresh(call); result = _call_json(call)
    other = call.receiver_id if user.id == call.caller_id else call.caller_id
    await _send_user(other, {"type": "call_update", "call": result})
    return result

@router.websocket("/api/calls/ws")
async def call_signaling_ws(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()
    db = SessionLocal()
    user = get_user_from_raw_token(token, db)
    if user is None:
        db.close(); await websocket.close(code=4401); return
    user_id = user.id
    _call_sockets.setdefault(user_id, set()).add(websocket)
    try:
        await websocket.send_json({"type": "connected", "user_id": user_id})
        while True:
            data = await websocket.receive_json()
            event_type = data.get("type")
            if event_type == "ping":
                await websocket.send_json({"type": "pong"}); continue
            if event_type not in {"offer", "answer", "ice_candidate", "hangup", "call_state"}:
                await websocket.send_json({"type": "error", "message": "Unsupported signaling type"}); continue
            try: call_id = int(data.get("call_id"))
            except (TypeError, ValueError):
                await websocket.send_json({"type": "error", "message": "call_id is required"}); continue
            call = db.query(models.Call).filter(models.Call.id == call_id).first()
            if call is None or not _participant(call, user_id):
                await websocket.send_json({"type": "error", "message": "Call not found or access denied"}); continue
            if call.status not in (models.CallStatus.ringing, models.CallStatus.accepted) and event_type != "hangup":
                await websocket.send_json({"type": "error", "message": "Call is not active"}); continue
            target = call.receiver_id if user_id == call.caller_id else call.caller_id
            await _send_user(target, {"type": event_type, "call_id": call_id, "sender_id": user_id, "payload": data.get("payload")})
    except WebSocketDisconnect:
        pass
    finally:
        _call_sockets.get(user_id, set()).discard(websocket)
        if not _call_sockets.get(user_id): _call_sockets.pop(user_id, None)
        db.close()

@router.post("/api/not-interested", status_code=status.HTTP_201_CREATED)
def mark_not_interested(data: NotInterestedIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if data.content_type == "post":
        exists = db.query(models.Post.id).filter(models.Post.id == data.content_id).first()
    else:
        exists = db.query(models.Reel.id).filter(models.Reel.id == data.content_id).first()
    if not exists: raise HTTPException(status_code=404, detail=f"{data.content_type.title()} not found")
    row = db.query(models.NotInterested).filter_by(user_id=user.id, content_type=data.content_type, content_id=data.content_id).first()
    if row:
        return {"message": "Content already marked not interested", "content_type": data.content_type, "content_id": data.content_id}
    row = models.NotInterested(user_id=user.id, content_type=data.content_type, content_id=data.content_id)
    db.add(row)
    try: db.commit()
    except IntegrityError:
        db.rollback()
    return {"message": "Content marked not interested", "content_type": data.content_type, "content_id": data.content_id}

@router.get("/api/not-interested")
def list_not_interested(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.query(models.NotInterested).filter(models.NotInterested.user_id == user.id).order_by(models.NotInterested.id.desc()).all()
    return {"items": [{"content_type": r.content_type, "content_id": r.content_id, "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]}

@router.delete("/api/not-interested/{content_type}/{content_id}")
def undo_not_interested(content_type: Literal["post", "reel"], content_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.NotInterested).filter_by(user_id=user.id, content_type=content_type, content_id=content_id).first()
    if not row: raise HTTPException(status_code=404, detail="Not Interested record not found")
    db.delete(row); db.commit()
    return {"message": "Not Interested preference removed"}

@router.get("/api/themes")
def get_themes():
    return {"themes": [{"id": "light", "name": "Light"}, {"id": "dark", "name": "Dark"}, {"id": "system", "name": "System"}]}

@router.get("/api/users/me/theme")
def get_my_theme(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserThemePreference).filter_by(user_id=user.id).first()
    return {"theme": row.theme if row else "system"}

@router.put("/api/users/me/theme")
def set_my_theme(data: ThemeIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    row = db.query(models.UserThemePreference).filter_by(user_id=user.id).first()
    if row: row.theme = data.theme; row.updated_at = datetime.now(timezone.utc)
    else: db.add(models.UserThemePreference(user_id=user.id, theme=data.theme))
    db.commit()
    return {"message": "Theme updated", "theme": data.theme}
