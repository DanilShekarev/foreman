"""Ядро gd-assist: SQLite + бизнес-логика, без FastAPI.

Дисциплина из проекта kanban: WAL, соединение на запрос, любая запись — внутри
write_tx() (BEGIN IMMEDIATE), init_db() вызывается ВНЕ транзакции.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GD_DB", str(HERE / "data" / "gd.sqlite")))
SCHEMA_VERSION = "1"

NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

AGENT_NAME = "Агент"
# Как зовут агента в сообщениях: @агент / @agent (регистр не важен).
AGENT_MENTION = re.compile(r"(^|\W)@(агент|agent)\b", re.IGNORECASE)
LOGIN_MENTION = re.compile(r"(^|\W)@([a-zA-Z0-9_.-]+)")


class StoreError(Exception):
    """Ошибка, которую можно показать человеку как есть."""


class NotFound(StoreError):
    pass


class Forbidden(StoreError):
    pass


# --------------------------------------------------------------- подключение

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Любая запись — только отсюда. BEGIN IMMEDIATE берёт блокировку сразу:
    апгрейд read->write посреди транзакции даёт SQLITE_BUSY, который
    busy_timeout не лечит."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn: sqlite3.Connection) -> None:
    """Идемпотентно. Вызывать ВНЕ write_tx: executescript сам коммитит."""
    conn.executescript((HERE / "schema.sql").read_text(encoding="utf-8-sig"))
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                 (SCHEMA_VERSION,))
    # Догоняющие миграции: CREATE IF NOT EXISTS не меняет существующую таблицу.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, ctype in (("edited_at", "TEXT"), ("deleted_at", "TEXT"),
                       ("pinned_at", "TEXT"), ("pinned_by", "INTEGER"),
                       ("reply_to_id", "INTEGER")):
        if col not in cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {ctype}")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(chat_reads)")}
    if "read_at" not in rcols:
        conn.execute("ALTER TABLE chat_reads ADD COLUMN read_at TEXT")


# --------------------------------------------------------------- пользователи

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), 200_000).hex()


def create_user(conn: sqlite3.Connection, login: str, password: str,
                display_name: str = "", is_admin: bool = False) -> dict:
    login = login.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{2,32}", login):
        raise StoreError("логин: 2–32 символа, латиница/цифры/._-")
    if len(password) < 4:
        raise StoreError("пароль слишком короткий (минимум 4 символа)")
    salt = secrets.token_hex(16)
    with write_tx(conn):
        try:
            cur = conn.execute(
                f"INSERT INTO users(login, pass_hash, pass_salt, display_name,"
                f" is_admin, created_at) VALUES (?,?,?,?,?,{NOW_SQL})",
                (login, _hash_password(password, salt), salt,
                 display_name.strip() or login, int(is_admin)))
        except sqlite3.IntegrityError:
            raise StoreError(f"логин «{login}» уже занят") from None
        user_id = cur.lastrowid
        # Личный чат с агентом — сразу, у каждого пользователя ровно один.
        cur = conn.execute(
            f"INSERT INTO chats(kind, title, is_private, owner_user_id, created_at)"
            f" VALUES ('agent', ?, 1, ?, {NOW_SQL})",
            (f"Агент · {display_name.strip() or login}", user_id))
        conn.execute("INSERT INTO chat_members(chat_id, user_id) VALUES (?,?)",
                     (cur.lastrowid, user_id))
    return get_user(conn, user_id)


def get_user(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого пользователя")
    return dict(row)


def user_by_login(conn: sqlite3.Connection, login: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM users WHERE login = ?",
                       (login.strip().lower(),)).fetchone()
    return dict(row) if row else None


def check_password(user: dict, password: str) -> bool:
    return secrets.compare_digest(
        user["pass_hash"], _hash_password(password, user["pass_salt"]))


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    if len(password) < 4:
        raise StoreError("пароль слишком короткий (минимум 4 символа)")
    salt = secrets.token_hex(16)
    with write_tx(conn):
        conn.execute("UPDATE users SET pass_hash = ?, pass_salt = ? WHERE id = ?",
                     (_hash_password(password, salt), salt, user_id))


def set_disabled(conn: sqlite3.Connection, user_id: int, disabled: bool) -> None:
    with write_tx(conn):
        conn.execute(
            f"UPDATE users SET disabled_at = {'(' + NOW_SQL + ')' if disabled else 'NULL'}"
            f" WHERE id = ?", (user_id,))


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, login, display_name, is_admin, created_at, disabled_at"
        " FROM users ORDER BY login").fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- auth-сессии

SESSION_TTL_DAYS = 30


def create_auth_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with write_tx(conn):
        conn.execute(
            f"INSERT INTO auth_sessions(token, user_id, created_at, expires_at)"
            f" VALUES (?,?,{NOW_SQL}, strftime('%Y-%m-%dT%H:%M:%fZ','now','+{SESSION_TTL_DAYS} days'))",
            (token, user_id))
    return token


def user_by_token(conn: sqlite3.Connection, token: str) -> Optional[dict]:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM auth_sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        " AND u.disabled_at IS NULL", (token,)).fetchone()
    return dict(row) if row else None


def drop_auth_session(conn: sqlite3.Connection, token: str) -> None:
    with write_tx(conn):
        conn.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))


# --------------------------------------------------------------- приглашения

INVITE_TTL_DAYS = 7


def create_invite(conn: sqlite3.Connection, user: dict) -> str:
    token = secrets.token_urlsafe(16)
    with write_tx(conn):
        conn.execute(
            f"INSERT INTO invites(token, created_by, created_at, expires_at)"
            f" VALUES (?,?,{NOW_SQL},"
            f" strftime('%Y-%m-%dT%H:%M:%fZ','now','+{INVITE_TTL_DAYS} days'))",
            (token, user["id"]))
    return token


def list_invites(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT i.*, u.login AS used_login,"
        "  (i.used_at IS NULL AND i.expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        "  AS pending"
        " FROM invites i LEFT JOIN users u ON u.id = i.used_by_user_id"
        " ORDER BY i.created_at DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]


def revoke_invite(conn: sqlite3.Connection, token: str) -> None:
    with write_tx(conn):
        conn.execute("DELETE FROM invites WHERE token = ? AND used_at IS NULL",
                     (token,))


def invite_pending(conn: sqlite3.Connection, token: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM invites WHERE token = ? AND used_at IS NULL"
        " AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')", (token,)).fetchone()
    return row is not None


def claim_invite(conn: sqlite3.Connection, token: str) -> bool:
    """Атомарно погасить ссылку. True — мы её захватили первыми."""
    with write_tx(conn):
        cur = conn.execute(
            f"UPDATE invites SET used_at = {NOW_SQL} WHERE token = ?"
            f" AND used_at IS NULL"
            f" AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')", (token,))
        return cur.rowcount == 1


def unclaim_invite(conn: sqlite3.Connection, token: str) -> None:
    """Вернуть ссылку, если регистрация после захвата не удалась (логин занят)."""
    with write_tx(conn):
        conn.execute("UPDATE invites SET used_at = NULL WHERE token = ?"
                     " AND used_by_user_id IS NULL", (token,))


def bind_invite(conn: sqlite3.Connection, token: str, user_id: int) -> None:
    with write_tx(conn):
        conn.execute("UPDATE invites SET used_by_user_id = ? WHERE token = ?",
                     (user_id, token))


# --------------------------------------------------------------- чаты

def _add_change(conn: sqlite3.Connection, chat_id: int, kind: str, ref_id: int) -> None:
    """Вызывается только внутри уже открытой write_tx."""
    conn.execute(f"INSERT INTO changes(chat_id, ts, kind, ref_id)"
                 f" VALUES (?, {NOW_SQL}, ?, ?)", (chat_id, kind, ref_id))


def get_chat(conn: sqlite3.Connection, chat_id: int) -> dict:
    row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого чата")
    return dict(row)


def chat_member_ids(conn: sqlite3.Connection, chat_id: int) -> list[int]:
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM chat_members WHERE chat_id = ?", (chat_id,))]


def chat_visible(conn: sqlite3.Connection, chat: dict, user: dict) -> bool:
    """Публичный канал видят все; приватный канал, ЛС и агентский — участники."""
    if chat["kind"] == "channel" and not chat["is_private"]:
        return True
    return user["id"] in chat_member_ids(conn, chat["id"])


def require_chat(conn: sqlite3.Connection, chat_id: int, user: dict) -> dict:
    chat = get_chat(conn, chat_id)
    if not chat_visible(conn, chat, user):
        raise Forbidden("этот чат вам не виден")
    return chat


def create_channel(conn: sqlite3.Connection, user: dict, title: str,
                   is_private: bool, member_ids: list[int]) -> dict:
    title = title.strip()
    if not title:
        raise StoreError("у канала должно быть название")
    members = set(member_ids) | {user["id"]}
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO chats(kind, title, is_private, owner_user_id, created_at)"
            f" VALUES ('channel', ?, ?, ?, {NOW_SQL})",
            (title, int(is_private), user["id"]))
        chat_id = cur.lastrowid
        for uid in members:
            conn.execute("INSERT OR IGNORE INTO chat_members(chat_id, user_id)"
                         " VALUES (?,?)", (chat_id, uid))
        _add_change(conn, chat_id, "chat_new", chat_id)
    return get_chat(conn, chat_id)


def ensure_dm(conn: sqlite3.Connection, user: dict, other_id: int) -> dict:
    """ЛС пары создаётся лениво при первом обращении."""
    if other_id == user["id"]:
        raise StoreError("ЛС с самим собой не нужен — есть чат с агентом")
    get_user(conn, other_id)  # существует?
    row = conn.execute(
        "SELECT c.* FROM chats c"
        " JOIN chat_members a ON a.chat_id = c.id AND a.user_id = ?"
        " JOIN chat_members b ON b.chat_id = c.id AND b.user_id = ?"
        " WHERE c.kind = 'dm'", (user["id"], other_id)).fetchone()
    if row:
        return dict(row)
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO chats(kind, title, is_private, created_at)"
            f" VALUES ('dm', '', 1, {NOW_SQL})")
        chat_id = cur.lastrowid
        for uid in (user["id"], other_id):
            conn.execute("INSERT INTO chat_members(chat_id, user_id) VALUES (?,?)",
                         (chat_id, uid))
        _add_change(conn, chat_id, "chat_new", chat_id)
    return get_chat(conn, chat_id)


def join_channel(conn: sqlite3.Connection, chat_id: int, user: dict) -> None:
    chat = get_chat(conn, chat_id)
    if chat["kind"] != "channel" or chat["is_private"]:
        raise Forbidden("вступать можно только в публичные каналы")
    with write_tx(conn):
        conn.execute("INSERT OR IGNORE INTO chat_members(chat_id, user_id)"
                     " VALUES (?,?)", (chat_id, user["id"]))


def leave_channel(conn: sqlite3.Connection, chat_id: int, user: dict) -> None:
    chat = get_chat(conn, chat_id)
    if chat["kind"] != "channel":
        raise Forbidden("выйти можно только из канала")
    with write_tx(conn):
        conn.execute("DELETE FROM chat_members WHERE chat_id = ? AND user_id = ?",
                     (chat_id, user["id"]))


DEFAULT_AGENT_TITLE = "Новый чат"


def create_agent_chat(conn: sqlite3.Connection, user: dict) -> dict:
    """Дополнительная сессия с агентом: сессий сколько угодно, у каждой свой
    контекст (agent_sessions по chat_id)."""
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO chats(kind, title, is_private, owner_user_id, created_at)"
            f" VALUES ('agent', ?, 1, ?, {NOW_SQL})",
            (DEFAULT_AGENT_TITLE, user["id"]))
        conn.execute("INSERT INTO chat_members(chat_id, user_id) VALUES (?,?)",
                     (cur.lastrowid, user["id"]))
        _add_change(conn, cur.lastrowid, "chat_new", cur.lastrowid)
    return get_chat(conn, cur.lastrowid)


def rename_chat(conn: sqlite3.Connection, chat_id: int, title: str) -> None:
    with write_tx(conn):
        conn.execute("UPDATE chats SET title = ? WHERE id = ?",
                     (title.strip()[:80], chat_id))
        _add_change(conn, chat_id, "chat_upd", chat_id)


def auto_title(text: str) -> str:
    """Название сессии из первого сообщения: одна строка, до ~42 символов."""
    t = " ".join(text.split())
    if len(t) > 42:
        cut = t[:42].rsplit(" ", 1)[0].rstrip(",.;:—-")
        t = (cut or t[:42]) + "…"
    return t or DEFAULT_AGENT_TITLE


def message_count(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?",
                        (chat_id,)).fetchone()["n"]


def delete_agent_chat(conn: sqlite3.Connection, chat_id: int,
                      user: dict) -> list[str]:
    """Удаление сессии с агентом целиком (только своей). Возвращает
    vault_path загруженных в чат файлов — сервер подчистит их на диске."""
    chat = get_chat(conn, chat_id)
    if chat["kind"] != "agent":
        raise StoreError("удалять можно только сессии с агентом")
    if chat["owner_user_id"] != user["id"]:
        raise Forbidden("это не ваша сессия")
    with write_tx(conn):
        upload_paths = [r["vault_path"] for r in conn.execute(
            "SELECT vault_path FROM uploads WHERE chat_id = ?", (chat_id,))]
        conn.execute("DELETE FROM reactions WHERE message_id IN"
                     " (SELECT id FROM messages WHERE chat_id = ?)", (chat_id,))
        conn.execute("DELETE FROM mentions WHERE message_id IN"
                     " (SELECT id FROM messages WHERE chat_id = ?)", (chat_id,))
        # Порядок важен (foreign_keys=ON): agent_turns.user_message_id и
        # thread_root_id ссылаются на messages — сначала ходы, потом ответы
        # тредов, и только затем корни.
        conn.execute("DELETE FROM agent_turns WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM messages WHERE chat_id = ?"
                     " AND thread_root_id IS NOT NULL", (chat_id,))
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM agent_sessions WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chat_reads WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chat_members WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM uploads WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM changes WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
    return upload_paths


def agent_chat_for(conn: sqlite3.Connection, user: dict) -> dict:
    row = conn.execute(
        "SELECT * FROM chats WHERE kind = 'agent' AND owner_user_id = ?",
        (user["id"],)).fetchone()
    if row is None:  # пользователи, заведённые до этой логики
        with write_tx(conn):
            cur = conn.execute(
                f"INSERT INTO chats(kind, title, is_private, owner_user_id, created_at)"
                f" VALUES ('agent', ?, 1, ?, {NOW_SQL})",
                (f"Агент · {user['display_name']}", user["id"]))
            conn.execute("INSERT INTO chat_members(chat_id, user_id) VALUES (?,?)",
                         (cur.lastrowid, user["id"]))
        return get_chat(conn, cur.lastrowid)
    return dict(row)


def list_chats(conn: sqlite3.Connection, user: dict) -> list[dict]:
    """Все видимые чаты + счётчики непрочитанных и флаг «вас упомянули».

    Непрочитанные считаются по сообщениям ленты (треды не пугают бейджем).
    """
    rows = conn.execute(
        "SELECT c.*, (m.user_id IS NOT NULL) AS is_member,"
        "       COALESCE(r.last_read_message_id, 0) AS last_read"
        " FROM chats c"
        " LEFT JOIN chat_members m ON m.chat_id = c.id AND m.user_id = ?"
        " LEFT JOIN chat_reads r ON r.chat_id = c.id AND r.user_id = ?"
        " WHERE c.archived_at IS NULL"
        "   AND (m.user_id IS NOT NULL OR (c.kind = 'channel' AND c.is_private = 0))"
        " ORDER BY c.kind,"
        "   CASE WHEN c.kind = 'agent' THEN -c.id ELSE 0 END, c.title",
        (user["id"], user["id"])).fetchall()
    out = []
    for row in rows:
        chat = dict(row)
        unread = conn.execute(
            "SELECT COUNT(*) AS n, MAX(EXISTS("
            "  SELECT 1 FROM mentions mn WHERE mn.message_id = messages.id"
            "  AND mn.user_id = ?)) AS mentioned"
            " FROM messages WHERE chat_id = ? AND id > ? AND thread_root_id IS NULL"
            " AND NOT (author_kind = 'user' AND author_user_id = ?)",
            (user["id"], chat["id"], chat["last_read"], user["id"])).fetchone()
        chat["unread"] = unread["n"]
        chat["mentioned"] = bool(unread["mentioned"])
        if chat["kind"] == "dm":
            other = conn.execute(
                "SELECT u.display_name FROM chat_members m JOIN users u ON u.id = m.user_id"
                " WHERE m.chat_id = ? AND m.user_id != ?",
                (chat["id"], user["id"])).fetchone()
            chat["title"] = other["display_name"] if other else "ЛС"
        out.append(chat)
    return out


def mark_read(conn: sqlite3.Connection, chat_id: int, user: dict,
              last_message_id: int) -> None:
    with write_tx(conn):
        row = conn.execute(
            "SELECT last_read_message_id FROM chat_reads"
            " WHERE chat_id = ? AND user_id = ?", (chat_id, user["id"])).fetchone()
        if row is not None and last_message_id <= row["last_read_message_id"]:
            return  # ничего нового — не двигаем read_at и не шумим в changes
        conn.execute(
            f"INSERT INTO chat_reads(chat_id, user_id, last_read_message_id, read_at)"
            f" VALUES (?,?,?,{NOW_SQL})"
            f" ON CONFLICT(chat_id, user_id) DO UPDATE SET"
            f"   last_read_message_id = MAX(last_read_message_id,"
            f"     excluded.last_read_message_id),"
            f"   read_at = excluded.read_at",
            (chat_id, user["id"], last_message_id))
        _add_change(conn, chat_id, "read", user["id"])


def chat_reads_info(conn: sqlite3.Connection, chat_id: int) -> list[dict]:
    """Кто из участников до какого сообщения дочитал и когда (для галочек
    и списка «кто прочитал»)."""
    rows = conn.execute(
        "SELECT r.user_id, u.display_name AS name, r.last_read_message_id,"
        "       r.read_at"
        " FROM chat_reads r JOIN users u ON u.id = r.user_id"
        " WHERE r.chat_id = ?", (chat_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- сообщения

def _parse_mentions(conn: sqlite3.Connection, text: str) -> list[int]:
    ids = []
    for m in LOGIN_MENTION.finditer(text):
        user = user_by_login(conn, m.group(2))
        if user:
            ids.append(user["id"])
    return sorted(set(ids))


def mentions_agent(text: str) -> bool:
    return bool(AGENT_MENTION.search(text))


def add_message(conn: sqlite3.Connection, chat_id: int, *, author_kind: str,
                author_user_id: Optional[int], author_name: str, text: str,
                thread_root_id: Optional[int] = None,
                attachments: Optional[list] = None,
                status: str = "final", turn_id: Optional[int] = None,
                reply_to_id: Optional[int] = None) -> dict:
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO messages(chat_id, author_kind, author_user_id, author_name,"
            f" text, thread_root_id, attachments_json, status, turn_id,"
            f" reply_to_id, created_at, updated_at)"
            f" VALUES (?,?,?,?,?,?,?,?,?,?,{NOW_SQL},{NOW_SQL})",
            (chat_id, author_kind, author_user_id, author_name, text,
             thread_root_id, json.dumps(attachments or [], ensure_ascii=False),
             status, turn_id, reply_to_id))
        msg_id = cur.lastrowid
        for uid in _parse_mentions(conn, text):
            conn.execute("INSERT OR IGNORE INTO mentions(message_id, user_id)"
                         " VALUES (?,?)", (msg_id, uid))
        _add_change(conn, chat_id, "message_new", msg_id)
    return get_message(conn, msg_id)


def get_message(conn: sqlite3.Connection, msg_id: int) -> dict:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого сообщения")
    return _msg_dict(conn, row)


def _msg_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    msg = dict(row)
    msg["attachments"] = json.loads(msg.pop("attachments_json") or "[]")
    msg["reactions"] = [dict(r) for r in conn.execute(
        "SELECT emoji, COUNT(*) AS n, GROUP_CONCAT(user_id) AS user_ids"
        " FROM reactions WHERE message_id = ? GROUP BY emoji", (msg["id"],))]
    if msg["thread_root_id"] is None:
        msg["reply_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE thread_root_id = ?",
            (msg["id"],)).fetchone()["n"]
    if msg.get("reply_to_id"):
        r = conn.execute(
            "SELECT id, author_name, text, deleted_at FROM messages WHERE id = ?",
            (msg["reply_to_id"],)).fetchone()
        if r is not None:
            quote = " ".join((r["text"] or "").split())
            msg["reply_to"] = {
                "id": r["id"], "author_name": r["author_name"],
                "text": quote[:160] + ("…" if len(quote) > 160 else ""),
                "deleted": bool(r["deleted_at"])}
    return msg


def list_messages(conn: sqlite3.Connection, chat_id: int, *,
                  before: Optional[int] = None, limit: int = 60,
                  thread_root_id: Optional[int] = None) -> list[dict]:
    where = "chat_id = ?"
    args: list[Any] = [chat_id]
    if thread_root_id is not None:
        where += " AND thread_root_id = ?"
        args.append(thread_root_id)
    else:
        where += " AND thread_root_id IS NULL"
    if before:
        where += " AND id < ?"
        args.append(before)
    rows = conn.execute(
        f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
        (*args, limit)).fetchall()
    return [_msg_dict(conn, r) for r in reversed(rows)]


def update_message_text(conn: sqlite3.Connection, msg_id: int, text: str,
                        status: str) -> None:
    with write_tx(conn):
        row = conn.execute("SELECT chat_id FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            f"UPDATE messages SET text = ?, status = ?, updated_at = {NOW_SQL}"
            f" WHERE id = ?", (text, status, msg_id))
        _add_change(conn, row["chat_id"], "message_upd", msg_id)


def _require_own_message(conn: sqlite3.Connection, msg_id: int,
                         user: dict) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого сообщения")
    if row["deleted_at"]:
        raise StoreError("сообщение уже удалено")
    if row["author_kind"] != "user" or row["author_user_id"] != user["id"]:
        raise Forbidden("можно только со своими сообщениями")
    return row


def edit_message(conn: sqlite3.Connection, msg_id: int, user: dict,
                 text: str) -> dict:
    text = text.strip()
    if not text:
        raise StoreError("пустой текст — если хотите убрать сообщение, удалите его")
    with write_tx(conn):
        row = _require_own_message(conn, msg_id, user)
        conn.execute(
            f"UPDATE messages SET text = ?, edited_at = {NOW_SQL},"
            f" updated_at = {NOW_SQL} WHERE id = ?", (text, msg_id))
        # Упоминания пересобираются: @логин могли добавить или убрать.
        conn.execute("DELETE FROM mentions WHERE message_id = ?", (msg_id,))
        for uid in _parse_mentions(conn, text):
            conn.execute("INSERT OR IGNORE INTO mentions(message_id, user_id)"
                         " VALUES (?,?)", (msg_id, uid))
        _add_change(conn, row["chat_id"], "message_upd", msg_id)
    return get_message(conn, msg_id)


def delete_message(conn: sqlite3.Connection, msg_id: int, user: dict) -> None:
    """Мягкое удаление: текст и вложения стираются, каркас остаётся —
    иначе рвались бы треды и changefeed. Свои сообщения — везде; в своей
    сессии с агентом владелец может удалять и ответы агента."""
    with write_tx(conn):
        row = conn.execute("SELECT * FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
        if row is None:
            raise NotFound("нет такого сообщения")
        if row["deleted_at"]:
            raise StoreError("сообщение уже удалено")
        own = (row["author_kind"] == "user" and
               row["author_user_id"] == user["id"])
        if not own:
            chat = get_chat(conn, row["chat_id"])
            if not (chat["kind"] == "agent" and
                    chat["owner_user_id"] == user["id"] and
                    row["author_kind"] == "agent"):
                raise Forbidden("можно только со своими сообщениями")
        conn.execute(
            f"UPDATE messages SET text = '', attachments_json = '[]',"
            f" deleted_at = {NOW_SQL}, updated_at = {NOW_SQL} WHERE id = ?",
            (msg_id,))
        conn.execute("DELETE FROM mentions WHERE message_id = ?", (msg_id,))
        conn.execute("DELETE FROM reactions WHERE message_id = ?", (msg_id,))
        _add_change(conn, row["chat_id"], "message_upd", msg_id)


def replies_to_thread(conn: sqlite3.Connection, msg_id: int) -> dict:
    """Перевести цепочку ответов (reply_to_id) в тред: находим корень цепочки,
    все связанные с ним ответы уходят из ленты в его тред. Возвращает корень."""
    with write_tx(conn):
        row = conn.execute("SELECT * FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
        if row is None:
            raise NotFound("нет такого сообщения")
        if row["thread_root_id"]:
            raise StoreError("сообщение уже в треде")
        chat_id = row["chat_id"]
        # Вверх к корню цепочки.
        root = row
        seen = {row["id"]}
        while root["reply_to_id"]:
            parent = conn.execute("SELECT * FROM messages WHERE id = ?",
                                  (root["reply_to_id"],)).fetchone()
            if (parent is None or parent["thread_root_id"]
                    or parent["id"] in seen):
                break
            seen.add(parent["id"])
            root = parent
        # Вниз по всем ответвлениям в этой же ленте.
        chain = {root["id"]}
        frontier = [root["id"]]
        while frontier:
            qmarks = ",".join("?" * len(frontier))
            rows = conn.execute(
                f"SELECT id FROM messages WHERE chat_id = ?"
                f" AND thread_root_id IS NULL AND reply_to_id IN ({qmarks})",
                (chat_id, *frontier)).fetchall()
            frontier = [r["id"] for r in rows if r["id"] not in chain]
            chain.update(frontier)
        moved = sorted(chain - {root["id"]})
        if not moved:
            raise StoreError("у этого сообщения нет цепочки ответов")
        qmarks = ",".join("?" * len(moved))
        conn.execute(
            f"UPDATE messages SET thread_root_id = ?, updated_at = {NOW_SQL}"
            f" WHERE id IN ({qmarks})", (root["id"], *moved))
        for mid in moved:
            _add_change(conn, chat_id, "message_upd", mid)
        _add_change(conn, chat_id, "message_upd", root["id"])
    return get_message(conn, root["id"])


def toggle_pin(conn: sqlite3.Connection, msg_id: int, user: dict) -> bool:
    """Закреп/съём — любым участником чата (проверка доступа на ручке).
    Возвращает новое состояние: True = закреплено."""
    with write_tx(conn):
        row = conn.execute("SELECT * FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
        if row is None:
            raise NotFound("нет такого сообщения")
        if row["deleted_at"]:
            raise StoreError("удалённое не закрепить")
        pinned = not row["pinned_at"]
        if pinned:
            conn.execute(f"UPDATE messages SET pinned_at = {NOW_SQL},"
                         f" pinned_by = ? WHERE id = ?", (user["id"], msg_id))
        else:
            conn.execute("UPDATE messages SET pinned_at = NULL,"
                         " pinned_by = NULL WHERE id = ?", (msg_id,))
        _add_change(conn, row["chat_id"], "message_upd", msg_id)
    return pinned


def list_pins(conn: sqlite3.Connection, chat_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND pinned_at IS NOT NULL"
        " AND deleted_at IS NULL ORDER BY pinned_at DESC", (chat_id,)).fetchall()
    return [_msg_dict(conn, r) for r in rows]


def pin_count(conn: sqlite3.Connection, chat_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?"
        " AND pinned_at IS NOT NULL AND deleted_at IS NULL",
        (chat_id,)).fetchone()["n"]


def list_chat_files(conn: sqlite3.Connection, chat_id: int,
                    limit: int = 500) -> list[dict]:
    """Все вложения чата (вкладка «Файлы»), новые сверху."""
    rows = conn.execute(
        "SELECT id, author_name, created_at, attachments_json FROM messages"
        " WHERE chat_id = ? AND deleted_at IS NULL AND attachments_json != '[]'"
        " ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
    out = []
    for r in rows:
        for a in json.loads(r["attachments_json"] or "[]"):
            out.append({**a, "message_id": r["id"], "author": r["author_name"],
                        "created_at": r["created_at"]})
    return out


def toggle_reaction(conn: sqlite3.Connection, msg_id: int, user: dict,
                    emoji: str) -> None:
    emoji = emoji.strip()[:8]
    if not emoji:
        raise StoreError("пустая реакция")
    with write_tx(conn):
        row = conn.execute("SELECT chat_id FROM messages WHERE id = ?",
                           (msg_id,)).fetchone()
        if row is None:
            raise NotFound("нет такого сообщения")
        cur = conn.execute(
            "DELETE FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (msg_id, user["id"], emoji))
        if cur.rowcount == 0:
            conn.execute("INSERT INTO reactions(message_id, user_id, emoji)"
                         " VALUES (?,?,?)", (msg_id, user["id"], emoji))
        _add_change(conn, row["chat_id"], "reaction", msg_id)


# --------------------------------------------------------------- ходы агента

def create_turn(conn: sqlite3.Connection, chat_id: int, user_message_id: int) -> int:
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO agent_turns(chat_id, user_message_id, started_at)"
            f" VALUES (?,?,{NOW_SQL})", (chat_id, user_message_id))
        return cur.lastrowid


def get_turn(conn: sqlite3.Connection, turn_id: int) -> dict:
    row = conn.execute("SELECT * FROM agent_turns WHERE id = ?", (turn_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого хода")
    turn = dict(row)
    turn["files"] = json.loads(turn.pop("files_json") or "[]")
    turn["tools"] = json.loads(turn.pop("tools_json") or "[]")
    return turn


def finish_turn(conn: sqlite3.Connection, turn_id: int, *, status: str,
                sdk_session_id: Optional[str] = None,
                cost_usd: Optional[float] = None,
                files: Optional[list[str]] = None,
                tools: Optional[list[dict]] = None,
                commit_hash: Optional[str] = None,
                error: Optional[str] = None) -> None:
    with write_tx(conn):
        row = conn.execute("SELECT chat_id FROM agent_turns WHERE id = ?",
                           (turn_id,)).fetchone()
        if row is None:
            return
        conn.execute(
            f"UPDATE agent_turns SET status = ?, sdk_session_id = ?, cost_usd = ?,"
            f" files_json = ?, tools_json = ?, commit_hash = ?, error = ?,"
            f" finished_at = {NOW_SQL} WHERE id = ?",
            (status, sdk_session_id, cost_usd,
             json.dumps(files or [], ensure_ascii=False),
             json.dumps(tools or [], ensure_ascii=False),
             commit_hash, error, turn_id))
        if sdk_session_id:
            conn.execute(
                f"INSERT INTO agent_sessions(chat_id, sdk_session_id, updated_at)"
                f" VALUES (?,?,{NOW_SQL})"
                f" ON CONFLICT(chat_id) DO UPDATE SET sdk_session_id = excluded.sdk_session_id,"
                f" updated_at = excluded.updated_at", (row["chat_id"], sdk_session_id))
        _add_change(conn, row["chat_id"], "turn_upd", turn_id)


def stored_sdk_session(conn: sqlite3.Connection, chat_id: int) -> Optional[str]:
    row = conn.execute("SELECT sdk_session_id FROM agent_sessions WHERE chat_id = ?",
                       (chat_id,)).fetchone()
    return row["sdk_session_id"] if row else None


# --------------------------------------------------------------- changefeed

def max_change_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM changes").fetchone()
    return row["n"]


def changes_since(conn: sqlite3.Connection, user: dict, since: int,
                  limit: int = 200) -> list[dict]:
    """События чатов, видимых пользователю."""
    rows = conn.execute(
        "SELECT ch.* FROM changes ch JOIN chats c ON c.id = ch.chat_id"
        " LEFT JOIN chat_members m ON m.chat_id = c.id AND m.user_id = ?"
        " WHERE ch.id > ? AND"
        "   (m.user_id IS NOT NULL OR (c.kind = 'channel' AND c.is_private = 0))"
        " ORDER BY ch.id LIMIT ?", (user["id"], since, limit)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- поиск

def _snippet(text: str, idx: int, qlen: int, radius: int = 60) -> str:
    start = max(0, idx - radius)
    end = min(len(text), idx + qlen + radius)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


def search_messages(conn: sqlite3.Connection, user: dict, q: str,
                    limit: int = 20) -> list[dict]:
    """Поиск по сообщениям видимых чатов. Фильтр — в питоне: lower() у SQLite
    не знает кириллицу. Просматриваются последние 5000 сообщений."""
    ql = q.lower()
    rows = conn.execute(
        "SELECT m.id, m.chat_id, m.author_name, m.text, m.thread_root_id,"
        "       m.created_at, c.kind AS chat_kind, c.title AS chat_title"
        " FROM messages m JOIN chats c ON c.id = m.chat_id"
        " LEFT JOIN chat_members cm ON cm.chat_id = c.id AND cm.user_id = ?"
        " WHERE (cm.user_id IS NOT NULL OR (c.kind = 'channel' AND c.is_private = 0))"
        "   AND m.author_kind != 'system'"
        " ORDER BY m.id DESC LIMIT 5000", (user["id"],))
    out = []
    for r in rows:
        text = r["text"] or ""
        idx = text.lower().find(ql)
        if idx < 0:
            continue
        out.append({"id": r["id"], "chat_id": r["chat_id"],
                    "chat_kind": r["chat_kind"], "chat_title": r["chat_title"],
                    "author_name": r["author_name"],
                    "thread_root_id": r["thread_root_id"],
                    "created_at": r["created_at"],
                    "snippet": _snippet(text, idx, len(q))})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------- загрузки

def add_upload(conn: sqlite3.Connection, user: dict, orig_name: str,
               vault_path: str, chat_id: Optional[int] = None) -> int:
    with write_tx(conn):
        cur = conn.execute(
            f"INSERT INTO uploads(user_id, chat_id, orig_name, vault_path, created_at)"
            f" VALUES (?,?,?,?,{NOW_SQL})",
            (user["id"], chat_id, orig_name, vault_path))
        return cur.lastrowid


def rebind_uploads(conn: sqlite3.Connection, upload_ids: list[int],
                   chat_id: int, user: dict) -> None:
    """Файл могли прикрепить в одном чате, а отправить в другом — доступ
    к нему проверяется по uploads.chat_id, поэтому при отправке сообщения
    перепривязываем свои загрузки к чату сообщения."""
    if not upload_ids:
        return
    with write_tx(conn):
        for uid in upload_ids:
            conn.execute("UPDATE uploads SET chat_id = ? WHERE id = ?"
                         " AND user_id = ?", (chat_id, uid, user["id"]))


def get_upload(conn: sqlite3.Connection, upload_id: int) -> dict:
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row is None:
        raise NotFound("нет такого файла")
    return dict(row)
