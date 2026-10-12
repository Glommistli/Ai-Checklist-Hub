"""Native Windows launcher for AI Checklist Hub.

The desktop shell hosts the existing production React application in WebView2
and runs FastAPI on a private, dynamically selected localhost port.
"""

from __future__ import annotations

import ctypes
import os
import socket
import sqlite3
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path


APP_NAME = "AI Checklist Hub"


def bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[1]


def persistent_data_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    target = root / APP_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def legacy_database_candidates() -> list[Path]:
    candidates = [Path.cwd() / "backend" / "data" / "checklist.db"]
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if len(executable.parents) >= 3:
            candidates.append(executable.parents[2] / "backend" / "data" / "checklist.db")
    else:
        candidates.append(bundle_root() / "backend" / "data" / "checklist.db")
    return candidates


def migrate_legacy_database(target: Path) -> None:
    """One-time SQLite-safe copy from the original project-local database."""
    if target.exists():
        return
    for source in legacy_database_candidates():
        if source.is_file() and source.resolve() != target.resolve():
            with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
                source_db.backup(target_db)
            return


def backup_before_upgrade(database: Path, data_dir: Path) -> None:
    """Keep a native SQLite backup before importing a new backend/schema."""
    marker = data_dir / "backups" / "before-2.2.0.db"
    if database.exists() and not marker.exists():
        marker.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as source, sqlite3.connect(marker) as target:
            source.backup(target)
        os.environ["AI_CHECKLIST_UPGRADE_BACKUP"] = str(marker)
    elif marker.exists():
        os.environ["AI_CHECKLIST_UPGRADE_BACKUP"] = str(marker)


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until_ready(url: str, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # service is still starting
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError("The local application service did not start in time") from last_error


def show_error(message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)


def main() -> int:
    try:
        data_dir = persistent_data_dir()
        database = data_dir / "checklist.db"
        migrate_legacy_database(database)
        backup_before_upgrade(database, data_dir)
        os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
        os.environ["AI_CHECKLIST_DATA_DIR"] = str(data_dir)
        os.environ["AI_CHECKLIST_DESKTOP"] = "1"

        root = bundle_root()
        backend = root / "backend"
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))

        import uvicorn
        import webview
        from app.main import app

        port = reserve_port()
        url = f"http://127.0.0.1:{port}?desktop=1"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        thread = threading.Thread(target=server.run, name="checklist-api", daemon=True)
        thread.start()
        wait_until_ready(url)

        window = webview.create_window(
            APP_NAME,
            url,
            width=1240,
            height=820,
            min_size=(820, 600),
            frameless=False,
            background_color="#181817",
            text_select=True,
        )

        def use_dark_native_titlebar() -> None:
            native = getattr(window, "native", None)
            handle = getattr(native, "Handle", None)
            if handle is None:
                return
            hwnd = handle.ToInt32()
            enabled = ctypes.c_int(1)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (current / legacy)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )

        window.events.shown += use_dark_native_titlebar
        try:
            webview.start(gui="edgechromium", debug=False, private_mode=False)
        finally:
            server.should_exit = True
            thread.join(timeout=5)
        return 0
    except Exception as exc:
        try:
            log_file = persistent_data_dir() / "desktop-error.log"
            log_file.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        show_error(f"AI Checklist Hub could not start.\n\n{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
