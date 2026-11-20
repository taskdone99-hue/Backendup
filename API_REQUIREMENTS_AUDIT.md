# Requested API work — codebase audit

This audit reflects the uploaded source tree. It distinguishes existing routes from the new addition in this package; it is not a claim that every requested screen is fully implemented.

## Existing routes found in the source

| Requirement | Existing implementation | Notes |
|---|---|---|
| Not interested | `POST/GET/DELETE /api/not-interested` | Currently supports `post` and `reel`; no reason field and no long-video type. |
| Chat notifications | `GET/PUT /api/chat/conversations/{conversation_id}/notification-settings` | Per-conversation notification preferences. |
| Chat mute | `POST/DELETE /api/chat/conversations/{conversation_id}/mute` | Existing mute actions. |
| Chat font | `GET/PUT /api/chat/settings/font` | Font preference exists. |
| Stickers background removal | `POST /api/stickers/remove-bg` | Requires the optional `rembg` engine at runtime. |
| Sticker CRUD/edit | `POST /api/stickers/upload`, `PUT/DELETE /api/stickers/{sticker_id}` | Existing owner-scoped sticker management. |
| Block/unblock | `POST/DELETE /api/privacy/block/{user_id}`, `GET /api/privacy/blocked` | Existing. |
| Interactions / engagement controls | `GET/PUT /api/users/me/engagement-settings` | Includes hide-like-count, hide-comments, hide-share-count and hide-saved-count. |
| Account privacy status | `GET/PUT /v1/me/privacy-settings` | Visibility/activity/privacy preferences. |
| Personal information | `GET/PATCH /v1/me/personal-details` | DOB and gender; profile update routes also exist. |
| Notifications | `GET /api/notifications`, `GET/PUT /api/notifications/preferences` | Existing notification feed and preferences. |
| Language | `GET/PUT /api/users/me/language` | Translation language preference. |
| Appearance | `GET/PUT /api/users/me/theme`, `GET /api/themes` | Light/dark/system theme. |
| Tags and mentions | `GET/PUT /api/tags/settings`, `/api/users/me/mention-settings` and tag workflow routes | Existing controls and tag actions. |
| Comments | `/api/posts/{post_id}/comments`, `/api/reels/{reel_id}/comments`, replies and delete routes | Existing post/reel comments. |
| Likes | `/api/posts/{post_id}/likes`, `/api/reels/{reel_id}/likes`, `/api/comments/{comment_id}/like` and unlike routes | Existing post/reel/comment likes. |
| Reel remixing | Reel remix and audio-remix routes in `content_routes.py`; reel settings in `video_routes.py` | Existing reel remix support. |

## New endpoint added in this package

### Search the creator's content for the Analytics picker

`GET /api/analytics/content`

Query parameters:

- `q` — optional search over caption; for long videos, title and description.
- `content_type` — `all` (default), `post`, `reel`, or `video`.
- `page` — 1-based page number (default 1).
- `limit` — 1–100 (default 20).

Example:

```http
GET /api/analytics/content?q=travel&content_type=all&page=1&limit=20
Authorization: Bearer <access-token>
```

Response shape:

```json
{
  "items": [
    {
      "content_type": "reel",
      "id": 42,
      "title": null,
      "caption": "Travel day",
      "media_url": "...",
      "thumbnail_url": "...",
      "created_at": "..."
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_previous": false
  },
  "filters": {"query": "travel", "content_type": "all"},
  "supported_content_types": ["post", "reel", "video"]
}
```

## Still requiring implementation / product decisions

1. Disappearing messages: define timer options (e.g. off/24h/7d/90d), whether it applies to new messages only, and whether both participants can change it. Expiry needs a persisted `expires_at` and a cleanup/read policy.
2. Chat themes: define supported themes and whether preference is per user per conversation or shared by all participants.
3. Sticker edit/remove background: edit CRUD exists and remove-bg exists; confirm whether the requested editor needs server-side crop/rotate/filters or only metadata and asset replacement.
4. Not-interested reason: add controlled reason codes and support for videos if required.
5. Comments/likes on long-form videos: current generic Like enum and comment routes are post/reel-oriented. Add video support only after deciding whether video comments/likes share the existing tables and response contracts.
6. Full analytics parity: the new endpoint supplies the searchable post/reel/video picker. Existing detailed insight-event calculations are reel-specific. Post/video views, watch time, reach, retention, and timelines need event ingestion and persistence before reliable metrics can be returned; they must not be fabricated from likes/comments.

## Safety / deployment

- No production database or server was accessed by this source edit.
- Run the application's normal migration process before deploying any future schema changes. This package's new analytics picker does not require a schema migration.
- The original uploaded `.env` file is deliberately excluded from the output archive. Recreate local configuration from `.env.example`; never distribute live credentials.
