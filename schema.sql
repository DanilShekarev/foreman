-- Схема gd-assist. Правила те же, что в kanban: версия в meta, любая запись —
-- внутри store.write_tx() (BEGIN IMMEDIATE). Без STRICT: SQLite питона 3.10
-- (3.34) его ещё не знает.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    login        TEXT NOT NULL UNIQUE,
    pass_hash    TEXT NOT NULL,
    pass_salt    TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    disabled_at  TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- kind: 'agent' — личный чат пользователя с агентом (owner_user_id = владелец);
--       'dm'    — пара людей (title пуст, участники в chat_members);
--       'channel' — канал; is_private=0 виден всем, 1 — только участникам.
CREATE TABLE IF NOT EXISTS chats (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('agent','dm','channel')),
    title         TEXT NOT NULL DEFAULT '',
    is_private    INTEGER NOT NULL DEFAULT 0,
    owner_user_id INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL,
    archived_at   TEXT
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY,
    chat_id         INTEGER NOT NULL REFERENCES chats(id),
    author_kind     TEXT NOT NULL CHECK (author_kind IN ('user','agent','system')),
    author_user_id  INTEGER REFERENCES users(id),
    author_name     TEXT NOT NULL,
    text            TEXT NOT NULL DEFAULT '',
    thread_root_id  INTEGER REFERENCES messages(id),   -- NULL = сообщение ленты
    attachments_json TEXT NOT NULL DEFAULT '[]',       -- [{path, title}] — доки волта
    status          TEXT NOT NULL DEFAULT 'final' CHECK (status IN ('final','streaming','error')),
    turn_id         INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    edited_at       TEXT,   -- правка автором (updated_at дёргает и стрим агента)
    deleted_at      TEXT,   -- мягкое удаление: текст стирается, каркас остаётся
    pinned_at       TEXT,   -- закреп: любой участник чата может пинить/распинить
    pinned_by       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    emoji      TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);

-- Распарсенные @логин из текста сообщения (заполняется при сохранении).
CREATE TABLE IF NOT EXISTS mentions (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (message_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_reads (
    chat_id              INTEGER NOT NULL REFERENCES chats(id),
    user_id              INTEGER NOT NULL REFERENCES users(id),
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS agent_turns (
    id              INTEGER PRIMARY KEY,
    chat_id         INTEGER NOT NULL REFERENCES chats(id),
    user_message_id INTEGER NOT NULL REFERENCES messages(id),
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running','done','error','interrupted')),
    sdk_session_id  TEXT,
    cost_usd        REAL,
    files_json      TEXT NOT NULL DEFAULT '[]',  -- пути, тронутые Edit/Write (отн. волта)
    tools_json      TEXT NOT NULL DEFAULT '[]',  -- [{name, input}] для отображения в чате
    commit_hash     TEXT,
    error           TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);

-- Актуальный sdk_session_id чата — для resume после рестарта/таймаута.
CREATE TABLE IF NOT EXISTS agent_sessions (
    chat_id        INTEGER PRIMARY KEY REFERENCES chats(id),
    sdk_session_id TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- Changefeed: клиенты поллят max(id) и добирают события своих чатов.
CREATE TABLE IF NOT EXISTS changes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id),
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- message_new | message_upd | turn_upd | reaction | chat_new
    ref_id  INTEGER NOT NULL
);

-- Одноразовые ссылки-приглашения: регистрация гасит ссылку (used_at).
CREATE TABLE IF NOT EXISTS invites (
    token           TEXT PRIMARY KEY,
    created_by      INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used_by_user_id INTEGER REFERENCES users(id),
    used_at         TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    chat_id    INTEGER REFERENCES chats(id),
    orig_name  TEXT NOT NULL,
    vault_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
