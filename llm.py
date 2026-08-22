"""Реестр провайдеров LLM (#49).

Провайдер — это способ добраться до модели: `claude` (подписочный CLI через
Agent SDK) или любой OpenAI-совместимый эндпоинт (OpenRouter, локальный
сервер). Живут в таблице llm_providers, правит их админ через UI.

Модель чата хранится в chats.model строкой «ключ-провайдера:id-модели».
Разделяется по ПЕРВОМУ двоеточию: id моделей OpenRouter сами содержат
двоеточие («z-ai/glm-5.2:free»). Значение без двоеточия — из времён, когда
провайдер был один, и читается как claude.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

import store

CLAUDE_KEY = "claude"
# Дефолтные модели провайдера claude: те же алиасы CLI, что были до #49.
_SEED_CLAUDE = os.environ.get("GD_MODELS", "sonnet,opus,haiku")
_SEED_DEFAULT = os.environ.get("GD_MODEL", "sonnet")


def parse_choice(choice: str) -> tuple[str, str]:
    raw = (choice or "").strip()
    if not raw:
        return CLAUDE_KEY, ""
    key, sep, model = raw.partition(":")
    if not sep:
        return CLAUDE_KEY, raw
    return key.strip().lower(), model.strip()


def make_choice(provider_key: str, model_id: str) -> str:
    return f"{provider_key}:{model_id}"


def catalog(conn: sqlite3.Connection) -> list[dict]:
    """Что показывать в селекторе чата: только включённые провайдеры,
    у которых есть хотя бы одна модель."""
    out = []
    for p in store.llm_providers(conn):
        if not p["enabled"] or not p["models"]:
            continue
        out.append({
            "key": p["key"], "title": p["title"], "kind": p["kind"],
            "models": [{"value": make_choice(p["key"], m["id"]),
                        "id": m["id"], "title": m.get("title") or m["id"]}
                       for m in p["models"]],
        })
    return out


def resolve(conn: sqlite3.Connection, choice: str) -> Optional[dict]:
    """Выбор → чем и на чём считать. None, если провайдера/модели больше нет."""
    key, model_id = parse_choice(choice)
    if not model_id:
        return None
    p = store.llm_provider_by_key(conn, key)
    if p is None or not p["enabled"]:
        return None
    model = next((m for m in p["models"] if m["id"] == model_id), None)
    if model is None:
        return None
    return {"provider": p, "kind": p["kind"], "model_id": model_id,
            "title": model.get("title") or model_id,
            "vision": bool(model.get("vision")),
            "choice": make_choice(p["key"], model_id)}


def default_choice(conn: sqlite3.Connection) -> Optional[str]:
    """Модель сервера по умолчанию: GD_MODEL, если он ещё жив, иначе первая
    доступная в каталоге."""
    if resolve(conn, _SEED_DEFAULT) is not None:
        return make_choice(*parse_choice(_SEED_DEFAULT))
    cat = catalog(conn)
    return cat[0]["models"][0]["value"] if cat and cat[0]["models"] else None


def resolve_for_chat(conn: sqlite3.Connection,
                     chat_model: Optional[str]) -> Optional[dict]:
    """Модель чата с откатом на дефолт сервера: провайдера могли выключить
    или удалить уже после того, как чат его выбрал."""
    got = resolve(conn, chat_model or "")
    if got is not None:
        return got
    if chat_model:
        print(f"[llm] модель «{chat_model}» недоступна — беру дефолт сервера")
    default = default_choice(conn)
    return resolve(conn, default) if default else None


def api_key(provider: dict) -> str:
    """Ключ провайдера. «env:ИМЯ» — читать из окружения (секрет не ложится
    в БД); пустой ключ у OpenRouter — взять привычный OPENROUTER_API_KEY."""
    raw = (provider.get("api_key") or "").strip()
    if raw.startswith("env:"):
        return os.environ.get(raw[4:].strip(), "").strip()
    if raw:
        return raw
    if "openrouter.ai" in (provider.get("base_url") or ""):
        return os.environ.get("OPENROUTER_API_KEY", "").strip()
    return ""


def seed(conn: sqlite3.Connection) -> None:
    """Первый старт: провайдер claude из прежних GD_MODEL/GD_MODELS, чтобы
    старые значения chats.model («sonnet») продолжали работать, и заготовка
    OpenRouter — выключенная, ключ читается из окружения."""
    if store.llm_providers(conn):
        return
    titles = {"sonnet": "Sonnet", "opus": "Opus", "haiku": "Haiku"}
    models = [{"id": m.strip(), "title": titles.get(m.strip(), m.strip())}
              for m in _SEED_CLAUDE.split(",") if m.strip()]
    store.llm_provider_create(
        conn, key=CLAUDE_KEY, title="Claude (подписка)", kind="claude",
        models=models or [{"id": "sonnet", "title": "Sonnet"}])
    store.llm_provider_create(
        conn, key="openrouter", title="OpenRouter", kind="openai",
        base_url="https://openrouter.ai/api/v1",
        api_key="env:OPENROUTER_API_KEY", enabled=False,
        models=[{"id": "z-ai/glm-5.2:free", "title": "GLM 5.2 (free)"},
                {"id": "cohere/north-mini-code:free",
                 "title": "North Mini Code (free)"}])
    print("[llm] заведены провайдеры: claude, openrouter (выключен)")
