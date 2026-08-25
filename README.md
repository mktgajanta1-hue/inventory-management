# MediaTrack — DOOH Inventory & Live Tracking (MVP)

Track which client is live on which digital screen, see loop availability at a glance, and know exactly when the next slot opens.

## Quick start

```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://mediatrack:mediatrack@localhost:5432/mediatrack
python seed.py                      # loads Ajanta's screens into PostgreSQL
uvicorn app:app --reload --port 8000
```

Or run the whole stack (app + PostgreSQL) with Docker:

```bash
cp .env.example .env                # set SECRET_KEY and ADMIN_PASSWORD
docker compose up --build           # → http://localhost:8000
```

Configuration is read from the environment — see `.env.example`.
Deploying to Render is covered in [DEPLOYMENT.md](DEPLOYMENT.md).

Then open `frontend/index.html` in any browser.
- If the API is running → header shows **source: API live**.
- If not → the dashboard still works on built-in **demo data** (same seed), so you can demo it anywhere, even offline on your phone.

Interactive API docs: http://localhost:8000/docs

## Architecture

```
mediatrack/
├── backend/
│   ├── app.py           # FastAPI routes (inventory, live, availability, booking)
│   ├── database.py      # Schema + PostgreSQL connection pool (DATABASE_URL)
│   ├── seed.py          # Ajanta's 8 digital screens + pipeline clients
│   └── requirements.txt
├── frontend/
│   └── index.html       # Single-file dashboard (no build step)
├── Dockerfile           # Production image
├── docker-compose.yml   # Local app + PostgreSQL
├── render.yaml          # Render blueprint (web service + DB + photo disk)
├── .env.example         # Every configuration variable
├── DEPLOYMENT.md        # Deployment guide
└── README.md
```

## Interface

One HTML file, no build step — the backend serves it. It is organised as a
design system rather than per-screen CSS:

* **Tokens** in `:root` — colour roles (surface scale, text scale, brand,
  status), a 4/8/12/16/24/32/40 spacing scale, radii, type scale, control
  heights, elevation. Nothing hard-codes a colour or a one-off margin.
* **Components** — `.card` (+ head/body/foot), `.btn` (primary · secondary ·
  ghost · danger · success, one height), `.field`/`.input`/`.check`, `.tablewrap`
  + `table`, `.badge`, `.kpi`, `.state` (empty · loading · error), `.toast`.
* **Dark select** — Chrome and Edge on Windows paint the native dropdown list in
  the OS light theme, which made the team switcher unreadable. Every `<select>`
  inside a `.selwrap` is mirrored by an accessible listbox (`role="listbox"`,
  arrow keys, Home/End, Escape, `aria-activedescendant`). The real `<select>`
  stays in the DOM, so form reads and `change` listeners are unchanged.

Navigation follows the job, not the database: **Dashboard · Media Inventory ·
Availability · Bookings · Clients**, then admin-only **Revenue & Sales ·
Administration**, then **My Account**. User management lives in Administration —
never mixed into the revenue report. Sales users never see the admin sections,
and the backend refuses those calls regardless of what the browser shows.

## Users, roles and access

| Field | Meaning |
|-------|---------|
| Role | `admin` (manages users, sees Revenue and Administration) or `sales` |
| Team | Odisha, Raipur, or all teams (admins) |
| Upload access | May add and edit displays and upload board photos |
| Status | Active or disabled — a disabled account cannot sign in, and any session it still holds stops working on its next request |

Administration → User management is the only place users are created or changed.
Each row has an actions menu: **Edit user** (name, role, team, upload access,
status), **Reset password**, **Disable / Enable**, **Sign out of all devices**,
**Delete**. Guards that hold regardless of what the browser sends:

* every one of those calls is refused for a sales user (403);
* an admin viewing one team cannot see or touch the other team's users (404);
* nobody can disable or demote their own account, or the last active admin;
* changing a role, team or status bumps the user's token version, so their open
  sessions pick up the change immediately;
* an admin password reset uses the same hashing as a self-service change, signs
  the user out everywhere, and never echoes the password back.

## Future modules

Plans, leads and proposals do not exist in the data model, and the navigation
only lists what actually works. When they are built they belong alongside
Bookings, and — like every other table — will need a `team` column and the same
scoping in every query.

## Teams — Odisha and Raipur

Two business units share one installation and never see each other's book.

* Every business row carries a `team` code: `screens`, `clients`, `campaigns`,
  `slots`, `users` and `activity_log`. The `teams` table holds the two codes
  (`odisha`, `raipur`).
* Each user has a **Team**: *Odisha*, *Raipur*, or *Admin* (all teams, stored as
  `all`). A team user's scope comes from their user row and nothing else — the
  `X-Team` header and `?team=` parameter are read **only** for users who
  legitimately span both teams, so editing a URL, an id or a filter in the
  browser cannot reach the other team's data.
* Isolation is enforced in the API layer, not the UI: every list query filters on
  team and every lookup by id is team-scoped, returning **404** for the other
  team's row (404, not 403, so an id's existence stays private). That covers
  dashboards, inventory, digital screens, bookings, clients, live campaigns,
  revenue, the activity log, search/filter results and every Excel/CSV export.
* Admins get a team switcher in the header — **Odisha / Raipur / All Teams** —
  and their dashboard, revenue and reports follow the selection.
* Upgrading an existing database is automatic: everything already in it is
  Bhubaneswar inventory and is backfilled to the Odisha team; existing admins
  become all-teams. Raipur starts empty and adds its own displays and clients.
* A display can be moved between teams (with its bookings) by an All Teams admin:
  `PATCH /api/screens/{id}/team`.

## Data model

| Table     | Purpose                                                                 |
|-----------|-------------------------------------------------------------------------|
| teams     | The two business units: `odisha`, `raipur`                               |
| clients   | Advertiser directory (company, contact, industry) — one team             |
| screens   | Physical inventory: size, resolution, loop capacity, slot length, rate — one team |
| campaigns | A client's creative/commercial container — one team                      |
| slots     | The airtime reservation: campaign ⇄ screen ⇄ loop position ⇄ date range — one team |

A company that both teams sell to is two client records, one per team: company
names are unique **per team**, not globally.

**Availability is never stored — always derived from `slots`,** so it can't go stale.
A screen with an 8-slot loop and 5 overlapping bookings today has 3 open slots today.

## API

| Method | Route                 | What it returns                                        |
|--------|-----------------------|--------------------------------------------------------|
| GET    | /api/screens          | All screens + live count, open slots, next opening, 30-day calendar |
| GET    | /api/screens/{id}     | One screen, same payload                               |
| GET    | /api/campaigns/live   | Everything on air today, sorted by soonest expiry      |
| GET    | /api/clients          | Advertiser directory                                   |
| POST   | /api/campaigns        | Book a slot (auto-assigns first free loop position, rejects if loop is full for the window) |
| GET    | /api/health           | Liveness + readiness (database, photo storage) — no auth |
| GET    | /api/teams            | The teams the caller may view, and whether they can switch |
| PATCH  | /api/screens/{id}/team| Move a display + its bookings to the other team (All Teams admin) |
| PATCH  | /api/users/{id}/team  | Move a user to the other team (admin)                  |
| PATCH  | /api/users/{id}       | Edit name, role, team, upload access, active status (admin) |
| POST   | /api/users/{id}/password | Reset another user's password and sign them out (admin) |

Admins select the team they are looking at with an `X-Team: odisha|raipur|all`
header (or `?team=` on download links). The header is ignored for users who
belong to a single team.

Booking example:

```json
POST /api/campaigns
{
  "screen_id": 2,
  "client_id": 5,
  "campaign_name": "Evos Diwali Burst",
  "creative": "Alchemy Tower 15s",
  "start_date": "2026-10-15",
  "end_date": "2026-11-15"
}
```

## Make it real software (.exe)

One-time build on any Windows PC:

1. Open the `backend` folder
2. Double-click **build.bat** — wait 2–5 minutes
3. Your software appears at `backend\dist\MediaTrack.exe`

After that, forget Python entirely:
- Double-click **MediaTrack.exe** → server starts + dashboard opens in the browser
- Point `DATABASE_URL` at a PostgreSQL the PC can reach; back up with the
  in-app backup button (streams a `pg_dump`)
- Team access stays the same: `http://<pc-ip>:8000` on office WiFi
- To stop: close the MediaTrack window

## Database

PostgreSQL, read from `DATABASE_URL`. All SQL stays isolated in `database.py`,
which keeps the original sqlite-style call surface (`?` placeholders,
`cursor.lastrowid`) and translates to PostgreSQL in one place — so the routes in
`app.py` are unchanged. Dates remain ISO `TEXT`, exactly as before.

## Roadmap ideas

- Booking form + drag-to-book on the calendar strip
- Rate card / revenue view per screen per month
- Proof-of-play photo upload per slot
- WhatsApp expiry alerts ("Sriya Square opens in 5 days — pitch Great Eastern")
