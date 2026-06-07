"""
SQLite-сховище для feedback-бота — єдина локальна база (feedback.db).

Замінює розрізнені JSON-файли. Дає атомарні транзакції, один файл, відсутність
пошкоджень. Google Sheets лишається ДЗЕРКАЛОМ (бот туди дублює, але джерело
правди — ця база).

Таблиці:
  profiles(key, data)  — профілі (data = JSON), key = telegram_id (рядком)
  msgstore(key, data)  — кеш повідомлень {ids,text,photo,photo_path}, key = feedback_id
  assign(key, data)    — доручення, key = feedback_id
  chat(key, data)      — трекінг повідомлень для очищення, key = chat_id (рядком)
  shiftreports(key, data) — підсумки змін персоналу (рефлексивний відгук), key = "telegram_id:YYYY-MM-DD"
  tasks(...)           — канонічний запис задачі (джерело правди для дайджесту/переліку)
  meta(k, v)           — службове

Модель доступу в bot.py: in-memory кеші (dict) завантажуються при старті й
пишуться наскрізь (write-through) сюди. PTB обробляє апдейти послідовно, тож
мутації кешів безпечні; запис у БД серіалізовано через RLock.
"""

import os
import json
import sqlite3
import threading
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "feedback.db")

_conn = None
_lock = threading.RLock()

_KV_KEYCOL = "key"   # уніфікований ключ для KV-таблиць

TASK_COLS = [
    "fid", "created_date", "created_time", "restaurant", "sender_name", "sender_role",
    "category", "summary", "responsible", "urgency", "has_photo", "status", "log",
    "seq", "updated_at",
    # додані поля (аудит): зворотний зв'язок зі звітником, вік/ескалація, дата закриття
    "reporter_id", "created_ts", "acknowledged_at", "completed_date", "escalated", "assignee_id",
]

# Очікувані колонки таблиці tasks (для авто-міграції ADD COLUMN на старій БД)
_TASK_TABLE_COLS = {
    "fid": "TEXT PRIMARY KEY", "created_date": "TEXT", "created_time": "TEXT",
    "restaurant": "TEXT", "sender_name": "TEXT", "sender_role": "TEXT",
    "category": "TEXT", "summary": "TEXT", "responsible": "TEXT", "urgency": "TEXT",
    "has_photo": "INTEGER DEFAULT 0", "status": "TEXT DEFAULT 'Нове'", "log": "TEXT DEFAULT ''",
    "seq": "INTEGER", "updated_at": "TEXT",
    "reporter_id": "INTEGER", "created_ts": "TEXT", "acknowledged_at": "TEXT",
    "completed_date": "TEXT", "escalated": "INTEGER DEFAULT 0", "assignee_id": "INTEGER",
}


def init():
    """Відкриває з'єднання й створює схему. Безпечно викликати повторно."""
    global _conn
    if _conn is not None:
        return
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        cols_sql = ",\n                ".join(f"{c} {t}" for c, t in _TASK_TABLE_COLS.items())
        _conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS profiles (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS msgstore (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assign   (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chat     (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shiftreports (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                {cols_sql}
            );
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            """
        )
        # Авто-міграція: додаємо відсутні колонки в існуючу таблицю tasks
        existing = {r["name"] for r in _conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for col, decl in _TASK_TABLE_COLS.items():
            if col not in existing:
                add_decl = decl.replace(" PRIMARY KEY", "")
                try:
                    _conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {add_decl}")
                    logger.info(f"Schema migration: added tasks.{col}")
                except Exception as e:
                    logger.error(f"ALTER tasks ADD {col} failed: {e}")
        _conn.commit()


# ─── ДОВГОВІЧНІСТЬ: checkpoint / backup / meta / close ───────────────────────

def checkpoint():
    """Зливає WAL у основний файл (щоб бекап-копія .db була повною, а WAL не ріс)."""
    try:
        with _lock:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.error(f"WAL checkpoint failed: {e}")


def backup_to(dest_path):
    """Створює узгоджену копію всієї БД (WAL+main) одним файлом через VACUUM INTO."""
    tmp = dest_path + ".tmp"
    with _lock:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        _conn.execute("VACUUM INTO ?", (tmp,))
    os.replace(tmp, dest_path)
    return dest_path


def integrity_ok(path):
    """Перевіряє цілісність файлу-бекапу (open read-only)."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            return ok and n >= 0
        finally:
            c.close()
    except Exception as e:
        logger.error(f"Backup integrity check failed {path}: {e}")
        return False


def meta_get(k, default=None):
    with _lock:
        row = _conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(k, v):
    with _lock:
        with _conn:
            _conn.execute(
                "INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)),
            )


def close():
    """Коректно зливає WAL і закриває з'єднання (виклик у finally при зупинці)."""
    global _conn
    if _conn is None:
        return
    try:
        with _lock:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.close()
    except Exception as e:
        logger.error(f"DB close failed: {e}")
    finally:
        _conn = None


# ─── KV-сховища (profiles / msgstore / assign / chat) ────────────────────────

def load_kv(table):
    """Повертає {key(str): value(dict)} для KV-таблиці."""
    with _lock:
        rows = _conn.execute(f"SELECT key, data FROM {table}").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["data"])
        except Exception as e:
            logger.error(f"KV decode error in {table}/{r['key']}: {e}")
    return out


def save_kv(table, store):
    """Атомарно перезаписує всю KV-таблицю зі словника {key: value(dict)}."""
    rows = [(str(k), json.dumps(v, ensure_ascii=False)) for k, v in store.items()]
    with _lock:
        with _conn:  # транзакція
            _conn.execute(f"DELETE FROM {table}")
            _conn.executemany(f"INSERT INTO {table}(key, data) VALUES (?, ?)", rows)


def kv_count(table):
    with _lock:
        return _conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def kv_put(table, key, value):
    """Upsert одного запису (ефективно для гарячих шляхів, напр. трекінгу)."""
    with _lock:
        with _conn:
            _conn.execute(
                f"INSERT INTO {table}(key, data) VALUES (?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                (str(key), json.dumps(value, ensure_ascii=False)),
            )


def kv_delete(table, key):
    with _lock:
        with _conn:
            _conn.execute(f"DELETE FROM {table} WHERE key = ?", (str(key),))


# ─── TASKS (канонічний запис задачі) ─────────────────────────────────────────

def _row_to_task(row):
    d = dict(row)
    d["has_photo"] = bool(d.get("has_photo"))
    return d


def load_tasks():
    """Повертає {fid: task_dict} (нативні поля)."""
    with _lock:
        rows = _conn.execute("SELECT * FROM tasks").fetchall()
    return {r["fid"]: _row_to_task(r) for r in rows}


def save_task(rec):
    """Upsert одного запису задачі. rec — dict із полями TASK_COLS (fid обов'язковий)."""
    rec = dict(rec)
    rec.setdefault("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    rec["has_photo"] = 1 if rec.get("has_photo") else 0
    vals = [rec.get(c) for c in TASK_COLS]
    placeholders = ", ".join(["?"] * len(TASK_COLS))
    cols = ", ".join(TASK_COLS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in TASK_COLS if c != "fid")
    with _lock:
        with _conn:
            _conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(fid) DO UPDATE SET {updates}",
                vals,
            )


def bulk_save_tasks(recs):
    """Масовий upsert (для міграції). recs — iterable dict."""
    for rec in recs:
        save_task(rec)


def delete_task(fid):
    with _lock:
        with _conn:
            _conn.execute("DELETE FROM tasks WHERE fid = ?", (fid,))


def tasks_count():
    with _lock:
        return _conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]


def next_seq():
    """Наступний порядковий номер задачі (для впорядкування дайджесту)."""
    with _lock:
        row = _conn.execute("SELECT MAX(seq) AS m FROM tasks").fetchone()
    return (row["m"] or 0) + 1


# ─── ОДНОРАЗОВА МІГРАЦІЯ KV-СТОРІВ ІЗ JSON ───────────────────────────────────

def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Migration read failed {path}: {e}")
        return default


def migrate_kv_if_empty(base_dir):
    """Якщо KV-таблиці порожні — імпортує дані зі старих JSON-файлів (бекап лишається)."""
    mapping = {
        "profiles": "user_profiles.json",
        "msgstore": "message_store.json",
        "assign":   "assign_store.json",
        "chat":     "chat_msgs.json",
    }
    for table, fname in mapping.items():
        try:
            if kv_count(table) > 0:
                continue
            data = _read_json(os.path.join(base_dir, fname), {})
            if isinstance(data, dict) and data:
                save_kv(table, data)
                logger.info(f"Migrated {len(data)} rows into {table} from {fname}")
        except Exception as e:
            logger.error(f"Migration error for {table}: {e}")
