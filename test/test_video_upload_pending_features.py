from app.routers import video_routes


def test_comment_dm_automation_owner_can_save_and_read(client, make_user, make_reel):
    owner = make_user("automation_owner")
    reel = make_reel(owner)
    client.login(owner)

    response = client.put(
        f"/api/videos/{reel.id}/comment-dm-automation",
        json={
            "enabled": True,
            "greeting": "Thanks for commenting!",
            "message": "Here is the link you asked for.",
            "link_url": "https://example.com/info",
            "button_label": "Open link",
            "button_url": "https://example.com/info",
            "follow_gate_enabled": True,
            "follow_message": "Thanks for following!",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
    assert response.json()["follow_gate_enabled"] is True

    read = client.get(f"/api/videos/{reel.id}/comment-dm-automation")
    assert read.status_code == 200
    assert read.json()["greeting"] == "Thanks for commenting!"


def test_comment_dm_automation_rejects_non_http_urls(client, make_user, make_reel):
    owner = make_user("automation_invalid_url")
    reel = make_reel(owner)
    client.login(owner)
    response = client.put(
        f"/api/videos/{reel.id}/comment-dm-automation",
        json={"enabled": True, "message": "Hi", "button_url": "javascript:alert(1)"},
    )
    assert response.status_code == 400


def test_reel_caption_upload_list_and_delete(client, make_user, make_reel, monkeypatch, tmp_path):
    owner = make_user("caption_owner")
    reel = make_reel(owner)
    client.login(owner)
    monkeypatch.setattr(video_routes, "STATIC_ROOT", tmp_path)

    response = client.post(
        f"/api/videos/{reel.id}/captions",
        data={"language": "English"},
        files={"file": ("captions.vtt", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n", "text/vtt")},
    )
    assert response.status_code == 201, response.text
    track = response.json()
    assert track["format"] == "vtt"
    assert track["language"] == "English"

    listed = client.get(f"/api/videos/{reel.id}/captions")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    deleted = client.delete(f"/api/videos/{reel.id}/captions/{track['id']}")
    assert deleted.status_code == 204


def test_reel_draft_is_private_until_published(client, make_user, monkeypatch):
    owner = make_user("draft_owner")
    client.login(owner)
    monkeypatch.setattr(video_routes, "save_upload_file", lambda *args, **kwargs: ("/static/reels/draft.mp4", "video"))
    monkeypatch.setattr(video_routes, "generate_video_thumbnail", lambda *args, **kwargs: "/static/thumbnails/draft.jpg")

    created = client.post(
        "/api/videos/drafts/upload",
        data={"title": "Saved for later", "description": "Draft description", "visibility": "private"},
        files={"file": ("draft.mp4", b"fake-video-bytes", "video/mp4")},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["title"] == "Saved for later"

    drafts = client.get("/api/videos/drafts")
    assert drafts.status_code == 200
    assert any(item["id"] == draft["id"] for item in drafts.json())
    assert client.get(f"/api/reels/{draft['id']}").status_code == 404

    published = client.post(f"/api/videos/drafts/{draft['id']}/publish")
    assert published.status_code == 201, published.text
    assert published.json()["reel"]["title"] == "Saved for later"
    assert client.get("/api/videos/drafts").json() == []
