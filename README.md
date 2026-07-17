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

## Data model

| Table     | Purpose                                                                 |
|-----------|-------------------------------------------------------------------------|
| clients   | Advertiser directory (company, contact, industry)                        |
| screens   | Physical inventory: size, resolution, loop capacity, slot length, rate  |
| campaigns | A client's creative/commercial container                                 |
| slots     | The airtime reservation: campaign ⇄ screen ⇄ loop position ⇄ date range |

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
