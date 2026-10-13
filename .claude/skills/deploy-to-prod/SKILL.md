---
name: deploy-to-prod
description: Деплой змін на продакшн-сервер борда TROFIM (manager_tracker, call_qc). Використовувати при будь-якій правці коду на проді, рестарті сервісів, роботі з бойовою БД.
---

# Деплой на прод TROFIM

## Критичне — перевірити ПЕРЕД будь-чим
1. **Справжній прод = 167.71.1.243.** Спершу `dig +short dashboard.trofim.com.ua` і звірити.
   `167.172.40.137` = двійник з інциденту 05.07 — туди НЕ деплоїти ніколи.
2. **Сервер живе в UTC.** Київ = UTC+3 влітку: 09:00 Київ = `hour=6` у CronTrigger.
3. **Прод ≠ локал:** різні гілки, код редагується прямо на сервері. Не «синхронізувати» з локальною копією наосліп.
4. Бойова БД = `/opt/manager_tracker/manager_tracker.db` (SQLite, **WAL** з 10.07; 1 uvicorn worker — так і треба: apscheduler всередині процесу, 2 workers = подвійні розсилки).
5. **З 10.07 є STAGING** — зміни спершу туди: `/opt/manager_tracker_staging`, порт 127.0.0.1:8001, systemd `manager_tracker_staging`, DISABLE_SCHEDULER=1, бот-токени заглушені, своя копія БД. Процес: правка на staging → тест → `git commit` → на проді `git pull /opt/manager_tracker_staging main` → restart. Освіжити staging-БД: sqlite3 `.backup` з прода.

## Шляхи і сервіси
| Що | Де | Venv | Systemd |
|---|---|---|---|
| Борд (FastAPI) | /opt/manager_tracker | /opt/venv | manager_tracker (uvicorn main:app :8000) |
| QC дзвінків | /opt/call_qc | /opt/call_qc/venv | call_qc (CPUQuota=40% drop-in!) |
| Вартовий | /opt/board_watchdog/watchdog.sh | — | cron кожні 5 хв |

Планувальник джобів = `scheduler/jobs.py` (apscheduler всередині uvicorn-процесу; зміна сервісу → потрібен рестарт).

## Порядок деплою
1. Бекап: `cp file.py file.py.bak_<тег>_$(date +%Y%m%d)` — завжди, до правки.
2. Правити НА STAGING (див. вище), не на проді напряму; після правки:
   `cd /opt/manager_tracker && /opt/venv/bin/python -c 'import <module>'` — перевірка імпорту.
3. **Рестарт — ЛИШЕ `/opt/scripts/restart_board.sh "причина"`** (з 09.09, #409). НЕ `systemctl restart` напряму:
   08.09 борд рестартили 70 разів за день, кожен = ~10 с 502 + 15 хв розігріву → колеги бачили «повільно».
   У робочий час (пн-сб 08:00–18:30 Київ) скрипт коалесує рестарти (раз на 20 хв, черга кроном);
   терміновий хотфікс — `--now`. Роби ВСІ патчі задачі, потім ОДИН рестарт. Лог: `/var/log/board_restarts.log`.
4. `curl -so /dev/null -w '%{time_starttransfer}s %{http_code}' http://127.0.0.1:8000/` — TTFB має бути < 0.5s.
5. Вартовий сам алертне власнику в @TOPhanterBot, якщо щось впало — але не покладатися, перевіряти самому.

## Пастки
- Вебхук @TOPhanter МУСИТЬ мати `channel_post` в allowed_updates — при переставленні вебхука зберігати.
- Залізо з 10.07: **2 vCPU / 4 GB** (swap має бути 0 — якщо >0, щось тече). Важкі фонові — все одно з CPUQuota.
- Кеш залишків 1С (_stock_1c_cache) — in-memory: після рестарту зробити `POST /api/procurement/stock/sync`.
- З 09.09 (#409) стартові джоби рознесені: DB-джоби (client_turnover, abandoned_track, client_categories, pinned_clients…) після рестарту НЕ повторюються, якщо бігали менш ніж інтервал тому (мітки `cache/job_last_run.json`); прогрівачі памʼяті стартують за 0–5 хв. Uvicorn стопиться за ≤3 с (`--timeout-graceful-shutdown 3`).
- Вартовий (`watchdog.sh`) тепер алертить на >3 рестартів/год, p90 API >3 с (nginx `rt=/urt=`), >15 502/504 за 5 хв.
- Секрети: 1С-токен ротований 07.07 живе у 10 файлах прода; API-ключі на прод НЕ класти поза .env.
