"""
Seed MediaTrack with Ajanta's real digital inventory and live pipeline.
Run once:  python seed.py
"""

from datetime import date, timedelta

from database import get_conn, init_db

TODAY = date.today()


def d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


SCREENS = [
    # name, location, w, h, res_w, res_h, loop, slot_s, spots, rate, photo
    ("Sishubhawan LED", "Sishubhawan Square", 12.5, 12.5, 960, 960, 8, 15, 360, 150000, "sishubhawan.jpg"),
    ("Housing Board LED", "Housing Board, Opp. Akash Institute", 12.5, 12.5, 960, 960, 8, 15, 360, 150000, "housing_board.jpg"),
    ("Janpath Lyfe LED", "Janpath, Near Hotel Lyfe", 20, 20, 1280, 1280, 8, 15, 360, 200000, "janpath_lyfe.jpg"),
    ("Kalpana Square LED", "Kalpana Sq, Facing Utkal Galleria", 19, 21, 1216, 1344, 8, 15, 360, 170000, "kalpana.jpg"),
    ("Sriya Square LED", "Near Sriya Sq, Janpath Traffic", 22, 9, 1760, 720, 8, 15, 360, 180000, "sriya.jpg"),
    ("Rajmahal Panorama", "Rajmahal Square", 45, 6.5, 7740, 1120, 6, 15, 360, 260000, "rajmahal.jpg"),
    ("Rupali Square LED", "Rupali Square", 20, 10, 1600, 800, 8, 15, 360, 160000, "rupali.jpg"),
    ("Vani Vihar LED", "Vani Vihar", 25, 25, 1600, 1600, 8, 15, 360, 220000, "vani_vihar.jpg"),
    ("Jaydev Vihar LED", "Jaydev Vihar Junction", 25, 10.5, 1920, 810, 8, 15, 360, 300000, None),
]

CLIENTS = [
    ("Max Kalinga Hospital", "Marketing Team", "", "Healthcare"),
    ("Union Bank of India", "Zonal Office", "", "BFSI"),
    ("Great Eastern Electronics", "Marketing Head", "", "Consumer Electronics"),
    ("VinFast Bhubaneswar", "Dealer Marketing", "", "Automobile / EV"),
    ("Evos Buildcon", "Kalinga Keshari Rath", "", "Real Estate"),
    ("Ori Plast", "Alok Sir", "", "Building Materials"),
    ("Indriya Jewellery", "Brand Team", "", "Jewellery"),
    ("Saluja Gold", "Ashish Sir", "", "Jewellery"),
]

# screen_id, client_id, campaign name, creative, pos, start, end, rate
SLOTS = [
    (9, 1, "Max Kalinga Launch", "Cardiac Sciences 15s", 1, d(-20), d(40), 300000),
    (9, 3, "GE Monsoon Sale", "AC EMI Offer 15s", 2, d(-10), d(5), 300000),
    (9, 7, "Indriya Debut", "Wedding Collection 15s", 3, d(-5), d(55), 300000),
    (1, 2, "UB Home Loan Utsav", "8.35% Rate 15s", 1, d(-15), d(15), 150000),
    (3, 3, "GE Janpath Store Drive", "500m Store Arrow 15s", 1, d(-30), d(0), 200000),
    (3, 8, "Saluja Akshaya Push", "Gold Rate Live 15s", 2, d(-8), d(22), 200000),
    (8, 1, "Max Kalinga OPD", "Neuro OPD Timings 15s", 1, d(-12), d(48), 220000),
    (6, 4, "VinFast VF6 Tease", "Panorama Reveal 15s", 1, d(3), d(63), 260000),
    (6, 5, "Evos Alchemy", "Tallest Tower 15s", 2, d(-25), d(35), 260000),
    (4, 6, "Ori Plast Monsoon", "Leak-Proof Pipes 15s", 1, d(-2), d(28), 170000),
    (7, 2, "UB Gold Loan", "Per-Gram Rate 15s", 1, d(-40), d(-3), 160000),
    (2, 1, "Max Kalinga Arrivals", "Emergency No. 15s", 1, d(-18), d(42), 150000),
    (5, 7, "Indriya Sriya Glow", "Solitaire Focus 15s", 1, d(6), d(66), 180000),
]


def seed_inventory() -> bool:
    """Load Ajanta's real screens (with photos) + client directory if empty.
    No demo bookings — used automatically by the .exe on first run."""
    init_db()
    with get_conn() as conn:
        cur = conn.cursor()
        if cur.execute("SELECT COUNT(*) FROM screens").fetchone()[0]:
            return False
        cur.executemany(
            "INSERT INTO screens (name, location, width_ft, height_ft, res_w, res_h,"
            " loop_slots, slot_seconds, spots_per_day, rate_month, photo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            SCREENS,
        )
        cur.executemany(
            "INSERT INTO clients (company, contact_person, phone, industry) VALUES (?,?,?,?)",
            CLIENTS,
        )
        conn.commit()
    return True


def seed() -> None:
    if not seed_inventory():
        print("Database already seeded — skipping.")
        return
    with get_conn() as conn:
        cur = conn.cursor()
        for screen_id, client_id, name, creative, pos, start, end, rate in SLOTS:
            cur.execute(
                "INSERT INTO campaigns (client_id, name, creative) VALUES (?,?,?)",
                (client_id, name, creative),
            )
            cur.execute(
                "INSERT INTO slots (screen_id, campaign_id, position_no, start_date,"
                " end_date, rate_month, booked_by) VALUES (?,?,?,?,?,?,?)",
                (screen_id, cur.lastrowid, pos, start, end, rate,
                 "Krutarth" if screen_id % 2 else "Satish"),
            )
        conn.commit()
    print(f"Seeded {len(SCREENS)} screens, {len(CLIENTS)} clients, {len(SLOTS)} bookings.")


if __name__ == "__main__":
    seed()
