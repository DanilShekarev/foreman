"""Свой агентный цикл для OpenAI-совместимых провайдеров (#49).

Claude-движок (agent.py) отдаёт всю работу CLI: тот сам крутит tool-use,
сам держит историю (resume) и сам умеет Read/Grep/Edit. У OpenRouter и
прочих OpenAI-совместимых API ничего этого нет — цикл здесь.

Наружу отдаётся тот же поток событий, что разбирает SessionManager:
init / text / tool / result. Инструменты и права — общие с Claude-движком
(agent_tools), история хода — в таблице agent_history.

HTTP — напрямую через httpx2 (он и так стоит как зависимость Agent SDK):
клиент openai поверх httpx2 сыплет трейсами при закрытии SSE-потока, а
разбор чанков и без него в десяток строк.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx2 as httpx

import agent_tools
import store

REQUEST_TIMEOUT = float(os.environ.get("GD_LLM_TIMEOUT", "300"))
# Бюджет истории своего цикла (символы). Контекст ленты чата сервер и так
# кладёт в промпт каждого хода — это про память между ходами.
HISTORY_CHARS = int(os.environ.get("GD_HISTORY_CHARS", "40000"))
MAX_IMAGE_BYTES = 4 * 1024 * 1024
TOOL_RESULT_CHARS = 60_000
# Текст модели идёт мелкими дельтами: копим, чтобы не плодить NDJSON-строки.
TEXT_FLUSH_CHARS = 200
TEXT_FLUSH_SECONDS = 0.25
RETRY_LIMIT = 3


class LoopError(Exception):
    """Ошибка, которую не стыдно показать человеку в чате."""


class ProviderError(Exception):
    def __init__(self, status: int, detail: str,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


def is_openrouter(provider: dict) -> bool:
    return "openrouter.ai" in (provider.get("base_url") or "")


def endpoint(provider: dict, path: str) -> str:
    return (provider.get("base_url") or "").rstrip("/") + path


def headers(provider: dict, api_key: str) -> dict:
    out = {"Authorization": f"Bearer {api_key}",
           "Content-Type": "application/json"}
    if is_openrouter(provider):
        # OpenRouter просит представиться — в его статистике будет видно,
        # чей это трафик.
        out["X-Title"] = "gd-assist"
    return out


def _provider_error(status: int, raw: str) -> ProviderError:
    """Из тела ответа — понятное объяснение и, для 429, сколько ждать.
    У OpenRouter самое внятное лежит в error.metadata.raw."""
    detail, wait = raw.strip()[:500], None
    try:
        node = json.loads(raw).get("error")
    except ValueError:
        node = None
    if isinstance(node, dict):
        meta = node.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        detail = str(meta.get("raw") or node.get("message") or detail).strip()
        try:
            wait = float(meta.get("retry_after_seconds"))
        except (TypeError, ValueError):
            wait = None
    elif isinstance(node, str):
        detail = node
    return ProviderError(status, detail, wait)


def _explain(err: ProviderError) -> str:
    if err.status == 401:
        return f"провайдер не принял ключ (401): {err.detail}"
    if err.status == 402:
        return f"на счету провайдера кончились средства (402): {err.detail}"
    if err.status == 404:
        return (f"провайдер не знает такой модели (404): {err.detail}."
                f" Проверьте id модели в настройках.")
    if err.status == 429:
        return (f"провайдер просит подождать — лимит запросов (429):"
                f" {err.detail}. У бесплатных моделей он низкий: попробуйте"
                f" позже или возьмите другую модель.")
    return f"провайдер вернул ошибку {err.status}: {err.detail}"


def _tool_text(result: Any) -> str:
    """Ответ инструмента (формат MCP) → текст для роли tool."""
    if not isinstance(result, dict):
        return str(result)
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    text = "\n".join(p for p in parts if p) or "готово"
    if len(text) > TOOL_RESULT_CHARS:
        text = text[:TOOL_RESULT_CHARS] + "\n[…результат обрезан]"
    return text


def _image_part(path: str) -> Optional[dict]:
    p = Path(path)
    try:
        if not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _own_text(prompt: str) -> str:
    """В историю кладём только саму реплику человека: контекст ленты чата
    сервер подставляет заново на каждом ходу, и копии в истории быстро
    расходятся с правдой (сообщения правят и удаляют)."""
    return prompt.split("\n\n---\n", 1)[-1].strip() or prompt


async def _chunks(client: httpx.AsyncClient, url: str, head: dict,
                  payload: dict) -> AsyncIterator[dict]:
    async with client.stream("POST", url, json=payload, headers=head) as resp:
        if resp.status_code >= 400:
            raw = (await resp.aread()).decode("utf-8", "replace")
            raise _provider_error(resp.status_code, raw)
        done, tail = False, 0
        async for line in resp.aiter_lines():
            if done:
                # Хвост после «[DONE]» дочитываем: тело, брошенное на
                # середине, httpcore добивает только сборщиком мусора и
                # сыплет при этом трейсами в лог. Обычно хвоста нет вовсе,
                # но упрямый провайдер не должен держать нас вечно.
                tail += 1
                if tail > 50:
                    return
                continue
            if not line.startswith("data:"):
                continue        # пустые строки и комментарии-пинги SSE
            data = line[5:].strip()
            if data == "[DONE]":
                done = True
                continue
            try:
                yield json.loads(data)
            except ValueError:
                continue


async def run(*, chat_id: int, system_prompt: str, prompt: str,
              access: agent_tools.Access, provider: dict, api_key: str,
              model_id: str, vision: bool, max_steps: int,
              cancel: Optional[asyncio.Event] = None,
              turn_id: Optional[int] = None) -> AsyncIterator[dict]:
    """Один ход: крутим модель, пока она зовёт инструменты."""
    if not (provider.get("base_url") or "").strip():
        raise LoopError(f"у провайдера «{provider['title']}» не задан адрес API")
    if not api_key:
        raise LoopError(f"у провайдера «{provider['title']}» не задан ключ"
                        f" API — добавьте его в настройках моделей")

    tools = agent_tools.loop_tools(access, chat_id)
    by_name = {t.name: t for t in tools}
    specs = agent_tools.openai_specs(tools)

    conn = store.connect()
    try:
        history = store.history_recent(conn, chat_id, HISTORY_CHARS)
    finally:
        conn.close()

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    fresh: list[dict] = [{"role": "user", "content": _own_text(prompt)}]
    messages.append({"role": "user", "content": prompt})

    url = endpoint(provider, "/chat/completions")
    head = headers(provider, api_key)
    reported_model: Optional[str] = None
    cost_usd: Optional[float] = None

    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    try:
        for step in range(max_steps):
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            payload: dict[str, Any] = {"model": model_id, "messages": messages,
                                       "tools": specs, "stream": True}
            if is_openrouter(provider):
                # OpenRouter пришлёт в последнем чанке usage со стоимостью.
                payload["usage"] = {"include": True}

            text_parts: list[str] = []
            calls: dict[int, dict] = {}
            for attempt in range(RETRY_LIMIT):
                text_parts, calls = [], {}
                buf, buf_at, started = "", time.monotonic(), False
                try:
                    async for chunk in _chunks(client, url, head, payload):
                        started = True
                        if cancel is not None and cancel.is_set():
                            raise asyncio.CancelledError
                        usage = chunk.get("usage") or {}
                        if usage.get("cost"):
                            cost_usd = (cost_usd or 0.0) + float(usage["cost"])
                        if reported_model is None and chunk.get("model"):
                            reported_model = chunk["model"]
                            yield {"type": "init", "model": reported_model}
                        choices = chunk.get("choices") or []
                        delta = (choices[0].get("delta") or {}) if choices else {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            buf += piece
                            if (len(buf) >= TEXT_FLUSH_CHARS
                                    or time.monotonic() - buf_at
                                    >= TEXT_FLUSH_SECONDS):
                                yield {"type": "text", "text": buf}
                                buf, buf_at = "", time.monotonic()
                        for tc in delta.get("tool_calls") or []:
                            slot = calls.setdefault(
                                tc.get("index", 0),
                                {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
                    if buf:
                        yield {"type": "text", "text": buf}
                    break
                except ProviderError as err:
                    # Бесплатные модели упираются в общий пул провайдера и
                    # просят подождать пару секунд — это не повод ронять ход.
                    retriable = (err.status == 429 and not started
                                 and attempt < RETRY_LIMIT - 1)
                    if not retriable:
                        raise LoopError(_explain(err)) from err
                    wait = min(err.retry_after or 5.0, 15.0)
                    print(f"[llm] чат {chat_id}: лимит провайдера,"
                          f" жду {wait:.0f} с")
                    await asyncio.sleep(wait)
                except httpx.HTTPError as err:
                    raise LoopError(f"не достучаться до провайдера:"
                                    f" {type(err).__name__}: {err}") from err

            text = "".join(text_parts)
            ordered = [calls[i] for i in sorted(calls)]
            assistant: dict[str, Any] = {"role": "assistant", "content": text}
            if ordered:
                assistant["tool_calls"] = [
                    {"id": c["id"] or f"call_{step}_{n}", "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": c["args"] or "{}"}}
                    for n, c in enumerate(ordered)]
            messages.append(assistant)
            fresh.append(assistant)

            if not ordered:
                break

            images: list[dict] = []
            for n, c in enumerate(ordered):
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError
                name = c["name"].strip()
                try:
                    args = json.loads(c["args"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("ожидался объект с аргументами")
                except ValueError as err:
                    args, out = {}, f"аргументы не разобрались как JSON: {err}"
                else:
                    yield {"type": "tool", "name": name, "input": args}
                    t = by_name.get(name)
                    if t is None:
                        out = (f"нет инструмента «{name}»; доступны: "
                               + ", ".join(by_name))
                    else:
                        try:
                            result = await t.handler(args)
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:  # инструмент не роняет ход
                            out = f"инструмент упал: {type(err).__name__}: {err}"
                        else:
                            out = _tool_text(result)
                            img = (result.get("image_path")
                                   if isinstance(result, dict) else None)
                            if img and vision:
                                part = _image_part(img)
                                if part is not None:
                                    images.append(part)
                            elif img:
                                out += ("\n(эта модель не умеет смотреть"
                                        " картинки — опиши задачу словами)")
                tool_msg = {"role": "tool",
                            "tool_call_id": assistant["tool_calls"][n]["id"],
                            "content": out}
                messages.append(tool_msg)
                fresh.append(tool_msg)

            if images:
                # Картинку в роль tool класть нельзя — отдаём отдельной
                # репликой. В историю она не идёт: base64 съест весь бюджет.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Вот запрошенные изображения."},
                    *images]})
        else:
            yield {"type": "text",
                   "text": f"\n\n_Остановился: {max_steps} шагов подряд с"
                           f" инструментами. Напишите, что делать дальше._"}
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
        if len(fresh) > 1:
            conn = store.connect()
            try:
                store.history_append(conn, chat_id, turn_id, fresh)
            finally:
                conn.close()

    yield {"type": "result", "model": reported_model, "cost_usd": cost_usd}
