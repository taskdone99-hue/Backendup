"""User language preferences and authenticated LibreTranslate proxy."""
import hashlib
import os
import re
import time
from collections import defaultdict, deque
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User

router = APIRouter(tags=["translation"])

LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "https://libretranslate.com") .rstrip("/")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "")
MAX_TEXT_LENGTH = 5000
# Small process-local cache; replace with Redis if a shared cache is configured.
_translation_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = 3600
_rate_events: dict[int, deque[float]] = defaultdict(deque)
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
SUPPORTED_FALLBACK = [
    {"code": "en", "name": "English"}, {"code": "te", "name": "తెలుగు"},
    {"code": "hi", "name": "हिन्दी"}, {"code": "ta", "name": "தமிழ்"},
    {"code": "kn", "name": "ಕನ್ನಡ"}, {"code": "ml", "name": "മലയാളം"},
    {"code": "mr", "name": "मराठी"}, {"code": "bn", "name": "বাংলা"},
    {"code": "ur", "name": "اردو"},
]

class LanguageUpdate(BaseModel):
    language: str = Field(min_length=2, max_length=10, examples=["te"])
    @field_validator("language")
    @classmethod
    def valid_language(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", value):
            raise ValueError("Invalid language code")
        return value

class TranslationRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    source: str = Field(default="auto", min_length=2, max_length=10)
    target: str = Field(min_length=2, max_length=10, examples=["te"])
    @field_validator("text")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text cannot be blank")
        return value
    @field_validator("source", "target")
    @classmethod
    def valid_code(cls, value: str) -> str:
        value = value.strip().lower()
        if value != "auto" and not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", value):
            raise ValueError("Invalid language code")
        return value

@router.get("/api/users/me/language", summary="Get my preferred language")
def get_my_language(user: User = Depends(get_current_user)):
    return {"language": user.preferred_language or "en"}

@router.put("/api/users/me/language", summary="Update my preferred language")
def update_my_language(payload: LanguageUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.preferred_language = payload.language
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"language": user.preferred_language}

@router.get("/api/translate/languages", summary="List languages supported by translation provider")
async def list_languages(user: User = Depends(get_current_user)):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"{LIBRETRANSLATE_URL}/languages")
            response.raise_for_status()
            rows = response.json()
            if isinstance(rows, list) and rows:
                return {"items": [{"code": x.get("code"), "name": x.get("name")} for x in rows if x.get("code") and x.get("name")]}
    except (httpx.HTTPError, ValueError):
        pass
    return {"items": SUPPORTED_FALLBACK, "source": "fallback"}

@router.post("/api/translate", summary="Translate text without changing stored original content")
async def translate(payload: TranslationRequest, request: Request, user: User = Depends(get_current_user)):
    # Per-process best-effort rate limit; use shared Redis rate limiting in multi-worker deployments.
    now = time.time()
    events = _rate_events[user.id]
    while events and events[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
        events.popleft()
    if len(events) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Translation rate limit exceeded; try again shortly")
    events.append(now)
    source = payload.source
    target = payload.target
    if source == target:
        return {"translatedText": payload.text, "source": source, "target": target, "cached": True}
    cache_key = hashlib.sha256(f"{source}\0{target}\0{payload.text}".encode("utf-8")).hexdigest()
    now = time.time()
    cached = _translation_cache.get(cache_key)
    if cached and cached[0] > now:
        return {**cached[1], "cached": True}
    body = {"q": payload.text, "source": source, "target": target, "format": "text"}
    if LIBRETRANSLATE_API_KEY:
        body["api_key"] = LIBRETRANSLATE_API_KEY
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{LIBRETRANSLATE_URL}/translate", json=body)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=502, detail="Translation service is unavailable")
    translated = data.get("translatedText") if isinstance(data, dict) else None
    if not isinstance(translated, str):
        raise HTTPException(status_code=502, detail="Translation provider returned an invalid response")
    result = {"translatedText": translated, "source": source, "target": target}
    if len(_translation_cache) > 2000:
        # Opportunistic cleanup; no background task needed.
        for key, (expiry, _) in list(_translation_cache.items()):
            if expiry <= now:
                _translation_cache.pop(key, None)
        if len(_translation_cache) > 2000:
            _translation_cache.clear()
    _translation_cache[cache_key] = (now + CACHE_TTL_SECONDS, result)
    return {**result, "cached": False}
