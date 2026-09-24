"""
Shared links: GET /api/share/{id}/link and /api/share/reels/{id}/link hand out
{PUBLIC_BASE_URL}/p/{id} and /r/{id}; GET /p/{id} and /r/{id} answer them with
a link-preview page (Open Graph tags) that never exposes private content.
"""

import pytest

from app import models

BASE = "https://app.example.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_DEEP_LINK_SCHEME", raising=False)
    monkeypatch.delenv("APP_DOWNLOAD_URL", raising=False)
    # share_routes reads the base URL at import time
    from app.routers import share_routes
    monkeypatch.setattr(share_routes, "PUBLIC_BASE_URL", BASE)


@pytest.fixture()
def make_named_user(db, make_user):
    """make_user plus profile fields the shared fixture doesn't take."""
    def _make(username, *, full_name=None, is_suspended=False, **kw):
        user = make_user(username, **kw)
        user.full_name = full_name
        user.is_suspended = is_suspended
        db.commit()
        return user
    return _make


@pytest.fixture()
def make_post(db):
    def _make(owner, caption="hello", media_url="/static/posts/a.jpg", **kw):
        post = models.Post(user_id=owner.id, media_url=media_url, caption=caption, **kw)
        db.add(post)
        db.commit()
        db.refresh(post)
        return post
    return _make


# ------------------------------------------------------- link generation

def test_link_endpoints_return_p_and_r_urls(client, make_user, make_post, make_reel):
    owner = make_user("owner")
    post, reel = make_post(owner), make_reel(owner)
    assert client.get(f"/api/share/{post.id}/link").json()["url"] == f"{BASE}/p/{post.id}"
    assert client.get(f"/api/share/reels/{reel.id}/link").json()["url"] == f"{BASE}/r/{reel.id}"


def test_link_unknown_content_is_404(client):
    assert client.get("/api/share/9999/link").status_code == 404
    assert client.get("/api/share/reels/9999/link").status_code == 404


def test_link_for_private_account_requires_following(
    client, make_user, make_post, make_reel, follow
):
    owner = make_user("priv", is_private=True)
    follower, outsider = make_user("follower"), make_user("outsider")
    follow(follower, owner)
    post, reel = make_post(owner), make_reel(owner)

    # anonymous and non-followers can't mint a link
    assert client.get(f"/api/share/{post.id}/link").status_code == 403
    client.login(outsider)
    assert client.get(f"/api/share/{post.id}/link").status_code == 403
    assert client.get(f"/api/share/reels/{reel.id}/link").status_code == 403

    client.login(follower)
    assert client.get(f"/api/share/{post.id}/link").status_code == 200
    assert client.get(f"/api/share/reels/{reel.id}/link").status_code == 200
    client.login(owner)
    assert client.get(f"/api/share/{post.id}/link").status_code == 200


def test_link_for_blocked_author_is_404(client, db, make_user, make_post):
    owner, viewer = make_user("owner"), make_user("viewer")
    db.add(models.UserBlock(blocker_id=owner.id, blocked_id=viewer.id))
    db.commit()
    post = make_post(owner)
    client.login(viewer)
    assert client.get(f"/api/share/{post.id}/link").status_code == 404


# ------------------------------------------------------------ post page

def test_post_page_has_preview_tags_and_needs_no_login(client, make_named_user, make_post):
    owner = make_named_user("owner", full_name="Olive Owner")
    post = make_post(owner, caption="Sunset at the beach")

    r = client.get(f"/p/{post.id}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    page = r.text
    assert 'property="og:title" content="Olive Owner (@owner) on the app"' in page
    assert 'property="og:description" content="Sunset at the beach"' in page
    assert f'property="og:image" content="{BASE}/static/posts/a.jpg"' in page  # made absolute
    assert f'property="og:url" content="{BASE}/p/{post.id}"' in page
    assert 'name="twitter:card" content="summary_large_image"' in page
    assert "noindex" in page


def test_post_page_uses_first_carousel_item_and_video_posts_have_no_image(
    client, db, make_user, make_post
):
    owner = make_user("owner")
    post = make_post(owner, media_url="/static/posts/legacy.jpg")
    db.add_all([
        models.PostMedia(post_id=post.id, media_url="/static/posts/second.jpg", position=1),
        models.PostMedia(post_id=post.id, media_url="/static/posts/first.jpg", position=0),
    ])
    db.commit()
    assert f'og:image" content="{BASE}/static/posts/first.jpg"' in client.get(f"/p/{post.id}").text

    vid = make_post(owner, media_url="/static/posts/v.mp4", media_type=models.MediaType.video)
    page = client.get(f"/p/{vid.id}").text
    assert "og:image" not in page and f'og:video" content="{BASE}/static/posts/v.mp4"' in page


def test_captions_are_html_escaped(client, make_named_user, make_post):
    owner = make_named_user("owner", full_name='<b>"Evil"</b>')
    post = make_post(owner, caption='"><script>alert(1)</script>')
    page = client.get(f"/p/{post.id}").text
    assert "<script>" not in page and "<b>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_long_caption_is_truncated_and_whitespace_collapsed(client, make_user, make_post):
    post = make_post(make_user("owner"), caption="word   " * 100)
    page = client.get(f"/p/{post.id}").text
    desc = page.split('property="og:description" content="')[1].split('"')[0]
    assert len(desc) <= 200 and desc.endswith("...") and "  " not in desc


def test_post_page_for_private_account_shows_nothing_about_it(client, make_named_user, make_post):
    owner = make_named_user("secretive", is_private=True, full_name="Hidden Person")
    post = make_post(owner, caption="top secret plans")
    r = client.get(f"/p/{post.id}")
    assert r.status_code == 200
    for leaked in ("top secret plans", "secretive", "Hidden Person", "a.jpg", "og:image"):
        assert leaked not in r.text


def test_page_for_inactive_or_suspended_author_is_generic(client, make_named_user, make_post):
    for name, kw in (("inactive", {"is_active": False}), ("suspended", {"is_suspended": True})):
        owner = make_named_user(name, **kw)
        post = make_post(owner, caption="should not appear")
        r = client.get(f"/p/{post.id}")
        assert "should not appear" not in r.text and "og:image" not in r.text, name


def test_missing_post_and_reel_pages_404(client):
    assert client.get("/p/9999").status_code == 404
    assert client.get("/r/9999").status_code == 404
    assert client.get("/p/notanumber").status_code in (400, 422)


# ------------------------------------------------------------ reel page

def test_reel_page_has_video_preview_tags(client, make_user, make_reel):
    owner = make_user("owner")
    reel = make_reel(
        owner, caption="dance", video_url="/static/reels/x.mp4", thumbnail_url="/static/thumbnails/x.jpg"
    )
    page = client.get(f"/r/{reel.id}").text
    assert 'property="og:type" content="video.other"' in page
    assert f'og:video" content="{BASE}/static/reels/x.mp4"' in page
    assert f'og:image" content="{BASE}/static/thumbnails/x.jpg"' in page
    assert 'og:description" content="dance"' in page


def test_reel_page_private_account_is_generic(client, make_user, make_reel):
    owner = make_user("priv", is_private=True)
    reel = make_reel(owner, caption="hush", thumbnail_url="/static/thumbnails/x.jpg")
    page = client.get(f"/r/{reel.id}").text
    assert "hush" not in page and "x.mp4" not in page and "og:image" not in page


# ------------------------------------------------------ app open buttons

def test_open_in_app_and_download_buttons_only_when_configured(
    client, monkeypatch, make_user, make_post
):
    post = make_post(make_user("owner"))
    page = client.get(f"/p/{post.id}").text
    assert "Open in app" not in page and "Get the app" not in page

    monkeypatch.setenv("APP_DEEP_LINK_SCHEME", "myapp")
    monkeypatch.setenv("APP_DOWNLOAD_URL", "https://example.test/get")
    monkeypatch.setenv("APP_NAME", "Snapgram")
    page = client.get(f"/p/{post.id}").text
    assert 'href="myapp://p/%d"' % post.id in page
    assert 'href="https://example.test/get"' in page
    assert "on Snapgram" in page


def test_bad_deep_link_scheme_and_download_url_are_ignored(
    client, monkeypatch, make_user, make_post
):
    post = make_post(make_user("owner"))
    monkeypatch.setenv("APP_DEEP_LINK_SCHEME", 'javascript:alert(1)//')
    monkeypatch.setenv("APP_DOWNLOAD_URL", "javascript:alert(1)")
    page = client.get(f"/p/{post.id}").text
    assert "javascript" not in page and "Open in app" not in page
