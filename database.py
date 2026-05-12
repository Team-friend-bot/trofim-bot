import sqlite3
from typing import List, Dict


class Database:
    def __init__(self, db_path: str = "tasks.db"):
        self.db_path = db_path
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
            for col, col_type in [
                ("done_at", "TEXT"),
                ("reminded_1d", "INTEGER DEFAULT 0"),
                ("reminded_2h", "INTEGER DEFAULT 0"),
                ("reminded_15m", "INTEGER DEFAULT 0"),
                ("reminded_overdue", "INTEGER DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass

    def add_task(self, chat_id, task_text, assignee, deadline, created_by):
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by) VALUES (?, ?, ?, ?, ?)",
                (chat_id, task_text, assignee, deadline, created_by)
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

    def mark_done(self, task_id):
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET is_done = 1, done_at = datetime('now') WHERE id = ?",
                (task_id,)
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
                    SUM(CASE WHEN is_done = 0 AND deadline < datetime('now') THEN 1 ELSE 0 END) as overdue
                FROM tasks
                WHERE chat_id = ?
                GROUP BY assignee
            """, (chat_id,)).fetchall()
            return [dict(row) for row in rows]
