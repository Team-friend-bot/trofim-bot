import os
import sqlite3
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    KYIV_TZ = timezone(timedelta(hours=3))  # EEST fallback

# Every timestamp in the DB is naive Kyiv local time in this one format.
# Two formats used to coexist: deadlines went in via isoformat() ("...T18:00:00")
# while done_at/created_at came from SQLite datetime('now') ("... 18:00:00").
# Since " " < "T" in ASCII, string comparison put any done_at before any deadline
# on the same date, so /stats counted every task as on-time and never overdue.
SQL_TS_FMT = "%Y-%m-%d %H:%M:%S"


def now_kyiv() -> datetime:
    return datetime.now(KYIV_TZ).replace(tzinfo=None)


def today_kyiv() -> date:
    return now_kyiv().date()


def now_kyiv_str() -> str:
    return now_kyiv().strftime(SQL_TS_FMT)


def norm_ts(value):
    """Coerce any timestamp into the canonical DB format. Unparseable input is
    passed through untouched rather than silently turned into a wrong date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return text
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return text
    if dt.tzinfo is not None:
        dt = dt.astimezone(KYIV_TZ).replace(tzinfo=None)
    return dt.strftime(SQL_TS_FMT)


def utc_to_kyiv(value):
    """For rows written by SQLite datetime('now'), which is UTC."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV_TZ).replace(tzinfo=None).strftime(SQL_TS_FMT)


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.environ.get("DATABASE_PATH", "tasks.db")
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    task_text TEXT NOT NULL,
                    assignee TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    created_by TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    is_done INTEGER DEFAULT 0,
                    done_at TEXT,
                    reminded_1d INTEGER DEFAULT 0,
                    reminded_2h INTEGER DEFAULT 0,
                    reminded_15m INTEGER DEFAULT 0,
                    reminded_overdue INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    username TEXT,
                    user_id INTEGER,
                    is_manager INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, name)
                )
            """)
            # Migration: add is_manager to existing DB if column missing
            try:
                conn.execute("ALTER TABLE members ADD COLUMN is_manager INTEGER DEFAULT 0")
            except Exception:
                pass
            # Migration: `started` = user pressed Start in a private chat (DM reachable).
            # Distinct from user_id, which is also filled by passive group tracking.
            try:
                conn.execute("ALTER TABLE members ADD COLUMN started INTEGER DEFAULT 0")
            except Exception:
                pass
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self._migrate_timestamps(conn)

    def _migrate_timestamps(self, conn):
        """One-off: bring existing rows onto the canonical format (see SQL_TS_FMT).

        deadline only needs its separator normalised — it was already Kyiv time.
        created_at/done_at were written by SQLite datetime('now'), i.e. UTC, so
        they get shifted. Guarded by a meta flag: shifting twice would be worse
        than the original bug.
        """
        if conn.execute("SELECT 1 FROM meta WHERE key = 'ts_normalized'").fetchone():
            return
        rows = conn.execute(
            "SELECT id, deadline, created_at, done_at FROM tasks"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET deadline = ?, created_at = ?, done_at = ? WHERE id = ?",
                (
                    norm_ts(row["deadline"]),
                    utc_to_kyiv(row["created_at"]),
                    utc_to_kyiv(row["done_at"]),
                    row["id"],
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('ts_normalized', ?)",
            (now_kyiv_str(),),
        )

    def add_task(self, chat_id, task_text, assignee, deadline, created_by):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, task_text, assignee, norm_ts(deadline), created_by, now_kyiv_str())
            )
            return cursor.lastrowid

    def get_active_tasks(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND is_done = 0 ORDER BY deadline",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_tasks_for_reminder(self):
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM tasks WHERE is_done = 0").fetchall()
            return [dict(row) for row in rows]

    def get_task(self, task_id):
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_member_by_username(self, chat_id, username):
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND LOWER(username) = LOWER(?)",
                (chat_id, username.lstrip("@"))
            ).fetchone()
            return dict(row) if row else None

    def get_member_by_user_id(self, chat_id, user_id):
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id)
            ).fetchone()
            return dict(row) if row else None

    def mark_done(self, task_id):
        # Kyiv time, not SQLite's datetime('now') — that is UTC and would not be
        # comparable with deadline.
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET is_done = 1, done_at = ? WHERE id = ?",
                (now_kyiv_str(), task_id)
            )

    def update_deadline(self, task_id, new_deadline):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET deadline = ?, reminded_1d = 0, reminded_2h = 0, "
                "reminded_15m = 0, reminded_overdue = 0 WHERE id = ?",
                (norm_ts(new_deadline), task_id)
            )

    def mark_reminded(self, task_id, field):
        with self._get_conn() as conn:
            conn.execute(f"UPDATE tasks SET {field} = 1 WHERE id = ?", (task_id,))

    def get_stats(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT
                    assignee,
                    COUNT(*) as total,
                    SUM(CASE WHEN is_done = 1 THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN is_done = 1 AND done_at <= deadline THEN 1 ELSE 0 END) as on_time,
                    SUM(CASE WHEN is_done = 1 AND done_at > deadline THEN 1 ELSE 0 END) as late,
                    SUM(CASE WHEN is_done = 0 AND deadline < ? THEN 1 ELSE 0 END) as overdue
                FROM tasks WHERE chat_id = ? GROUP BY assignee
            """, (now_kyiv_str(), chat_id)).fetchall()
            return [dict(row) for row in rows]

    def add_member(self, chat_id, name, username, user_id=None):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO members (chat_id, name, username, user_id) VALUES (?, ?, ?, ?)",
                (chat_id, name, username, user_id)
            )

    def get_member(self, chat_id, name):
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND LOWER(name) = LOWER(?)",
                (chat_id, name)
            ).fetchone()
            return dict(row) if row else None

    def get_team(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? ORDER BY name",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def remove_member(self, chat_id, name):
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM members WHERE chat_id = ? AND LOWER(name) = LOWER(?)",
                (chat_id, name)
            )

    def set_manager(self, chat_id, name, is_manager: bool):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE members SET is_manager = ? WHERE chat_id = ? AND LOWER(name) = LOWER(?)",
                (1 if is_manager else 0, chat_id, name)
            )

    def upsert_seen_member(self, chat_id, username, user_id, first_name):
        """Passively register anyone who talks in the group.
        Matches existing rows by username (updates their user_id without touching
        name/is_manager); otherwise adds a new member keyed by first_name.
        Skips users without a username (can't be tagged or DMed reliably)."""
        if not username:
            return
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT name FROM members WHERE chat_id = ? AND LOWER(username) = LOWER(?)",
                (chat_id, username)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE members SET user_id = ? WHERE chat_id = ? AND LOWER(username) = LOWER(?)",
                    (user_id, chat_id, username)
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO members (chat_id, name, username, user_id, is_manager) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (chat_id, first_name or username, username, user_id)
                )

    def update_user_id_by_username(self, username, user_id):
        """Called on private /start — marks the member as DM-reachable (started=1)."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE members SET user_id = ?, started = 1 WHERE LOWER(username) = LOWER(?)",
                (user_id, username)
            )
            return cursor.rowcount

    def get_unconnected(self, chat_id):
        """Members who haven't pressed Start privately yet (no DM possible)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND COALESCE(started, 0) = 0 ORDER BY name",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_chat_ids(self):
        with self._get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT chat_id FROM members").fetchall()
            return [r["chat_id"] for r in rows]
