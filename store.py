"""Receipt ledger and per-repository settings in the plugin's own data directory."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path

PLUGIN_NAME = "receipts"
MAX_RECEIPTS_PER_REPO = 500
_LOCK = threading.Lock()

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS checks (
        root TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        set_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        root TEXT NOT NULL,
        command TEXT NOT NULL,
        tree TEXT NOT NULL,
        head_tree TEXT NOT NULL,
        overlay_json TEXT NOT NULL,
        status TEXT NOT NULL,
        exit_code INTEGER,
        duration_ms INTEGER,
        output_sha256 TEXT NOT NULL,
        output_chars INTEGER NOT NULL,
        session_id TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS receipts_root_tree ON receipts (root, tree, id)",
)
# Added after 0.1.x; older ledgers gain these in place when they can be written.
_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_ADDED_COLUMNS = (("model", "TEXT NOT NULL DEFAULT ''"), ("issuer", "TEXT NOT NULL DEFAULT ''"))
_RETRY_UPGRADE_S = 60.0


def field(row, name: str) -> str:
    """A column added after 0.1.x, or "" when this ledger could not be upgraded."""
    return (row[name] or "") if name in row.keys() else ""


def data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # Hermes >= 0.22
        return plugin_data_dir(PLUGIN_NAME)
    except Exception:
        home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        path = Path(home) / "plugin-data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "receipts.db")
        self._upgraded = False
        self._upgrade_after = 0.0

    def _connect(self, writing: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        for ddl in _SCHEMA:
            conn.execute(ddl)
        self._upgrade(conn, force=writing)
        return conn

    def _upgrade(self, conn: sqlite3.Connection, force: bool = False) -> None:
        """Add the 0.2 table and columns. Best effort: a ledger that cannot be written
        right now (read-only, locked by another process) keeps working as a 0.1 ledger.

        Reads try briefly and then leave it alone for a while, so the gate is never held
        up. A write (``force``) always tries, and waits as long as the write itself would.
        """
        if self._upgraded or (not force and time.monotonic() < self._upgrade_after):
            return
        try:
            have = {row["name"] for row in conn.execute("PRAGMA table_info(receipts)")}
            missing = [(n, ddl) for n, ddl in _ADDED_COLUMNS if n not in have]
            has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            if missing or not has_meta:
                if not force:
                    conn.execute("PRAGMA busy_timeout = 250")
                try:
                    with conn:
                        conn.execute(_META)
                        for name, ddl in missing:
                            conn.execute(f"ALTER TABLE receipts ADD COLUMN {name} {ddl}")
                finally:
                    conn.execute("PRAGMA busy_timeout = 5000")
            self._upgraded = True
        except sqlite3.Error:
            self._upgrade_after = time.monotonic() + _RETRY_UPGRADE_S

    # ── identity ────────────────────────────────────────────────────────────
    def issuer_id(self) -> str:
        """The id this ledger issues receipts under: minted once, then stable.
        Empty when the ledger could not be upgraded to hold one."""
        with _LOCK, closing(self._connect(writing=True)) as conn, conn:
            if not self._upgraded:
                return ""
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('issuer', ?)", (str(uuid.uuid4()),))
            return conn.execute("SELECT value FROM meta WHERE key = 'issuer'").fetchone()["value"]

    # ── settings ────────────────────────────────────────────────────────────
    def check_for(self, root: str) -> str | None:
        with _LOCK, closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT command FROM checks WHERE root = ?", (root,)).fetchone()
        return row["command"] if row else None

    def set_check(self, root: str, command: str | None) -> None:
        with _LOCK, closing(self._connect()) as conn, conn:
            if command:
                conn.execute("INSERT INTO checks (root, command, set_at) VALUES (?, ?, ?) "
                             "ON CONFLICT(root) DO UPDATE SET command = excluded.command, set_at = excluded.set_at",
                             (root, command, time.time()))
            else:
                conn.execute("DELETE FROM checks WHERE root = ?", (root,))

    def configured_roots(self) -> list[tuple[str, str]]:
        with _LOCK, closing(self._connect()) as conn, conn:
            return [(r["root"], r["command"]) for r in conn.execute("SELECT root, command FROM checks ORDER BY root")]

    # ── receipts ────────────────────────────────────────────────────────────
    def add(self, *, root: str, command: str, tree: str, head_tree: str, overlay: dict, status: str,
            exit_code: int | None, duration_ms: int | None, output_sha256: str, output_chars: int,
            session_id: str, model: str = "", issuer: str = "") -> int:
        columns = ["created_at", "root", "command", "tree", "head_tree", "overlay_json", "status", "exit_code",
                   "duration_ms", "output_sha256", "output_chars", "session_id"]
        values = [time.time(), root, command, tree, head_tree, json.dumps(overlay, sort_keys=True), status,
                  exit_code, duration_ms, output_sha256, output_chars, session_id or ""]
        with _LOCK, closing(self._connect(writing=True)) as conn, conn:
            if self._upgraded:                # a ledger still in the 0.1 layout keeps the receipt itself
                columns += ["model", "issuer"]
                values += [model or "", issuer or ""]
            cur = conn.execute(f"INSERT INTO receipts ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                               values)
            conn.execute("DELETE FROM receipts WHERE root = ? AND id NOT IN "
                         "(SELECT id FROM receipts WHERE root = ? ORDER BY id DESC LIMIT ?)",
                         (root, root, MAX_RECEIPTS_PER_REPO))
            return int(cur.lastrowid)

    def latest_for_tree(self, root: str, tree: str, command: str) -> sqlite3.Row | None:
        """Most recent conclusive receipt (passed or failed) for exactly this content and check."""
        with _LOCK, closing(self._connect()) as conn, conn:
            return conn.execute(
                "SELECT * FROM receipts WHERE root = ? AND tree = ? AND command = ? AND status IN ('passed','failed')"
                " ORDER BY id DESC LIMIT 1", (root, tree, command)).fetchone()

    def latest_passing(self, root: str, command: str) -> sqlite3.Row | None:
        with _LOCK, closing(self._connect()) as conn, conn:
            return conn.execute(
                "SELECT * FROM receipts WHERE root = ? AND command = ? AND status = 'passed' ORDER BY id DESC LIMIT 1",
                (root, command)).fetchone()

    def recent(self, root: str, limit: int = 10) -> list[sqlite3.Row]:
        with _LOCK, closing(self._connect()) as conn, conn:
            return list(conn.execute("SELECT * FROM receipts WHERE root = ? ORDER BY id DESC LIMIT ?", (root, limit)))

    def clear(self, root: str) -> int:
        with _LOCK, closing(self._connect()) as conn, conn:
            return conn.execute("DELETE FROM receipts WHERE root = ?", (root,)).rowcount
