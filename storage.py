import os
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "watchlist.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS watches (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                PRIMARY KEY (chat_id, username)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS status (
                username TEXT PRIMARY KEY,
                confirmed_status TEXT NOT NULL,
                pending_status TEXT,
                pending_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )"""
        )


def add_watch(chat_id, username):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watches (chat_id, username) VALUES (?, ?)",
            (chat_id, username),
        )


def remove_watch(chat_id, username):
    with _lock, _conn() as conn:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id=? AND username=?", (chat_id, username)
        )
        return cur.rowcount > 0


def list_watches(chat_id):
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT username FROM watches WHERE chat_id=? ORDER BY username", (chat_id,)
        ).fetchall()
        return [r["username"] for r in rows]


def all_watched_usernames():
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT DISTINCT username FROM watches").fetchall()
        return [r["username"] for r in rows]


def chats_watching(username):
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT chat_id FROM watches WHERE username=?", (username,)
        ).fetchall()
        return [r["chat_id"] for r in rows]


def get_state(username):
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM status WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            return {"confirmed_status": None, "pending_status": None, "pending_count": 0}
        return dict(row)


def set_confirmed(username, status):
    with _lock, _conn() as conn:
        conn.execute(
            """INSERT INTO status (username, confirmed_status, pending_status, pending_count, updated_at)
               VALUES (?, ?, NULL, 0, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET
                 confirmed_status=excluded.confirmed_status,
                 pending_status=NULL,
                 pending_count=0,
                 updated_at=excluded.updated_at""",
            (username, status),
        )


def bump_pending(username, status):
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT pending_status, pending_count FROM status WHERE username=?", (username,)
        ).fetchone()
        count = row["pending_count"] + 1 if row and row["pending_status"] == status else 1
        conn.execute(
            """INSERT INTO status (username, confirmed_status, pending_status, pending_count, updated_at)
               VALUES (?, 'unknown', ?, ?, datetime('now'))
               ON CONFLICT(username) DO UPDATE SET
                 pending_status=excluded.pending_status,
                 pending_count=excluded.pending_count,
                 updated_at=excluded.updated_at""",
            (username, status, count),
        )
        return status, count


def clear_pending(username):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE status SET pending_status=NULL, pending_count=0 WHERE username=?",
            (username,),
        )
