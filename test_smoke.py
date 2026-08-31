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

print()
print("=" * 60)
all_pass = all(ok for _, _, ok in results)
print("ALL PASS" if all_pass else "SOME FAILED")
for name, code, ok in results:
    print(f"  [{code}] {'OK' if ok else 'FAIL'} - {name}")
