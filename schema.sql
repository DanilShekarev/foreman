-- Схема Foreman. Правила те же, что в kanban: версия в meta, любая запись —
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
    archived_at   TEXT,
    model         TEXT   -- модель агента этого чата; NULL = дефолт сервера
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
    pinned_by       INTEGER,
    reply_to_id     INTEGER  -- ответ в ленте (как в ТГ), не тред
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
-- Лента «Активность» (#82): выборка ответов на мои сообщения и упоминаний
-- меня бегает каждый поллинг — без индексов это фулскан messages/mentions.
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to_id)
    WHERE reply_to_id IS NOT NULL;

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
CREATE INDEX IF NOT EXISTS idx_mentions_user ON mentions(user_id, message_id);

CREATE TABLE IF NOT EXISTS chat_reads (
    chat_id              INTEGER NOT NULL REFERENCES chats(id),
    user_id              INTEGER NOT NULL REFERENCES users(id),
    last_read_message_id INTEGER NOT NULL DEFAULT 0,
    read_at              TEXT,  -- когда дочитал (для «кто прочитал и во сколько»)
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
    model           TEXT,   -- фактическая модель хода (из init-сообщения SDK)
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

-- Канбан задач команды (одна общая доска). Колонки фиксированы:
-- backlog «Планы» / todo «К работе» / doing «В работе» / done «Готово».
-- position — REAL: перетаскивание вставляет карточку между соседями как
-- (prev+next)/2 без перенумерации всей колонки.
CREATE TABLE IF NOT EXISTS kanban_cards (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    column_key   TEXT NOT NULL DEFAULT 'todo'
                 CHECK (column_key IN ('backlog','todo','doing','done')),
    position     REAL NOT NULL DEFAULT 0,
    created_by   INTEGER REFERENCES users(id),  -- NULL = агент
    creator_name TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    archived_at  TEXT,
    -- поля ниже дублируются ALTER-миграцией в init_db (живые БД);
    -- CHECK на них не вешать — ALTER ADD COLUMN его не умеет
    priority     TEXT NOT NULL DEFAULT 'normal',  -- low|normal|high|urgent
    due_date     TEXT,                            -- YYYY-MM-DD
    updated_by   INTEGER                          -- NULL = агент
);

CREATE TABLE IF NOT EXISTS kanban_assignees (
    card_id INTEGER NOT NULL REFERENCES kanban_cards(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (card_id, user_id)
);

-- color — ключ палитры (--lab-* в style.css), не hex: темизация на фронте.
CREATE TABLE IF NOT EXISTS kanban_labels (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT 'blue'
);

CREATE TABLE IF NOT EXISTS kanban_card_labels (
    card_id  INTEGER NOT NULL REFERENCES kanban_cards(id),
    label_id INTEGER NOT NULL REFERENCES kanban_labels(id),
    PRIMARY KEY (card_id, label_id)
);

CREATE TABLE IF NOT EXISTS kanban_checklist (
    id         INTEGER PRIMARY KEY,
    card_id    INTEGER NOT NULL REFERENCES kanban_cards(id),
    text       TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    position   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kanban_comments (
    id             INTEGER PRIMARY KEY,
    card_id        INTEGER NOT NULL REFERENCES kanban_cards(id),
    author_user_id INTEGER REFERENCES users(id),  -- NULL = агент
    author_name    TEXT NOT NULL,
    text           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Журнал изменений карточки: kind + детали в data_json
-- ({"from":"todo","to":"doing"} и т.п.), формат строк собирает фронт.
CREATE TABLE IF NOT EXISTS kanban_activity (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER NOT NULL REFERENCES kanban_cards(id),
    actor_user_id INTEGER,  -- NULL = агент
    actor_name    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    data_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_checklist_card ON kanban_checklist(card_id, id);
CREATE INDEX IF NOT EXISTS idx_kb_comments_card  ON kanban_comments(card_id, id);
CREATE INDEX IF NOT EXISTS idx_kb_activity_card  ON kanban_activity(card_id, id);

CREATE TABLE IF NOT EXISTS uploads (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    chat_id    INTEGER REFERENCES chats(id),
    orig_name  TEXT NOT NULL,
    vault_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- История созвонов (#86). Активное состояние комнат — в памяти процесса
-- (server.py: CALLS); здесь только факт звонка и его записи через uploads.
CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL REFERENCES chats(id),
    started_by INTEGER NOT NULL REFERENCES users(id),
    started_at TEXT NOT NULL,
    ended_at   TEXT     -- NULL = не финализирован (добивается при старте сервера)
);

-- Провайдеры LLM (#49): kind='claude' — подписочный CLI через Agent SDK,
-- kind='openai' — любой OpenAI-совместимый эндпоинт (OpenRouter, локальный).
-- key — префикс в chats.model («openrouter:z-ai/glm-5.2:free»).
CREATE TABLE IF NOT EXISTS llm_providers (
    id          INTEGER PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('claude','openai')),
    base_url    TEXT,
    api_key     TEXT,   -- «env:ИМЯ» читается из окружения; наружу не отдаётся
    models_json TEXT NOT NULL DEFAULT '[]',  -- [{id, title, vision}]
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- Контекст своего агентного цикла: у Claude-движка историю держит CLI (resume
-- по sdk_session_id), у OpenAI-движка ей жить больше негде. Строка = один ход
-- целиком, поэтому обрезка по бюджету не рвёт пары tool_call/tool.
CREATE TABLE IF NOT EXISTS agent_history (
    id            INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL REFERENCES chats(id),
    turn_id       INTEGER,
    messages_json TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_history_chat ON agent_history(chat_id, id);
