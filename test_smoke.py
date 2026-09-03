import os
os.environ["SECRET_KEY"] = "test-secret-key-for-smoke-test"
os.environ["DB_HOST"] = "localhost"
os.environ["DB_NAME"] = "test"
os.environ["DB_USER"] = "test"
os.environ["DB_PASSWORD"] = "test"

import app.database as database
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Swap in sqlite in-memory (file-based so multiple connections share state)
test_engine = create_engine(
    "sqlite:///./smoke_test.db", connect_args={"check_same_thread": False}
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
database.engine = test_engine
database.SessionLocal = TestSessionLocal

from app import models
from app.auth import create_access_token
from app.database import Base, get_db

if os.path.exists("smoke_test.db"):
    os.remove("smoke_test.db")
Base.metadata.create_all(bind=test_engine)

from app.main import app

def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

from fastapi.testclient import TestClient
client = TestClient(app)

db = TestSessionLocal()
u1 = models.User(username="anjali", full_name="Anjali", avatar_url="/static/avatars/anjali.png", is_phone_verified=True)
u2 = models.User(username="rahul", full_name="Rahul", avatar_url="/static/avatars/rahul.png", is_phone_verified=True)
db.add_all([u1, u2])
db.commit()
db.refresh(u1)
db.refresh(u2)

# u2 follows u1 (so u2 sees u1's stories in feed)
db.add(models.Follow(follower_id=u2.id, following_id=u1.id))

reel = models.Reel(user_id=u1.id, caption="test reel", video_url="/static/reel1.mp4")
post = models.Post(user_id=u1.id, caption="test post", media_url="/static/post1.jpg", media_type=models.MediaType.image)
db.add_all([reel, post])
db.commit()
db.refresh(reel)
db.refresh(post)

token1 = create_access_token({"sub": str(u1.id)})
token2 = create_access_token({"sub": str(u2.id)})
h1 = {"Authorization": f"Bearer {token1}"}
h2 = {"Authorization": f"Bearer {token2}"}

results = []

def check(name, resp, expect_status):
    ok = resp.status_code == expect_status
    results.append((name, resp.status_code, ok))
    print(f"{'PASS' if ok else 'FAIL'} | {name} -> {resp.status_code}")
    return resp

# 1. Create story (u1)
r = check("POST /api/stories (create story)", client.post(
    "/api/stories", headers=h1, data={"caption": "hi"},
    files={"file": ("s.jpg", b"fakeimgbytes", "image/jpeg")}
), 201)
story = r.json()
print("  user:", story.get("user"))
assert story["user"]["username"] == "anjali"
assert story["user"]["avatar_url"] == "/static/avatars/anjali.png"

# 2. GET /api/stories/mine (u1)
r = check("GET /api/stories/mine", client.get("/api/stories/mine", headers=h1), 200)
mine = r.json()
assert mine["items"][0]["user"]["username"] == "anjali"
assert mine["items"][0]["user"]["full_name"] == "Anjali"
print("  mine[0].user:", mine["items"][0]["user"])

# 3. GET story feed (u2, follows u1)
r = check("GET /api/stories/feed", client.get("/api/stories/feed", headers=h2), 200)
feed = r.json()
assert feed["items"][0]["user"]["username"] == "anjali"
assert feed["items"][0]["stories"][0]["user"]["username"] == "anjali"
print("  feed[0].user:", feed["items"][0]["user"])
print("  feed[0].stories[0].user:", feed["items"][0]["stories"][0]["user"])

# 4. Create post comment (u2)
r = check("POST /api/posts/{id}/comments", client.post(
    f"/api/posts/{post.id}/comments", headers=h2, json={"content": "nice post!"}
), 201)
comment = r.json()
assert comment["user"]["username"] == "rahul"
assert comment["user"]["avatar_url"] == "/static/avatars/rahul.png"
print("  comment.user:", comment["user"])

# 5. GET post comments
r = check("GET /api/posts/{id}/comments", client.get(f"/api/posts/{post.id}/comments", headers=h1), 200)
assert r.json()["items"][0]["user"]["username"] == "rahul"

# 6. Reply to comment
r = check("POST /api/comments/{id}/reply", client.post(
    f"/api/comments/{comment['id']}/reply", headers=h1, json={"content": "thanks!"}
), 201)
reply = r.json()
assert reply["user"]["username"] == "anjali"
print("  reply.user:", reply["user"])

# 7. GET replies
r = check("GET /api/comments/{id}/replies", client.get(f"/api/comments/{comment['id']}/replies", headers=h2), 200)
assert r.json()["items"][0]["user"]["username"] == "anjali"
assert r.json()["total"] == 1

# 8. Create reel comment (u2)
r = check("POST /api/reels/{id}/comments", client.post(
    f"/api/reels/{reel.id}/comments", headers=h2, json={"content": "cool reel!"}
), 201)
reel_comment = r.json()
assert reel_comment["user"]["username"] == "rahul"
print("  reel comment.user:", reel_comment["user"])

# 9. GET reel comments
r = check("GET /api/reels/{id}/comments", client.get(f"/api/reels/{reel.id}/comments", headers=h1), 200)
assert r.json()["items"][0]["user"]["username"] == "rahul"
assert r.json()["items"][0]["replies_count"] == 0
assert r.json()["items"][0]["is_liked"] == False

# 10. Like the reel comment (u1)
r = check("POST /api/comments/{id}/like", client.post(
    f"/api/comments/{reel_comment['id']}/like", headers=h1
), 200)
like_resp = r.json()
assert like_resp["like"]["user"]["username"] == "anjali"
print("  like.user:", like_resp["like"]["user"])

# 11. Generic like on the post (u2)
r = check("POST /api/likes", client.post(
    "/api/likes", headers=h2, json={"target_type": "post", "target_id": post.id}
), 201)
like2 = r.json()
assert like2["like"]["user"]["username"] == "rahul"
print("  generic like.user:", like2["like"]["user"])

# 12. GET post likes list
r = check("GET /api/posts/{id}/likes", client.get(f"/api/posts/{post.id}/likes", headers=h1), 200)
assert r.json()["items"][0]["username"] == "rahul"
assert r.json()["items"][0]["avatar_url"] == "/static/avatars/rahul.png"

# 13. GET reel likes list (like the reel first)
client.post("/api/likes", headers=h2, json={"target_type": "reel", "target_id": reel.id})
r = check("GET /api/reels/{id}/likes", client.get(f"/api/reels/{reel.id}/likes", headers=h1), 200)
assert r.json()["items"][0]["username"] == "rahul"

# 14. Unlike/unlike-by-target sanity
r = check("DELETE /api/likes (by target)", client.request(
    "DELETE", "/api/likes", headers=h2, params={"target_type": "post", "target_id": post.id}
), 200)

# 15. View a story (view count / viewed_by_me)
r = check("POST /api/stories/{id}/view", client.post(f"/api/stories/{story['id']}/view", headers=h2), 200)

# 16. GET single story (should still have owner)
r = check("GET /api/stories/{id}", client.get(f"/api/stories/{story['id']}", headers=h2), 200)
assert r.json()["user"]["username"] == "anjali"

# 17. Story viewers (owner-only) — u2 viewed u1's story above
r = check("GET /api/stories/{id}/viewers", client.get(f"/api/stories/{story['id']}/viewers", headers=h1), 200)
viewers = r.json()
assert viewers["items"][0]["user_id"] == u2.id
assert viewers["items"][0]["username"] == "rahul"
assert viewers["items"][0]["full_name"] == "Rahul"
print("  viewer entry:", viewers["items"][0])

# 18. No-avatar user: avatar_url should be null, not error
u3 = models.User(username="noavatar", full_name=None, is_phone_verified=True)
db.add(u3)
db.commit()
db.refresh(u3)
token3 = create_access_token({"sub": str(u3.id)})
h3 = {"Authorization": f"Bearer {token3}"}
r = check("POST /api/posts/{id}/comments (no avatar user)", client.post(
    f"/api/posts/{post.id}/comments", headers=h3, json={"content": "hi from noavatar"}
), 201)
noavatar_comment = r.json()
assert noavatar_comment["user"]["avatar_url"] is None
assert noavatar_comment["user"]["full_name"] is None
print("  no-avatar user:", noavatar_comment["user"])

# 19. Post/Reel detail: single 'user' field (no more 'author'/'owner' aliases)
r = check("GET /api/posts/{id}", client.get(f"/api/posts/{post.id}", headers=h1), 200)
pd = r.json()
assert pd["user"]["username"] == "anjali"
assert "author" not in pd
print("  post detail user:", pd["user"])

r = check("GET /api/reels/{id}", client.get(f"/api/reels/{reel.id}", headers=h1), 200)
rd = r.json()
assert rd["user"]["username"] == "anjali"
assert "author" not in rd
print("  reel detail user:", rd["user"])

# 20. Phone country code is now optional (defaults to DEFAULT_PHONE_REGION=IN)
r = check("POST /api/auth/request-otp (bare number, no country code) -> now OK", client.post(
    "/api/auth/request-otp", json={"identifier": "9876543210"}
), 200)
print("  bare-number OTP request body:", r.json())

# Validation error shape still holds for genuinely invalid numbers -> {"message": ...}, 400
r = check("POST /api/auth/request-otp (invalid number)", client.post(
    "/api/auth/request-otp", json={"identifier": "123"}
), 400)
body = r.json()
assert set(body.keys()) == {"message"}, f"unexpected keys: {body.keys()}"
print("  validation error body:", body)

# 21. HTTPException shape: 404 -> {"message": ...} only
r = check("GET /api/posts/{id} (nonexistent)", client.get("/api/posts/999999", headers=h1), 404)
body404 = r.json()
assert set(body404.keys()) == {"message"}, f"unexpected keys: {body404.keys()}"
print("  404 body:", body404)

# 22. HTTPException shape: missing token -> 403 {"message": ...} (FastAPI's HTTPBearer default)
r = check("GET /api/auth/me (no token)", client.get("/api/auth/me"), 403)
body403 = r.json()
assert set(body403.keys()) == {"message"}, f"unexpected keys: {body403.keys()}"
print("  403 body:", body403)

# 23. HTTPException shape: invalid token -> 401 {"message": ...} + WWW-Authenticate header preserved
r = check("GET /api/auth/me (bad token)", client.get(
    "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
), 401)
body401 = r.json()
assert set(body401.keys()) == {"message"}, f"unexpected keys: {body401.keys()}"
assert r.headers.get("www-authenticate") == "Bearer"
print("  401 body:", body401, "| WWW-Authenticate:", r.headers.get("www-authenticate"))

# 24. Private account visibility
u_private = models.User(username="privatepal", full_name="Private Pal", is_phone_verified=True, is_private=True)
db.add(u_private)
db.commit()
db.refresh(u_private)
priv_post = models.Post(user_id=u_private.id, caption="secret post", media_url="/static/priv.jpg", media_type=models.MediaType.image)
priv_reel = models.Reel(user_id=u_private.id, caption="secret reel", video_url="/static/priv.mp4")
db.add_all([priv_post, priv_reel])
db.commit()
db.refresh(priv_post)
db.refresh(priv_reel)

token_priv = create_access_token({"sub": str(u_private.id)})
h_priv = {"Authorization": f"Bearer {token_priv}"}

# u2 (rahul) is a stranger — doesn't follow u_private
r = check("GET /api/users/{id}/posts (private, stranger) -> 403", client.get(
    f"/api/users/{u_private.id}/posts", headers=h2
), 403)
r = check("GET /api/users/{id}/reels (private, stranger) -> 403", client.get(
    f"/api/users/{u_private.id}/reels", headers=h2
), 403)
r = check("GET /api/users/{id}/followers (private, stranger) -> 403", client.get(
    f"/api/users/{u_private.id}/followers", headers=h2
), 403)
r = check("GET /api/users/{id}/following (private, stranger) -> 403", client.get(
    f"/api/users/{u_private.id}/following", headers=h2
), 403)
r = check("GET /api/posts/{id} (private, stranger) -> 403", client.get(
    f"/api/posts/{priv_post.id}", headers=h2
), 403)
r = check("GET /api/reels/{id} (private, stranger) -> 403", client.get(
    f"/api/reels/{priv_reel.id}", headers=h2
), 403)
# anonymous (no auth) is also blocked
r = check("GET /api/users/{id}/posts (private, anonymous) -> 403", client.get(
    f"/api/users/{u_private.id}/posts"
), 403)
# owner can always see their own
r = check("GET /api/users/{id}/posts (private, owner) -> 200", client.get(
    f"/api/users/{u_private.id}/posts", headers=h_priv
), 200)

# u2 requests to follow u_private (private account -> pending, not immediate)
r = check("POST /api/follow/{id} (private account) -> request_pending", client.post(
    f"/api/follow/{u_private.id}", headers=h2
), 200)
follow_resp = r.json()
assert follow_resp["following"] is False
assert follow_resp["request_pending"] is True
print("  private-account follow response:", follow_resp)

# Not actually a follower yet -- still blocked until the request is accepted
r = check("GET /api/users/{id}/posts (private, pending request) -> still 403", client.get(
    f"/api/users/{u_private.id}/posts", headers=h2
), 403)

# u_private accepts the request
r = check("GET /api/follow-requests (u_private)", client.get(
    "/api/follow-requests", headers=h_priv
), 200)
pending_requests = r.json()["items"]
assert pending_requests and pending_requests[0]["requester"]["username"] == "rahul"
request_id = pending_requests[0]["id"]

r = check("POST /api/follow-requests/{id}/accept", client.post(
    f"/api/follow-requests/{request_id}/accept", headers=h_priv
), 200)
assert r.json()["following"] is True

# Now u2 is an actual follower and can see the private content
r = check("GET /api/users/{id}/posts (private, follower) -> 200", client.get(
    f"/api/users/{u_private.id}/posts", headers=h2
), 200)
assert r.json()["total"] == 1
r = check("GET /api/posts/{id} (private, follower) -> 200", client.get(
    f"/api/posts/{priv_post.id}", headers=h2
), 200)

# Explore/reels feeds must never surface the private account's content to a non-follower
r = check("GET /api/reels/feed (excludes private non-followed accounts)", client.get(
    "/api/reels/feed", headers=h3
), 200)
reel_ids_in_feed = {item["id"] for item in r.json()["items"]}
assert priv_reel.id not in reel_ids_in_feed, "private reel leaked into global reels feed"

r = check("GET /api/posts/explore (excludes private non-followed accounts)", client.get(
    "/api/posts/explore", headers=h3
), 200)
post_ids_in_explore = {item["id"] for item in r.json()["items"]}
assert priv_post.id not in post_ids_in_explore, "private post leaked into explore feed"

# 25. Profile GET now includes counts + viewer-relative is_following, and updates on follow/unfollow
r = check("GET /api/users/{id} (u2 viewing u1, before follow)", client.get(
    f"/api/users/{u1.id}", headers=h2
), 200)
prof = r.json()
assert prof["is_following"] is True  # u2 already follows u1 from initial setup
assert prof["followers_count"] >= 1
print("  profile (u2 already follows u1):", {k: prof[k] for k in ("posts_count", "reels_count", "followers_count", "following_count", "is_following")})

# u3 has no follow relationship with u1 yet -> cleaner before/after check
r = check("GET /api/users/{id} (u3 viewing u1, before follow)", client.get(
    f"/api/users/{u1.id}", headers=h3
), 200)
assert r.json()["is_following"] is False

client.post(f"/api/follow/{u1.id}", headers=h3)
r = check("GET /api/users/{id} (u3 viewing u1, after follow)", client.get(
    f"/api/users/{u1.id}", headers=h3
), 200)
prof_after = r.json()
assert prof_after["is_following"] is True
followers_after_follow = prof_after["followers_count"]
print("  profile after follow: is_following =", prof_after["is_following"], "followers_count =", followers_after_follow)

client.delete(f"/api/follow/{u1.id}", headers=h3)
r = check("GET /api/users/{id} (u3 viewing u1, after unfollow)", client.get(
    f"/api/users/{u1.id}", headers=h3
), 200)
prof_unfollowed = r.json()
assert prof_unfollowed["is_following"] is False
assert prof_unfollowed["followers_count"] == followers_after_follow - 1
print("  profile after unfollow: is_following =", prof_unfollowed["is_following"], "followers_count =", prof_unfollowed["followers_count"])

# 26. Empty identifier -> clear required-field message, not a misleading one
r = check("POST /api/auth/login (empty identifier)", client.post(
    "/api/auth/login", json={"identifier": "", "password": "test12345"}
), 400)
body = r.json()
assert body == {"message": "Please enter your email, phone number, or username"}, body
print("  empty login identifier body:", body)

r = check("POST /api/auth/request-otp (empty identifier)", client.post(
    "/api/auth/request-otp", json={"identifier": ""}
), 400)
body = r.json()
assert body == {"message": "Please enter your email or phone number"}, body
print("  empty request-otp identifier body:", body)

# Public accounts remain fully visible to everyone (anjali/rahul are public by default)
r = check("GET /api/users/{id}/posts (public, stranger) -> 200", client.get(
    f"/api/users/{u1.id}/posts", headers=h3
), 200)
r = check("GET /api/users/{id}/posts (public, anonymous) -> 200", client.get(
    f"/api/users/{u1.id}/posts"
), 200)

# 27. DELETE /api/notifications/{id}
# Trigger a real notification: u3 follows u1 (public account -> immediate
# follow + "started following you" notification, see user_routes.follow_user).
client.delete(f"/api/follow/{u1.id}", headers=h3)  # in case u3 already followed u1 above
r = check("POST /api/follow/{id} (u3 -> u1, generates a notification)", client.post(
    f"/api/follow/{u1.id}", headers=h3
), 200)

r = check("GET /api/notifications (u1, before delete)", client.get(
    "/api/notifications", headers=h1
), 200)
notifs_before = r.json()
follow_notifs = [n for n in notifs_before["items"] if n["type"] == "follow" and n["actor_id"] == u3.id]
assert follow_notifs, f"expected a 'follow' notification from u3, got: {notifs_before['items']}"
notif_id = follow_notifs[0]["id"]
print("  notification to delete:", follow_notifs[0])

# Wrong owner (u2) can't delete u1's notification -> 403
r = check("DELETE /api/notifications/{id} (wrong owner) -> 403", client.delete(
    f"/api/notifications/{notif_id}", headers=h2
), 403)

# Nonexistent notification -> 404
r = check("DELETE /api/notifications/{id} (nonexistent) -> 404", client.delete(
    "/api/notifications/999999", headers=h1
), 404)

# No auth at all -> 403 (HTTPBearer default, same shape as the rest of this API)
r = check("DELETE /api/notifications/{id} (no token) -> 403", client.delete(
    f"/api/notifications/{notif_id}"
), 403)

# Owner deletes their own notification -> 200 with the exact message
r = check("DELETE /api/notifications/{id} (owner) -> 200", client.delete(
    f"/api/notifications/{notif_id}", headers=h1
), 200)
assert r.json() == {"message": "Notification deleted successfully"}, r.json()
print("  delete response:", r.json())

# It's actually gone -- doesn't reappear in the list, and deleting it again is a 404
r = check("GET /api/notifications (u1, after delete)", client.get(
    "/api/notifications", headers=h1
), 200)
remaining_ids = {n["id"] for n in r.json()["items"]}
assert notif_id not in remaining_ids, "deleted notification still present in GET /api/notifications"

r = check("DELETE /api/notifications/{id} (already deleted) -> 404", client.delete(
    f"/api/notifications/{notif_id}", headers=h1
), 404)

# Existing GET /api/notifications behavior (pagination/unread_count) is untouched
r = check("GET /api/notifications (shape unchanged)", client.get(
    "/api/notifications", headers=h1
), 200)
body = r.json()
assert set(("total", "unread_count", "limit", "offset", "items")).issubset(body.keys())

# 28. Home Feed — public users' posts appear without following (Prasanna/user 22 case)
prasanna = models.User(username="prasanna", full_name="Prasanna", is_phone_verified=True)
public_stranger = models.User(username="public_stranger_22", full_name="Public Stranger", is_phone_verified=True)
db.add_all([prasanna, public_stranger])
db.commit()
db.refresh(prasanna)
db.refresh(public_stranger)
token_prasanna = create_access_token({"sub": str(prasanna.id)})
h_prasanna = {"Authorization": f"Bearer {token_prasanna}"}

public_post = models.Post(
    user_id=public_stranger.id, caption="public post", media_url="/static/pub22.jpg",
    media_type=models.MediaType.image,
)
db.add(public_post)
db.commit()
db.refresh(public_post)

# Prasanna does NOT follow public_stranger
r = check("GET /api/users/{id} (prasanna viewing public_stranger, not following)", client.get(
    f"/api/users/{public_stranger.id}", headers=h_prasanna
), 200)
assert r.json()["is_following"] is False

r = check("GET /api/posts/feed (public user's post visible without follow)", client.get(
    "/api/posts/feed", headers=h_prasanna
), 200)
feed_body = r.json()
feed_post_ids = {item["id"] for item in feed_body["items"]}
assert public_post.id in feed_post_ids, "public user's post missing from home feed for a non-follower"
print("  home feed (prasanna, no follow) includes public post:", public_post.id in feed_post_ids)

# Private, non-followed account's post must NOT appear in the home feed
r = check("GET /api/posts/feed (private, non-follower excludes private post)", client.get(
    "/api/posts/feed", headers=h_prasanna
), 200)
feed_post_ids = {item["id"] for item in r.json()["items"]}
assert priv_post.id not in feed_post_ids, "private user's post leaked into home feed for a non-follower"

# Own posts and followed users' posts still show up (u2/rahul follows u1/anjali)
r = check("GET /api/posts/feed (u2: own + followed still visible)", client.get(
    "/api/posts/feed", headers=h2
), 200)
u2_feed_ids = {item["id"] for item in r.json()["items"]}
assert post.id in u2_feed_ids, "u1's post (followed by u2) missing from u2's home feed"

# A pending follow request must NOT grant home-feed access to a private account's posts
r = check("POST /api/follow/{id} (prasanna -> u_private) -> request_pending", client.post(
    f"/api/follow/{u_private.id}", headers=h_prasanna
), 200)
assert r.json()["request_pending"] is True
r = check("GET /api/posts/feed (pending request still excludes private post)", client.get(
    "/api/posts/feed", headers=h_prasanna
), 200)
feed_post_ids = {item["id"] for item in r.json()["items"]}
assert priv_post.id not in feed_post_ids, "private post leaked into home feed despite only a pending follow request"

# Response shape (pagination/counts) unchanged
assert set(("total", "limit", "offset", "items")).issubset(feed_body.keys())

# 29. Follow Requests — is_following / is_followed_by returned independently
# One-way: prasanna -> u_private is pending; u_private does not follow prasanna back.
r = check("GET /api/follow-requests (u_private, one-way case)", client.get(
    "/api/follow-requests", headers=h_priv
), 200)
reqs = r.json()["items"]
prasanna_req = next(item for item in reqs if item["requester"]["username"] == "prasanna")
assert prasanna_req["requester"]["is_following"] is False  # u_private doesn't follow prasanna
assert prasanna_req["requester"]["is_followed_by"] is False  # prasanna's request is still pending, not an actual follow
print("  follow-request (one-way):", prasanna_req["requester"])

# Follow-Back case: u_private already follows prasanna (mutual-follow direction check)
client.post(f"/api/follow/{prasanna.id}", headers=h_priv)
r = check("GET /api/follow-requests (u_private, follow-back case)", client.get(
    "/api/follow-requests", headers=h_priv
), 200)
reqs = r.json()["items"]
prasanna_req = next(item for item in reqs if item["requester"]["username"] == "prasanna")
assert prasanna_req["requester"]["is_following"] is True  # u_private now follows prasanna -> "Follow Back" UI
assert prasanna_req["requester"]["is_followed_by"] is False  # prasanna's follow of u_private is still just a pending request
print("  follow-request (follow-back):", prasanna_req["requester"])
client.delete(f"/api/follow/{prasanna.id}", headers=h_priv)  # reset

# 30. Profile API — is_following/is_followed_by/request_pending stay independent
r = check("GET /api/users/{id} (prasanna viewing u_private, pending + mutual check)", client.get(
    f"/api/users/{u_private.id}", headers=h_prasanna
), 200)
prof = r.json()
assert prof["is_following"] is False
assert prof["is_followed_by"] is False
assert prof["request_pending"] is True
print("  profile (prasanna -> u_private, pending):", {k: prof[k] for k in ("is_following", "is_followed_by", "request_pending")})

# 31. Notifications WebSocket — real-time delivery
# Connect as u1 (anjali); u3 (noavatar) follows u1 over REST while the socket
# is open, and the "connected" + "notification" events must arrive live.
with client.websocket_connect(f"/api/notifications/ws?token={token1}") as ws:
    connected_evt = ws.receive_json()
    ok = connected_evt.get("type") == "connected" and "unread_count" in connected_evt
    results.append(("WS /api/notifications/ws -> connected event", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | WS connected event -> {connected_evt}")

    client.delete(f"/api/follow/{u1.id}", headers=h3)  # in case u3 already follows u1
    r = client.post(f"/api/follow/{u1.id}", headers=h3)  # triggers notify_user -> WS push
    assert r.status_code == 200

    notif_evt = ws.receive_json()
    ok = (
        notif_evt.get("type") == "notification"
        and notif_evt["notification"]["type"] == "follow"
        and notif_evt["notification"]["actor_id"] == u3.id
        and "noavatar" in notif_evt["notification"]["message"]
    )
    results.append(("WS /api/notifications/ws -> live notification push", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | WS live notification -> {notif_evt}")

    # Same shape as a GET /api/notifications item
    assert set(notif_evt["notification"].keys()) == {
        "id", "type", "actor_id", "message", "target_type", "target_id", "is_read", "created_at",
    }, notif_evt["notification"]

    ws.send_json({"type": "ping"})
    pong = ws.receive_json()
    ok = pong == {"type": "pong"}
    results.append(("WS /api/notifications/ws -> ping/pong", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | WS ping/pong -> {pong}")

# It was also persisted normally — GET /api/notifications is unaffected by the WS push
r = check("GET /api/notifications (u1, after WS-delivered follow)", client.get(
    "/api/notifications", headers=h1
), 200)
follow_notifs = [n for n in r.json()["items"] if n["type"] == "follow" and n["actor_id"] == u3.id]
assert follow_notifs, "WS-delivered notification was not also persisted to the DB"

# 31b. Dedicated regression test for the traced flow:
# POST /api/follow/{user_id} -> notify_user() -> notification_manager.send_to_user()
# User A connects to /api/notifications/ws; User B then sends User A a FOLLOW REQUEST
# (User A's account is private, so this exercises the follow_request path, not the
# plain-follow path already covered above). User A must receive the WS notification
# event immediately -- no polling/sleeping, just the next message on the open socket --
# and the notification must also be durably persisted via GET /api/notifications.
user_a = models.User(username="user_a_wstest", full_name="User A", is_phone_verified=True, is_private=True)
user_b = models.User(username="user_b_wstest", full_name="User B", is_phone_verified=True)
db.add_all([user_a, user_b])
db.commit()
db.refresh(user_a)
db.refresh(user_b)
token_a = create_access_token({"sub": str(user_a.id)})
token_b = create_access_token({"sub": str(user_b.id)})
h_a = {"Authorization": f"Bearer {token_a}"}
h_b = {"Authorization": f"Bearer {token_b}"}

with client.websocket_connect(f"/api/notifications/ws?token={token_a}") as ws_a:
    connected_evt = ws_a.receive_json()
    ok = connected_evt.get("type") == "connected"
    results.append(("WS User A connects to /api/notifications/ws", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | User A connected -> {connected_evt}")

    # Trace point 1: POST /api/follow/{user_id} (User B -> User A, private account)
    r = client.post(f"/api/follow/{user_a.id}", headers=h_b)
    assert r.status_code == 200 and r.json()["request_pending"] is True, r.json()

    # Trace point 2-4: notification_service.notify_user() creates the row, then
    # pushes it via notification_manager -> the connection registered for User A's
    # user_id. Assert User A receives it immediately on the already-open socket.
    notif_evt = ws_a.receive_json()
    ok = (
        notif_evt.get("type") == "notification"
        and notif_evt["notification"]["type"] == "follow_request"
        and notif_evt["notification"]["actor_id"] == user_b.id
        and "user_b_wstest" in notif_evt["notification"]["message"]
    )
    results.append(("WS User A receives live follow-request notification from User B", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | User A live notification -> {notif_evt}")
    live_notification_id = notif_evt["notification"]["id"]

# Trace point: verify DB persistence independently of the WebSocket delivery
r = check("GET /api/notifications (User A, DB persistence check)", client.get(
    "/api/notifications", headers=h_a
), 200)
persisted = [n for n in r.json()["items"] if n["id"] == live_notification_id]
assert persisted, "notification delivered over WS was not found via GET /api/notifications"
assert persisted[0]["type"] == "follow_request"
assert persisted[0]["actor_id"] == user_b.id
print("  persisted notification:", persisted[0])

# 31c. Chat-message notification -> notifications WebSocket.
# This is the path that previously bypassed notify_user() (it built the
# Notification row and FCM push directly in chat_routes.send_message),
# so a recipient connected to /api/notifications/ws but NOT to
# /api/chat/ws never got a live push for a new message. User A is
# connected to the notifications socket ONLY (never opens /api/chat/ws),
# User B sends them a chat message over REST, and User A must receive
# the live {"type":"notification","notification":{"type":"message",...}}
# event -- not just a bump in unread_count.
r = check("POST /api/chat/conversations (User B -> User A)", client.post(
    "/api/chat/conversations", headers=h_b, json={"participant_ids": [user_a.id]}
), 201)
conversation_id = r.json()["id"]

r = check("GET /api/notifications (User A, unread_count before message)", client.get(
    "/api/notifications", headers=h_a
), 200)
unread_before = r.json()["unread_count"]

with client.websocket_connect(f"/api/notifications/ws?token={token_a}") as ws_a:
    connected_evt = ws_a.receive_json()
    assert connected_evt["type"] == "connected"

    # User A is deliberately NOT connected to /api/chat/ws here -- that's
    # the exact condition (offline_ids in send_message) that used to skip
    # notification_manager entirely.
    r = client.post(
        f"/api/chat/conversations/{conversation_id}/messages",
        headers=h_b,
        json={"content": "hey User A, see this live?"},
    )
    assert r.status_code == 201, r.json()

    notif_evt = ws_a.receive_json()
    ok = (
        notif_evt.get("type") == "notification"
        and notif_evt["notification"]["type"] == "message"
        and notif_evt["notification"]["actor_id"] == user_b.id
        and notif_evt["notification"]["target_type"] == "conversation"
        and notif_evt["notification"]["target_id"] == conversation_id
    )
    results.append(("WS User A receives live chat-message notification from User B", "n/a", ok))
    print(f"{'PASS' if ok else 'FAIL'} | User A live chat-message notification -> {notif_evt}")
    chat_notification_id = notif_evt["notification"]["id"]

# Prove it's the *event*, not just a count bump: assert the exact row by id,
# and separately assert unread_count actually moved (both must hold).
r = check("GET /api/notifications (User A, chat-message DB persistence)", client.get(
    "/api/notifications", headers=h_a
), 200)
body = r.json()
persisted = [n for n in body["items"] if n["id"] == chat_notification_id]
assert persisted, "chat-message notification delivered over WS was not found via GET /api/notifications"
assert persisted[0]["type"] == "message"
assert persisted[0]["actor_id"] == user_b.id
assert body["unread_count"] == unread_before + 1
print("  persisted chat-message notification:", persisted[0])

# Invalid/missing token -> connection closed with 4401
try:
    with client.websocket_connect("/api/notifications/ws?token=not-a-real-token") as ws:
        ws.receive_json()
    ws_auth_ok = False
except Exception as e:
    # starlette's test client raises WebSocketDisconnect with the close code
    ws_auth_ok = getattr(e, "code", None) == 4401
results.append(("WS /api/notifications/ws (bad token) -> closes 4401", "n/a", ws_auth_ok))
print(f"{'PASS' if ws_auth_ok else 'FAIL'} | WS bad token close code check")

print()
print("=" * 60)
all_pass = all(ok for _, _, ok in results)
print("ALL PASS" if all_pass else "SOME FAILED")
for name, code, ok in results:
    print(f"  [{code}] {'OK' if ok else 'FAIL'} - {name}")
