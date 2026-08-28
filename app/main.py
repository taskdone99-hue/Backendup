import os

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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
)

# Creates tables if they don't exist yet (fine for dev; use Alembic migrations in production).
# For an existing DB that already has a `users` table, also run
# `python -m app.add_profile_columns` once to add the new profile columns —
# create_all() only creates missing tables, it doesn't alter existing ones.
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


@app.get("/")
def health_check():
    return {"status": "ok"}