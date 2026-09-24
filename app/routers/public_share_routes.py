"""
What a shared link opens. GET /api/share/{post_id}/link and
/api/share/reels/{reel_id}/link hand out `{PUBLIC_BASE_URL}/p/{id}` and
`/r/{id}`; these are the routes that answer those URLs.

Each returns a small, self-contained HTML page for people who don't have the
app open (browser, or a chat app's link-preview crawler): Open Graph / Twitter
tags so WhatsApp, Telegram, iMessage etc. show a preview card, plus an "Open
in app" button when APP_DEEP_LINK_SCHEME is set and a "Get the app" link when
APP_DOWNLOAD_URL is set. In-app, the client should intercept the same URL and
open the post/reel natively.

Privacy: this page is public and unauthenticated, so it only ever shows
content that an anonymous visitor could already see through the API (author
active, not suspended, account not private). A private account's content —
and a blocked/hidden one, which can't be known without a viewer — gets a
generic "open in the app" page with none of the caption/author/media. Every
piece of user text is HTML-escaped. Pages are marked noindex so search
engines don't list users' posts (link-preview crawlers ignore noindex).
"""

import html
import os
import re

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import models
from app.database import get_db

router = APIRouter(tags=["public-share"], include_in_schema=False)

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*$")


def _base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _absolute(url: str | None) -> str | None:
    """Preview crawlers need absolute URLs; uploads may be stored relative."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    base = _base_url()
    return f"{base}{url if url.startswith('/') else '/' + url}" if base else None


def _app_name() -> str:
    return os.getenv("APP_NAME", "").strip() or "the app"


def _deep_link(kind_path: str, content_id: int) -> str | None:
    scheme = os.getenv("APP_DEEP_LINK_SCHEME", "").strip().lower()
    if scheme and _SCHEME_RE.match(scheme):
        return f"{scheme}://{kind_path}/{content_id}"
    return None


def _download_url() -> str | None:
    url = os.getenv("APP_DOWNLOAD_URL", "").strip()
    return url if url.startswith(("http://", "https://")) else None


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _page(
    *,
    title: str,
    description: str,
    canonical: str | None,
    image: str | None = None,
    video: str | None = None,
    og_type: str = "website",
    open_url: str | None = None,
    body_heading: str,
    body_text: str,
) -> str:
    name = _app_name()
    meta = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(title)}</title>",
        '<meta name="robots" content="noindex, nofollow">',
        f'<meta name="description" content="{_e(description)}">',
        f'<meta property="og:site_name" content="{_e(name)}">',
        f'<meta property="og:type" content="{_e(og_type)}">',
        f'<meta property="og:title" content="{_e(title)}">',
        f'<meta property="og:description" content="{_e(description)}">',
        f'<meta name="twitter:card" content="{"summary_large_image" if image else "summary"}">',
        f'<meta name="twitter:title" content="{_e(title)}">',
        f'<meta name="twitter:description" content="{_e(description)}">',
    ]
    if canonical:
        meta.append(f'<meta property="og:url" content="{_e(canonical)}">')
    if image:
        meta.append(f'<meta property="og:image" content="{_e(image)}">')
        meta.append(f'<meta name="twitter:image" content="{_e(image)}">')
    if video:
        meta.append(f'<meta property="og:video" content="{_e(video)}">')

    buttons = []
    if open_url:
        buttons.append(f'<a class="btn" href="{_e(open_url)}">Open in app</a>')
    if _download_url():
        buttons.append(f'<a class="btn alt" href="{_e(_download_url())}">Get the app</a>')

    preview = f'<img src="{_e(image)}" alt="">' if image else ""
    return (
        "<!doctype html>\n<html lang=\"en\"><head>\n"
        + "\n".join(meta)
        + "\n<style>body{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#111}"
        ".card{max-width:420px;margin:8vh auto;background:#fff;border:1px solid #ddd;"
        "border-radius:12px;padding:20px;text-align:center}"
        "img{max-width:100%;border-radius:8px}"
        ".btn{display:inline-block;margin:8px 4px;padding:10px 18px;border-radius:8px;"
        "background:#0095f6;color:#fff;text-decoration:none}.btn.alt{background:#eee;color:#111}"
        "</style>\n</head><body><div class=\"card\">"
        f"{preview}<h1 style=\"font-size:1.1rem\">{_e(body_heading)}</h1>"
        f"<p>{_e(body_text)}</p>{''.join(buttons)}</div></body></html>"
    )


def _unavailable(status_code: int, canonical: str | None, open_url: str | None) -> HTMLResponse:
    name = _app_name()
    return HTMLResponse(
        _page(
            title=f"Open in {name}" if status_code == 200 else "Content not available",
            description=f"Open this link in {name} to view it.",
            canonical=None,
            open_url=open_url,
            body_heading=(
                f"Open this in {name}" if status_code == 200 else "This content isn't available"
            ),
            body_text=(
                "Sign in to the app to see it." if status_code == 200
                else "It may have been removed."
            ),
        ),
        status_code=status_code,
    )


def _author_publicly_visible(author: models.User | None) -> bool:
    return (
        author is not None
        and author.is_active
        and not author.is_suspended
        and not author.is_private
    )


def _who(author: models.User) -> str:
    handle = f"@{author.username}"
    return f"{author.full_name} ({handle})" if author.full_name else handle


def _snippet(text: str | None, fallback: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return fallback
    return text if len(text) <= 200 else text[:197] + "..."


@router.get("/p/{post_id}", response_class=HTMLResponse)
def post_landing(post_id: int, db: Session = Depends(get_db)):
    open_url = _deep_link("p", post_id)
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if post is None:
        return _unavailable(404, None, open_url)
    if not _author_publicly_visible(post.user):
        return _unavailable(200, None, open_url)

    media = sorted(post.media_items, key=lambda m: m.position) if post.media_items else []
    first_url = media[0].media_url if media else post.media_url
    first_type = media[0].media_type if media else post.media_type
    is_image = first_type == models.MediaType.image
    name = _app_name()
    title = f"{_who(post.user)} on {name}"
    description = _snippet(post.caption, f"See this post on {name}")
    canonical = f"{_base_url()}/p/{post_id}" if _base_url() else None
    return HTMLResponse(_page(
        title=title,
        description=description,
        canonical=canonical,
        image=_absolute(first_url) if is_image else None,
        video=None if is_image else _absolute(first_url),
        og_type="article",
        open_url=open_url,
        body_heading=title,
        body_text=description,
    ))


@router.get("/r/{reel_id}", response_class=HTMLResponse)
def reel_landing(reel_id: int, db: Session = Depends(get_db)):
    open_url = _deep_link("r", reel_id)
    reel = db.query(models.Reel).filter(models.Reel.id == reel_id).first()
    if reel is None:
        return _unavailable(404, None, open_url)
    if not _author_publicly_visible(reel.user):
        return _unavailable(200, None, open_url)

    name = _app_name()
    title = f"{_who(reel.user)} on {name}"
    description = _snippet(reel.caption, f"Watch this reel on {name}")
    canonical = f"{_base_url()}/r/{reel_id}" if _base_url() else None
    return HTMLResponse(_page(
        title=title,
        description=description,
        canonical=canonical,
        image=_absolute(reel.thumbnail_url),
        video=_absolute(reel.video_url),
        og_type="video.other",
        open_url=open_url,
        body_heading=title,
        body_text=description,
    ))
