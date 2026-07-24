import os
import sqlite3
import threading

DB_PATH = os.environ.get("DB_PATH", "watchlist.db")
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(conn, table, column, coltype):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


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
        _add_column_if_missing(conn, "watches", "paused", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "status", "full_name", "TEXT")
        _add_column_if_missing(conn, "status", "follower_count", "INTEGER")
        _add_column_if_missing(conn, "status", "is_private", "INTEGER")
        _add_column_if_missing(conn, "status", "profile_pic_url", "TEXT")
        _add_column_if_missing(conn, "status", "down_since", "TEXT")


def add_watch(chat_id, username):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watches (chat_id, username, paused) VALUES (?, ?, 0)",
            (chat_id, username),
        )


def remove_watch(chat_id, username):
    with _lock, _conn() as conn:
        cur = conn.execute(
            "DELETE FROM watches WHERE chat_id=? AND username=?", (chat_id, username)
        )
        return cur.rowcount > 0


def set_paused(chat_id, username, paused):
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE watches SET paused=? WHERE chat_id=? AND username=?",
            (1 if paused else 0, chat_id, username),
        )
        return cur.rowcount > 0


def list_watches(chat_id):
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT username, paused FROM watches WHERE chat_id=? ORDER BY username", (chat_id,)
        ).fetchall()
        return [{"username": r["username"], "paused": bool(r["paused"])} for r in rows]


def all_watched_usernames():
    with _lock, _conn() as conn:
        rows = conn.execute("SELECT DISTINCT username FROM watches").fetchall()
        return [r["username"] for r in rows]


def chats_watching(username, only_unpaused=False):
    with _lock, _conn() as conn:
        query = "SELECT chat_id FROM watches WHERE username=?"
        if only_unpaused:
            query += " AND paused=0"
        rows = conn.execute(query, (username,)).fetchall()
        return [r["chat_id"] for r in rows]


def get_state(username):
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM status WHERE username=?", (username,)
        ).fetchone()
        if row is None:
            return {
                "confirmed_status": None,
                "pending_status": None,
                "pending_count": 0,
                "full_name": None,
                "follower_count": None,
                "is_private": None,
                "profile_pic_url": None,
                "down_since": None,
            }
        return dict(row)


def set_confirmed(username, status, profile=None):
    profile = profile or {}
    with _lock, _conn() as conn:
        now_str = conn.execute("SELECT datetime('now')").fetchone()[0]
        prev = conn.execute(
            "SELECT confirmed_status, down_since FROM status WHERE username=?", (username,)
        ).fetchone()
        prev_status = prev["confirmed_status"] if prev else None
        prev_down_since = prev["down_since"] if prev else None

        if status == "not_found" and prev_status != "not_found":
            down_since = now_str
        elif status == "live":
            down_since = None
        else:
            down_since = prev_down_since

        conn.execute(
            """INSERT INTO status
                 (username, confirmed_status, pending_status, pending_count, updated_at,
                  full_name, follower_count, is_private, profile_pic_url, down_since)
               VALUES (?, ?, NULL, 0, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 confirmed_status=excluded.confirmed_status,
                 pending_status=NULL,
                 pending_count=0,
                 updated_at=excluded.updated_at,
                 full_name=excluded.full_name,
                 follower_count=excluded.follower_count,
                 is_private=excluded.is_private,
                 profile_pic_url=excluded.profile_pic_url,
                 down_since=excluded.down_since""",
            (
                username, status, now_str,
                profile.get("full_name"), profile.get("follower_count"),
                profile.get("is_private"), profile.get("profile_pic_url"),
                down_since,
            ),
        )
        return prev_status


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
