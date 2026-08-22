"""Агент-сессии: по одной на чат, живут в процессе сервера.

Паттерн из voxels/agent_runner.py, но сессий много (словарь chat_id -> Session).
Ход считает один из двух движков — какой, решает провайдер выбранной моделью
(llm.py):

* claude — ClaudeSDKClient, подпроцесс CLI. Создаётся лениво при первом
  сообщении с resume сохранённого sdk_session_id, закрывается по
  idle-таймауту (session_id остаётся в БД — следующее сообщение делает resume).
  Права — ТОЛЬКО PreToolUse-хуками: запись в allowed_tools авторазрешает вызов
  до can_use_tool (CanUseToolShadowedWarning), поэтому колбэк-гард молча не
  работает. Bash запрещён целиком, git-операции делает сервер.
* openai — свой цикл (agent_openai) поверх OpenAI-совместимого API; права там
  проверяют сами инструменты (agent_tools.Access), хуков в этом пути нет.

Всё, что после движка — сбор текста, список инструментов, git-коммит хода,
события NDJSON — общее.
"""
from __future__ import annotations

import asyncio
import json
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
)

import agent_openai
import agent_tools
import gitops
import llm
import prompts
import store
import vaultlib
from common import setup_auth
from vaultlib import VAULT

import os

MAX_TURNS = int(os.environ.get("GD_MAX_TURNS", "100"))
IDLE_SECONDS = 30 * 60          # закрыть молчащий клиент (resume вернёт контекст)
MAX_CONCURRENT_TURNS = 3        # глобальный предел одновременных ходов
DB_THROTTLE = 1.5               # сек между апдейтами стримящегося текста в БД
# Верхний предел ВЫБОРКИ из БД; реальный объём режет prompts.trim_context
# по бюджету символов (GD_CONTEXT_CHARS).
CHANNEL_CONTEXT_MESSAGES = 100

WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
FS_TOOLS = ("mcp__fs__copy_file", "mcp__fs__move_file", "mcp__fs__delete_file",
            "mcp__fs__delete_dir")
KANBAN_TOOLS = ("mcp__kanban__list_cards", "mcp__kanban__create_card",
                "mcp__kanban__move_card", "mcp__kanban__update_card",
                "mcp__kanban__get_card", "mcp__kanban__comment_card",
                "mcp__kanban__checklist_add", "mcp__kanban__checklist_set")
THREAD_TOOLS = ("mcp__threads__list_recent", "mcp__threads__read_thread",
                "mcp__threads__move_to_thread")
# Deny-by-default: CLI-харнес объявляет агенту множество своих инструментов
# (ToolSearch, Cron*, SendMessage, Task* и т.п.) — всё, чего нет в этом
# белом списке, режется гардом до выполнения.
SAFE_TOOLS = frozenset(WRITE_TOOLS) | set(FS_TOOLS) | set(KANBAN_TOOLS) | set(
    THREAD_TOOLS) | {"Read", "Grep", "Glob", "TodoWrite"}


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


def _make_guards(access: agent_tools.Access):
    """Гарды CLI: те же права, что у инструментов своего цикла — write-корни
    сессии на запись, read-корни (shared + свой personal + Unity) и точечные
    разрешения на приложенные файлы на чтение. Каталог uploads целиком
    остаётся закрыт: Glob по нему показал бы файлы чужих чатов."""

    async def write_guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name") or ""
        if name not in WRITE_TOOLS:
            return {}
        raw = _tool_path(input_data.get("tool_input") or {})
        if not raw:
            return _deny("не указан путь файла")
        p = access.resolve(raw)
        if ".git" in p.parts:
            return _deny(".git трогать нельзя")
        if not access.writable(p):
            allowed = ", ".join(str(r) for r in access.write_roots)
            return _deny(f"писать можно только в: {allowed}. Код игры — только чтение.")
        return {}

    async def read_guard(input_data, tool_use_id, context):
        name = input_data.get("tool_name") or ""
        if name not in ("Read", "Grep", "Glob"):
            return {}
        raw = _tool_path(input_data.get("tool_input") or {})
        if not raw:
            return {}  # без пути инструмент работает от cwd (vault/shared)
        p = access.resolve(raw)
        if ".git" in p.parts:
            return _deny(".git читать незачем")
        if not access.readable(p):
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
        # Прерывание своего цикла: у CLI для этого есть client.interrupt().
        self.cancel = asyncio.Event()
        # Точечные read-разрешения на приложенные файлы; set мутируется в
        # submit(), замыкание гарда видит пополнения без переподключения.
        self.extra_read: set[Path] = set()

    def access(self) -> agent_tools.Access:
        return agent_tools.Access(
            vaultlib.agent_scopes(self.chat_kind, self.owner_login),
            self.extra_read)

    def prompt_for(self, title: str) -> str:
        # Модель не знает своего имени сама (его нет ни в весах, ни в среде) —
        # сообщаем, иначе на «кто ты» она отвечает наугад.
        return (self.system_prompt + f"\n\nТехническая справка: ты работаешь"
                                     f" на модели «{title}».")

    def _options(self, resume: Optional[str],
                 model: str) -> ClaudeAgentOptions:
        access = self.access()
        write_guard, read_guard = _make_guards(access)
        add_dirs = [str(vaultlib.unity_root())]  # зеркало main, не рабочая копия
        if self.chat_kind == "agent" and self.owner_login:
            add_dirs.append(str(VAULT / "personal" / self.owner_login))
        return ClaudeAgentOptions(
            system_prompt=self.prompt_for(f"Claude {model}"),
            model=model,
            cwd=str(VAULT / "shared"),
            add_dirs=add_dirs,
            allowed_tools=["Read", "Grep", "Glob", "Edit", "Write", "TodoWrite",
                           *FS_TOOLS, *KANBAN_TOOLS, *THREAD_TOOLS],
            disallowed_tools=["Bash", "Task", "Agent", "WebSearch", "WebFetch",
                              "NotebookEdit", "ToolSearch", "SlashCommand"],
            permission_mode="acceptEdits",
            setting_sources=[],
            mcp_servers=agent_tools.mcp_servers(access, self.chat_id),
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
        if not (s and s.busy):
            return False
        if s.client is not None:
            try:
                await s.client.interrupt()
                return True
            except Exception:
                return False
        # Свой цикл смотрит флаг между чанками ответа и вызовами инструментов.
        s.cancel.set()
        return True

    def status(self, chat_id: int) -> dict:
        s = self.sessions.get(chat_id)
        return {"active": s is not None,
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

    async def _claude_stream(self, s: AgentSession, job: TurnJob,
                             model: str) -> AsyncIterator[dict]:
        """Ход на CLI: сообщения SDK переводятся в общие события движка."""
        conn = store.connect()
        try:
            resume = store.stored_sdk_session(conn, s.chat_id)
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

        await s.client.query(job.prompt)
        async for msg in s.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif isinstance(block, ToolUseBlock):
                        yield {"type": "tool", "name": block.name,
                               "input": block.input or {}}
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "init" and msg.data.get("model"):
                    yield {"type": "init", "model": msg.data["model"]}
            elif isinstance(msg, ResultMessage):
                cost = getattr(msg, "total_cost_usd", None)
                yield {"type": "result",
                       "session_id": getattr(msg, "session_id", None),
                       "cost_usd": float(cost) if cost else None}

    async def _engine_stream(self, s: AgentSession, job: TurnJob,
                             picked: dict) -> AsyncIterator[dict]:
        if picked["kind"] == "claude":
            async for ev in self._claude_stream(s, job, picked["model_id"]):
                yield ev
            return
        # Подписочный CLI и чужой провайдер в одной сессии не уживаются:
        # клиент держит свою модель и свой контекст.
        await s.disconnect()
        provider = picked["provider"]
        title = f"{picked['title']} (провайдер {provider['title']})"
        async for ev in agent_openai.run(
                chat_id=s.chat_id, system_prompt=s.prompt_for(title),
                prompt=job.prompt, access=s.access(), provider=provider,
                api_key=llm.api_key(provider), model_id=picked["model_id"],
                vision=picked["vision"], max_steps=MAX_TURNS,
                cancel=s.cancel, turn_id=job.turn_id):
            # Текст своего цикла идёт дельтами: склеивать их абзацным
            # отступом, как блоки Claude, нельзя — порвёт слова.
            yield {**ev, "chunk": True} if ev.get("type") == "text" else ev

    async def _run_turn(self, s: AgentSession, job: TurnJob) -> None:
        conn = store.connect()
        try:
            chat = store.get_chat(conn, s.chat_id)
            picked = llm.resolve_for_chat(conn, chat.get("model"))
        finally:
            conn.close()
        if picked is None:
            await self._fail_turn(
                s, job, "не настроено ни одной модели — админ заводит"
                        " провайдеров в настройках")
            return
        s.cancel.clear()

        text = ""
        tools: list[dict] = []
        touched: list[Path] = []
        sdk_session_id: Optional[str] = None
        cost_usd: Optional[float] = None
        real_model: Optional[str] = None  # что ответил провайдер, ground truth
        last_db_push = 0.0

        async def push_db() -> None:
            conn = store.connect()
            try:
                store.update_message_text(conn, job.agent_msg_id, text,
                                          "streaming")
            finally:
                conn.close()

        await job.emit("start", turn=job.turn_id, message_id=job.agent_msg_id)
        try:
            async for ev in self._engine_stream(s, job, picked):
                kind = ev.get("type")
                if kind == "text":
                    piece, chunk = ev["text"], bool(ev.get("chunk"))
                    text += ("" if chunk or not text else "\n\n") + piece
                    await job.emit("text", text=piece, chunk=chunk)
                    if time.monotonic() - last_db_push > DB_THROTTLE:
                        last_db_push = time.monotonic()
                        await push_db()
                elif kind == "tool":
                    data = ev.get("input") or {}
                    entry = {"name": ev["name"], "input": _short_input(data)}
                    tools.append(entry)
                    await job.emit("tool", **entry)
                    # В БД сразу: перезагруженная посреди хода страница и
                    # наблюдатели видят 🔧 живьём.
                    conn = store.connect()
                    try:
                        store.update_turn_tools(conn, job.turn_id, tools,
                                                job.agent_msg_id)
                    finally:
                        conn.close()
                    touched.extend(_touched_paths(ev["name"], data))
                elif kind == "init":
                    real_model = ev.get("model")
                    print(f"[agent] чат {s.chat_id}: ход {job.turn_id}"
                          f" на модели {real_model}")
                elif kind == "result":
                    sdk_session_id = ev.get("session_id") or sdk_session_id
                    real_model = real_model or ev.get("model")
                    if ev.get("cost_usd"):
                        cost_usd = float(ev["cost_usd"])
        except asyncio.CancelledError:
            if not s.cancel.is_set():
                raise  # это гасят сессию, а не жмут «стоп»
            await self._settle(s, job, status="interrupted", text=text,
                               tools=tools, touched=touched,
                               real_model=real_model,
                               note="\n\n_(остановлено)_")
            return
        except agent_openai.LoopError as err:
            await self._fail_turn(s, job, str(err), text=text)
            return
        except Exception as err:
            await self._fail_turn(s, job, f"{type(err).__name__}: {err}",
                                  text=text, sdk_session_id=sdk_session_id)
            return

        await self._settle(s, job, status="done", text=text, tools=tools,
                           touched=touched, sdk_session_id=sdk_session_id,
                           cost_usd=cost_usd, real_model=real_model)

    async def _settle(self, s: AgentSession, job: TurnJob, *, status: str,
                      text: str, tools: list[dict], touched: list[Path],
                      sdk_session_id: Optional[str] = None,
                      cost_usd: Optional[float] = None,
                      real_model: Optional[str] = None,
                      note: str = "") -> None:
        """Закрыть ход: коммит тронутых файлов, запись в БД, события клиенту.
        Прерванный ход закрывается так же — правки уже на диске, и без
        коммита их нечем было бы откатить."""
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
            store.finish_turn(conn, job.turn_id, status=status,
                              sdk_session_id=sdk_session_id, cost_usd=cost_usd,
                              files=files_rel, tools=tools,
                              commit_hash=commit_hash, model=real_model)
            store.update_message_text(conn, job.agent_msg_id,
                                      (text + note).strip(), "final")
        finally:
            conn.close()
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


def _touched_paths(name: str, data: dict) -> list[Path]:
    """Файлы, которые ход мог изменить: их и коммитим. Имена приходят и от
    CLI (mcp__fs__copy_file), и от своего цикла (copy_file) — сверяем хвост."""
    keys: tuple[str, ...] = ()
    if name in WRITE_TOOLS:
        keys = ("file_path", "path")
    elif name.endswith("copy_file"):
        keys = ("dst",)
    elif name.endswith("move_file"):
        keys = ("src", "dst")   # src — удаление
    elif name.endswith(("delete_file", "delete_dir")):
        keys = ("path",)
    out: list[Path] = []
    for key in keys:
        raw = data.get(key)
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = VAULT / "shared" / p
        out.append(p)
        if name in WRITE_TOOLS:
            break  # file_path ИЛИ path — один файл
    return out


def _short_input(data: Any, limit: int = 300) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return text if len(text) <= limit else text[:limit] + "…"


manager = SessionManager()
