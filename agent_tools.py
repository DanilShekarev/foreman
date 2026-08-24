"""Инструменты агента — одна реализация на оба движка (#49).

Claude-движок заворачивает их в SDK-MCP-серверы и отдаёт CLI, свой цикл
(agent_openai) зовёт handler напрямую, а схемы отдаёт модели как
function-calling. Поэтому тут нет ничего, специфичного для транспорта:
описания инструментов — это @tool из claude_agent_sdk, то есть обычный
dataclass с name/description/input_schema/handler.

Права путей — те же, что у PreToolUse-гардов CLI: читать можно read-корни
сессии плюс приложенные к сообщениям файлы, писать — только write-корни,
.git закрыт всегда.
"""
from __future__ import annotations

import fnmatch
import json
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

import store
import vaultlib
from vaultlib import VAULT

# Сколько отдавать модели за раз: транспорт SDK — 1 МБ на строку JSON, а
# чужие модели ещё и платят за каждый символ контекста.
READ_MAX_LINES = 1200
READ_MAX_CHARS = 120_000
GREP_MAX_HITS = 200
GLOB_MAX_FILES = 300


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """1 сообщение / 2 сообщения / 5 сообщений."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return few
    return many


class Access:
    """Права сессии на файлы: read-корни, write-корни и точечные разрешения
    на приложенные к сообщениям файлы (extra_read — живой set, пополняется
    из SessionManager.submit без переподключения)."""

    def __init__(self, scopes: dict, extra_read: set) -> None:
        self.read_roots = [p.resolve() for p in scopes["read"]]
        self.write_roots = [p.resolve() for p in scopes["write"]]
        self.extra_read = extra_read

    def resolve(self, raw: str) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = VAULT / "shared" / p
        return p.resolve()

    def readable(self, p: Path) -> bool:
        return (".git" not in p.parts
                and (vaultlib.path_under(p, self.read_roots)
                     or p in self.extra_read))

    def writable(self, p: Path) -> bool:
        return ".git" not in p.parts and vaultlib.path_under(p, self.write_roots)


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def fs_tools(access: Access) -> list[SdkMcpTool]:
    """Байтовые файловые операции: Write пишет только текст, который агент сам
    формирует, поэтому картинку или docx он без этих тулзов не перенесёт."""
    write_roots = access.write_roots
    _resolve, _readable, _writable = (access.resolve, access.readable,
                                      access.writable)

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

    @tool("move_file", "Переместить или переименовать файл или папку внутри "
          "твоих областей записи.", {"src": str, "dst": str})
    async def move_file(args):
        src, dst = _resolve(args["src"]), _resolve(args["dst"])
        if not _writable(src):
            return _err(f"src вне твоих областей записи: {src}")
        if not src.exists():
            return _err(f"src не существует: {src}")
        if src in write_roots:
            return _err(f"корневую папку области записи двигать нельзя: {src}")
        if not _writable(dst):
            return _err(f"dst вне твоих областей записи: {dst}")
        if src.is_dir() and (dst == src or src in dst.parents):
            return _err(f"нельзя переместить папку внутрь самой себя: {dst}")
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

    @tool("delete_dir", "Удалить папку со всем содержимым в твоих областях "
          "записи (git сохраняет историю файлов, удаление откатываемо).",
          {"path": str})
    async def delete_dir(args):
        p = _resolve(args["path"])
        if not _writable(p):
            return _err(f"путь вне твоих областей записи: {p}")
        if p in write_roots:
            return _err(f"корневую папку области записи удалять нельзя: {p}")
        if not p.is_dir():
            return _err(f"не папка или не существует: {p}")
        shutil.rmtree(p)
        return _ok(f"папка удалена: {p}")

    return [copy_file, move_file, delete_file, delete_dir]


def kanban_tools() -> list[SdkMcpTool]:
    """Доска задач команды: агент видит и ведёт её наравне с людьми.
    Одна общая доска, права не нужны — её видят все пользователи."""

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

    return [list_cards, create_card, move_card, update_card, get_card,
            comment_card, checklist_add, checklist_set]


def thread_tools(chat_id: int) -> list[SdkMcpTool]:
    """Наведение порядка в чате: агент раскидывает сообщения ленты по тредам
    (плоская модель — messages.thread_root_id, один уровень, как в Slack).
    Права не нужны — это тот же чат, где уже идёт разговор с агентом."""

    def _parse_ids(raw: str) -> list[int]:
        ids = []
        for tok in (raw or "").split(","):
            # id сообщений подписаны как m123 (см. prompts.channel_context);
            # решётку и «m» принимаем и срезаем — агенту проще.
            tok = tok.strip().lstrip("#m")
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
            # m123, не #123: решётка в чате зарезервирована под ссылки на
            # задачи канбана.
            lines.append(f"[m{m['id']}] {m['author_name']} ({hhmm}): {text}")
        return _ok("\n".join(lines))

    @tool("read_thread", "Прочитать тред целиком: корень и все ответы, "
          "полными текстами. Годится для длинных тредов и расшифровок "
          "созвонов (тред под системным «Звонок завершён»). "
          "root_message_id — id корневого сообщения ленты (в ленте и "
          "list_recent это m123; можно передать id любого ответа — корень "
          "найдётся сам).", {"root_message_id": str})
    async def read_thread(args):
        raw_root = (args.get("root_message_id") or "").strip().lstrip("#m")
        if not raw_root.isdigit():
            return _err(f"root_message_id должен быть числом,"
                        f" получил «{raw_root}»")
        conn = store.connect()
        try:
            root = store.get_message(conn, int(raw_root))
            if root["chat_id"] != chat_id:
                return _err("это сообщение из другого чата")
            if root.get("thread_root_id"):
                root = store.get_message(conn, root["thread_root_id"])
            msgs = store.list_messages(conn, chat_id, limit=2000,
                                       thread_root_id=root["id"])
        except store.StoreError as err:
            return _err(str(err))
        finally:
            conn.close()
        lines = [f"[m{root['id']}] (корень) {root['author_name']}:"
                 f" {root.get('text') or '(без текста)'}"]
        for m in msgs:
            text = ("(удалено)" if m.get("deleted_at")
                    else (m.get("text") or "(без текста)"))
            lines.append(f"[m{m['id']}] {m['author_name']}: {text}")
        out = "\n".join(lines)
        # Страховка от простыней: транспорт SDK — 1 МБ на строку JSON.
        if len(out) > 400_000:
            out = out[:400_000] + "\n[…тред обрезан, показано начало]"
        if not msgs:
            out += "\n(ответов в треде нет)"
        return _ok(out)

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
            raw_root = (args.get("root_message_id") or "").strip().lstrip("#m")
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

    return [list_recent, read_thread, move_to_thread]



def _int(raw: Any, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def native_tools(access: Access) -> list[SdkMcpTool]:
    """То, что у CLI встроено, а своему циклу приходится давать самому:
    чтение, поиск и правка файлов. Имена и ключи аргументов совпадают с
    инструментами Claude Code — их знает и промпт, и разбор ходов в чате."""

    async def read_file(args):
        raw = (args.get("file_path") or "").strip()
        if not raw:
            return _err("не указан file_path")
        p = access.resolve(raw)
        if not access.readable(p):
            return _err(f"путь вне доступных тебе областей: {p}")
        if p.is_dir():
            return _err(f"это папка, а не файл: {p} (список файлов — Glob)")
        if not p.is_file():
            return _err(f"файл не найден: {p}")
        if p.suffix.lower() in vaultlib.IMG_EXT:
            # Картинку в роль tool положить нельзя — движок приложит её
            # отдельным сообщением, если модель зрячая.
            return {"content": [{"type": "text",
                                 "text": f"изображение {p.name}"}],
                    "image_path": str(p)}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            return _err(f"файл не читается: {err}")
        lines = text.splitlines()
        offset = max(0, _int(args.get("offset"), 0))
        limit = min(max(1, _int(args.get("limit"), READ_MAX_LINES)),
                    READ_MAX_LINES)
        chunk = lines[offset:offset + limit]
        if not chunk:
            return _ok("(пусто: файл короче, чем offset)" if offset
                       else "(пустой файл)")
        body = "\n".join(f"{offset + i + 1:6d}\t{ln}"
                         for i, ln in enumerate(chunk))
        if len(body) > READ_MAX_CHARS:
            body = body[:READ_MAX_CHARS] + "\n[…дальше обрезано]"
        shown = offset + len(chunk)
        if shown < len(lines):
            body += (f"\n[…показаны строки {offset + 1}–{shown}"
                     f" из {len(lines)}; дальше — Read с offset={shown}]")
        return _ok(body)

    async def glob_files(args):
        pattern = (args.get("pattern") or "").strip() or "**/*"
        root = access.resolve((args.get("path") or "").strip()
                              or str(VAULT / "shared"))
        if not access.readable(root):
            return _err(f"путь вне доступных тебе областей: {root}")
        if not root.is_dir():
            return _err(f"не папка: {root}")
        try:
            found = list(root.glob(pattern))
        except (ValueError, OSError) as err:
            return _err(f"негодный шаблон «{pattern}»: {err}")
        files = []
        for f in found:
            try:
                if f.is_file() and access.readable(f.resolve()):
                    files.append((f.stat().st_mtime, f))
            except OSError:
                continue
        files.sort(key=lambda x: x[0], reverse=True)
        if not files:
            return _ok(f"ничего не найдено по «{pattern}» в {root}")
        head = files[:GLOB_MAX_FILES]
        out = "\n".join(str(f) for _, f in head)
        if len(files) > len(head):
            out += f"\n[…всего {len(files)}, показаны первые {len(head)}]"
        return _ok(out)

    async def grep(args):
        raw_pat = args.get("pattern") or ""
        if not raw_pat:
            return _err("не указан pattern")
        try:
            rx = re.compile(raw_pat, re.IGNORECASE)
        except re.error as err:
            return _err(f"негодное регулярное выражение: {err}")
        target = access.resolve((args.get("path") or "").strip()
                                or str(VAULT / "shared"))
        if not access.readable(target):
            return _err(f"путь вне доступных тебе областей: {target}")
        mask = (args.get("glob") or "").strip()
        if target.is_file():
            candidates = [target]
        elif target.is_dir():
            candidates = [f for f in target.rglob("*") if f.is_file()]
        else:
            return _err(f"путь не найден: {target}")
        hits, scanned = [], 0
        for f in candidates:
            if len(hits) >= GREP_MAX_HITS:
                break
            if mask and not fnmatch.fnmatch(f.name, mask):
                continue
            if f.suffix.lower() in vaultlib.IMG_EXT:
                continue
            try:
                if (not access.readable(f.resolve())
                        or f.stat().st_size > 2_000_000):
                    continue
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    hits.append(f"{f}:{n}: {line.strip()[:300]}")
                    if len(hits) >= GREP_MAX_HITS:
                        break
        if not hits:
            return _ok(f"совпадений нет (просмотрено файлов: {scanned})")
        out = "\n".join(hits)
        if len(hits) >= GREP_MAX_HITS:
            out += f"\n[…показаны первые {GREP_MAX_HITS} совпадений]"
        return _ok(out)

    async def edit_file(args):
        p = access.resolve((args.get("file_path") or "").strip())
        if not access.writable(p):
            allowed = ", ".join(str(r) for r in access.write_roots)
            return _err(f"писать можно только в: {allowed}."
                        f" Код игры — только чтение.")
        if not p.is_file():
            return _err(f"файл не найден: {p} (новый файл создаёт Write)")
        old = args.get("old_string") or ""
        new = args.get("new_string") or ""
        if not old:
            return _err("old_string пустой: чтобы создать или переписать файл"
                        " целиком, используй Write")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as err:
            return _err(f"файл не читается: {err}")
        count = text.count(old)
        if count == 0:
            return _err("такого фрагмента в файле нет — перечитай файл Read и"
                        " скопируй кусок точно, вместе с отступами")
        replace_all = str(args.get("replace_all") or "").strip().lower() in (
            "1", "true", "yes", "да")
        if count > 1 and not replace_all:
            return _err(f"фрагмент встречается {count} раз(а): возьми кусок"
                        f" подлиннее или передай replace_all=true")
        try:
            p.write_text(text.replace(old, new) if replace_all
                         else text.replace(old, new, 1),
                         encoding="utf-8", newline="\n")
        except OSError as err:
            return _err(f"файл не записывается: {err}")
        return _ok(f"файл изменён: {p} (замен: {count if replace_all else 1})")

    async def write_file(args):
        p = access.resolve((args.get("file_path") or "").strip())
        if not access.writable(p):
            allowed = ", ".join(str(r) for r in access.write_roots)
            return _err(f"писать можно только в: {allowed}."
                        f" Код игры — только чтение.")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.get("content") or "", encoding="utf-8",
                         newline="\n")
        except OSError as err:
            return _err(f"файл не записывается: {err}")
        return _ok(f"файл записан: {p}")

    return [
        SdkMcpTool(
            name="Read",
            description="Прочитать файл (текст возвращается с номерами"
                        " строк). file_path — абсолютный путь или путь"
                        " относительно shared-папки волта. offset/limit —"
                        " с какой строки и сколько строк читать, если файл"
                        " большой.",
            input_schema=_obj({"file_path": {"type": "string"},
                               "offset": {"type": "integer"},
                               "limit": {"type": "integer"}}, ["file_path"]),
            handler=read_file),
        SdkMcpTool(
            name="Glob",
            description="Найти файлы по шаблону пути, например «**/*.md» или"
                        " «механики/*.md». path — где искать (по умолчанию"
                        " shared-папка волта). Свежие файлы идут первыми.",
            input_schema=_obj({"pattern": {"type": "string"},
                               "path": {"type": "string"}}, ["pattern"]),
            handler=glob_files),
        SdkMcpTool(
            name="Grep",
            description="Поиск по содержимому файлов регулярным выражением"
                        " (регистр не важен). path — файл или папка (по"
                        " умолчанию shared-папка волта), glob — маска имени"
                        " файла, например «*.cs». Возвращает путь, номер"
                        " строки и саму строку.",
            input_schema=_obj({"pattern": {"type": "string"},
                               "path": {"type": "string"},
                               "glob": {"type": "string"}}, ["pattern"]),
            handler=grep),
        SdkMcpTool(
            name="Edit",
            description="Заменить в файле точный фрагмент old_string на"
                        " new_string. Фрагмент должен встречаться один раз —"
                        " бери кусок с запасом по контексту; replace_all=true"
                        " заменит все вхождения. Перед правкой прочитай файл"
                        " Read: его могли изменить параллельно.",
            input_schema=_obj({"file_path": {"type": "string"},
                               "old_string": {"type": "string"},
                               "new_string": {"type": "string"},
                               "replace_all": {"type": "string"}},
                              ["file_path", "old_string", "new_string"]),
            handler=edit_file),
        SdkMcpTool(
            name="Write",
            description="Записать файл целиком (создаёт новый или полностью"
                        " перезаписывает существующий). Существующий файл"
                        " сначала прочитай Read — иначе затрёшь чужие правки.",
            input_schema=_obj({"file_path": {"type": "string"},
                               "content": {"type": "string"}},
                              ["file_path", "content"]),
            handler=write_file),
    ]


_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}


def json_schema(t: SdkMcpTool) -> dict:
    """Схема инструмента в JSON Schema. У MCP-инструментов она задана словарём
    питоновских типов ({"src": str}), у нативных — уже готовой схемой."""
    schema = t.input_schema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    props, required = {}, []
    for name, typ in (schema or {}).items():
        props[name] = {"type": _PY_TO_JSON.get(typ, "string")}
        required.append(name)
    return _obj(props, required)


def openai_specs(tools: list[SdkMcpTool]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": json_schema(t)}} for t in tools]


def mcp_servers(access: Access, chat_id: int) -> dict:
    """Инструменты для Claude-движка: три in-process MCP-сервера."""
    return {
        "fs": create_sdk_mcp_server(name="fs", version="1.0.0",
                                    tools=fs_tools(access)),
        "kanban": create_sdk_mcp_server(name="kanban", version="1.0.0",
                                        tools=kanban_tools()),
        "threads": create_sdk_mcp_server(name="threads", version="1.0.0",
                                         tools=thread_tools(chat_id)),
    }


def loop_tools(access: Access, chat_id: int) -> list[SdkMcpTool]:
    """Инструменты для своего цикла: нативные плюс те же MCP-шные, одним
    списком — цикл зовёт их handler напрямую."""
    return [*native_tools(access), *fs_tools(access), *kanban_tools(),
            *thread_tools(chat_id)]
