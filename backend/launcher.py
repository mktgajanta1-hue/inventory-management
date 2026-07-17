"""
MediaTrack launcher — double-click entry point for the packaged .exe.

Starts the server, opens the dashboard in the default browser, and keeps
running until the window is closed. The database (mediatrack.db) is stored
next to the .exe so data survives updates.
"""

import threading
import time
import webbrowser

import uvicorn

from app import app
from database import init_db
from seed import seed_inventory


def open_browser() -> None:
    time.sleep(1.5)
    webbrowser.open("http://localhost:8000")


def ensure_seed_photos() -> None:
    """First run of the .exe: copy bundled seed photos to the writable photos folder."""
    import shutil
    import sys
    if not getattr(sys, "frozen", False):
        return
    bundled = Path(sys._MEIPASS) / "frontend" / "photos"
    target = Path(sys.executable).parent / "photos"
    target.mkdir(exist_ok=True)
    if bundled.exists():
        for f in bundled.iterdir():
            if not (target / f.name).exists():
                shutil.copy(f, target / f.name)


if __name__ == "__main__":
    from pathlib import Path
    ensure_seed_photos()
    init_db()
    if seed_inventory():
        print("  First run: loaded Ajanta's 9 screens with photos.")
    threading.Thread(target=open_browser, daemon=True).start()
    print("=" * 52)
    print("  MediaTrack is running.")
    print("  Dashboard:  http://localhost:8000")
    print("  Team link:  http://<this-pc-ip>:8000  (same WiFi)")
    print("  Close this window to stop MediaTrack.")
    print("=" * 52)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
