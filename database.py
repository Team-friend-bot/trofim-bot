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
                    reminded_24h INTEGER DEFAULT 0,
                    reminded_0h INTEGER DEFAULT 0,
                    reminded_overdue INTEGER DEFAULT 0
                )
            """)

    def add_task(self, chat_id: int, task_text: str, assignee: str, deadline: str, created_by: str) -> int:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by) VALUES (?, ?, ?, ?, ?)",
                (chat_id, task_text, assignee, deadline, created_by)
            )
            return cursor.lastrowid

    def get_active_tasks(self, chat_id: int) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE chat_id = ? AND is_done = 0 ORDER BY deadline",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_tasks_for_reminder(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE is_done = 0"
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_done(self, task_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE tasks SET is_done = 1 WHERE id = ?", (task_id,))

    def mark_reminded_24h(self, task_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE tasks SET reminded_24h = 1 WHERE id = ?", (task_id,))

    def mark_reminded_0h(self, task_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE tasks SET reminded_0h = 1 WHERE id = ?", (task_id,))

    def mark_reminded_overdue(self, task_id: int):
        with self._get_conn() as conn:
            conn.execute("UPDATE tasks SET reminded_overdue = 1 WHERE id = ?", (task_id,))
