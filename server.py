"""gd-assist: сайт геймдизайнеров — чаты (каналы/ЛС/агент) + волт диздоков.

Запуск: python server.py [--host 0.0.0.0] [--port 8010]
Один воркер uvicorn, без reload: агент-сессии живут в процессе.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import common  # noqa: F401  (utf-8 консоль, QuickEdit) — импорт с побочным эффектом
import uvicorn
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import docx_io
import gitops
import prompts
import store
import vaultlib
from agent import manager
from vaultlib import VAULT

HERE = Path(__file__).resolve().parent
AUTH_COOKIE = "gd_auth"
# Публикация за реверс-прокси под путём (Caddy: handle /gd/* без среза).
# Starlette снимает префикс из root_path, только если он реально пришёл в
# запросе, поэтому локальный доступ без префикса переменная не ломает.
WEB_BASE = os.environ.get("GD_WEB_BASE", "").rstrip("/")


# --------------------------------------------------------------- запуск/останов

@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = store.connect()
    try:
        store.init_db(conn)
        users = store.list_users(conn)
        if not users:
            password = os.environ.get("GD_ADMIN_PASSWORD") or secrets.token_urlsafe(8)
            store.create_user(conn, "admin", password, "Админ", is_admin=True)
            print(f"[init] создан пользователь admin, пароль: {password}")
            print("[init] смените его после первого входа (или задайте GD_ADMIN_PASSWORD)")
        vaultlib.ensure_layout([u["login"] for u in store.list_users(conn)])
        # Встроенный общий канал.
        row = conn.execute("SELECT id FROM chats WHERE kind='channel' AND title='Общий'").fetchone()
        if row is None:
            admin = store.user_by_login(conn, "admin") or store.list_users(conn)[0]
            store.create_channel(conn, admin, "Общий", False, [])
    finally:
        conn.close()
    # Карта Unity-проекта для агента: сеется из vault_seed при первом старте
    # (сам волт в .gitignore — он отдельный репозиторий).
    seed = HERE / "vault_seed" / "UNITY_PROJECT_MAP.md"
    target = VAULT / "UNITY_PROJECT_MAP.md"
    if seed.is_file() and not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
    gitops.ensure_repo_sync()
    manager.start()
    import unity_mirror
    mirror_task = asyncio.create_task(unity_mirror.loop())
    yield
    mirror_task.cancel()
    await manager.stop()


app = FastAPI(title="GD-ассистент", lifespan=lifespan, root_path=WEB_BASE)


# --------------------------------------------------------------- авторизация

class AuthMiddleware:
    """Pure-ASGI (BaseHTTPMiddleware буферизовал бы NDJSON-стрим)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        # За Caddy путь приходит с префиксом (handle без strip) — срезаем для
        # проверок; сам роутинг префикс снимает Starlette через root_path.
        if WEB_BASE and path.startswith(WEB_BASE):
            path = path[len(WEB_BASE):] or "/"
        if path in ("/login", "/favicon.ico") or path.startswith("/invite/"):
            return await self.app(scope, receive, send)
        req = Request(scope)
        conn = store.connect()
        try:
            user = store.user_by_token(conn, req.cookies.get(AUTH_COOKIE, ""))
        finally:
            conn.close()
        if user is not None:
            scope["state"] = {"user": user}
            return await self.app(scope, receive, send)
        if path.startswith("/api"):
            resp = JSONResponse({"error": "auth required"}, status_code=401)
        else:
            resp = RedirectResponse(WEB_BASE + "/login")
        return await resp(scope, receive, send)


app.add_middleware(AuthMiddleware)


def me(request: Request) -> dict:
    return request.state.user


_LOGIN_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Вход — GD-ассистент</title>
<style>body{{background:#141518;color:#e8e6e3;font:16px/1.5 "Segoe UI",system-ui,sans-serif;
display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#1d1f24;border:1px solid #33363d;border-radius:14px;padding:28px 26px;width:320px;text-align:center}}
h1{{font-size:20px;color:#8ab4f8;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;background:#141518;color:#e8e6e3;border:1px solid #33363d;border-radius:9px;
padding:11px 13px;font:inherit;margin-bottom:12px}}
input:focus{{outline:none;border-color:#8ab4f8}}
button{{width:100%;background:#2b4c7e;color:#e8e6e3;border:1px solid #8ab4f8;border-radius:9px;
padding:11px;font:inherit;cursor:pointer}}button:hover{{background:#35598f}}
.err{{color:#e57373;font-size:13px;margin-bottom:10px;min-height:16px}}</style></head>
<body><form method="post">
<h1>🎮 GD-ассистент</h1>
<div class="err">{err}</div>
<input name="login" placeholder="Логин" autofocus autocomplete="username">
<input type="password" name="password" placeholder="Пароль" autocomplete="current-password">
<button type="submit">Войти</button></form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return _LOGIN_HTML.format(err="")


@app.post("/login")
async def login_submit(request: Request):
    from urllib.parse import parse_qs
    body = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(body)
    login = form.get("login", [""])[0]
    password = form.get("password", [""])[0]
    conn = store.connect()
    try:
        user = store.user_by_login(conn, login)
        if user is None or user["disabled_at"] or not store.check_password(user, password):
            return HTMLResponse(_LOGIN_HTML.format(err="Неверный логин или пароль"),
                                status_code=401)
        token = store.create_auth_session(conn, user["id"])
    finally:
        conn.close()
    resp = RedirectResponse(WEB_BASE + "/", status_code=303)
    resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * store.SESSION_TTL_DAYS)
    return resp


_INVITE_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Регистрация — GD-ассистент</title>
<style>body{{background:#141518;color:#e8e6e3;font:16px/1.5 "Segoe UI",system-ui,sans-serif;
display:grid;place-items:center;height:100vh;margin:0}}
form{{background:#1d1f24;border:1px solid #33363d;border-radius:14px;padding:28px 26px;width:340px;text-align:center}}
h1{{font-size:20px;color:#8ab4f8;margin:0 0 6px}}
p{{font-size:13px;color:#989ca6;margin:0 0 16px}}
input{{width:100%;box-sizing:border-box;background:#141518;color:#e8e6e3;border:1px solid #33363d;border-radius:9px;
padding:11px 13px;font:inherit;margin-bottom:12px}}
input:focus{{outline:none;border-color:#8ab4f8}}
button{{width:100%;background:#2b4c7e;color:#e8e6e3;border:1px solid #8ab4f8;border-radius:9px;
padding:11px;font:inherit;cursor:pointer}}button:hover{{background:#35598f}}
.err{{color:#e57373;font-size:13px;margin-bottom:10px;min-height:16px}}</style></head>
<body><form method="post">
<h1>🎮 GD-ассистент</h1>
<p>Вас пригласили. Придумайте логин и пароль.</p>
<div class="err">{err}</div>
<input name="login" placeholder="Логин (латиница, 2–32)" value="{login}" autofocus>
<input name="display_name" placeholder="Имя (как показывать)" value="{display_name}">
<input type="password" name="password" placeholder="Пароль">
<input type="password" name="password2" placeholder="Пароль ещё раз">
<button type="submit">Зарегистрироваться</button></form></body></html>"""

_INVITE_DEAD = ("<html><body style='background:#141518;color:#e8e6e3;font:16px "
                "Segoe UI,sans-serif;display:grid;place-items:center;height:100vh'>"
                "<div>Ссылка-приглашение недействительна: уже использована, "
                "отозвана или истекла.<br>Попросите новую.</div></body></html>")


@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(token: str):
    conn = store.connect()
    try:
        store.init_db(conn)
        ok = store.invite_pending(conn, token)
    finally:
        conn.close()
    if not ok:
        return HTMLResponse(_INVITE_DEAD, status_code=410)
    return _INVITE_HTML.format(err="", login="", display_name="")


@app.post("/invite/{token}")
async def invite_submit(token: str, request: Request):
    from urllib.parse import parse_qs
    body = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(body)
    login = form.get("login", [""])[0].strip()
    display_name = form.get("display_name", [""])[0].strip()
    password = form.get("password", [""])[0]
    password2 = form.get("password2", [""])[0]

    def again(err: str, code: int = 400):
        return HTMLResponse(_INVITE_HTML.format(
            err=err, login=login, display_name=display_name), status_code=code)

    if password != password2:
        return again("Пароли не совпадают")
    conn = store.connect()
    try:
        # Сначала атомарно гасим ссылку (страховка от двойной регистрации),
        # и возвращаем её, если создать пользователя не вышло (логин занят).
        if not store.claim_invite(conn, token):
            return HTMLResponse(_INVITE_DEAD, status_code=410)
        try:
            user = store.create_user(conn, login, password, display_name)
        except store.StoreError as exc:
            store.unclaim_invite(conn, token)
            return again(str(exc))
        store.bind_invite(conn, token, user["id"])
        vaultlib.ensure_layout([user["login"]])
        auth = store.create_auth_session(conn, user["id"])
    finally:
        conn.close()
    resp = RedirectResponse(WEB_BASE + "/", status_code=303)
    resp.set_cookie(AUTH_COOKIE, auth, httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * store.SESSION_TTL_DAYS)
    return resp


@app.get("/api/admin/invites")
async def admin_invites(request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    conn = store.connect()
    try:
        return store.list_invites(conn)
    finally:
        conn.close()


@app.post("/api/admin/invites")
async def admin_create_invite(request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    conn = store.connect()
    try:
        token = store.create_invite(conn, me(request))
    finally:
        conn.close()
    return {"token": token}


@app.delete("/api/admin/invites/{token}")
async def admin_revoke_invite(token: str, request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    conn = store.connect()
    try:
        store.revoke_invite(conn, token)
    finally:
        conn.close()
    return {"ok": True}


@app.get("/logout")
async def logout(request: Request):
    conn = store.connect()
    try:
        store.drop_auth_session(conn, request.cookies.get(AUTH_COOKIE, ""))
    finally:
        conn.close()
    resp = RedirectResponse(WEB_BASE + "/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# --------------------------------------------------------------- helpers

def _err(exc: Exception, code: int = 400) -> JSONResponse:
    if isinstance(exc, store.Forbidden):
        code = 403
    elif isinstance(exc, store.NotFound):
        code = 404
    return JSONResponse({"error": str(exc)}, status_code=code)


# --------------------------------------------------------------- пользователи

@app.get("/api/me")
async def api_me(request: Request):
    u = me(request)
    return {"id": u["id"], "login": u["login"], "display_name": u["display_name"],
            "is_admin": bool(u["is_admin"])}


@app.get("/api/users")
async def api_users(request: Request):
    conn = store.connect()
    try:
        users = [u for u in store.list_users(conn) if not u["disabled_at"]]
    finally:
        conn.close()
    return [{"id": u["id"], "login": u["login"], "display_name": u["display_name"]}
            for u in users]


@app.get("/api/admin/users")
async def admin_users(request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    conn = store.connect()
    try:
        return store.list_users(conn)
    finally:
        conn.close()


@app.post("/api/admin/users")
async def admin_create_user(request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    body = await request.json()
    conn = store.connect()
    try:
        user = store.create_user(conn, body.get("login", ""),
                                 body.get("password", ""),
                                 body.get("display_name", ""),
                                 bool(body.get("is_admin")))
        vaultlib.ensure_layout([user["login"]])
        return {"id": user["id"], "login": user["login"]}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.patch("/api/admin/users/{user_id}")
async def admin_patch_user(user_id: int, request: Request):
    if not me(request)["is_admin"]:
        return JSONResponse({"error": "только для админа"}, status_code=403)
    body = await request.json()
    conn = store.connect()
    try:
        if body.get("password"):
            store.set_password(conn, user_id, body["password"])
        if "disabled" in body:
            store.set_disabled(conn, user_id, bool(body["disabled"]))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


# --------------------------------------------------------------- чаты

@app.get("/api/chats")
async def api_chats(request: Request):
    conn = store.connect()
    try:
        user = me(request)
        store.agent_chat_for(conn, user)  # гарантируем личный чат с агентом
        return store.list_chats(conn, user)
    finally:
        conn.close()


@app.post("/api/chats")
async def api_create_chat(request: Request):
    body = await request.json()
    conn = store.connect()
    try:
        if body.get("kind") == "dm":
            return store.ensure_dm(conn, me(request), int(body.get("user_id", 0)))
        if body.get("kind") == "agent":
            return store.create_agent_chat(conn, me(request))
        return store.create_channel(conn, me(request), body.get("title", ""),
                                    bool(body.get("is_private")),
                                    [int(x) for x in body.get("members", [])])
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.delete("/api/chats/{chat_id}")
async def api_delete_chat(chat_id: int, request: Request):
    """Удаление своей сессии с агентом вместе с историей и файлами."""
    if manager.status(chat_id)["busy"]:
        return JSONResponse({"error": "агент ещё работает — остановите ход"},
                            status_code=409)
    await manager.drop(chat_id)
    conn = store.connect()
    try:
        upload_paths = store.delete_agent_chat(conn, chat_id, me(request))
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    for rel in upload_paths:
        try:
            (HERE / "data" / rel).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


@app.post("/api/chats/{chat_id}/join")
async def api_join(chat_id: int, request: Request):
    conn = store.connect()
    try:
        store.join_channel(conn, chat_id, me(request))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.post("/api/chats/{chat_id}/leave")
async def api_leave(chat_id: int, request: Request):
    conn = store.connect()
    try:
        store.leave_channel(conn, chat_id, me(request))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.get("/api/chats/{chat_id}/messages")
async def api_messages(chat_id: int, request: Request, before: int | None = None,
                       thread: int | None = None, limit: int = 60):
    conn = store.connect()
    try:
        chat = store.require_chat(conn, chat_id, me(request))
        msgs = store.list_messages(conn, chat_id, before=before,
                                   limit=min(limit, 200), thread_root_id=thread)
        members = store.chat_member_ids(conn, chat_id)
        return {"chat": chat, "messages": msgs, "member_ids": members,
                "agent": manager.status(chat_id),
                "pin_count": store.pin_count(conn, chat_id),
                "reads": store.chat_reads_info(conn, chat_id)}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.post("/api/chats/{chat_id}/read")
async def api_read(chat_id: int, request: Request):
    body = await request.json()
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, me(request))
        store.mark_read(conn, chat_id, me(request), int(body.get("last_message_id", 0)))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.post("/api/messages/{msg_id}/react")
async def api_react(msg_id: int, request: Request):
    body = await request.json()
    conn = store.connect()
    try:
        msg = store.get_message(conn, msg_id)
        store.require_chat(conn, msg["chat_id"], me(request))
        store.toggle_reaction(conn, msg_id, me(request), body.get("emoji", ""))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.post("/api/messages/{msg_id}/pin")
async def api_pin(msg_id: int, request: Request):
    conn = store.connect()
    try:
        msg = store.get_message(conn, msg_id)
        store.require_chat(conn, msg["chat_id"], me(request))
        return {"pinned": store.toggle_pin(conn, msg_id, me(request))}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.get("/api/chats/{chat_id}/pins")
async def api_pins(chat_id: int, request: Request):
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, me(request))
        return {"pins": store.list_pins(conn, chat_id)}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.get("/api/chats/{chat_id}/files")
async def api_chat_files(chat_id: int, request: Request):
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, me(request))
        return {"files": store.list_chat_files(conn, chat_id)}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.post("/api/messages/{msg_id}/to-thread")
async def api_to_thread(msg_id: int, request: Request):
    """Перевести цепочку ответов в тред (корень остаётся в ленте)."""
    conn = store.connect()
    try:
        msg = store.get_message(conn, msg_id)
        store.require_chat(conn, msg["chat_id"], me(request))
        root = store.replies_to_thread(conn, msg_id)
        return {"root": root}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.patch("/api/messages/{msg_id}")
async def api_msg_edit(msg_id: int, request: Request):
    body = await request.json()
    conn = store.connect()
    try:
        msg = store.get_message(conn, msg_id)
        store.require_chat(conn, msg["chat_id"], me(request))
        return store.edit_message(conn, msg_id, me(request),
                                  body.get("text") or "")
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.delete("/api/messages/{msg_id}")
async def api_msg_delete(msg_id: int, request: Request):
    conn = store.connect()
    try:
        msg = store.get_message(conn, msg_id)
        store.require_chat(conn, msg["chat_id"], me(request))
        store.delete_message(conn, msg_id, me(request))
        return {"ok": True}
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _ensure_docx_md(p: Path) -> Optional[Path]:
    """Read агента не умеет docx — рядом с загруженным .docx держим
    извлечённый markdown (<имя>.docx.md). Конвертируем лениво, один раз."""
    md = p.parent / (p.name + ".md")
    if md.is_file():
        return md
    if not docx_io.AVAILABLE:
        return None
    try:
        text = docx_io.docx_to_markdown(
            p.read_bytes(), p.parent / (p.name + ".assets"), p.name + ".assets")
        md.write_text(text, encoding="utf-8")
        return md
    except Exception:
        return None


def _attachment_paths(conn, att: dict) -> list[tuple[Path, str]]:
    """[(абс. путь для Read агента, подпись)] одного вложения сообщения.
    Фронт шлёт доки волта с ключом path, старые записи — vault_path."""
    out: list[tuple[Path, str]] = []
    try:
        rel = att.get("vault_path") or att.get("path")
        if rel:
            p = (vaultlib.VAULT / rel).resolve()
            if p.is_file():
                out.append((p, att.get("title") or p.name))
        elif att.get("upload_id"):
            up = store.get_upload(conn, int(att["upload_id"]))
            p = (HERE / "data" / up["vault_path"]).resolve()
            if p.is_file():
                title = att.get("title") or up["orig_name"]
                if p.suffix.lower() == ".docx":
                    md = _ensure_docx_md(p)
                    if md:
                        out.append((md, f"{title} (текст из docx)"))
                        return out
                out.append((p, title))
    except (store.StoreError, ValueError, OSError):
        pass
    return out


@app.post("/api/chats/{chat_id}/send")
async def api_send(chat_id: int, request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "пустое сообщение"}, status_code=400)
    user = me(request)
    conn = store.connect()
    try:
        chat = store.require_chat(conn, chat_id, user)
        # Пишешь в публичный канал — становишься участником (для непрочитанных).
        if chat["kind"] == "channel" and not chat["is_private"]:
            if user["id"] not in store.chat_member_ids(conn, chat_id):
                store.join_channel(conn, chat_id, user)
        # Ответ на сообщение (как в ТГ): валиден только внутри этого же чата.
        reply_msg = None
        try:
            rid = int(body.get("reply_to_id") or 0)
            if rid:
                cand = store.get_message(conn, rid)
                if cand["chat_id"] == chat_id and not cand.get("deleted_at"):
                    reply_msg = cand
        except (store.StoreError, TypeError, ValueError):
            pass
        user_msg = store.add_message(
            conn, chat_id, author_kind="user", author_user_id=user["id"],
            author_name=user["display_name"], text=text,
            thread_root_id=body.get("thread_root_id"),
            attachments=body.get("attachments") or [],
            reply_to_id=reply_msg["id"] if reply_msg else None)
        # Автоназвание сессии с агентом — из первого сообщения.
        if (chat["kind"] == "agent" and chat["title"] == store.DEFAULT_AGENT_TITLE
                and store.message_count(conn, chat_id) == 1):
            store.rename_chat(conn, chat_id, store.auto_title(text))
        # Свои загрузки перепривязываются к чату отправки — иначе файл,
        # выбранный в одном чате и отправленный в другом, не открывался бы
        # у остальных участников («этот чат вам не виден»).
        upload_ids = []
        for a in body.get("attachments") or []:
            try:
                if a.get("upload_id"):
                    upload_ids.append(int(a["upload_id"]))
            except (TypeError, ValueError):
                pass
        store.rebind_uploads(conn, upload_ids, chat_id, user)
        # Агента зовут: его чат, @агент, или ответ на его сообщение.
        call_agent = (chat["kind"] == "agent" or store.mentions_agent(text)
                      or bool(reply_msg and reply_msg["author_kind"] == "agent"))
        prompt = text
        if reply_msg and call_agent:
            q = " ".join((reply_msg["text"] or "").split())[:600]
            prompt = (f"[Это ответ на сообщение от "
                      f"{reply_msg['author_name']}: «{q}»]\n{text}")
        # Приложенные файлы: агенту — абсолютные пути, гарду — разрешение читать.
        attach_files: list[str] = []
        attach_note = ""
        if call_agent:
            lines = []
            for att in body.get("attachments") or []:
                for p, title in _attachment_paths(conn, att):
                    attach_files.append(str(p))
                    lines.append(f"- {p} — «{title}»")
            attach_note = ("\n\n[К сообщению приложены файлы — открой инструментом"
                           " Read те, что нужны для ответа (картинки Read тоже"
                           " показывает):\n" + "\n".join(lines) + "]") if lines else ""
            # Агенту доступны и все прежние вложения этого чата, не только
            # из сообщений с @агент.
            for f in store.list_chat_files(conn, chat_id, limit=200):
                for p, _t in _attachment_paths(conn, f):
                    attach_files.append(str(p))
        if call_agent and chat["kind"] != "agent":
            # Контекст разговора: для треда — корень и ветка, иначе лента чата.
            thread_root = body.get("thread_root_id")
            if thread_root:
                recent = ([store.get_message(conn, int(thread_root))] +
                          store.list_messages(conn, chat_id,
                                              before=user_msg["id"],
                                              limit=prompts_context_limit(),
                                              thread_root_id=int(thread_root)))
            else:
                recent = store.list_messages(conn, chat_id,
                                             before=user_msg["id"],
                                             limit=prompts_context_limit())
            if thread_root:
                # Корень треда — всегда в контексте, бюджет режет ветку.
                recent = recent[:1] + prompts.trim_context(recent[1:])
            else:
                recent = prompts.trim_context(recent)
            # Пометки вложений в контексте: агент знает, что и где лежит.
            for m in recent:
                notes = [f"{title}: {p}" for att in m.get("attachments") or []
                         for p, title in _attachment_paths(conn, att)]
                if notes:
                    m["text"] = ((m["text"] or "") +
                                 "\n[вложения: " + "; ".join(notes) + "]").strip()
            prompt = (prompts.channel_context(recent) +
                      f"{user['display_name']}: {prompt}")
        prompt += attach_note
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()

    async def event_stream():
        yield _line({"type": "message", "message": user_msg})
        if not call_agent:
            yield _line({"type": "done"})
            return
        job = await manager.submit(chat, user_msg, prompt,
                                   extra_files=attach_files)
        st = manager.status(chat_id)
        if st["busy"] or st["queued"] > 1:
            yield _line({"type": "queued", "position": st["queued"]})
        while True:
            ev = await job.events.get()
            if ev is None:
                break
            yield _line(ev)

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def prompts_context_limit() -> int:
    from agent import CHANNEL_CONTEXT_MESSAGES
    return CHANNEL_CONTEXT_MESSAGES


@app.post("/api/chats/{chat_id}/stop")
async def api_stop(chat_id: int, request: Request):
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, me(request))
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    ok = await manager.interrupt(chat_id)
    return {"ok": ok}


# Присутствие — в памяти процесса (1 воркер): переживать рестарт не обязано.
LAST_SEEN: dict[int, float] = {}          # user_id -> monotonic последнего запроса
TYPING: dict[tuple[int, int], float] = {}  # (chat_id, user_id) -> monotonic
ONLINE_SECONDS = 40   # фоновая вкладка поллит раз в 15 с — с запасом
TYPING_SECONDS = 6


@app.post("/api/chats/{chat_id}/typing")
async def api_typing(chat_id: int, request: Request):
    user = me(request)
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, user)
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    TYPING[(chat_id, user["id"])] = time.monotonic()
    return {"ok": True}


@app.get("/api/changes")
async def api_changes(request: Request, since: int = -1, chat: int = 0):
    user = me(request)
    now_mono = time.monotonic()
    LAST_SEEN[user["id"]] = now_mono
    online = [uid for uid, ts in LAST_SEEN.items()
              if now_mono - ts < ONLINE_SECONDS]
    typing_names: list[str] = []
    if chat:
        for (cid, uid), ts in list(TYPING.items()):
            if now_mono - ts > TYPING_SECONDS:
                TYPING.pop((cid, uid), None)
            elif cid == chat and uid != user["id"]:
                typing_names.append(uid)
    conn = store.connect()
    try:
        if chat:
            try:
                store.require_chat(conn, chat, user)
            except store.StoreError:
                chat, typing_names = 0, []
        if typing_names:
            typing_names = [store.get_user(conn, uid)["display_name"]
                            for uid in typing_names]
        reads = store.chat_reads_info(conn, chat) if chat else []
        if since < 0:
            return {"now": store.max_change_id(conn), "events": [],
                    "online_ids": online, "typing": typing_names,
                    "reads": reads}
        events = store.changes_since(conn, me(request), since)
        now = events[-1]["id"] if events else since
        return {"now": now, "events": events,
                "online_ids": online, "typing": typing_names,
                "reads": reads}
    finally:
        conn.close()


# --------------------------------------------------------------- ходы агента

@app.get("/api/turn/{turn_id}")
async def api_turn(turn_id: int, request: Request):
    conn = store.connect()
    try:
        turn = store.get_turn(conn, turn_id)
        store.require_chat(conn, turn["chat_id"], me(request))
        return turn
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()


@app.get("/api/turn/{turn_id}/diff")
async def api_turn_diff(turn_id: int, request: Request):
    conn = store.connect()
    try:
        turn = store.get_turn(conn, turn_id)
        store.require_chat(conn, turn["chat_id"], me(request))
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    if not turn["commit_hash"]:
        return {"diff": "", "note": "в этом ходе файлы не менялись"}
    diff = await gitops.show(turn["commit_hash"])
    return {"diff": diff, "commit": turn["commit_hash"]}


@app.post("/api/turn/{turn_id}/revert")
async def api_turn_revert(turn_id: int, request: Request):
    user = me(request)
    conn = store.connect()
    try:
        turn = store.get_turn(conn, turn_id)
        chat = store.require_chat(conn, turn["chat_id"], user)
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    if not turn["commit_hash"]:
        return JSONResponse({"error": "нечего откатывать"}, status_code=400)
    try:
        new_hash = await gitops.revert(turn["commit_hash"], user["login"])
    except gitops.GitError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    conn = store.connect()
    try:
        store.add_message(conn, chat["id"], author_kind="system",
                          author_user_id=user["id"], author_name="система",
                          text=f"{user['display_name']} откатил(а) правки хода"
                               f" (коммит {turn['commit_hash'][:10]})")
    finally:
        conn.close()
    return {"ok": True, "revert_commit": new_hash}


# --------------------------------------------------------------- файлы чата

UPLOADS_DIR = HERE / "data" / "uploads"
MAX_UPLOAD_MB = int(os.environ.get("GD_MAX_UPLOAD_MB", "50"))
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


@app.post("/api/chats/{chat_id}/upload")
async def api_chat_upload(chat_id: int, request: Request, file: UploadFile):
    """Файл с компьютера к сообщению: хранится в data/uploads (НЕ в волте —
    в git произвольным бинарям не место), отдаётся через /api/upload/{id}."""
    user = me(request)
    conn = store.connect()
    try:
        store.require_chat(conn, chat_id, user)
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        return JSONResponse({"error": f"файл больше {MAX_UPLOAD_MB} МБ"},
                            status_code=413)
    orig = Path(file.filename or "file").name
    safe = re.sub(r"[^\w.\-а-яёА-ЯЁ ]", "_", orig)[:120] or "file"
    fname = f"{secrets.token_hex(6)}_{safe}"
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / fname).write_bytes(data)
    # docx агент читать не умеет — извлекаем текст сразу при загрузке.
    if fname.lower().endswith(".docx"):
        _ensure_docx_md(UPLOADS_DIR / fname)
    conn = store.connect()
    try:
        upload_id = store.add_upload(conn, user, orig, f"uploads/{fname}", chat_id)
    finally:
        conn.close()
    return {"upload_id": upload_id, "title": orig,
            "image": Path(orig).suffix.lower() in IMAGE_EXT}


@app.get("/api/upload/{upload_id}")
async def api_upload_get(upload_id: int, request: Request, dl: int = 0):
    """dl=1 — принудительное скачивание (кнопка «скачать» в просмотрщике)."""
    from fastapi.responses import FileResponse
    conn = store.connect()
    try:
        up = store.get_upload(conn, upload_id)
        if up["chat_id"]:
            store.require_chat(conn, up["chat_id"], me(request))
    except store.StoreError as exc:
        return _err(exc)
    finally:
        conn.close()
    p = HERE / "data" / up["vault_path"]
    if not p.is_file():
        return JSONResponse({"error": "файл потерян на диске"}, status_code=404)
    ext = p.suffix.lower()
    disposition = "inline" if ext in IMAGE_EXT and not dl else "attachment"
    # Содержимое upload-а никогда не меняется (имя файла случайное) — браузеру
    # можно кэшировать вечно; private, потому что доступ закрыт авторизацией.
    return FileResponse(p, filename=up["orig_name"],
                        content_disposition_type=disposition,
                        headers={"Cache-Control":
                                 "private, max-age=31536000, immutable"})


# --------------------------------------------------------------- волт

@app.get("/api/vault/tree")
async def api_vault_tree(request: Request):
    return vaultlib.tree(me(request))


@app.get("/api/vault/file")
async def api_vault_file(request: Request, path: str):
    try:
        info = vaultlib.read_file(me(request), path)
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    if info.get("binary"):
        p = vaultlib.safe_path(path)
        ext = p.suffix.lstrip(".").lower()
        mime = {"svg": "image/svg+xml", "jpg": "image/jpeg"}.get(ext, f"image/{ext}")
        return Response(p.read_bytes(), media_type=mime)
    return info


@app.put("/api/vault/file")
async def api_vault_put(request: Request):
    body = await request.json()
    user = me(request)
    path, text = body.get("path", ""), body.get("text", "")
    base_mtime = body.get("base_mtime")
    try:
        p = vaultlib.safe_path(path)
        if p.exists() and base_mtime and abs(p.stat().st_mtime - float(base_mtime)) > 1e-6:
            return JSONResponse(
                {"error": "файл изменился, пока вы редактировали — обновите и"
                          " слейте правки", "conflict": True}, status_code=409)
        vaultlib.write_file(user, path, text)
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    commit = await gitops.commit_paths([p], f"{user['login']}: правка {path}",
                                       user["login"])
    return {"ok": True, "commit": commit, "mtime": p.stat().st_mtime}


@app.post("/api/vault/upload")
async def api_vault_upload(request: Request, file: UploadFile, folder: str = "shared"):
    user = me(request)
    data = await file.read()
    name = file.filename or "doc"
    ext = Path(name).suffix.lower()
    slug = docx_io.slugify(name)
    try:
        if ext == ".docx":
            if not docx_io.AVAILABLE:
                return JSONResponse(
                    {"error": "docx-импорт недоступен: pip install mammoth markdownify"},
                    status_code=501)
            rel_dir = folder.strip("/") or "shared"
            target = vaultlib.safe_path(f"{rel_dir}/{slug}.md")
            if not vaultlib.can_write(user, target):
                return JSONResponse({"error": "сюда вам писать нельзя"}, status_code=403)
            assets = vaultlib.safe_path(f"shared/_assets/{slug}")
            depth = len(Path(rel_dir).parts)
            assets_rel = "../" * depth + f"shared/_assets/{slug}"
            md = docx_io.docx_to_markdown(data, assets, assets_rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")
        elif ext in (".md", ".markdown", ".txt"):
            target = vaultlib.safe_path(f"{folder.strip('/') or 'shared'}/{slug}.md")
            if not vaultlib.can_write(user, target):
                return JSONResponse({"error": "сюда вам писать нельзя"}, status_code=403)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data.decode("utf-8", "replace"), encoding="utf-8")
        else:
            return JSONResponse({"error": "поддерживаются .docx и .md"}, status_code=400)
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    rel = target.relative_to(VAULT).as_posix()
    paths = [target]
    assets_dir = VAULT / "shared" / "_assets" / slug
    if assets_dir.is_dir():
        paths.append(assets_dir)
    commit = await gitops.commit_paths(paths, f"{user['login']}: загрузка {name}",
                                      user["login"])
    conn = store.connect()
    try:
        store.add_upload(conn, user, name, rel)
    finally:
        conn.close()
    return {"ok": True, "path": rel, "commit": commit}


@app.get("/api/vault/resolve")
async def api_vault_resolve(request: Request, name: str):
    path = vaultlib.resolve_wikilink(me(request), name)
    if path is None:
        return JSONResponse({"error": f"документ «{name}» не найден"}, status_code=404)
    return {"path": path}


@app.get("/api/vault/history")
async def api_vault_history(request: Request, path: str):
    try:
        p = vaultlib.safe_path(path)
        if not vaultlib.can_see(me(request), p):
            raise vaultlib.VaultError("этот файл вам не виден")
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    return await gitops.file_history(path)


@app.get("/api/vault/commit")
async def api_vault_commit(request: Request, hash: str):
    """Diff одного коммита истории файла. Хеши приходят из /api/vault/history,
    видимость которой уже отфильтрована; сам diff может задеть соседние файлы
    коммита, но по-товарищески это ок (personal-папки коммитятся отдельными
    ходами и правками, в чужие коммиты не попадают)."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", hash):
        return JSONResponse({"error": "плохой хеш"}, status_code=400)
    try:
        return {"diff": await gitops.show(hash)}
    except gitops.GitError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/api/vault/backlinks")
async def api_vault_backlinks(request: Request, path: str):
    try:
        p = vaultlib.safe_path(path)
        if not vaultlib.can_see(me(request), p):
            raise vaultlib.VaultError("этот файл вам не виден")
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    return vaultlib.backlinks(me(request), path)


@app.get("/api/vault/export")
async def api_vault_export(request: Request, path: str):
    """md -> docx через pandoc (если стоит в PATH, иначе 501 с подсказкой)."""
    import shutil
    from fastapi.responses import FileResponse
    try:
        p = vaultlib.safe_path(path)
        if not vaultlib.can_see(me(request), p):
            raise vaultlib.VaultError("этот файл вам не виден")
    except vaultlib.VaultError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    if p.suffix.lower() != ".md" or not p.is_file():
        return JSONResponse({"error": "экспортируются только .md"}, status_code=400)
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return JSONResponse(
            {"error": "pandoc не установлен — экспорт в docx недоступен."
                      " Поставьте: winget install pandoc — и перезапустите сервер."},
            status_code=501)
    out_dir = HERE / "data" / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (p.stem + ".docx")
    proc = await asyncio.create_subprocess_exec(
        pandoc, str(p), "-o", str(out), "--from", "gfm",
        cwd=str(p.parent),  # относительные картинки документа
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    log, _ = await proc.communicate()
    if proc.returncode != 0:
        return JSONResponse({"error": "pandoc: " +
                             log.decode("utf-8", "replace")[:500]}, status_code=500)
    return FileResponse(out, filename=p.stem + ".docx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".wordprocessingml.document")


@app.get("/api/search")
async def api_search(request: Request, q: str = ""):
    q = q.strip()
    user = me(request)
    if len(q) < 2:
        return {"docs": [], "messages": []}
    docs = vaultlib.search_docs(user, q)
    conn = store.connect()
    try:
        msgs = store.search_messages(conn, user, q)
    finally:
        conn.close()
    return {"docs": docs, "messages": msgs}


# --------------------------------------------------------------- статика (последней!)

# Кэш-схема для работы через интернет: index.html отдаётся роутом ниже с
# ?v=<mtime> на каждом ассете, поэтому сами js/css можно кэшировать вечно —
# изменение файла меняет его URL, и браузер тянет свежую версию сам. Итог:
# повторная загрузка страницы — один запрос index.html, остальное из кэша
# без единой ревалидации.
_ASSET_RE = re.compile(r'(href|src)="([\w./-]+\.(?:css|js))"')


def _index_html() -> str:
    raw = (HERE / "web" / "index.html").read_text("utf-8")

    def stamp(m: re.Match) -> str:
        f = HERE / "web" / m.group(2)
        try:
            return f'{m.group(1)}="{m.group(2)}?v={int(f.stat().st_mtime)}"'
        except OSError:
            return m.group(0)

    return _ASSET_RE.sub(stamp, raw)


@app.get("/")
@app.get("/index.html")
async def index_page():
    return HTMLResponse(_index_html(),
                        headers={"Cache-Control": "no-cache"})


class ImmutableStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        if str(getattr(resp, "path", "")).endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount("/", ImmutableStatic(directory=str(HERE / "web")), name="web")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("GD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("GD_PORT", "8010")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, workers=1)
