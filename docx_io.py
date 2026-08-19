"""Импорт docx в markdown: mammoth (docx -> HTML + картинки) + markdownify
(HTML -> MD). Оба — чистый pip, pandoc не нужен."""
from __future__ import annotations

import io
import re
from pathlib import Path

try:
    import mammoth
    from markdownify import markdownify
    AVAILABLE = True
except ImportError:  # зависимости не поставлены — загрузка docx отключена
    AVAILABLE = False


def slugify(name: str) -> str:
    stem = Path(name).stem.strip().lower()
    stem = re.sub(r"[^\w\-а-яё ]", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", "-", stem)
    return stem or "doc"


def docx_to_markdown(data: bytes, assets_dir: Path, assets_rel: str) -> str:
    """Конвертирует docx. Картинки складываются в assets_dir, в MD — ссылки
    вида assets_rel/img-N.png (относительно файла документа)."""
    counter = {"n": 0}
    saved: list[str] = []

    def convert_image(image):
        counter["n"] += 1
        ext = (image.content_type or "image/png").split("/")[-1].split("+")[0]
        fname = f"img-{counter['n']}.{ext}"
        assets_dir.mkdir(parents=True, exist_ok=True)
        with image.open() as f:
            (assets_dir / fname).write_bytes(f.read())
        saved.append(fname)
        return {"src": f"{assets_rel}/{fname}"}

    result = mammoth.convert_to_html(
        io.BytesIO(data),
        convert_image=mammoth.images.img_element(convert_image))
    md = markdownify(result.value, heading_style="ATX", bullets="-")
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    if not saved:
        try:
            assets_dir.rmdir()  # пустую папку не оставляем
        except OSError:
            pass
    return md
