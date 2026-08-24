"""Волт (база знаний): раскладка, права видимости и записи путей.

Единственный источник правил «кому какие пути можно» — им пользуются и
PreToolUse-хуки агента, и HTTP-ручки, и дерево в UI. Раскладка:

    vault/
      shared/            — общая база, видят и правят все
        _assets/         — картинки из docx-импорта
      personal/<login>/  — личная область (владелец + админ)
      CODE_MAP.md        — карта кодовой базы проекта для агента
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import common  # noqa: F401  — .env должен загрузиться до чтения GD_* ниже

HERE = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("GD_VAULT", str(HERE / "vault"))).resolve()
# Рабочая копия программистов — источник зеркала, агент её НЕ читает.
# Не задана — кодовой базы у агента нет вовсе, остаётся только волт.
_code_repo = os.environ.get("GD_CODE_REPO", "").strip()
CODE_REPO = Path(_code_repo).resolve() if _code_repo else None
# Зеркало рабочей ветки (sparse-клон, синхронизирует code_mirror.py):
# только закоммиченное, без рабочих правок и чужих веток.
CODE_MIRROR = Path(os.environ.get(
    "GD_CODE_MIRROR", str(HERE / "data" / "code-mirror"))).resolve()


def code_root() -> Path | None:
    """Что читает агент: зеркало, когда оно уже склонировано, иначе (первый
    запуск, пока идёт клон) — рабочая копия. None — кода у агента нет."""
    if CODE_REPO is None:
        return None
    if (CODE_MIRROR / ".git").is_dir():
        try:
            if any(p.name != ".git" for p in CODE_MIRROR.iterdir()):
                return CODE_MIRROR
        except OSError:
            pass
    return CODE_REPO

MD_EXT = {".md", ".markdown", ".txt"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


class VaultError(Exception):
    pass


def ensure_layout(logins: list[str]) -> None:
    (VAULT / "shared" / "_assets").mkdir(parents=True, exist_ok=True)
    for login in logins:
        (VAULT / "personal" / login).mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------- права путей

def safe_path(rel: str) -> Path:
    """Относительный путь волта -> абсолютный, с защитой от выхода наружу."""
    p = (VAULT / rel).resolve()
    try:
        p.relative_to(VAULT)
    except ValueError:
        raise VaultError("путь вне волта") from None
    if ".git" in p.relative_to(VAULT).parts:
        raise VaultError(".git трогать нельзя")
    return p


def can_see(user: dict, abs_path: Path) -> bool:
    """Видимость для пользователя: shared и корень — всем, personal/<login> —
    владельцу и админу."""
    try:
        rel = abs_path.resolve().relative_to(VAULT)
    except ValueError:
        return False
    parts = rel.parts
    if ".git" in parts:
        return False
    if not parts or parts[0] != "personal":
        return True
    if len(parts) < 2:
        # Сам контейнер personal/ виден всем — иначе дерево не дойдёт до
        # собственной папки пользователя; чужие папки фильтруются ниже.
        return True
    return parts[1] == user["login"] or bool(user.get("is_admin"))


def can_write(user: dict, abs_path: Path) -> bool:
    """Запись: shared — всем, свой personal — владельцу (админу — любой)."""
    if not can_see(user, abs_path):
        return False
    rel = abs_path.resolve().relative_to(VAULT)
    parts = rel.parts
    if not parts:
        return False
    if parts[0] == "shared":
        return True
    if parts[0] == "personal":
        return len(parts) >= 2 and (parts[1] == user["login"] or bool(user.get("is_admin")))
    # корень волта (CODE_MAP.md и т.п.) — только админ
    return bool(user.get("is_admin"))


def agent_scopes(chat_kind: str, owner_login: str | None) -> dict:
    """Области доступа агент-сессии по типу чата.

    read: откуда можно читать (плюс кодовая база — всегда read-only).
    write: куда можно писать.
    """
    read = [VAULT / "shared", VAULT / "CODE_MAP.md", VAULT / "PROJECT.md"]
    code = code_root()
    if code is not None:
        read.append(code)
    write = [VAULT / "shared"]
    if chat_kind == "agent" and owner_login:
        personal = VAULT / "personal" / owner_login
        read.append(personal)
        write.append(personal)
    return {"read": read, "write": write}


def path_under(abs_path: Path, roots: list[Path]) -> bool:
    try:
        p = abs_path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------- дерево и файлы

def tree(user: dict) -> list[dict]:
    """Дерево волта, отфильтрованное правами. Узел: {name, path, dir, children?}."""
    def walk(d: Path) -> list[dict]:
        out = []
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return out
        for p in entries:
            if p.name.startswith(".") or not can_see(user, p):
                continue
            rel = p.relative_to(VAULT).as_posix()
            if p.is_dir():
                out.append({"name": p.name, "path": rel, "dir": True,
                            "children": walk(p)})
            elif p.suffix.lower() in MD_EXT | IMG_EXT:
                out.append({"name": p.name, "path": rel, "dir": False})
        return out
    return walk(VAULT)


def read_file(user: dict, rel: str) -> dict:
    p = safe_path(rel)
    if not can_see(user, p):
        raise VaultError("этот файл вам не виден")
    if not p.is_file():
        raise VaultError(f"нет файла {rel}")
    if p.suffix.lower() in IMG_EXT:
        return {"path": rel, "binary": True, "mtime": p.stat().st_mtime}
    return {"path": rel, "text": p.read_text(encoding="utf-8", errors="replace"),
            "mtime": p.stat().st_mtime}


def write_file(user: dict, rel: str, text: str) -> Path:
    p = safe_path(rel)
    if not can_write(user, p):
        raise VaultError("сюда вам писать нельзя")
    if p.suffix.lower() not in MD_EXT:
        raise VaultError("через редактор сохраняются только .md")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


# --------------------------------------------------------------- поиск и бэклинки

def _snippet(text: str, idx: int, qlen: int, radius: int = 60) -> str:
    start = max(0, idx - radius)
    end = min(len(text), idx + qlen + radius)
    piece = " ".join(text[start:end].split())
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")


def search_docs(user: dict, q: str, limit: int = 20) -> list[dict]:
    """Поиск по волту: имя файла и текст, без учёта регистра."""
    ql = q.lower()
    out = []
    for p in sorted(VAULT.rglob("*.md")):
        if ".git" in p.parts or not can_see(user, p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = text.lower().find(ql)
        name_hit = ql in p.stem.lower()
        if idx < 0 and not name_hit:
            continue
        out.append({"path": p.relative_to(VAULT).as_posix(),
                    "name": p.stem,
                    "snippet": _snippet(text, idx, len(q)) if idx >= 0 else ""})
        if len(out) >= limit:
            break
    return out


def backlinks(user: dict, rel: str) -> list[dict]:
    """Какие документы ссылаются на этот через [[Имя]] (с #якорями и |алиасами)."""
    stem = Path(rel).stem
    pat = re.compile(r"\[\[" + re.escape(stem) + r"([#|][^\]]*)?\]\]", re.IGNORECASE)
    out = []
    for p in sorted(VAULT.rglob("*.md")):
        if ".git" in p.parts or not can_see(user, p):
            continue
        if p.relative_to(VAULT).as_posix() == rel:
            continue
        try:
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                out.append({"path": p.relative_to(VAULT).as_posix(), "name": p.stem})
        except OSError:
            continue
    return out


# --------------------------------------------------------------- wikilinks

_WIKILINK_CLEAN = re.compile(r"[#|].*$")


def resolve_wikilink(user: dict, name: str) -> str | None:
    """[[Название]] -> путь файла. Ищем по имени файла (без расширения),
    без учёта регистра; shared предпочтительнее personal."""
    stem = _WIKILINK_CLEAN.sub("", name).strip().lower()
    if not stem:
        return None
    best: str | None = None
    for p in VAULT.rglob("*.md"):
        if ".git" in p.parts or not can_see(user, p):
            continue
        if p.stem.lower() == stem:
            rel = p.relative_to(VAULT).as_posix()
            if rel.startswith("shared/"):
                return rel
            best = best or rel
    return best
