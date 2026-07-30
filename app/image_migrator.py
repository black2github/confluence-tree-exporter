# app/image_migrator.py
#
# Слой миграции картинок: разрешает плейсхолдеры картинок, которые выдаёт
# content_extractor._process_image, в реальные локальные файлы в подкаталоге img/.
#
# Конвертер не знает ни page_id, ни пути выходного .md, поэтому ссылается на картинку
# через плейсхолдер. Здесь, на слое миграции, известны и page_id, и путь .md, поэтому
# мы скачиваем картинку в img/ рядом с .md и подставляем относительную ссылку. Приём
# зеркалит разрешение ссылок confluence:// (Pass 2 в migrate_confluence_tree).
#
# Поддерживаются два режима доступа (по типу плейсхолдера):
#   • confluence-attachment://<filename> — REST API (storage-формат): URL вложения
#     ищется через get_attachments_from_content, скачивание — сессией atlassian-клиента.
#   • confluence-download://<path>        — прямой HTTP (закрытый контур, рендеренный
#     HTML): готовый путь скачивается браузерным запросом (basic-auth, verify=False),
#     как и сами страницы в HTTP-режиме page_cache.

import hashlib
import html as _html
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlparse, urlsplit

from app.config import CONFLUENCE_BASE_URL
from app.confluence_loader import confluence

logger = logging.getLogger(__name__)

# Плейсхолдеры картинок из content_extractor._process_image:
#   confluence-attachment://<filename>  — storage-формат (REST API): имя вложения,
#       URL для скачивания ищется через get_attachments_from_content.
#   confluence-download://<path>        — рендеренный HTML (прямой HTTP, закрытый
#       контур): готовый путь скачивания /download/attachments/..., качается
#       браузерным запросом (basic-auth), как и сами страницы в HTTP-режиме.
# group(1) — ссылка (возможно HTML-экранированная), group(2) — хвост атрибутов до '>'.
_ATTACHMENT_IMG_RE = re.compile(r'<img\s+src="confluence-attachment://([^"]+)"([^>]*)>')
_DOWNLOAD_IMG_RE = re.compile(r'<img\s+src="confluence-download://([^"]+)"([^>]*)>')

# Ссылки на вложения (ac:link + ri:attachment) из content_extractor._process_link.
# Тот же плейсхолдер confluence-attachment://, но в форме ссылки, а не <img>:
#   • HTML-таблица: <a href="confluence-attachment://<file>">
#   • Markdown:     [текст](confluence-attachment://<file>)
# Разрешаются той же логикой, что и картинки-вложения (скачать в img/ или fallback-URL).
_ATTACHMENT_A_LINK_RE = re.compile(r'<a href="confluence-attachment://([^"]+)">')
_ATTACHMENT_MD_LINK_RE = re.compile(r'\]\(confluence-attachment://([^)]+)\)')


def migrate_images_in_content(
    content_md: str,
    page_id: str,
    md_filepath: Path,
) -> Tuple[str, int, int]:
    """Скачивает вложения (картинки <img> и ссылки <a>/markdown на файлы) и заменяет
    плейсхолдеры confluence-attachment://… на относительные ссылки.

    Картинки сохраняются в подкаталог img/ рядом с md_filepath под детерминированным
    именем uid+расширение, где uid = sha1(f"{page_id}/{filename}")[:8]. Детерминизм
    разводит коллизии одноимённых вложений с разных страниц (соседние .md делят один
    img/) и обеспечивает идемпотентность: повторный прогон не качает файл заново и не
    мусорит git-диффом.

    При невозможности скачать (ошибка, или HTTP-режим без доступа к API) деградирует
    мягко: плейсхолдер заменяется на абсолютный URL вложения в Confluence, чтобы в
    тексте не осталось битой ссылки confluence-attachment://.

    Возвращает (новый_текст, downloaded, failed).
    """
    has_attach = "confluence-attachment://" in content_md
    has_download = "confluence-download://" in content_md
    if not (has_attach or has_download):
        return content_md, 0, 0

    img_dir = md_filepath.parent / "img"
    resolved: Dict[str, str] = {}            # кэш в пределах страницы: ключ -> ссылка
    counts = {"downloaded": 0, "failed": 0}
    new_text = content_md

    # storage-формат (REST API): имя вложения -> URL ищем через API один раз на страницу.
    if has_attach:
        attachments = _get_attachment_download_urls(page_id)

        def _attach_ref(raw_filename: str) -> str:
            # confluence-attachment://a&amp;b.png -> a&b.png; кэш на страницу разводит
            # повторы (одно вложение в нескольких <img>/<a>) — скачиваем один раз.
            filename = _html.unescape(raw_filename)
            key = "att::" + filename
            if key not in resolved:
                resolved[key] = _resolve_attachment(filename, page_id, attachments, img_dir, counts)
            return resolved[key]

        # Картинки-вложения: <img src="confluence-attachment://...">
        new_text = _ATTACHMENT_IMG_RE.sub(
            lambda m: f'<img src="{_attach_ref(m.group(1))}"{m.group(2)}>', new_text
        )
        # Ссылки на вложения в HTML-форме (внутри таблиц): <a href="confluence-attachment://...">
        new_text = _ATTACHMENT_A_LINK_RE.sub(
            lambda m: f'<a href="{_attach_ref(m.group(1))}">', new_text
        )
        # Ссылки на вложения в Markdown-форме: [текст](confluence-attachment://...)
        new_text = _ATTACHMENT_MD_LINK_RE.sub(
            lambda m: f']({_attach_ref(m.group(1))})', new_text
        )

    # рендеренный HTML (прямой HTTP): готовый путь скачивания, качаем браузерным запросом.
    if has_download:
        def _replace_download(m: re.Match) -> str:
            ref = _html.unescape(m.group(1))
            rest = m.group(2)
            key = "dl::" + ref
            if key not in resolved:
                resolved[key] = _resolve_download(ref, page_id, img_dir, counts)
            return f'<img src="{resolved[key]}"{rest}>'

        new_text = _DOWNLOAD_IMG_RE.sub(_replace_download, new_text)

    return new_text, counts["downloaded"], counts["failed"]


def _resolve_attachment(
    filename: str,
    page_id: str,
    attachments: Dict[str, str],
    img_dir: Path,
    counts: Dict[str, int],
) -> str:
    """Возвращает относительную ссылку img/<uid><ext> (скачав файл при необходимости)
    либо абсолютный fallback-URL при неудаче."""
    uid = hashlib.sha1(f"{page_id}/{filename}".encode("utf-8")).hexdigest()[:8]
    ext = os.path.splitext(filename)[1]      # сохраняем оригинальное расширение
    target_name = f"{uid}{ext}"
    target_path = img_dir / target_name
    rel = f"img/{target_name}"               # ссылка относительно .md, POSIX-слеши

    # Идемпотентность: файл уже скачан в прошлый прогон — повторно не качаем.
    if target_path.exists():
        return rel

    url = attachments.get(filename)
    if not url:
        logger.warning(
            "[image_migrator] Вложение '%s' не найдено среди вложений page_id=%s — fallback на URL",
            filename, page_id,
        )
        counts["failed"] += 1
        return _fallback_url(page_id, filename)

    data = _http_get_bytes(url)
    if data is None:
        counts["failed"] += 1
        return _fallback_url(page_id, filename)

    img_dir.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    counts["downloaded"] += 1
    logger.info("[image_migrator] Сохранена картинка '%s' -> %s", filename, rel)
    return rel


def _resolve_download(
    ref: str,
    page_id: str,
    img_dir: Path,
    counts: Dict[str, int],
) -> str:
    """Разрешает плейсхолдер confluence-download://<path> (рендеренный HTML, HTTP-режим).

    ref — путь скачивания (обычно /download/attachments/<id>/<file>?version=...),
    возможно абсолютный. Качаем браузерным запросом (basic-auth), кладём в img/ под
    детерминированным именем uid+ext (uid = sha1(page_id/filename)[:8] — то же правило,
    что и для вложений из API, чтобы одинаковые картинки совпадали по имени между
    режимами). При неудаче — fallback на абсолютный URL вложения.
    """
    url = _absolute_url(ref)
    filename = unquote(os.path.basename(urlsplit(ref).path)) or "image"  # без query
    uid = hashlib.sha1(f"{page_id}/{filename}".encode("utf-8")).hexdigest()[:8]
    ext = os.path.splitext(filename)[1]
    target_name = f"{uid}{ext}"
    target_path = img_dir / target_name
    rel = f"img/{target_name}"

    # Идемпотентность: файл уже скачан в прошлый прогон — повторно не качаем.
    if target_path.exists():
        return rel

    data = _browser_get_bytes(url)
    if data is None:
        counts["failed"] += 1
        return url  # fallback: абсолютный URL вложения в Confluence

    img_dir.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    counts["downloaded"] += 1
    logger.info("[image_migrator] Сохранена картинка (HTTP) '%s' -> %s", filename, rel)
    return rel


def _get_attachment_download_urls(page_id: str) -> Dict[str, str]:
    """filename -> абсолютный URL для скачивания. При ошибке (или закрытый API в
    HTTP-режиме) возвращает пустую карту — вызывающий код деградирует мягко."""
    try:
        resp = confluence.get_attachments_from_content(page_id, limit=200)
    except Exception as e:
        logger.warning(
            "[image_migrator] Не удалось получить список вложений page_id=%s: %s "
            "(в HTTP-режиме скачивание картинок не поддержано)", page_id, e,
        )
        return {}

    result: Dict[str, str] = {}
    for att in (resp or {}).get("results", []):
        title = att.get("title")
        download = (att.get("_links") or {}).get("download")
        if title and download:
            result[title] = _absolute_url(download)
    return result


def _absolute_url(link: str) -> str:
    """Достраивает относительную download-ссылку Confluence до абсолютного URL."""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    p = urlparse(CONFLUENCE_BASE_URL)
    return f"{p.scheme}://{p.netloc}{link}"


def _fallback_url(page_id: str, filename: str) -> str:
    """Абсолютный URL вложения в Confluence — на случай, когда скачать не удалось."""
    return f"{CONFLUENCE_BASE_URL}/download/attachments/{page_id}/{quote(filename)}"


def _http_get_bytes(url: str) -> Optional[bytes]:
    """Скачивает бинарные данные, переиспользуя авторизованную сессию клиента
    atlassian.Confluence. Возвращает None при ошибке."""
    session = getattr(confluence, "_session", None) or getattr(confluence, "session", None)
    try:
        if session is not None:
            resp = session.get(url, timeout=60)
        else:
            import requests
            resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.warning("[image_migrator] Ошибка скачивания %s: %s", url, e)
        return None


def _browser_get_bytes(url: str) -> Optional[bytes]:
    """Скачивает бинарные данные браузероподобным запросом (basic-auth, verify=False) —
    тем же механизмом, что page_cache тянет страницы в HTTP-режиме закрытого контура.
    Переиспользуем _confluence_http_get, чтобы авторизация была в одном месте."""
    try:
        from app.page_cache import _confluence_http_get
        resp = _confluence_http_get(url)
        return resp.content if resp is not None else None
    except Exception as e:
        logger.warning("[image_migrator] Ошибка браузерного скачивания %s: %s", url, e)
        return None
