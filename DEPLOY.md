# Phone OTP Auth API — gender field added

This is a clean copy of your `otp-auth-api-instagram-flow` project (source
code only — no `venv/`, `.git/`, or `.env`) with a `gender` field added to
registration.

## What changed

- **`app/models.py`** — new `Gender` enum (`male`, `female`, `non_binary`,
  `prefer_not_to_say`); `gender` column added to both `User` and
  `PendingSignup` (nullable, so existing rows aren't affected).
- **`app/schemas.py`** — `gender` added to `RegisterRequest` as optional
  (defaults to `None` if the frontend doesn't send it) and to `UserOut` so
  it's returned in `/register`, `/login`, `/verify-otp`, and `/me` responses.
- **`app/routers/auth_routes.py`** — `register()` now stores `gender` on the
  pending signup; `verify_otp()` carries it over onto the real `User` record
  once the signup is confirmed.

Gender is optional by design — existing frontend calls to `/register` that
don't send it will keep working exactly as before, with `gender` simply
staying `null` until you're ready to require it.

## Deploy to your server (32.199.119.31)

Same process as last time:

```bash
scp -r otp-auth-api-gender user@32.199.119.31:/home/user/
ssh user@32.199.119.31
cd /home/user/otp-auth-api-gender

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in real DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, SECRET_KEY
```

Stop whatever is currently running on port 8000:
```bash
sudo lsof -i :8000
sudo kill -9 <PID>
```

Start the new version:
```bash
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
```

### Important — table already exists on your RDS instance

`Base.metadata.create_all(bind=engine)` in `app/main.py` only creates tables
that don't exist yet — it will **not** add the new `gender` column to your
existing `users` or `pending_signups` tables in RDS, since those tables are
already there.

Add the column manually before starting the new version, connecting to your
RDS database (via SQLTools, MySQL Workbench, or the `mysql` CLI):

```sql
ALTER TABLE users
  ADD COLUMN gender ENUM('male','female','non_binary','prefer_not_to_say') NULL;

ALTER TABLE pending_signups
  ADD COLUMN gender ENUM('male','female','non_binary','prefer_not_to_say') NULL;
```

Run those two statements once, then start the API as normal.

## Verify

```bash
curl -X POST http://32.199.119.31:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"testuser1","identifier":"test1@example.com","password":"testpass123","date_of_birth":"2000-01-01","gender":"female"}'
```

Then check `/docs` — the `RegisterRequest` schema in Swagger should now show
`gender` as an available (optional) field.
