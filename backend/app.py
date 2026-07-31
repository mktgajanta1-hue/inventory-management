"""
MediaTrack API — FastAPI + PostgreSQL.

Run:
    pip install -r requirements.txt
    export DATABASE_URL=postgresql://mediatrack:mediatrack@localhost:5432/mediatrack
    python seed.py
    uvicorn app:app --reload --port 8000

Configuration is read from the environment — see .env.example.
Docs auto-generated at http://localhost:8000/docs
"""

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import hash_pw, make_token, read_token, verify_pw
from database import database_url, get_conn, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%d-%b %H:%M:%S",
)
log = logging.getLogger("mediatrack")

ENV = os.environ.get("ENVIRONMENT", "development").lower()
IS_PROD = ENV == "production"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Cookies must be Secure over HTTPS in production; plain HTTP locally.
COOKIE_SECURE = _env_bool("COOKIE_SECURE", IS_PROD)

app = FastAPI(title="MediaTrack", version="1.0.0")


@app.exception_handler(Exception)
async def unhandled(request, exc):
    """Never leak a raw traceback to the UI — log it, return clean JSON."""
    from fastapi.responses import JSONResponse
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error — check server logs"})

# CORS. The dashboard is served by this same app, so in production the browser
# makes same-origin requests and needs no CORS at all. CORS_ORIGINS exists for
# the cases that do: the file:// demo mode and any separately hosted frontend.
#   CORS_ORIGINS="*"                                  → open (dev default)
#   CORS_ORIGINS="https://mediatrack.onrender.com"    → locked down (production)
_origins = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "" if IS_PROD else "*").split(",") if o.strip()]
if _origins == ["*"]:
    # Credentialed requests are impossible with a wildcard (browsers reject it),
    # so advertise the wildcard honestly instead of pretending cookies work.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if IS_PROD:
        log.warning("CORS_ORIGINS='*' in production — set explicit origins.")
elif _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("CORS restricted to: %s", ", ".join(_origins))
else:
    log.info("CORS disabled — dashboard is served same-origin.")

init_db()


def ensure_default_admin() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        username = os.environ.get("ADMIN_USERNAME", "admin").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD", "")
        if not password:
            if IS_PROD:
                raise RuntimeError(
                    "No users exist and ADMIN_PASSWORD is not set. Set ADMIN_USERNAME / "
                    "ADMIN_PASSWORD so the first admin can be created safely."
                )
            password = "ajanta123"   # local dev only
        salt, ph = hash_pw(password)
        conn.execute(
            "INSERT INTO users (username, name, role, salt, pw_hash, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (username, os.environ.get("ADMIN_NAME", "Admin"), "admin", salt, ph,
             date.today().isoformat()),
        )
        conn.commit()
        log.warning("Bootstrap admin created — username '%s'. Change the password after "
                    "first login.", username)


ensure_default_admin()


def bootstrap_inventory() -> None:
    """
    First boot against an empty database: load the real screens + client
    directory, exactly like the .exe launcher does. A no-op once screens exist.
    Set SEED_ON_START=false to start with a completely empty database.
    """
    if not _env_bool("SEED_ON_START", True):
        return
    try:
        from seed import seed_inventory
        if seed_inventory():
            log.info("First run: loaded the seed screens and client directory.")
    except Exception:                                        # noqa: BLE001
        log.exception("Inventory seeding skipped")


bootstrap_inventory()


def current_user(request: Request):
    tok = request.cookies.get("mt_session")
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:]
    if not tok:
        tok = request.query_params.get("token")
    parsed = read_token(tok) if tok else None
    if not parsed:
        return None
    uid, tver = parsed
    with get_conn() as conn:
        r = conn.execute(
            "SELECT id, username, name, role, can_upload, token_version"
            " FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if not r or r["token_version"] != tver:
            return None   # token from before a logout-all — invalid everywhere
        return dict(r)


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "Login required")
    return u


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Access denied — admin only")
    return user


def require_upload(user: dict = Depends(require_user)) -> dict:
    """Admins always can; sales users need the can_upload permission."""
    if user["role"] != "admin" and not user.get("can_upload"):
        raise HTTPException(
            403, "You don't have media-upload permission — ask your admin to enable it"
        )
    return user


def log_activity(user_name: str, action: str, detail: str = "") -> None:
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO activity_log (user_name, action, detail) VALUES (?,?,?)",
                (user_name, action, detail[:300]),
            )
            conn.commit()
    except Exception:                                    # noqa: BLE001
        log.exception("activity log write failed")       # never break the request


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=120)


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with get_conn() as conn:
        r = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
            (body.username.strip(),),
        ).fetchone()
    if not r or not verify_pw(body.password, r["salt"], r["pw_hash"]):
        log.warning("Failed login for '%s'", body.username)
        raise HTTPException(401, "Wrong username or password")
    token = make_token(r["id"], r["token_version"])
    response.set_cookie(
        "mt_session", token, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, max_age=30 * 86400,
    )
    log.info("Login: %s (%s)", r["name"], r["role"])
    log_activity(r["name"], "login", "")
    return {"name": r["name"], "role": r["role"], "username": r["username"],
            "can_upload": bool(r["can_upload"]) or r["role"] == "admin",
            "token": token}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("mt_session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(require_user)):
    return {"name": user["name"], "role": user["role"], "username": user["username"],
            "can_upload": bool(user.get("can_upload")) or user["role"] == "admin"}


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=120)
    new_password: str = Field(min_length=6, max_length=120)


@app.post("/api/me/password")
def change_password(body: PasswordChange, response: Response,
                    user: dict = Depends(require_user)):
    """Change own password. Also invalidates every other device's session."""
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not verify_pw(body.old_password, r["salt"], r["pw_hash"]):
            raise HTTPException(401, "Current password is wrong")
        salt, ph = hash_pw(body.new_password)
        new_ver = r["token_version"] + 1
        conn.execute(
            "UPDATE users SET salt = ?, pw_hash = ?, token_version = ? WHERE id = ?",
            (salt, ph, new_ver, user["id"]),
        )
        conn.commit()
    token = make_token(user["id"], new_ver)   # fresh token so THIS device stays in
    response.set_cookie("mt_session", token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=30 * 86400)
    log_activity(user["name"], "password_changed", "")
    return {"ok": True, "token": token}


@app.post("/api/logout-all")
def logout_all(response: Response, user: dict = Depends(require_user)):
    """Sign out from every device (invalidates all tokens, everywhere)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
            (user["id"],),
        )
        conn.commit()
    response.delete_cookie("mt_session")
    log_activity(user["name"], "logout_all_devices", "")
    return {"ok": True}


# ---- user management (admin) ----

class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=120)
    role: str = Field(pattern="^(admin|sales)$")


@app.get("/api/users")
def list_users(_: dict = Depends(require_admin)):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, username, name, role, can_upload, created_at FROM users ORDER BY id")]


@app.post("/api/users", status_code=201)
def add_user(u: UserIn, _: dict = Depends(require_admin)):
    salt, ph = hash_pw(u.password)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, name, role, salt, pw_hash, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (u.username.strip().lower(), u.name.strip(), u.role, salt, ph,
                 date.today().isoformat()),
            )
        except Exception:
            raise HTTPException(409, "That username already exists")
        conn.commit()
        log.info("User added: %s (%s)", u.username, u.role)
        log_activity(_["name"], "user_added", f"{u.name} ({u.role})")
        return {"created": True, "user_id": cur.lastrowid}


class PermissionIn(BaseModel):
    can_upload: bool


@app.patch("/api/users/{user_id}/permissions")
def set_permissions(user_id: int, body: PermissionIn,
                    admin: dict = Depends(require_admin)):
    with get_conn() as conn:
        target = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET can_upload = ? WHERE id = ?",
                     (body.can_upload, user_id))
        conn.commit()
    log_activity(admin["name"], "permission_changed",
                 f"{target['name']}: can_upload={'on' if body.can_upload else 'off'}")
    return {"updated": True}


@app.post("/api/users/{user_id}/logout-all")
def admin_logout_user(user_id: int, admin: dict = Depends(require_admin)):
    """Force-logout a user from every device."""
    with get_conn() as conn:
        target = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?",
                     (user_id,))
        conn.commit()
    log_activity(admin["name"], "force_logout", target["name"])
    return {"ok": True}


@app.get("/api/activity")
def activity(limit: int = 100, _: dict = Depends(require_admin)):
    limit = max(1, min(limit, 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT to_char(at, 'DD Mon HH24:MI') AS at, user_name, action, detail"
            " FROM activity_log ORDER BY at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(422, "You can't delete your own account")
    with get_conn() as conn:
        target = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        if target["role"] == "admin":
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
            if admins <= 1:
                raise HTTPException(422, "Can't delete the last admin")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return {"deleted": True}


CAL_DAYS = 30  # availability horizon


# ---------- helpers ----------

def today() -> date:
    return date.today()


def slot_rows_for_screen(conn, screen_id: int):
    return conn.execute(
        """
        SELECT s.*, c.name AS campaign, c.creative, cl.company, cl.industry
        FROM slots s
        JOIN campaigns c ON c.id = s.campaign_id
        JOIN clients cl  ON cl.id = c.client_id
        WHERE s.screen_id = ?
        ORDER BY s.start_date
        """,
        (screen_id,),
    ).fetchall()


def occupancy(slots, on: date) -> list:
    """Slots covering a given day."""
    iso = on.isoformat()
    return [s for s in slots if s["start_date"] <= iso <= s["end_date"]]


def screen_payload(conn, screen) -> dict:
    slots = slot_rows_for_screen(conn, screen["id"])
    t = today()
    live = occupancy(slots, t)

    # Next opening: first day within horizon when occupied count drops
    next_opening = None
    for i in range(CAL_DAYS + 1):
        day = t + timedelta(days=i)
        if len(occupancy(slots, day)) < screen["loop_slots"]:
            next_opening = day.isoformat()
            break

    calendar = [
        {
            "date": (t + timedelta(days=i)).isoformat(),
            "booked": len(occupancy(slots, t + timedelta(days=i))),
        }
        for i in range(CAL_DAYS)
    ]

    return {
        **dict(screen),
        "live_count": len(live),
        "open_now": screen["loop_slots"] - len(live),
        "next_opening": next_opening,
        "live": [
            {
                "slot_id": s["id"],
                "company": s["company"],
                "campaign": s["campaign"],
                "creative": s["creative"],
                "position_no": s["position_no"],
                "start_date": s["start_date"],
                "end_date": s["end_date"],
                "days_left": (date.fromisoformat(s["end_date"]) - t).days,
            }
            for s in sorted(live, key=lambda r: r["position_no"])
        ],
        "calendar": calendar,
    }


# ---------- routes ----------

@app.get("/api/screens")
def list_screens(user: dict = Depends(require_user)):
    """Full inventory with live occupancy, next opening and 30-day calendar."""
    with get_conn() as conn:
        screens = conn.execute("SELECT * FROM screens ORDER BY id").fetchall()
        return [screen_payload(conn, s) for s in screens]


@app.get("/api/screens/{screen_id}")
def get_screen(screen_id: int, user: dict = Depends(require_user)):
    with get_conn() as conn:
        screen = conn.execute(
            "SELECT * FROM screens WHERE id = ?", (screen_id,)
        ).fetchone()
        if not screen:
            raise HTTPException(404, "Screen not found")
        return screen_payload(conn, screen)


@app.get("/api/campaigns/live")
def live_campaigns(user: dict = Depends(require_user)):
    """Everything on air right now, across all screens."""
    iso = today().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id AS slot_id, s.position_no, s.start_date, s.end_date,
                   s.rate_month, s.booked_by, sc.name AS screen, sc.location,
                   c.name AS campaign, c.creative, cl.company, cl.industry
            FROM slots s
            JOIN screens sc  ON sc.id = s.screen_id
            JOIN campaigns c ON c.id = s.campaign_id
            JOIN clients cl  ON cl.id = c.client_id
            WHERE s.start_date <= ? AND s.end_date >= ?
            ORDER BY s.end_date
            """,
            (iso, iso),
        ).fetchall()
        return [
            {**dict(r), "days_left": (date.fromisoformat(r["end_date"]) - today()).days}
            for r in rows
        ]


@app.get("/api/clients")
def list_clients(user: dict = Depends(require_user)):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clients ORDER BY company")]


class Booking(BaseModel):
    screen_id: int = Field(gt=0)
    client_name: str = Field(min_length=2, max_length=100)
    campaign_name: str = Field(min_length=2, max_length=120)
    creative: str = Field(default="", max_length=200)
    start_date: date
    end_date: date
    rate_month: int = Field(default=0, ge=0)


@app.post("/api/campaigns", status_code=201)
def book_slot(b: Booking, user: dict = Depends(require_user)):
    """Create a campaign + slot if the screen's loop has a free position
    for the entire requested window."""
    if b.end_date < b.start_date:
        raise HTTPException(422, "end_date must be on or after start_date")
    with get_conn() as conn:
        screen = conn.execute(
            "SELECT * FROM screens WHERE id = ?", (b.screen_id,)
        ).fetchone()
        if not screen:
            raise HTTPException(404, "Screen not found")
        # Find-or-create the client by name (case-insensitive, no duplicates)
        name = b.client_name.strip()
        row = conn.execute(
            "SELECT id FROM clients WHERE LOWER(company) = LOWER(?)", (name,)
        ).fetchone()
        if row:
            client_id = row["id"]
        else:
            client_id = conn.execute(
                "INSERT INTO clients (company) VALUES (?)", (name,)
            ).lastrowid
            log.info("New client auto-created: %s", name)

        # Positions blocked by any overlapping booking
        taken = {
            r["position_no"]
            for r in conn.execute(
                """SELECT position_no FROM slots
                   WHERE screen_id = ? AND NOT (end_date < ? OR start_date > ?)""",
                (b.screen_id, b.start_date.isoformat(), b.end_date.isoformat()),
            )
        }
        free = [p for p in range(1, screen["loop_slots"] + 1) if p not in taken]
        if not free:
            raise HTTPException(409, "No free loop position for that date range")

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO campaigns (client_id, name, creative) VALUES (?,?,?)",
            (client_id, b.campaign_name, b.creative),
        )
        cur.execute(
            """INSERT INTO slots (screen_id, campaign_id, position_no,
               start_date, end_date, rate_month, booked_by) VALUES (?,?,?,?,?,?,?)""",
            (
                b.screen_id,
                cur.lastrowid,
                free[0],
                b.start_date.isoformat(),
                b.end_date.isoformat(),
                b.rate_month or round(
                    screen["rate_month"]
                    * ((b.end_date - b.start_date).days + 1) / 30
                ),
                user["name"],
            ),
        )
        conn.commit()
        log.info("Booked '%s' on screen %s (P%s) %s → %s",
                 b.campaign_name, b.screen_id, free[0], b.start_date, b.end_date)
        log_activity(user["name"], "booking_created",
                     f"{b.client_name} — {b.campaign_name} on screen {b.screen_id}")
        return {"booked": True, "position_no": free[0], "campaign_id": cur.lastrowid}


class ScreenIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    location: str = Field(min_length=2, max_length=120)
    city: str = Field(default="Bhubaneswar", max_length=60)
    width_ft: float = Field(gt=0, le=200)
    height_ft: float = Field(gt=0, le=200)
    res_w: int = Field(default=0, ge=0)
    res_h: int = Field(default=0, ge=0)
    loop_slots: int = Field(default=8, ge=1, le=24)
    slot_seconds: int = Field(default=15, ge=5, le=120)
    spots_per_day: int = Field(default=360, ge=1)
    rate_month: int = Field(default=0, ge=0)


@app.post("/api/screens", status_code=201)
def add_screen(s: ScreenIn, user: dict = Depends(require_upload)):
    """Add a new display to the network."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO screens (name, location, city, width_ft, height_ft,
               res_w, res_h, loop_slots, slot_seconds, spots_per_day, rate_month)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (s.name, s.location, s.city, s.width_ft, s.height_ft, s.res_w,
             s.res_h, s.loop_slots, s.slot_seconds, s.spots_per_day, s.rate_month),
        )
        conn.commit()
        log.info("Screen added: %s (%s)", s.name, s.location)
        log_activity(user["name"], "screen_added", s.name)
        return {"created": True, "screen_id": cur.lastrowid}


@app.put("/api/screens/{screen_id}")
def update_screen(screen_id: int, s: ScreenIn, user: dict = Depends(require_upload)):
    """Update an existing display's details."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM screens WHERE id = ?", (screen_id,)).fetchone():
            raise HTTPException(404, "Screen not found")
        conn.execute(
            """UPDATE screens SET name=?, location=?, city=?, width_ft=?, height_ft=?,
               res_w=?, res_h=?, loop_slots=?, slot_seconds=?, spots_per_day=?, rate_month=?
               WHERE id=?""",
            (s.name, s.location, s.city, s.width_ft, s.height_ft, s.res_w, s.res_h,
             s.loop_slots, s.slot_seconds, s.spots_per_day, s.rate_month, screen_id),
        )
        conn.commit()
        log.info("Screen %s updated", screen_id)
        return {"updated": True}


@app.delete("/api/screens/{screen_id}")
def delete_screen(screen_id: int, user: dict = Depends(require_upload)):
    """Remove a display and all its bookings."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM screens WHERE id = ?", (screen_id,)).fetchone():
            raise HTTPException(404, "Screen not found")
        conn.execute("DELETE FROM slots WHERE screen_id = ?", (screen_id,))
        conn.execute("DELETE FROM screens WHERE id = ?", (screen_id,))
        conn.commit()
        log.warning("Screen %s deleted with all bookings", screen_id)
        log_activity(user["name"], "screen_deleted", f"screen {screen_id}")
        return {"deleted": True}


@app.get("/api/bookings")
def all_bookings(user: dict = Depends(require_user)):
    """Every booking (live, upcoming, recently ended) for the Manage view."""
    t = today()
    iso = t.isoformat()
    cutoff = (t - timedelta(days=14)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id AS slot_id, s.position_no, s.start_date, s.end_date, s.rate_month,
                   s.booked_by, sc.name AS screen, c.name AS campaign, cl.company
            FROM slots s
            JOIN screens sc  ON sc.id = s.screen_id
            JOIN campaigns c ON c.id = s.campaign_id
            JOIN clients cl  ON cl.id = c.client_id
            WHERE s.end_date >= ?
            ORDER BY s.start_date DESC
            """,
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            if r["start_date"] <= iso <= r["end_date"]:
                status = "live"
            elif r["start_date"] > iso:
                status = "upcoming"
            else:
                status = "ended"
            out.append({**dict(r), "status": status})
        return out


@app.patch("/api/slots/{slot_id}/stop")
def stop_slot(slot_id: int, user: dict = Depends(require_user)):
    """Take an ad off air now: frees the loop position from today.
    Future bookings are removed entirely."""
    t = today().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Booking not found")
        if row["start_date"] > t:
            conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        else:
            yesterday = (today() - timedelta(days=1)).isoformat()
            conn.execute("UPDATE slots SET end_date = ? WHERE id = ?", (yesterday, slot_id))
        conn.commit()
        log.info("Slot %s taken off air", slot_id)
        log_activity(user["name"], "ad_stopped", f"slot {slot_id}")
        return {"stopped": True}


@app.delete("/api/slots/{slot_id}")
def delete_slot(slot_id: int, user: dict = Depends(require_user)):
    """Remove a booking entirely (wrong entry)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        conn.commit()
        return {"deleted": True}


@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int, user: dict = Depends(require_user)):
    """Remove a client — cascades and removes all their bookings."""
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
            raise HTTPException(404, "Client not found")
        # Delete all slots (bookings) for this client's campaigns
        conn.execute(
            "DELETE FROM slots WHERE campaign_id IN "
            "(SELECT id FROM campaigns WHERE client_id = ?)",
            (client_id,),
        )
        # Delete campaigns
        conn.execute("DELETE FROM campaigns WHERE client_id = ?", (client_id,))
        # Delete client
        conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        conn.commit()
        log.warning("Client %s deleted with all campaigns and bookings", client_id)
        return {"deleted": True}


def bundled_photos_dir() -> Path:
    """The photos shipped with the repo (seed inventory)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "frontend" / "photos"
    return Path(__file__).parent.parent / "frontend" / "photos"


def photos_dir() -> Path:
    """
    Writable photos folder.

    PHOTOS_DIR points at a persistent volume in production (a Render disk, a
    docker volume locally). Container filesystems are wiped on every deploy, so
    uploads must not live inside the image. Defaults to the original
    frontend/photos so local development is unchanged.
    """
    env = os.environ.get("PHOTOS_DIR", "").strip()
    if env:
        p = Path(env)
    elif getattr(sys, "frozen", False):
        p = Path(sys.executable).parent / "photos"
    else:
        p = Path(__file__).parent.parent / "frontend" / "photos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sync_seed_photos() -> None:
    """
    Copy the repo's seed photos into the writable photos folder on first boot.
    Uploaded files are never overwritten. Without this, the nine seeded screens
    would show broken images when PHOTOS_DIR points at an empty volume.
    """
    import shutil
    src_dir, dst_dir = bundled_photos_dir(), photos_dir()
    if not src_dir.exists() or src_dir.resolve() == dst_dir.resolve():
        return
    for f in src_dir.iterdir():
        if f.is_file() and not (dst_dir / f.name).exists():
            shutil.copy2(f, dst_dir / f.name)


@app.post("/api/screens/{screen_id}/photo")
async def upload_photo(screen_id: int, file: UploadFile = File(...), user: dict = Depends(require_upload)):
    """Attach a photo to a screen (jpg/png)."""
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        raise HTTPException(422, "Use a jpg/png/webp image")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "Image too large — keep it under 8 MB")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM screens WHERE id = ?", (screen_id,)).fetchone():
            raise HTTPException(404, "Screen not found")
        fname = f"screen_{screen_id}.{ext}"
        (photos_dir() / fname).write_bytes(data)
        conn.execute("UPDATE screens SET photo = ? WHERE id = ?", (fname, screen_id))
        conn.commit()
    return {"photo": fname}


def _overlap_days(s: str, e: str, a: str, b: str) -> int:
    lo, hi = max(s, a), min(e, b)
    if lo > hi:
        return 0
    return (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1


def _all_slots(conn):
    return conn.execute(
        """SELECT s.*, sc.name AS screen, sc.loop_slots, c.name AS campaign, cl.company
           FROM slots s JOIN screens sc ON sc.id=s.screen_id
           JOIN campaigns c ON c.id=s.campaign_id JOIN clients cl ON cl.id=c.client_id"""
    ).fetchall()


@app.get("/api/revenue")
def revenue(start: date | None = None, end: date | None = None,
            _: dict = Depends(require_admin)):
    """Revenue = the actual Booking Amount of each booking, counted in full,
    attributed to the booking's start date. No proration, no demo values.
    Occupancy is the only day-based metric (it measures inventory, not money)."""
    t = today()
    end = end or t
    start = start or (end - timedelta(days=29))
    if start > end:
        raise HTTPException(422, "start must be on or before end")
    a, b = start.isoformat(), end.isoformat()

    with get_conn() as conn:
        slots = _all_slots(conn)
        screens = conn.execute("SELECT * FROM screens").fetchall()
        clients = conn.execute("SELECT created_by FROM clients").fetchall()
        campaign_count = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]

    def amt(s) -> int:
        return s["rate_month"] or 0   # stored as the booking's total amount

    def range_total(x: str, y: str) -> int:
        return sum(amt(s) for s in slots if x <= s["start_date"] <= y)

    iso = t.isoformat()
    month_a = t.replace(day=1).isoformat()
    year_a = t.replace(month=1, day=1).isoformat()

    by_screen, by_sales = {}, {}
    filtered_bookings = 0
    for s in slots:
        if not (a <= s["start_date"] <= b):
            continue
        filtered_bookings += 1
        r = amt(s)
        by_screen.setdefault(s["screen"], {"revenue": 0, "bookings": 0})
        by_screen[s["screen"]]["revenue"] += r
        by_screen[s["screen"]]["bookings"] += 1
        who = s["booked_by"] or "Unassigned"
        by_sales.setdefault(who, {"revenue": 0, "bookings": 0, "clients_added": 0})
        by_sales[who]["revenue"] += r
        by_sales[who]["bookings"] += 1
    for c in clients:
        who = c["created_by"]
        if who and who in by_sales:
            by_sales[who]["clients_added"] += 1
        elif who:
            by_sales.setdefault(who, {"revenue": 0, "bookings": 0, "clients_added": 1})

    # occupancy over the filter window (inventory utilisation — day-based by nature)
    days_n = (end - start).days + 1
    cap_days = sum(sc["loop_slots"] for sc in screens) * days_n
    booked_days = sum(_overlap_days(s["start_date"], s["end_date"], a, b) for s in slots)
    occupancy = round(100 * booked_days / cap_days, 1) if cap_days else 0.0

    live_now = [s for s in slots if s["start_date"] <= iso <= s["end_date"]]
    total_loop = sum(sc["loop_slots"] for sc in screens)

    # 12-month trend: full booking amounts grouped by start month
    monthly = []
    y, m = t.year, t.month
    for _i in range(12):
        ma = date(y, m, 1)
        mb = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
        monthly.append({"month": ma.strftime("%b %y"),
                        "revenue": range_total(ma.isoformat(), mb.isoformat())})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    monthly.reverse()

    return {
        "range": {"start": a, "end": b},
        "summary": {
            "filtered_revenue": range_total(a, b),
            "today": range_total(iso, iso),
            "month": range_total(month_a, iso),
            "year": range_total(year_a, iso),
            "bookings_in_range": filtered_bookings,
            "campaigns_total": campaign_count,
            "occupancy_pct": occupancy,
            "slots_booked_now": len(live_now),
            "slots_open_now": total_loop - len(live_now),
        },
        "by_screen": sorted(
            [{"screen": k, "revenue": v["revenue"], "bookings": v["bookings"]}
             for k, v in by_screen.items()], key=lambda x: -x["revenue"]),
        "by_sales": sorted(
            [{"name": k, "revenue": v["revenue"], "bookings": v["bookings"],
              "clients_added": v["clients_added"]}
             for k, v in by_sales.items()], key=lambda x: -x["revenue"]),
        "monthly": monthly,
    }


@app.get("/api/export/bookings.csv")
def export_csv(start: date | None = None, end: date | None = None,
               _: dict = Depends(require_admin)):
    import csv
    import io
    t = today()
    end = end or t
    start = start or (end - timedelta(days=29))
    a, b = start.isoformat(), end.isoformat()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Screen", "Client", "Campaign", "Position", "Start", "End",
                "Booking Amount", "Booked By"])
    with get_conn() as conn:
        for s in _all_slots(conn):
            if not (a <= s["start_date"] <= b):
                continue
            w.writerow([s["screen"], s["company"], s["campaign"], s["position_no"],
                        s["start_date"], s["end_date"], s["rate_month"] or 0,
                        s["booked_by"] or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="bookings_{a}_to_{b}.csv"'})


@app.get("/api/export/revenue.xlsx")
def export_xlsx(start: date | None = None, end: date | None = None,
                admin: dict = Depends(require_admin)):
    import io
    from openpyxl import Workbook
    data = revenue(start, end, admin)
    wb = Workbook()
    ws = wb.active; ws.title = "Summary"
    ws.append(["MediaTrack Revenue Report", f"{data['range']['start']} to {data['range']['end']}"])
    for k, v in data["summary"].items():
        ws.append([k.replace("_", " ").title(), v])
    ws2 = wb.create_sheet("By Screen")
    ws2.append(["Screen", "Revenue", "Bookings"])
    for r in data["by_screen"]:
        ws2.append([r["screen"], r["revenue"], r["bookings"]])
    ws3 = wb.create_sheet("By Salesperson")
    ws3.append(["Salesperson", "Revenue", "Bookings", "Clients Added"])
    for r in data["by_sales"]:
        ws3.append([r["name"], r["revenue"], r["bookings"], r["clients_added"]])
    ws4 = wb.create_sheet("Monthly Trend")
    ws4.append(["Month", "Revenue"])
    for r in data["monthly"]:
        ws4.append([r["month"], r["revenue"]])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return StreamingResponse(buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="revenue_report.xlsx"'})


@app.get("/api/export/availability.xlsx")
def export_availability(user: dict = Depends(require_user)):
    """One click → polished Excel of every screen's availability, for any
    logged-in user (sales send this to clients)."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    t = today()
    with get_conn() as conn:
        screens = conn.execute("SELECT * FROM screens ORDER BY name").fetchall()
        payloads = [screen_payload(conn, s) for s in screens]

    wb = Workbook()
    HEAD = Font(bold=True, color="FFFFFF", size=11)
    FILL = PatternFill("solid", fgColor="C11527")
    THIN = Border(*[Side(style="thin", color="DDDDDD")] * 4)
    GREEN = Font(color="1A7A44", bold=True)
    RED = Font(color="C11527", bold=True)

    ws = wb.active
    ws.title = "Available Media"
    ws.append([f"AJANTA ADVERTISERS — Digital Media Availability — {t.strftime('%d %b %Y')}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=11)
    cols = ["Screen", "Location", "City", "Size (ft)", "Resolution",
            "Loop Slots", "Booked Now", "Open Now", "Next Opening",
            "Card Rate (₹/mo)", "Status"]
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = HEAD
        cell.fill = FILL
        cell.alignment = Alignment(horizontal="center")
    for p in sorted(payloads, key=lambda x: -x["open_now"]):
        status = "AVAILABLE" if p["open_now"] else "FULL"
        ws.append([
            p["name"], p["location"], p["city"],
            f"{p['width_ft']}x{p['height_ft']}",
            f"{p['res_w']}x{p['res_h']}",
            p["loop_slots"], p["live_count"], p["open_now"],
            p["next_opening"] or "—", p["rate_month"], status,
        ])
        r = ws.max_row
        ws.cell(row=r, column=8).font = GREEN if p["open_now"] else RED
        ws.cell(row=r, column=11).font = GREEN if p["open_now"] else RED
        for c in range(1, len(cols) + 1):
            ws.cell(row=r, column=c).border = THIN
    widths = [22, 30, 14, 10, 12, 10, 11, 10, 13, 15, 11]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A3"

    ws2 = wb.create_sheet("30-Day Grid")
    dates = [(t + timedelta(days=i)) for i in range(30)]
    ws2.append(["Screen"] + [d.strftime("%d %b") for d in dates])
    for c in range(1, 32):
        cell = ws2.cell(row=1, column=c)
        cell.font = HEAD
        cell.fill = FILL
    ok_fill = PatternFill("solid", fgColor="E8F5EE")
    full_fill = PatternFill("solid", fgColor="FBE3E5")
    for p in payloads:
        row = [p["name"]] + [p["loop_slots"] - d["booked"] for d in p["calendar"]]
        ws2.append(row)
        r = ws2.max_row
        for c in range(2, 32):
            cell = ws2.cell(row=r, column=c)
            cell.fill = ok_fill if cell.value else full_fill
            cell.alignment = Alignment(horizontal="center")
    ws2.column_dimensions["A"].width = 22
    for i in range(2, 32):
        ws2.column_dimensions[get_column_letter(i)].width = 7
    ws2.freeze_panes = "B2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    log_activity(user["name"], "availability_exported", "")
    fname = f"Ajanta_Available_Media_{t.isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/backup")
def download_backup(user: dict = Depends(require_user)):
    """
    Download a full backup of the live database.

    There is no database file to copy any more, so this streams a pg_dump —
    restore it anywhere with:  psql "$DATABASE_URL" < mediatrack_backup_....sql
    If pg_dump is unavailable, every table is exported as JSON instead, so the
    button never dead-ends.
    """
    import io
    import subprocess
    stamp = date.today().isoformat()
    try:
        out = subprocess.run(
            ["pg_dump", "--no-owner", "--no-privileges", database_url()],
            capture_output=True, timeout=120,
        )
        if out.returncode == 0 and out.stdout:
            return StreamingResponse(
                iter([out.stdout]), media_type="application/sql",
                headers={"Content-Disposition":
                         f'attachment; filename="mediatrack_backup_{stamp}.sql"'},
            )
        log.warning("pg_dump failed (%s) — falling back to JSON backup",
                    out.stderr.decode()[:200])
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("pg_dump unavailable (%s) — falling back to JSON backup", e)

    import json
    dump = {}
    with get_conn() as conn:
        for table in ("clients", "screens", "campaigns", "slots", "users"):
            dump[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    buf = io.BytesIO(json.dumps(dump, indent=2, default=str).encode())
    return StreamingResponse(
        buf, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="mediatrack_backup_{stamp}.json"'},
    )


APP_VERSION = "4.0"


@app.get("/api/version")
def version():
    return {"version": APP_VERSION}


@app.get("/api/health")
def health(response: Response):
    """
    Liveness + readiness for Render's health check.

    Unauthenticated on purpose (the platform probe has no session) and it
    reports nothing sensitive: an app that answers 200 but cannot reach its
    database is not actually healthy, so the DB round-trip is the point.
    """
    checks = {"database": "unknown", "photos": "unknown"}
    ok = True
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception as e:                                   # noqa: BLE001
        log.exception("Health check: database unreachable")
        checks["database"] = "unreachable"
        ok = False
    try:
        checks["photos"] = "ok" if os.access(photos_dir(), os.W_OK) else "not writable"
        ok = ok and checks["photos"] == "ok"
    except Exception:                                        # noqa: BLE001
        checks["photos"] = "unavailable"
        ok = False

    response.status_code = 200 if ok else 503
    return {"status": "healthy" if ok else "unhealthy",
            "version": APP_VERSION, "environment": ENV, "checks": checks}


@app.get("/")
def index_page():
    """Serve the dashboard with no-store so browsers never cache a stale UI."""
    f = _find_frontend() / "index.html"
    if not f.exists():
        raise HTTPException(404, "UI not found")
    return FileResponse(f, headers={"Cache-Control": "no-store, must-revalidate"})


def _find_frontend() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "frontend"
    return Path(__file__).parent.parent / "frontend"


# ---------- serve the dashboard itself ----------
# Anyone on the office network can open http://<this-pc-ip>:8000 — no file sharing needed.
if getattr(sys, "frozen", False):
    _frontend = Path(sys._MEIPASS) / "frontend"  # bundled inside the .exe
else:
    _frontend = Path(__file__).parent.parent / "frontend"
sync_seed_photos()
app.mount("/photos", StaticFiles(directory=photos_dir()), name="photos")
if _frontend.exists():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="ui")
