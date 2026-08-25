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
print("  owner:", story.get("owner"))
assert story["owner"]["username"] == "anjali"
assert story["owner"]["avatar_url"] == "/static/avatars/anjali.png"

# 2. GET /api/stories/mine (u1)
r = check("GET /api/stories/mine", client.get("/api/stories/mine", headers=h1), 200)
mine = r.json()
assert mine["items"][0]["owner"]["username"] == "anjali"
assert mine["items"][0]["owner"]["full_name"] == "Anjali"
print("  mine[0].owner:", mine["items"][0]["owner"])

# 3. GET story feed (u2, follows u1)
r = check("GET /api/stories/feed", client.get("/api/stories/feed", headers=h2), 200)
feed = r.json()
assert feed["items"][0]["user"]["username"] == "anjali"
assert feed["items"][0]["stories"][0]["owner"]["username"] == "anjali"
print("  feed[0].user:", feed["items"][0]["user"])
print("  feed[0].stories[0].owner:", feed["items"][0]["stories"][0]["owner"])

# 4. Create post comment (u2)
r = check("POST /api/posts/{id}/comments", client.post(
    f"/api/posts/{post.id}/comments", headers=h2, json={"content": "nice post!"}
), 201)
comment = r.json()
assert comment["author"]["username"] == "rahul"
assert comment["author"]["avatar_url"] == "/static/avatars/rahul.png"
print("  comment.author:", comment["author"])

# 5. GET post comments
r = check("GET /api/posts/{id}/comments", client.get(f"/api/posts/{post.id}/comments", headers=h1), 200)
assert r.json()["items"][0]["author"]["username"] == "rahul"

# 6. Reply to comment
r = check("POST /api/comments/{id}/reply", client.post(
    f"/api/comments/{comment['id']}/reply", headers=h1, json={"content": "thanks!"}
), 201)
reply = r.json()
assert reply["author"]["username"] == "anjali"
print("  reply.author:", reply["author"])

# 7. GET replies
r = check("GET /api/comments/{id}/replies", client.get(f"/api/comments/{comment['id']}/replies", headers=h2), 200)
assert r.json()["items"][0]["author"]["username"] == "anjali"
assert r.json()["total"] == 1

# 8. Create reel comment (u2)
r = check("POST /api/reels/{id}/comments", client.post(
    f"/api/reels/{reel.id}/comments", headers=h2, json={"content": "cool reel!"}
), 201)
reel_comment = r.json()
assert reel_comment["author"]["username"] == "rahul"
print("  reel comment.author:", reel_comment["author"])

# 9. GET reel comments
r = check("GET /api/reels/{id}/comments", client.get(f"/api/reels/{reel.id}/comments", headers=h1), 200)
assert r.json()["items"][0]["author"]["username"] == "rahul"
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
assert r.json()["owner"]["username"] == "anjali"

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
assert noavatar_comment["author"]["avatar_url"] is None
assert noavatar_comment["author"]["full_name"] is None
print("  no-avatar author:", noavatar_comment["author"])

print()
print("=" * 60)
all_pass = all(ok for _, _, ok in results)
print("ALL PASS" if all_pass else "SOME FAILED")
for name, code, ok in results:
    print(f"  [{code}] {'OK' if ok else 'FAIL'} - {name}")
