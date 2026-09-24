import os

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.database import Base, engine
from app.routers import (
    auth_routes,
    user_routes,
    story_routes,
    content_routes,
    watch_routes,
    comment_routes,
    video_routes,
    highlight_routes,
    saved_routes,
    share_routes,
    snap_routes,
    chat_routes,
    notification_routes,
    post_details_routes,
    membership_routes,
    payment_routes,
    discord_routes,
    ads_routes,
    config_routes,
    location_routes,
    monetization_routes,
    creator_collaboration_routes,
    brand_collaboration_routes,
    search_routes,
    hashtag_routes,
    privacy_routes,
    audio_routes,
    live_routes,
    report_routes,
    tag_routes,
    public_share_routes,
)

# Creates tables if they don't exist yet (fine for dev; use Alembic migrations in production).
# For an existing DB that already has a `users` table, also run
# `python -m app.add_profile_columns` once to add the new profile columns —
# create_all() only creates missing tables, it doesn't alter existing ones.
# The post_media/hashtags/post_hashtags/user_blocks/user_restricts/user_mutes/
# conversation_mutes/story_mentions/story_polls/story_poll_options/
# story_poll_votes/story_questions/story_question_responses tables are all
# brand new, so create_all() does create them automatically — but for a DB
# with pre-existing data, also run these once each, afterward:
#   python -m app.backfill_post_media_and_hashtags   (posts made before that update)
#   python -m app.add_reel_location_columns          (adds location_* to the existing reels table)
#   python -m app.add_dm_media_and_request_columns   (adds media/reply/request columns to messages + conversation_participants)
#   python -m app.add_post_media_caption_column      (adds per-photo caption to the existing post_media table)
Base.metadata.create_all(bind=engine)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app = FastAPI(title="Phone OTP Auth API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # replace with your frontend's actual origin(s) in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Replaces FastAPI's default {"detail": [...]} validation error body with a
    simpler shape the frontend can read directly, e.g.:
        {"message": "..."}
    Only the first error is surfaced (if the request had multiple invalid
    fields, only the first one is reported).
    """
    def clean_msg(msg: str) -> str:
        # Pydantic prefixes messages raised from @field_validator with
        # "Value error, " — strip that so the API doesn't leak internal wording.
        prefix = "Value error, "
        return msg[len(prefix):] if msg.startswith(prefix) else msg

    first = exc.errors()[0]

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"message": clean_msg(first["msg"])},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """
    Every `raise HTTPException(status_code=..., detail="...")` across the app
    (404 not found, 400 bad request, 403 forbidden, etc.) normally produces
    FastAPI's default body {"detail": "..."}. This overrides that so every
    endpoint returns the same simple shape instead: {"message": "..."}.
    Status code and any headers set on the exception are preserved as-is.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail},
        headers=getattr(exc, "headers", None),
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(auth_routes.router)
app.include_router(user_routes.router)
app.include_router(story_routes.router)
app.include_router(content_routes.router)
app.include_router(content_routes.reels_router)
app.include_router(watch_routes.router)
app.include_router(comment_routes.router)
app.include_router(video_routes.router)
app.include_router(highlight_routes.router)
app.include_router(saved_routes.router)
app.include_router(share_routes.router)
app.include_router(snap_routes.router)
app.include_router(chat_routes.router)
app.include_router(notification_routes.router)
app.include_router(post_details_routes.router)
app.include_router(membership_routes.router)
app.include_router(payment_routes.router)
app.include_router(discord_routes.router)
app.include_router(ads_routes.router)
app.include_router(config_routes.router)
app.include_router(location_routes.router)
app.include_router(monetization_routes.router)
app.include_router(creator_collaboration_routes.router)
app.include_router(brand_collaboration_routes.router)
app.include_router(privacy_routes.router)
app.include_router(search_routes.router)
app.include_router(hashtag_routes.router)
app.include_router(audio_routes.router)
app.include_router(live_routes.router)
app.include_router(report_routes.router)
app.include_router(report_routes.admin_router)
app.include_router(tag_routes.router)
app.include_router(public_share_routes.router)


@app.get("/")
def health_check():
    return {"status": "ok"}