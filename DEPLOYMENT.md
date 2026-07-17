# Deploying MediaTrack

MediaTrack runs as a single Docker container (FastAPI serving both the API and
the dashboard) plus a PostgreSQL database and a small disk for uploaded photos.

- **Application**: FastAPI + uvicorn, `backend/app.py`
- **Database**: PostgreSQL, connection read from `DATABASE_URL`
- **Uploads**: written to `PHOTOS_DIR`, which must be a persistent volume
- **Health check**: `GET /api/health`

---

## 1. Deploy to Render (blueprint)

The repo already contains `render.yaml`, so Render can create everything in one go.

1. Push this repo to GitHub.
2. In Render: **New → Blueprint** → select the repo → **Apply**.
   This creates the `mediatrack` web service, the `mediatrack-db` PostgreSQL
   instance, and the 1 GB `mediatrack-photos` disk.
3. Render will ask for the one value that is deliberately not in the file:

   | Variable | Value |
   |---|---|
   | `ADMIN_PASSWORD` | a strong password for the first admin login |

   Everything else is wired automatically: `DATABASE_URL` is injected from the
   database, and `SECRET_KEY` is generated once and then kept stable.
4. Wait for the first deploy. On boot the app creates its schema, loads the seed
   screens and client directory, and creates the admin account.
5. Open `https://<your-service>.onrender.com` and log in as `admin`.
   **Change the password immediately** (Users tab → add a new admin → delete the
   bootstrap one, or re-deploy with a new `ADMIN_PASSWORD` against an empty DB).

### Region and plan notes

- `render.yaml` uses `region: singapore` (lowest latency from Odisha). The web
  service and the database must be in the same region.
- The plan is `starter`, not `free`, for one specific reason: **persistent disks
  are not available on the free plan**, and free services also sleep after
  inactivity. If you deploy on free, uploaded photos are deleted on every
  deploy — see "Photos" below.

---

## 2. Run locally with Docker

```bash
cp .env.example .env         # then edit SECRET_KEY and ADMIN_PASSWORD
docker compose up --build
# dashboard: http://localhost:8000
```

`docker compose` starts PostgreSQL, waits for it to become healthy, then starts
the app. Uploaded photos go to the `photos` volume and the database to `db_data`,
so both survive `docker compose down` (use `down -v` to wipe them).

## 3. Run locally without Docker

```bash
# a PostgreSQL you can reach, e.g.
createdb mediatrack

cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://mediatrack:mediatrack@localhost:5432/mediatrack
python seed.py                  # optional: also loads 13 demo bookings
uvicorn app:app --reload --port 8000
```

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | **yes** | — | PostgreSQL DSN. `postgres://` is rewritten to `postgresql://`; `sslmode=require` is added automatically for non-local hosts. |
| `SECRET_KEY` | in production | random file | Signs session cookies. **If this changes, every user is logged out.** |
| `ADMIN_PASSWORD` | first boot | — | Password for the bootstrap admin. Only read when the `users` table is empty. In production the app refuses to start rather than create a default password. |
| `ADMIN_USERNAME` | no | `admin` | Bootstrap admin username. |
| `ADMIN_NAME` | no | `Admin` | Display name for the bootstrap admin. |
| `ENVIRONMENT` | no | `development` | `production` enables Secure cookies and the strict checks above. |
| `CORS_ORIGINS` | no | `*` dev / empty prod | Comma-separated allowed origins. Leave empty in production — the dashboard is same-origin. |
| `PHOTOS_DIR` | in production | `frontend/photos` | Where uploads are written. Must be on a persistent volume. |
| `PORT` | no | `8000` | Render sets this automatically. |
| `COOKIE_SECURE` | no | on in production | Override only if you terminate TLS unusually. |
| `SEED_ON_START` | no | `true` | Set `false` to start with an empty database instead of the seed screens. |
| `WEB_CONCURRENCY` | no | `1` | uvicorn worker count. Raise only on a multi-CPU plan. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | no | `1` / `10` | Connection pool size. Keep `DB_POOL_MAX` below your Postgres plan's connection limit. |
| `DB_POOL_TIMEOUT` | no | `10` | Seconds to wait for a connection before failing (keeps `/api/health` fast when the DB is down). |

Generate a secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## Health check

```
GET /api/health      # unauthenticated
```

```json
{"status":"healthy","version":"3.4","environment":"production",
 "checks":{"database":"ok","photos":"ok"}}
```

Returns **200** when the database answers and the photos directory is writable,
**503** otherwise — so a container that is running but cannot reach Postgres is
correctly reported as unhealthy and taken out of rotation. Render is pointed at
this path via `healthCheckPath` in `render.yaml`.

---

## Photos

Uploaded screen photos are files, and container filesystems are erased on every
deploy. They are therefore written to `PHOTOS_DIR` (`/data/photos`), which is a
Render disk in production and a Docker volume locally.

On boot the app copies the seed photos shipped in `frontend/photos/` into
`PHOTOS_DIR` if they are not already there, and **never overwrites an existing
file** — so the seeded screens show their photos on a fresh volume, and your
uploads are never clobbered by a deploy.

If you deploy on a plan without a disk, uploads will disappear on each deploy.
The fix is a paid plan with a disk, or moving uploads to S3/Cloudflare R2.

> A disk also means the service cannot run more than one instance (Render disks
> attach to a single instance). For this workload — a sales team, not the public
> internet — one instance is the right call. Scale vertically first; if you ever
> need multiple instances, move photos to object storage.

---

## Backups

The old "download the .db file" button now streams a **`pg_dump`** of the live
database (`GET /api/backup`, any logged-in user). Restore it with:

```bash
psql "$DATABASE_URL" < mediatrack_backup_2026-07-17.sql
```

If `pg_dump` is unavailable, the endpoint falls back to a JSON export of every
table so the button never dead-ends. Render also takes its own automatic
database backups — this is for your own off-platform copy.

---

## Migrating existing SQLite data

If you have a `mediatrack.db` with real bookings in it, move it across once:

```bash
pip install pgloader        # or: apt install pgloader
pgloader mediatrack.db "$DATABASE_URL"
```

Then sanity-check the row counts and reset the identity sequences:

```sql
SELECT setval(pg_get_serial_sequence('clients','id'),   COALESCE(MAX(id),1)) FROM clients;
SELECT setval(pg_get_serial_sequence('screens','id'),   COALESCE(MAX(id),1)) FROM screens;
SELECT setval(pg_get_serial_sequence('campaigns','id'), COALESCE(MAX(id),1)) FROM campaigns;
SELECT setval(pg_get_serial_sequence('slots','id'),     COALESCE(MAX(id),1)) FROM slots;
SELECT setval(pg_get_serial_sequence('users','id'),     COALESCE(MAX(id),1)) FROM users;
```

The column types are unchanged from the SQLite schema (dates are still ISO
`TEXT`), so the data maps across 1:1. Copy `frontend/photos/*` onto the disk at
`/data/photos`, and set `SEED_ON_START=false` so the seed inventory is not added
on top of your real data.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Deploy fails: `DATABASE_URL is not set` | The database isn't linked. Check the `fromDatabase` block in `render.yaml`. |
| Deploy fails: `No users exist and ADMIN_PASSWORD is not set` | Set `ADMIN_PASSWORD` in the Render dashboard. Intentional — it stops a known default password reaching the internet. |
| Everyone logged out after a deploy | `SECRET_KEY` changed. It must be a fixed value across deploys. |
| Login succeeds, next request 401 | Cookie rejected. Confirm you're on HTTPS with `ENVIRONMENT=production`. |
| Photos 404 after a deploy | `PHOTOS_DIR` isn't on a disk. Check the `disk:` block and that `PHOTOS_DIR=/data/photos`. |
| Health check 503 | Read `checks` in the response body: `database: unreachable` (DB down/wrong DSN) or `photos: not writable` (disk not mounted). |
| `too many connections` | Lower `DB_POOL_MAX` or raise the database plan. |
| Slow first request after idle | Free/starter instances spin down. Expected on those plans. |

---

## Local `.exe` build

`build.bat` and `launcher.py` are unchanged and still work, but the app now
speaks PostgreSQL only — the packaged `.exe` needs a reachable `DATABASE_URL`
(for example a Postgres running on the office PC) rather than a local
`mediatrack.db` file. For a networked team, the Render deployment above replaces
the `.exe` workflow entirely.
