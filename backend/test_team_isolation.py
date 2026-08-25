"""
Team isolation checks — the security guarantee behind the Odisha / Raipur split.

Run against a throwaway database (it creates users and a screen and leaves them
behind, so never point it at production):

    pip install httpx                     # FastAPI's TestClient needs it
    export DATABASE_URL=postgresql://mediatrack:mediatrack@localhost:5432/mediatrack
    python seed.py
    python test_team_isolation.py

Exit code 0 = every check passed. Each check asserts something a user of one
team must not be able to do, including by editing a URL, an id, a filter or a
request header by hand.
"""

import datetime
import os
import sys

os.environ.setdefault("SEED_ON_START", "false")
os.environ.setdefault("SECRET_KEY", "team-isolation-test")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient          # noqa: E402

import app as A                                    # noqa: E402
from auth import hash_pw                           # noqa: E402
from database import get_conn                      # noqa: E402

FAILED = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def login(username, password):
    c = TestClient(A.app)
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    return c, r.json()


# --- fixtures: one all-teams admin, one user per team ------------------------
SUFFIX = datetime.datetime.now().strftime("%H%M%S")
ROOT, ODI, RAI = f"tiroot{SUFFIX}", f"tiodi{SUFFIX}", f"tirai{SUFFIX}"
PW = "isolation-test-pw"

salt, ph = hash_pw(PW)
with get_conn() as conn:
    conn.execute(
        "INSERT INTO users (username, name, role, team, salt, pw_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (ROOT, "Isolation Root", "admin", "all", salt, ph,
         datetime.date.today().isoformat()),
    )
    conn.commit()

admin, me = login(ROOT, PW)
check(me["team"] == "all" and me["can_switch_teams"], "admin logs in with all-teams access")

for uname, team in ((ODI, "odisha"), (RAI, "raipur")):
    r = admin.post("/api/users", json={"username": uname, "name": uname, "password": PW,
                                       "role": "sales", "team": team})
    check(r.status_code == 201 and r.json()["team"] == team, f"create a {team} user")

odi, odi_me = login(ODI, PW)
rai, rai_me = login(RAI, PW)
check(odi_me["team"] == "odisha" and not odi_me["can_switch_teams"],
      "a team user is pinned to their own team")

base_odi_screens = {s["id"] for s in odi.get("/api/screens").json()}
base_rai_screens = {s["id"] for s in rai.get("/api/screens").json()}
base_odi_bookings = len(odi.get("/api/bookings").json())

# --- inventory ---------------------------------------------------------------
r = admin.post("/api/screens", json={"name": f"Isolation Test LED {SUFFIX}",
                                     "location": "Ring Road", "city": "Raipur",
                                     "width_ft": 20, "height_ft": 10, "rate_month": 120000})
check(r.status_code == 422, "an All Teams admin must name the team when adding a screen")
r = admin.post("/api/screens", json={"name": f"Isolation Test LED {SUFFIX}",
                                     "location": "Ring Road", "city": "Raipur",
                                     "width_ft": 20, "height_ft": 10, "rate_month": 120000,
                                     "team": "raipur"})
check(r.status_code == 201 and r.json()["team"] == "raipur", "admin adds a Raipur screen")
SCREEN = r.json()["screen_id"]

check({s["id"] for s in odi.get("/api/screens").json()} == base_odi_screens,
      "the new Raipur screen is invisible to the Odisha team")
check({s["id"] for s in rai.get("/api/screens").json()} == base_rai_screens | {SCREEN},
      "the Raipur team sees its new screen")
check(odi.get(f"/api/screens/{SCREEN}").status_code == 404,
      "an Odisha user cannot fetch a Raipur screen by id")
check({s["id"] for s in rai.get("/api/screens?team=odisha").json()} == base_rai_screens | {SCREEN},
      "?team=odisha is ignored for a Raipur user")
check({s["id"] for s in rai.get("/api/screens", headers={"X-Team": "all"}).json()}
      == base_rai_screens | {SCREEN},
      "X-Team: all is ignored for a Raipur user")

# --- bookings ----------------------------------------------------------------
t = datetime.date.today()
booking = {"screen_id": SCREEN, "client_name": f"Shared Advertiser {SUFFIX}",
           "campaign_name": "Isolation Test Campaign", "creative": "15s",
           "start_date": t.isoformat(), "end_date": (t + datetime.timedelta(days=20)).isoformat(),
           "rate_month": 90000}
check(odi.post("/api/campaigns", json=booking).status_code == 404,
      "an Odisha user cannot book on a Raipur screen")
check(rai.post("/api/campaigns", json=booking).status_code == 201,
      "the Raipur user books on their own screen")

odi_books = odi.get("/api/bookings").json()
rai_books = [b for b in rai.get("/api/bookings").json() if b["campaign"] == "Isolation Test Campaign"]
check(len(odi_books) == base_odi_bookings, "the Raipur booking never reaches the Odisha manage view")
check(len(rai_books) == 1, "the Raipur team sees its own booking")
check(all(c["campaign"] != "Isolation Test Campaign" for c in odi.get("/api/campaigns/live").json()),
      "the Raipur campaign is absent from Odisha's live list")

slot_id = rai_books[0]["slot_id"]
check(odi.patch(f"/api/slots/{slot_id}/stop").status_code == 404,
      "an Odisha user cannot take a Raipur ad off air")
check(odi.delete(f"/api/slots/{slot_id}").status_code == 404,
      "an Odisha user cannot delete a Raipur booking")

# --- clients -----------------------------------------------------------------
rai_clients = rai.get("/api/clients").json()
check(all(c["team"] == "raipur" for c in rai_clients), "the client list is team-only")
new_client = [c for c in rai_clients if c["company"] == f"Shared Advertiser {SUFFIX}"][0]
check(odi.delete(f"/api/clients/{new_client['id']}").status_code == 404,
      "an Odisha user cannot delete a Raipur client")
r = odi.post("/api/campaigns", json={**booking, "screen_id": sorted(base_odi_screens)[0]})
check(r.status_code in (201, 409),
      "the same company can be booked by the other team")
if r.status_code == 201:
    odi_same = [c for c in odi.get("/api/clients").json()
                if c["company"] == f"Shared Advertiser {SUFFIX}"]
    check(len(odi_same) == 1 and odi_same[0]["id"] != new_client["id"],
          "that company is a separate client record per team")

# --- money and reports -------------------------------------------------------
rev_rai = admin.get("/api/revenue", headers={"X-Team": "raipur"}).json()
check(rev_rai["team_name"] == "Raipur Team", "admin can pull revenue for one team")
check(odi.get("/api/revenue").status_code == 403, "sales users still cannot open revenue")
check(odi.get("/api/backup").status_code == 403, "the database backup is admin-only")
csv = admin.get("/api/export/bookings.csv", headers={"X-Team": "raipur"}).text
check(f"Isolation Test LED {SUFFIX}" in csv and "Odisha Team" not in csv,
      "the CSV export is team-scoped")
x = rai.get("/api/export/availability.xlsx")
check(x.status_code == 200 and "raipur" in x.headers["content-disposition"],
      "the availability export is team-scoped")
acts = admin.get("/api/activity", headers={"X-Team": "raipur"}).json()
check(all(a["team"] == "raipur" for a in acts), "the activity log is team-scoped")

# --- moving a screen between teams -------------------------------------------
check(rai.patch(f"/api/screens/{SCREEN}/team", json={"team": "odisha"}).status_code == 403,
      "a team user cannot move a screen to the other team")
check(admin.patch(f"/api/screens/{SCREEN}/team", json={"team": "odisha"}).status_code == 200,
      "an All Teams admin can move a screen")
check(any(s["id"] == SCREEN for s in odi.get("/api/screens").json())
      and all(s["id"] != SCREEN for s in rai.get("/api/screens").json()),
      "the screen moved, and its bookings with it")

print()
print(f"{len(FAILED)} CHECK(S) FAILED" if FAILED else "ALL CHECKS PASSED")
sys.exit(1 if FAILED else 0)
