#!/usr/bin/env python3
"""Проверка нормализации меток времени и миграции старой базы.

Тестов в проекте нет, а правка трогает боевые данные, поэтому она едет
со своей проверкой: `python3 scripts/test_timestamps.py`.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (  # noqa: E402
    Database, KYIV_TZ, SQL_TS_FMT, norm_ts, now_kyiv, utc_to_kyiv,
)

failures = []


def check(name, got, expected):
    if got == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       получили: {got!r}\n       ожидали:  {expected!r}")
        failures.append(name)


def make_legacy_db(path):
    """База в том виде, в каком она существует на VPS до этой правки."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, task_text TEXT NOT NULL,
            assignee TEXT NOT NULL, deadline TEXT NOT NULL, created_by TEXT,
            created_at TEXT DEFAULT (datetime('now')), is_done INTEGER DEFAULT 0,
            done_at TEXT, reminded_1d INTEGER DEFAULT 0, reminded_2h INTEGER DEFAULT 0,
            reminded_15m INTEGER DEFAULT 0, reminded_overdue INTEGER DEFAULT 0
        )""")
    conn.execute("""
        CREATE TABLE members (
            chat_id INTEGER NOT NULL, name TEXT NOT NULL, username TEXT,
            user_id INTEGER, is_manager INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, name)
        )""")
    # Дедлайн 09:00, закрыто в 23:59 того же дня — то есть с опозданием на 15 часов.
    # Старый формат: дедлайн через isoformat() с "T", done_at из datetime('now') в UTC.
    conn.execute(
        "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by, "
        "created_at, is_done, done_at) VALUES (?,?,?,?,?,?,?,?)",
        (1, "поздняя", "@vika", "2026-06-10T09:00:00", "boss",
         "2026-06-09 07:00:00", 1, "2026-06-10 20:59:00"),  # 20:59 UTC = 23:59 Киев
    )
    # Закрыта вовремя: дедлайн 18:00, закрыта в 12:00 по Киеву (09:00 UTC)
    conn.execute(
        "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by, "
        "created_at, is_done, done_at) VALUES (?,?,?,?,?,?,?,?)",
        (1, "вовремя", "@vika", "2026-06-10T18:00:00", "boss",
         "2026-06-09 07:00:00", 1, "2026-06-10 09:00:00"),
    )
    # Открыта, дедлайн в далёком прошлом — обязана считаться просроченной
    conn.execute(
        "INSERT INTO tasks (chat_id, task_text, assignee, deadline, created_by, "
        "created_at, is_done) VALUES (?,?,?,?,?,?,?)",
        (1, "висит", "@oleh", "2020-01-01T10:00:00", "boss", "2019-12-31 07:00:00", 0),
    )
    conn.execute("INSERT INTO members (chat_id, name, username) VALUES (1,'Vika','vika')")
    conn.commit()
    conn.close()


print("norm_ts приводит оба формата к одному:")
check("isoformat с T", norm_ts("2026-06-10T09:00:00"), "2026-06-10 09:00:00")
check("уже с пробелом", norm_ts("2026-06-10 09:00:00"), "2026-06-10 09:00:00")
check("объект datetime", norm_ts(datetime(2026, 6, 10, 9, 0)), "2026-06-10 09:00:00")
check("с зоной → Киев", norm_ts("2026-06-10T09:00:00+00:00"), "2026-06-10 12:00:00")
check("мусор не портится", norm_ts("завтра"), "завтра")
check("None остаётся None", norm_ts(None), None)

print("\nutc_to_kyiv сдвигает то, что писал SQLite:")
check("летом +3", utc_to_kyiv("2026-06-10 20:59:00"), "2026-06-10 23:59:00")
check("зимой +2", utc_to_kyiv("2026-01-10 20:59:00"), "2026-01-10 22:59:00")
check("пусто не трогаем", utc_to_kyiv(None), None)

print("\nСравнение строк после нормализации (корень бага):")
late_done, late_deadline = norm_ts("2026-06-10 23:59:00"), norm_ts("2026-06-10T09:00:00")
check("опоздание больше не «вовремя»", late_done <= late_deadline, False)

print("\nМиграция боевой базы:")
tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "tasks.db")
make_legacy_db(db_path)
db = Database(db_path)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
tasks = {r["task_text"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
check("дедлайн нормализован", tasks["поздняя"]["deadline"], "2026-06-10 09:00:00")
check("done_at сдвинут в Киев", tasks["поздняя"]["done_at"], "2026-06-10 23:59:00")
check("created_at сдвинут", tasks["поздняя"]["created_at"], "2026-06-09 10:00:00")

stats = {s["assignee"]: s for s in db.get_stats(1)}
check("опоздавшая учтена как late", stats["@vika"]["late"], 1)
check("своевременная учтена как on_time", stats["@vika"]["on_time"], 1)
check("зависшая учтена как overdue", stats["@oleh"]["overdue"], 1)

print("\nПовторный запуск не сдвигает второй раз:")
Database(db_path)
Database(db_path)
tasks2 = {r["task_text"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
check("done_at стабилен", tasks2["поздняя"]["done_at"], "2026-06-10 23:59:00")
check("created_at стабилен", tasks2["поздняя"]["created_at"], "2026-06-09 10:00:00")

print("\nНовые записи пишутся сразу в киевском времени:")
before = now_kyiv()
new_id = db.add_task(1, "свежая", "@vika", datetime(2030, 1, 1, 10, 0).isoformat(), "boss")
db.mark_done(new_id)
row = db.get_task(new_id)
check("дедлайн без T", row["deadline"], "2030-01-01 10:00:00")
done_at = datetime.strptime(row["done_at"], SQL_TS_FMT)
check("done_at в киевском времени", abs((done_at - before).total_seconds()) < 60, True)
utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
offset_h = round((done_at - utc_now).total_seconds() / 3600)
expected_offset = round(datetime.now(KYIV_TZ).utcoffset().total_seconds() / 3600)
check("сдвиг относительно UTC равен киевскому", offset_h, expected_offset)

print("\nСнуз тоже нормализуется:")
db.update_deadline(new_id, (datetime(2030, 1, 1, 10, 0) + timedelta(hours=3)).isoformat())
check("после снуза формат прежний", db.get_task(new_id)["deadline"], "2030-01-01 13:00:00")

conn.close()
print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
    sys.exit(1)
print("Все проверки пройдены.")
