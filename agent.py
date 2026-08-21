"""Агент-сессии: по одной на чат, живут в процессе сервера.

Паттерн из voxels/agent_runner.py, но сессий много (словарь chat_id -> Session).
Каждая сессия — свой ClaudeSDKClient (подпроцесс CLI). Создаётся лениво при
первом сообщении с resume сохранённого sdk_session_id, закрывается по
idle-таймауту (session_id остаётся в БД — следующее сообщение делает resume).

Права — ТОЛЬКО PreToolUse-хуками: запись в allowed_tools авторазрешает вызов
до can_use_tool (CanUseToolShadowedWarning), поэтому колбэк-гард молча не
работает. Bash запрещён целиком, git-операции делает сервер.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

import gitops
import prompts
import store
import vaultlib
from common import setup_auth
from vaultlib import VAULT

import os

MODEL = os.environ.get("GD_MODEL", "sonnet")
# Модели, доступные к выбору per-чат (селектор в шапке чата).
MODELS = tuple(m.strip() for m in
               os.environ.get("GD_MODELS", "sonnet,opus,haiku").split(",")
               if m.strip())
MAX_TURNS = int(os.environ.get("GD_MAX_TURNS", "100"))
IDLE_SECONDS = 30 * 60          # закрыть молчащий клиент (resume вернёт контекст)
MAX_CONCURRENT_TURNS = 3        # глобальный предел одновременных ходов
DB_THROTTLE = 1.5               # сек между апдейтами стримящегося текста в БД
# Верхний предел ВЫБОРКИ из БД; реальный объём режет prompts.trim_context
# по бюджету символов (GD_CONTEXT_CHARS).
CHANNEL_CONTEXT_MESSAGES = 100

WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
FS_TOOLS = ("mcp__fs__copy_file", "mcp__fs__move_file", "mcp__fs__delete_file")
KANBAN_TOOLS = ("mcp__kanban__list_cards", "mcp__kanban__create_card",
                "mcp__kanban__move_card", "mcp__kanban__update_card",
                "mcp__kanban__get_card", "mcp__kanban__comment_card",
                "mcp__kanban__checklist_add", "mcp__kanban__checklist_set")
THREAD_TOOLS = ("mcp__threads__list_recent", "mcp__threads__move_to_thread")
# Deny-by-default: CLI-харнес объявляет агенту множество своих инструментов
# (ToolSearch, Cron*, SendMessage, Task* и т.п.) — всё, чего нет в этом
# белом списке, режется гардом до выполнения.
SAFE_TOOLS = frozenset(WRITE_TOOLS) | set(FS_TOOLS) | set(KANBAN_TOOLS) | set(
    THREAD_TOOLS) | {"Read", "Grep", "Glob", "TodoWrite"}


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """1 сообщение / 2 сообщения / 5 сообщений."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return few
    return many


def _deny(message: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": message}}


def _tool_path(tool_input: dict) -> Optional[str]:
    return tool_input.get("file_path") or tool_input.get("path") or None


async def whitelist_guard(input_data, tool_use_id, context):
    name = input_data.get("tool_name") or ""
    if name not in SAFE_TOOLS:
        return _deny(f"инструмент «{name}» запрещён в этом сервисе")
    return {}


def _make_fs_server(scopes: dict, extra_read: set):
    """Байтовые файловые операции: Write пишет только текст, который агент сам
    формирует, поэтому картинку или docx он без этих тулзов не перенесёт.
    Права те же, что у гардов: читать — read-корни + extra_read, писать —
    write-корни; .git закрыт."""
    read_roots = [p.resolve() for p in scopes["read"]]
    write_roots = [p.resolve() for p in scopes["write"]]

    def _resolve(raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = VAULT / "shared" / p
        return p.resolve()

    def _ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}]}

    def _err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _readable(p: Path) -> bool:
        return (".git" not in p.parts
                and (vaultlib.path_under(p, read_roots) or p in extra_read))

    def _writable(p: Path) -> bool:
        return ".git" not in p.parts and vaultlib.path_under(p, write_roots)

    @tool("copy_file", "Скопировать файл байт-в-байт (картинки, docx и другие "
          "нетекстовые файлы). src — любой доступный тебе для чтения файл, "
          "dst — путь внутри твоих областей записи.",
          {"src": str, "dst": str})
    async def copy_file(args):
        src, dst = _resolve(args["src"]), _resolve(args["dst"])
        if not _readable(src):
            return _err(f"src вне доступных тебе областей: {src}")
        if not src.is_file():
            return _err(f"src не существует: {src}")
        if not _writable(dst):
            return _err(f"dst вне твоих областей записи: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return _ok(f"скопировано: {src} -> {dst}")

    @tool("move_file", "Переместить или переименовать файл внутри твоих "
          "областей записи.", {"src": str, "dst": str})
    async def move_file(args):
        src, dst = _resolve(args["src"]), _resolve(args["dst"])
        if not _writable(src):
            return _err(f"src вне твоих областей записи: {src}")
        if not src.is_file():
            return _err(f"src не существует: {src}")
        if not _writable(dst):
            return _err(f"dst вне твоих областей записи: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return _ok(f"перемещено: {src} -> {dst}")

    @tool("delete_file", "Удалить файл в твоих областях записи "
          "(git сохраняет историю, удаление откатываемо).", {"path": str})
    async def delete_file(args):
        p = _resolve(args["path"])
        if not _writable(p):
            return _err(f"путь вне твоих областей записи: {p}")
        if not p.is_file():
            return _err(f"не файл или не существует: {p}")
        p.unlink()
        return _ok(f"удалено: {p}")

    return create_sdk_mcp_server(name="fs", version="1.0.0",
                                 tools=[copy_file, move_file, delete_file])


def _make_kanban_server():
    """Доска задач команды: агент видит и ведёт её наравне с людьми.
    Одна общая доска, права не нужны — её видят все пользователи."""

    def _ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}]}

    def _err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _logins_to_ids(conn, raw: str) -> list[int]:
        """'ivan, petya' -> [id, id]. Пустая строка — пустой список."""
        ids = []
        for login in [s.strip().lstrip("@") for s in (raw or "").split(",")]:
            if not login:
                continue
            u = store.user_by_login(conn, login)
            if u is None:
                known = ", ".join(x["login"] for x in store.list_users(conn)
                                  if not x["disabled_at"])
                raise store.StoreError(
                    f"нет пользователя «{login}» (есть: {known})")
            ids.append(u["id"])
        return ids

    def _labels_to_ids(conn, raw: str) -> list[int]:
        """'баг, UI' -> [id, id]; несуществующие метки создаются с автоцветом."""
        ids = []
        existing = {l["name"].lower(): l for l in store.kanban_labels_list(conn)}
        for name in [s.strip() for s in (raw or "").split(",")]:
            if not name:
                continue
            lab = existing.get(name.lower())
            if lab is None:
                lab = store.kanban_label_create(conn, name)
                existing[lab["name"].lower()] = lab
            ids.append(lab["id"])
        return ids

    def _fmt_card(c: dict) -> str:
        who = ", ".join(a["login"] for a in c["assignees"]) or "никто"
        line = f"  #{c['id']} {c['title']} — исполнители: {who}"
        extra = []
        if c.get("priority") and c["priority"] != "normal":
            extra.append(f"приоритет {c['priority']}")
        if c.get("due_date"):
            extra.append(f"срок {c['due_date']}")
        if c.get("labels"):
            extra.append("метки: " + ", ".join(l["name"] for l in c["labels"]))
        if c.get("checklist_total"):
            extra.append(f"чеклист {c['checklist_done']}/{c['checklist_total']}")
        if c.get("comments_count"):
            extra.append(f"комментариев {c['comments_count']}")
        if extra:
            line += " · " + ", ".join(extra)
        body = " ".join((c.get("body") or "").split())
        if body:
            line += f"\n      {body[:200]}{'…' if len(body) > 200 else ''}"
        return line

    @tool("list_cards", "Показать доску задач команды: все карточки по "
          "колонкам (backlog=Планы, todo=К работе, doing=В работе, "
          "done=Готово) с id, заголовком, описанием и исполнителями.", {})
    async def list_cards(args):
        conn = store.connect()
        try:
            data = store.kanban_list(conn)
        finally:
            conn.close()
        by_col: dict[str, list[str]] = {}
        for c in data["cards"]:
            by_col.setdefault(c["column_key"], []).append(_fmt_card(c))
        out = []
        for col in data["columns"]:
            lines = by_col.get(col["key"], [])
            out.append(f"{col['title']} ({col['key']}) — {len(lines)}:")
            out.extend(lines or ["  (пусто)"])
        return _ok("\n".join(out))

    @tool("create_card", "Завести задачу на доске. column: backlog|todo|"
          "doing|done (по умолчанию todo — передай пустую строку). "
          "assignees — логины исполнителей через запятую (можно пусто).",
          {"title": str, "body": str, "column": str, "assignees": str})
    async def create_card(args):
        conn = store.connect()
        try:
            ids = _logins_to_ids(conn, args.get("assignees") or "")
            card = store.kanban_create(
                conn, args.get("title") or "",
                body=args.get("body") or "",
                column=(args.get("column") or "").strip() or "todo",
                actor_name=store.AGENT_NAME, assignee_ids=ids)
        except store.StoreError as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"создана карточка #{card['id']} в колонке "
                   f"{card['column_key']}")

    @tool("move_card", "Перенести карточку в другую колонку: "
          "backlog|todo|doing|done.", {"card_id": str, "column": str})
    async def move_card(args):
        conn = store.connect()
        try:
            card = store.kanban_update(
                conn, int(args.get("card_id") or 0),
                column=(args.get("column") or "").strip(),
                actor_name=store.AGENT_NAME)
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"карточка #{card['id']} теперь в {card['column_key']}")

    @tool("update_card", "Изменить карточку: заголовок, описание, "
          "исполнителей (логины через запятую; «-» — снять всех), "
          "priority (low|normal|high|urgent), due (ГГГГ-ММ-ДД; «-» — снять "
          "срок), labels (имена меток через запятую, новые создаются сами; "
          "«-» — снять все). Пустая строка в поле — не менять его.",
          {"card_id": str, "title": str, "body": str, "assignees": str,
           "priority": str, "due": str, "labels": str})
    async def update_card(args):
        conn = store.connect()
        try:
            kwargs = {"actor_name": store.AGENT_NAME}
            if (args.get("title") or "").strip():
                kwargs["title"] = args["title"]
            if (args.get("body") or "").strip():
                kwargs["body"] = args["body"]
            raw = (args.get("assignees") or "").strip()
            if raw == "-":
                kwargs["assignee_ids"] = []
            elif raw:
                kwargs["assignee_ids"] = _logins_to_ids(conn, raw)
            if (args.get("priority") or "").strip():
                kwargs["priority"] = args["priority"].strip()
            due = (args.get("due") or "").strip()
            if due == "-":
                kwargs["due_date"] = None
            elif due:
                kwargs["due_date"] = due
            labs = (args.get("labels") or "").strip()
            if labs == "-":
                kwargs["label_ids"] = []
            elif labs:
                kwargs["label_ids"] = _labels_to_ids(conn, labs)
            if len(kwargs) == 1:
                return _err("нечего менять: все поля пустые")
            card = store.kanban_update(conn, int(args.get("card_id") or 0),
                                       **kwargs)
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        who = ", ".join(a["login"] for a in card["assignees"]) or "никто"
        return _ok(f"карточка #{card['id']} обновлена (исполнители: {who})")

    _KIND_RU = {"create": "создание", "move": "перенос", "title": "заголовок",
                "body": "описание", "assignees": "исполнители",
                "labels": "метки", "priority": "приоритет", "due": "срок",
                "checklist": "чеклист", "archive": "в архив",
                "restore": "из архива"}

    @tool("get_card", "Полная карточка по id: описание целиком, приоритет, "
          "срок, метки, чеклист, комментарии и последние изменения. "
          "Смотри карточку перед тем, как её менять.", {"card_id": str})
    async def get_card(args):
        conn = store.connect()
        try:
            card = store.kanban_card_full(conn, int(args.get("card_id") or 0))
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        cols = dict(store.KANBAN_COLUMNS)
        out = [f"#{card['id']} {card['title']}",
               f"колонка: {cols.get(card['column_key'], card['column_key'])}"
               f" · приоритет: {card['priority']}"
               + (f" · срок: {card['due_date']}" if card["due_date"] else "")]
        if card["labels"]:
            out.append("метки: " + ", ".join(l["name"] for l in card["labels"]))
        who = ", ".join(a["login"] for a in card["assignees"]) or "никто"
        out.append(f"исполнители: {who}")
        out.append(f"создал(а): {card['creator_name'] or 'агент'}"
                   f" · {card['created_at'][:16]}")
        if card["body"]:
            out.append("\n" + card["body"])
        if card["checklist"]:
            out.append("\nЧеклист:")
            for n, it in enumerate(card["checklist"], start=1):
                out.append(f"  {n}. [{'x' if it['done'] else ' '}] {it['text']}")
        if card["comments"]:
            out.append("\nКомментарии:")
            for c in card["comments"][-10:]:
                out.append(f"  {c['author_name'] or 'агент'}"
                           f" ({c['created_at'][:16]}): {c['text']}")
        if card["activity"]:
            out.append("\nПоследние изменения:")
            for a in card["activity"][:10]:
                what = _KIND_RU.get(a["kind"], a["kind"])
                d = a["data"]
                detail = (f" {d['from']} -> {d['to']}"
                          if "from" in d or "to" in d else "")
                out.append(f"  {a['actor_name'] or 'агент'}"
                           f" ({a['created_at'][:16]}): {what}{detail}")
        return _ok("\n".join(out))

    @tool("comment_card", "Оставить комментарий к карточке — отмечай ход "
          "работы и находки.", {"card_id": str, "text": str})
    async def comment_card(args):
        conn = store.connect()
        try:
            c = store.kanban_comment_add(
                conn, int(args.get("card_id") or 0), args.get("text") or "",
                author_name=store.AGENT_NAME)
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"комментарий к #{c['card_id']} добавлен")

    @tool("checklist_add", "Добавить пункты чеклиста к карточке "
          "(по одному на строку).", {"card_id": str, "items": str})
    async def checklist_add(args):
        lines = [s.strip() for s in (args.get("items") or "").splitlines()
                 if s.strip()]
        if not lines:
            return _err("пустой список пунктов")
        conn = store.connect()
        try:
            cid = int(args.get("card_id") or 0)
            for line in lines:
                store.kanban_check_add(conn, cid, line,
                                       actor_name=store.AGENT_NAME)
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"добавлено пунктов: {len(lines)}")

    @tool("checklist_set", "Отметить пункт чеклиста: item — номер (1, 2, …) "
          "или кусок текста пункта; done — yes|no.",
          {"card_id": str, "item": str, "done": str})
    async def checklist_set(args):
        conn = store.connect()
        try:
            card = store.kanban_card_full(conn, int(args.get("card_id") or 0))
            items = card["checklist"]
            key = (args.get("item") or "").strip()
            found = None
            if key.isdigit() and 1 <= int(key) <= len(items):
                found = items[int(key) - 1]
            else:
                hits = [it for it in items
                        if key.lower() in it["text"].lower()] if key else []
                if len(hits) == 1:
                    found = hits[0]
                elif len(hits) > 1:
                    return _err("несколько пунктов подходят: "
                                + "; ".join(h["text"] for h in hits))
            if found is None:
                return _err("пункт не найден; чеклист: "
                            + "; ".join(f"{n}. {it['text']}"
                                        for n, it in enumerate(items, 1)))
            done = (args.get("done") or "yes").strip().lower() not in (
                "no", "false", "0", "нет")
            store.kanban_check_set(conn, found["id"], done=done)
        except (store.StoreError, ValueError) as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"пункт «{found['text']}» -> {'сделан' if done else 'снят'}")

    return create_sdk_mcp_server(name="kanban", version="1.0.0",
                                 tools=[list_cards, create_card, move_card,
                                        update_card, get_card, comment_card,
                                        checklist_add, checklist_set])


def _make_threads_server(chat_id: int):
    """Наведение порядка в чате: агент раскидывает сообщения ленты по тредам
    (плоская модель — messages.thread_root_id, один уровень, как в Slack).
    Права не нужны — это тот же чат, где уже идёт разговор с агентом."""

    def _ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}]}

    def _err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _parse_ids(raw: str) -> list[int]:
        ids = []
        for tok in (raw or "").split(","):
            tok = tok.strip().lstrip("#")
            if not tok:
                continue
            if not tok.isdigit():
                raise store.StoreError(f"не похоже на id сообщения: «{tok}»")
            ids.append(int(tok))
        return ids

    @tool("list_recent", "Последние сообщения ЛЕНТЫ этого чата (сообщения "
          "внутри тредов сюда не попадают) — id, автор, время и начало "
          "текста, чтобы решить, что и куда раскидать по тредам. count — "
          "сколько сообщений строкой, по умолчанию 50, максимум 200.",
          {"count": str})
    async def list_recent(args):
        raw = (args.get("count") or "").strip()
        try:
            n = int(raw) if raw else 50
        except ValueError:
            return _err(f"count должен быть числом, получил «{raw}»")
        n = max(1, min(200, n))
        conn = store.connect()
        try:
            msgs = store.list_messages(conn, chat_id, limit=n)
        finally:
            conn.close()
        if not msgs:
            return _ok("сообщений в ленте пока нет")
        lines = []
        for m in msgs:
            if m.get("deleted_at"):
                text = "(удалено)"
            else:
                text = " ".join((m.get("text") or "").split()) or "(без текста)"
                if len(text) > 120:
                    text = text[:120] + "…"
            hhmm = (m.get("created_at") or "")[11:16]
            lines.append(f"[#{m['id']}] {m['author_name']} ({hhmm}): {text}")
        return _ok("\n".join(lines))

    @tool("move_to_thread", "Перенести сообщения ленты этого чата в тред "
          "указанного корневого сообщения (сообщение остаётся видно в "
          "ленте как корень, остальные уходят в его тред). "
          "root_message_id — id сообщения-корня (само сообщение ленты, не "
          "находящееся в другом треде). message_ids — id переносимых "
          "сообщений через запятую, например «12, 13, 14» (id смотри в "
          "list_recent). Переноси сообщения ТОЛЬКО по явной просьбе "
          "пользователя.", {"root_message_id": str, "message_ids": str})
    async def move_to_thread(args):
        conn = store.connect()
        try:
            raw_root = (args.get("root_message_id") or "").strip().lstrip("#")
            if not raw_root.isdigit():
                return _err(f"root_message_id должен быть числом,"
                            f" получил «{raw_root}»")
            root_id = int(raw_root)
            ids = _parse_ids(args.get("message_ids") or "")
            if not ids:
                return _err("не указаны message_ids")
            root = store.messages_to_thread(conn, chat_id, root_id, ids)
            n = len(ids)
            store.add_message(
                conn, chat_id, author_kind="system", author_user_id=None,
                author_name="система",
                text=f"агент перенёс {n} "
                     f"{_ru_plural(n, 'сообщение', 'сообщения', 'сообщений')}"
                     f" в тред")
        except store.StoreError as err:
            return _err(str(err))
        finally:
            conn.close()
        return _ok(f"перенесено в тред #{root['id']}: {n}"
                   f" {_ru_plural(n, 'сообщение', 'сообщения', 'сообщений')}")

    return create_sdk_mcp_server(name="threads", version="1.0.0",
                                 tools=[list_recent, move_to_thread])


def _make_guards(scopes: dict, extra_read: set):
    """write_guard: только write-корни сессии; read_guard: только read-корни
    (shared + свой personal + Unity). Пути с .git режутся всегда.
    extra_read — живой set точечных разрешений (приложенные к сообщениям файлы
    из data/uploads); каталог uploads целиком остаётся закрыт — Glob по нему
    показал бы файлы чужих чатов."""
    read_roots = [p.resolve() for p in scopes["read"]]
    write_roots = [p.resolve() for p in scopes["write"]]

    def _bad_git(p: Path) -> bool:
        return ".git" in p.parts

    async def write_guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name") or ""
        if name not in WRITE_TOOLS:
            return {}
        raw = _tool_path(input_data.get("tool_input") or {})
        if not raw:
            return _deny("не указан путь файла")
        p = Path(raw)
        if not p.is_absolute():
            p = VAULT / "shared" / p
        p = p.resolve()
        if _bad_git(p):
            return _deny(".git трогать нельзя")
        if not vaultlib.path_under(p, write_roots):
            allowed = ", ".join(str(r) for r in write_roots)
            return _deny(f"писать можно только в: {allowed}. Код игры — только чтение.")
        return {}

    async def read_guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name") or ""
        if name not in ("Read", "Grep", "Glob"):
            return {}
        raw = _tool_path(input_data.get("tool_input") or {})
        if not raw:
            return {}  # без пути инструмент работает от cwd (vault/shared)
        p = Path(raw)
        if not p.is_absolute():
            p = VAULT / "shared" / p
        p = p.resolve()
        if _bad_git(p):
            return _deny(".git читать незачем")
        if not vaultlib.path_under(p, read_roots) and p not in extra_read:
            return _deny("этот путь вне твоих областей: доступны волт (shared"
                         " и твоя personal-папка), код игры (только чтение)"
                         " и файлы, приложенные к сообщениям этого чата.")
        return {}

    return write_guard, read_guard


class TurnJob:
    def __init__(self, turn_id: int, agent_msg_id: int, prompt: str) -> None:
        self.turn_id = turn_id
        self.agent_msg_id = agent_msg_id
        self.prompt = prompt
        self.events: asyncio.Queue[Optional[dict]] = asyncio.Queue()

    async def emit(self, kind: str, **data: Any) -> None:
        await self.events.put({"type": kind, **data})

    async def close(self) -> None:
        await self.events.put(None)


class AgentSession:
    def __init__(self, chat: dict, owner_login: Optional[str],
                 system_prompt: str) -> None:
        self.chat_id = chat["id"]
        self.chat_kind = chat["kind"]
        self.owner_login = owner_login
        self.system_prompt = system_prompt
        self.client: Optional[ClaudeSDKClient] = None
        self.model_in_use: Optional[str] = None  # модель живого клиента
        self.queue: asyncio.Queue[TurnJob] = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.last_used = time.monotonic()
        self.busy = False
        # Точечные read-разрешения на приложенные файлы; set мутируется в
        # submit(), замыкание гарда видит пополнения без переподключения.
        self.extra_read: set[Path] = set()

    def _options(self, resume: Optional[str],
                 model: str) -> ClaudeAgentOptions:
        scopes = vaultlib.agent_scopes(self.chat_kind, self.owner_login)
        write_guard, read_guard = _make_guards(scopes, self.extra_read)
        add_dirs = [str(vaultlib.unity_root())]  # зеркало main, не рабочая копия
        if self.chat_kind == "agent" and self.owner_login:
            add_dirs.append(str(VAULT / "personal" / self.owner_login))
        return ClaudeAgentOptions(
            # Модель не знает своего имени сама (его нет ни в весах, ни в
            # среде) — сообщаем, иначе на «кто ты» она отвечает наугад.
            system_prompt=(self.system_prompt +
                           f"\n\nТехническая справка: ты работаешь на модели"
                           f" Claude «{model}»."),
            model=model,
            cwd=str(VAULT / "shared"),
            add_dirs=add_dirs,
            allowed_tools=["Read", "Grep", "Glob", "Edit", "Write", "TodoWrite",
                           *FS_TOOLS, *KANBAN_TOOLS, *THREAD_TOOLS],
            disallowed_tools=["Bash", "Task", "Agent", "WebSearch", "WebFetch",
                              "NotebookEdit", "ToolSearch", "SlashCommand"],
            permission_mode="acceptEdits",
            setting_sources=[],
            mcp_servers={"fs": _make_fs_server(scopes, self.extra_read),
                         "kanban": _make_kanban_server(),
                         "threads": _make_threads_server(self.chat_id)},
            hooks={"PreToolUse": [
                # matcher=None — на все инструменты; deny любого хука побеждает
                HookMatcher(matcher=None, hooks=[whitelist_guard]),
                HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[write_guard]),
                HookMatcher(matcher="Read|Grep|Glob", hooks=[read_guard]),
            ]},
            max_turns=MAX_TURNS,
            # Read картинок гонит base64 одной JSON-строкой; дефолтный лимит
            # транспорта 1 МБ роняет ход на любом крупном скриншоте.
            max_buffer_size=64 * 1024 * 1024,
            resume=resume,
        )

    async def connect(self, resume: Optional[str], model: str) -> None:
        print(setup_auth())
        print(f"[agent] чат {self.chat_id}: подключение, модель={model}")
        self.client = ClaudeSDKClient(self._options(resume, model))
        await self.client.connect()
        self.model_in_use = model

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[int, AgentSession] = {}
        self.turn_gate = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        self._janitor: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._janitor = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        for s in list(self.sessions.values()):
            if s.worker:
                s.worker.cancel()
            await s.disconnect()
        self.sessions.clear()

    # ------------------------------------------------------------- публичное

    async def submit(self, chat: dict, user_msg: dict, prompt: str,
                     extra_files: Optional[list[str]] = None) -> TurnJob:
        """Поставить ход в очередь чата. Возвращает job — его events читает
        NDJSON-стрим отправителя. extra_files — приложенные файлы, которые
        гард чтения разрешит этой сессии."""
        conn = store.connect()
        try:
            turn_id = store.create_turn(conn, chat["id"], user_msg["id"])
            agent_msg = store.add_message(
                conn, chat["id"], author_kind="agent", author_user_id=None,
                author_name=store.AGENT_NAME, text="",
                thread_root_id=user_msg.get("thread_root_id"),
                status="streaming", turn_id=turn_id,
                # На ответ (как в ТГ) агент отвечает тоже ответом.
                reply_to_id=(user_msg["id"] if user_msg.get("reply_to_id")
                             else None))
        finally:
            conn.close()
        session = self._get_session(chat)
        for f in extra_files or []:
            session.extra_read.add(Path(f).resolve())
        job = TurnJob(turn_id, agent_msg["id"], prompt)
        await session.queue.put(job)
        return job

    async def drop(self, chat_id: int) -> None:
        """Закрыть сессию чата навсегда (чат удаляется)."""
        s = self.sessions.pop(chat_id, None)
        if s is None:
            return
        if s.worker:
            s.worker.cancel()
        await s.disconnect()

    async def interrupt(self, chat_id: int) -> bool:
        s = self.sessions.get(chat_id)
        if s and s.client and s.busy:
            try:
                await s.client.interrupt()
                return True
            except Exception:
                return False
        return False

    def status(self, chat_id: int) -> dict:
        s = self.sessions.get(chat_id)
        return {"active": s is not None and s.client is not None,
                "busy": bool(s and s.busy),
                "queued": s.queue.qsize() if s else 0}

    # ------------------------------------------------------------- внутренности

    def _get_session(self, chat: dict) -> AgentSession:
        s = self.sessions.get(chat["id"])
        if s is not None:
            return s
        conn = store.connect()
        try:
            owner_login = owner_name = None
            if chat.get("owner_user_id"):
                owner = store.get_user(conn, chat["owner_user_id"])
                owner_login, owner_name = owner["login"], owner["display_name"]
            member_names = [store.get_user(conn, uid)["display_name"]
                            for uid in store.chat_member_ids(conn, chat["id"])]
        finally:
            conn.close()
        system_prompt = prompts.build_prompt(
            chat, owner_login=owner_login, owner_name=owner_name,
            members=member_names)
        s = AgentSession(chat, owner_login, system_prompt)
        s.worker = asyncio.create_task(self._worker(s))
        self.sessions[chat["id"]] = s
        return s

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for chat_id, s in list(self.sessions.items()):
                if s.busy or s.queue.qsize() or now - s.last_used < IDLE_SECONDS:
                    continue
                if s.worker:
                    s.worker.cancel()
                await s.disconnect()
                self.sessions.pop(chat_id, None)
                print(f"[agent] сессия чата {chat_id} закрыта по бездействию")

    async def _worker(self, s: AgentSession) -> None:
        while True:
            job = await s.queue.get()
            s.last_used = time.monotonic()
            async with self.turn_gate:
                s.busy = True
                try:
                    await self._run_turn(s, job)
                except Exception as err:
                    await self._fail_turn(s, job, f"{type(err).__name__}: {err}")
                finally:
                    s.busy = False
                    s.last_used = time.monotonic()

    async def _run_turn(self, s: AgentSession, job: TurnJob) -> None:
        conn = store.connect()
        try:
            resume = store.stored_sdk_session(conn, s.chat_id)
            model = store.get_chat(conn, s.chat_id).get("model") or MODEL
        finally:
            conn.close()

        # Модель чата сменили — переподключаемся; resume вернёт контекст.
        if s.client is not None and s.model_in_use != model:
            await s.disconnect()
        if s.client is None:
            try:
                await s.connect(resume, model)
            except Exception:
                if resume is None:
                    raise
                # Сохранённая SDK-сессия могла пропасть (чистка ~/.claude) —
                # одна попытка без resume.
                await s.connect(None, model)

        text_parts: list[str] = []
        tools: list[dict] = []
        touched: list[Path] = []
        sdk_session_id: Optional[str] = None
        cost_usd: Optional[float] = None
        real_model: Optional[str] = None  # из init-сообщения SDK, ground truth
        last_db_push = 0.0

        async def push_db(status: str = "streaming") -> None:
            conn = store.connect()
            try:
                store.update_message_text(conn, job.agent_msg_id,
                                          "\n\n".join(text_parts), status)
            finally:
                conn.close()

        await job.emit("start", turn=job.turn_id, message_id=job.agent_msg_id)
        try:
            await s.client.query(job.prompt)
            async for msg in s.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            text_parts.append(block.text)
                            await job.emit("text", text=block.text)
                            if time.monotonic() - last_db_push > DB_THROTTLE:
                                last_db_push = time.monotonic()
                                await push_db()
                        elif isinstance(block, ToolUseBlock):
                            entry = {"name": block.name,
                                     "input": _short_input(block.input)}
                            tools.append(entry)
                            await job.emit("tool", **entry)
                            # В БД сразу: перезагруженная посреди хода
                            # страница и наблюдатели видят 🔧 живьём.
                            conn = store.connect()
                            try:
                                store.update_turn_tools(
                                    conn, job.turn_id, tools,
                                    job.agent_msg_id)
                            finally:
                                conn.close()
                            keys: tuple[str, ...] = ()
                            if block.name in WRITE_TOOLS:
                                keys = ("file_path", "path")
                            elif block.name.endswith("copy_file"):
                                keys = ("dst",)
                            elif block.name.endswith("move_file"):
                                keys = ("src", "dst")   # src — удаление
                            elif block.name.endswith("delete_file"):
                                keys = ("path",)
                            for key in keys:
                                raw = (block.input or {}).get(key)
                                if not raw:
                                    continue
                                p = Path(raw)
                                if not p.is_absolute():
                                    p = VAULT / "shared" / p
                                touched.append(p)
                                if block.name in WRITE_TOOLS:
                                    break  # file_path ИЛИ path — один файл
                elif isinstance(msg, SystemMessage):
                    if msg.subtype == "init" and msg.data.get("model"):
                        real_model = msg.data["model"]
                        print(f"[agent] чат {s.chat_id}: ход {job.turn_id}"
                              f" на модели {real_model}")
                elif isinstance(msg, ResultMessage):
                    sdk_session_id = getattr(msg, "session_id", None)
                    cost = getattr(msg, "total_cost_usd", None)
                    if cost:
                        cost_usd = float(cost)
        except Exception as err:
            await self._fail_turn(s, job, f"{type(err).__name__}: {err}",
                                  text="\n\n".join(text_parts),
                                  sdk_session_id=sdk_session_id)
            return

        # git-коммит хода — только тронутые файлы
        commit_hash = None
        files_rel: list[str] = []
        if touched:
            uniq = list(dict.fromkeys(p.resolve() for p in touched))
            for p in uniq:
                try:
                    files_rel.append(p.relative_to(VAULT).as_posix())
                except ValueError:
                    pass
            try:
                commit_hash = await gitops.commit_paths(
                    uniq, f"agent({s.chat_id}): {job.prompt[:70]}", "agent")
            except gitops.GitError as err:
                await job.emit("error", text=f"git: {err}")

        conn = store.connect()
        try:
            store.finish_turn(conn, job.turn_id, status="done",
                              sdk_session_id=sdk_session_id, cost_usd=cost_usd,
                              files=files_rel, tools=tools,
                              commit_hash=commit_hash, model=real_model)
        finally:
            conn.close()
        await push_db(status="final")
        await job.emit("turn", turn=job.turn_id, files=files_rel,
                       commit=commit_hash, cost_usd=cost_usd)
        await job.emit("done")
        await job.close()

    async def _fail_turn(self, s: AgentSession, job: TurnJob, error: str,
                         text: str = "", sdk_session_id: Optional[str] = None) -> None:
        conn = store.connect()
        try:
            store.finish_turn(conn, job.turn_id, status="error", error=error,
                              sdk_session_id=sdk_session_id)
            store.update_message_text(
                conn, job.agent_msg_id,
                (text + f"\n\n⚠️ Ошибка агента: {error}").strip(), "error")
        finally:
            conn.close()
        await job.emit("error", text=error)
        await job.emit("done")
        await job.close()
        # Клиент после ошибки надёжнее пересоздать (resume восстановит контекст).
        await s.disconnect()


def _short_input(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text if len(text) <= limit else text[:limit] + "…"


manager = SessionManager()
