import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    KYIV_TZ = timezone(timedelta(hours=3))  # EEST fallback


def _utc_str_to_kyiv_iso(value: str) -> str:
    """'2026-08-26 11:00:00' (UTC, sqlite datetime('now')) -> '2026-08-26T14:00:00' (Kyiv)."""
    dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV_TZ).replace(tzinfo=None).isoformat()


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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recurring (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    assignee TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    period TEXT NOT NULL,
                    at_time TEXT NOT NULL,
                    created_by TEXT,
                    is_active INTEGER DEFAULT 1,
                    last_created_date TEXT
                )
            """)
            # Migration: cancelled tasks stay in the table (audit trail) but are
            # excluded from reminders, /tasks and /stats.
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN is_cancelled INTEGER DEFAULT 0")
            except Exception:
                pass
            # Migration: proof of completion — the Telegram file_id of the photo or
            # document the assignee sent when closing (invoice scan, photo of goods).
            for column in ("proof_file_id TEXT", "proof_kind TEXT"):
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
                except Exception:
                    pass
            self._normalize_timestamps(conn)

    @staticmethod
    def _normalize_timestamps(conn):
        """created_at/done_at used to be sqlite datetime('now') — UTC, space-separated.
        Deadlines are Kyiv-local ISO ('...T...'), so the two were never comparable:
        a task closed on time looked 3 hours late in /stats. Rewrite the old rows to
        Kyiv ISO once; rows already carrying 'T' are left alone, so this is idempotent."""
        for column in ("created_at", "done_at"):
            rows = conn.execute(
                f"SELECT id, {column} FROM tasks "
                f"WHERE {column} IS NOT NULL AND {column} LIKE '____-__-__ __:__:__%'"
            ).fetchall()
            for row in rows:
                try:
                    converted = _utc_str_to_kyiv_iso(row[column])
                except ValueError:
                    continue
                conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (converted, row["id"]))

    def add_task(self, chat_id, task_text, assignee, deadline, created_by, created_at):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, task_text, assignee, deadline, created_by, created_at)
            )
            return cursor.lastrowid

    def get_active_tasks(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND is_done = 0 AND COALESCE(is_cancelled, 0) = 0 "
                "ORDER BY deadline",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_tasks_for_reminder(self):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE is_done = 0 AND COALESCE(is_cancelled, 0) = 0"
            ).fetchall()
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

    def mark_done(self, task_id, done_at):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET is_done = 1, done_at = ? WHERE id = ?",
                (done_at, task_id)
            )

    def cancel_task(self, task_id, cancelled_at):
        """Close a task without counting it as done — mistakes from voice recognition
        shouldn't spam reminders or drag the assignee's stats down."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET is_done = 1, is_cancelled = 1, done_at = ? WHERE id = ?",
                (cancelled_at, task_id)
            )

    def update_deadline(self, task_id, new_deadline):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET deadline = ?, reminded_1d = 0, reminded_2h = 0, "
                "reminded_15m = 0, reminded_overdue = 0 WHERE id = ?",
                (new_deadline, task_id)
            )

    REMINDER_FLAGS = ("reminded_1d", "reminded_2h", "reminded_15m", "reminded_overdue")

    def mark_reminded(self, task_id, *fields):
        """Several flags at once: sending a late-stage reminder also retires the
        earlier ones, so downtime can't make them all fire in a burst."""
        unknown = set(fields) - set(self.REMINDER_FLAGS)
        if unknown:
            raise ValueError(f"unknown reminder flags: {sorted(unknown)}")
        assignments = ", ".join(f"{f} = 1" for f in fields)
        with self._get_conn() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (task_id,))

    def get_stats(self, chat_id, now_iso, since_iso=None):
        """since_iso limits the report to tasks created after that moment
        (all-time stats stop being actionable once a team has months of history)."""
        period = "AND created_at >= :since" if since_iso else ""
        with self._get_conn() as conn:
            rows = conn.execute(f"""
                SELECT
                    assignee,
                    COUNT(*) as total,
                    SUM(CASE WHEN is_done = 1 THEN 1 ELSE 0 END) as done,
                    SUM(CASE WHEN is_done = 1 AND done_at <= deadline THEN 1 ELSE 0 END) as on_time,
                    SUM(CASE WHEN is_done = 1 AND done_at > deadline THEN 1 ELSE 0 END) as late,
                    SUM(CASE WHEN is_done = 0 AND deadline < :now THEN 1 ELSE 0 END) as overdue
                FROM tasks
                WHERE chat_id = :chat_id AND COALESCE(is_cancelled, 0) = 0 {period}
                GROUP BY assignee
            """, {"now": now_iso, "chat_id": chat_id, "since": since_iso}).fetchall()
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

    def get_active_tasks_for_user(self, user_id):
        """Every open task assigned to this person, across all their groups.
        The assignee column holds '@username' or a bare name, so match on both."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT t.* FROM tasks t
                JOIN members m ON m.chat_id = t.chat_id
                WHERE m.user_id = ?
                  AND t.is_done = 0 AND COALESCE(t.is_cancelled, 0) = 0
                  AND (LOWER(t.assignee) = LOWER('@' || COALESCE(m.username, ''))
                       OR LOWER(t.assignee) = LOWER(m.name))
                ORDER BY t.deadline
            """, (user_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_digest_recipients(self):
        """Everyone reachable in DM (pressed Start), one row per person."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT user_id, MIN(name) AS name FROM members "
                "WHERE user_id IS NOT NULL AND COALESCE(started, 0) = 1 "
                "GROUP BY user_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def reassign_task(self, task_id, assignee):
        """Point a task at a different person and let the reminders fire again for them."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET assignee = ?, reminded_1d = 0, reminded_2h = 0, "
                "reminded_15m = 0, reminded_overdue = 0 WHERE id = ?",
                (assignee, task_id)
            )

    def get_all_tasks(self, chat_id):
        """Full history for the export, newest first, cancelled ones included
        (they're flagged in the file rather than hidden)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? ORDER BY id DESC", (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def update_task_text(self, task_id, task_text):
        with self._get_conn() as conn:
            conn.execute("UPDATE tasks SET task_text = ? WHERE id = ?", (task_text, task_id))

    # --- recurring task templates -------------------------------------------------

    def add_recurring(self, chat_id, assignee, task_text, period, at_time, created_by):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO recurring (chat_id, assignee, task_text, period, at_time, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, assignee, task_text, period, at_time, created_by)
            )
            return cursor.lastrowid

    def get_recurring(self, chat_id):
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recurring WHERE chat_id = ? AND is_active = 1 ORDER BY id",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_all_recurring(self):
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM recurring WHERE is_active = 1 ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def deactivate_recurring(self, template_id, chat_id):
        """Scoped to the chat so one group can't switch off another group's template."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "UPDATE recurring SET is_active = 0 WHERE id = ? AND chat_id = ? AND is_active = 1",
                (template_id, chat_id)
            )
            return cursor.rowcount

    def mark_recurring_created(self, template_id, date_iso):
        """Guards against a second task on the same day after a restart."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE recurring SET last_created_date = ? WHERE id = ?", (date_iso, template_id)
            )

    def attach_proof(self, task_id, file_id, kind):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET proof_file_id = ?, proof_kind = ? WHERE id = ?",
                (file_id, kind, task_id)
            )

    def reopen_task(self, task_id):
        """Undo a close: back to open, reminders armed again from scratch."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET is_done = 0, is_cancelled = 0, done_at = NULL, "
                "reminded_1d = 0, reminded_2h = 0, reminded_15m = 0, reminded_overdue = 0 "
                "WHERE id = ?",
                (task_id,)
            )
