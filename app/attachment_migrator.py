# app/attachment_migrator.py
#
# Слой миграции файловых вложений (документы: .vsdx, .docx, .xlsx, архивы …):
# пост-проход по готовому markdown, находит ссылки на /download/attachments/…,
# скачивает файлы в подкаталог files/ рядом с .md и заменяет ссылку на
# относительную. Включается флагом --with-attachments (config.MIGRATE_ATTACHMENTS).
#
# Зачем отдельный слой: в rendered-HTML режиме (закрытый контур, --http) ссылки
# <a href="/download/attachments/…"> проходят конвертер как есть и остаются
# серверными путями Confluence — вне сервера они мертвы. REST-ветка
# (storage-формат) ссылки-вложения уже мигрирует плейсхолдерами
# confluence-attachment:// в image_migrator — этот модуль закрывает HTTP-ветку.
#
# Именование: <санированное-оригинальное-имя>-<uid8><ext>, uid = sha1(page_id/имя).
# Оригинальное имя сохраняется (пользователь скачивает документ — имя есть часть
# содержания), uid-хвост даёт детерминизм, развод коллизий одноимённых вложений
# разных страниц (соседние .md делят один files/) и идемпотентность повторного
# прогона. Длинное имя усекается с сохранением расширения (лог) — защита от
# MAX_PATH на глубоких деревьях Windows.
#
# Деградация мягкая и без тихих потерь: не скачалось / превышен размерный лимит —
# исходная серверная ссылка ОСТАЁТСЯ в тексте (она и есть fallback), в лог пишется
# причина; счётчики отдаются вызывающему для сводки.

import hashlib
import html as _html
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import unquote, urlparse, urlsplit

import app.config as _config
from app.image_migrator import _absolute_url, _browser_get_bytes

logger = logging.getLogger(__name__)

# Ссылки на файлы-вложения в двух формах записи (относительный путь или абсолютный
# URL); замыкающие ')' и '"' не входят в url за счёт классов исключения.
_MD_LINK_RE = re.compile(r'\]\((?P<url>(?:https?://[^)\s]+?)?/download/attachments/[^)\s]+)\)')
_A_HREF_RE = re.compile(r'<a href="(?P<url>(?:https?://[^"]+?)?/download/attachments/[^"]+)"')

# Запрещённые в именах файлов Windows символы + управляющие.
_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_STEM_LEN = 80  # предел «человеческой» части имени; сверх — усечение с логом


def migrate_file_attachments_in_content(
    content_md: str,
    page_id: str,
    md_filepath: Path,
) -> Tuple[str, int, int, int]:
    """Скачивает файлы-вложения по ссылкам /download/attachments/… в files/ рядом
    с md_filepath и заменяет ссылки на относительные.

    Возвращает (новый_текст, downloaded, failed, skipped_oversize).
    Ссылки, которые не удалось (или нельзя) скачать, остаются как в исходнике.
    """
    if "/download/attachments/" not in content_md:
        return content_md, 0, 0, 0

    files_dir = md_filepath.parent / "files"
    resolved: Dict[str, Optional[str]] = {}  # url -> относительная ссылка | None
    counts = {"downloaded": 0, "failed": 0, "skipped": 0}

    def _resolve(raw_url: str) -> Optional[str]:
        # В HTML-островах href может нести &amp; — нормализуем для скачивания,
        # ключ кэша — нормализованный URL (одно вложение качается один раз).
        url = _html.unescape(raw_url)
        if url in resolved:
            return resolved[url]
        resolved[url] = _download_one(url, page_id, files_dir, counts)
        return resolved[url]

    def _sub_md(m: re.Match) -> str:
        rel = _resolve(m.group("url"))
        return f"]({rel})" if rel else m.group(0)

    def _sub_href(m: re.Match) -> str:
        rel = _resolve(m.group("url"))
        return f'<a href="{rel}"' if rel else m.group(0)

    new_text = _MD_LINK_RE.sub(_sub_md, content_md)
    new_text = _A_HREF_RE.sub(_sub_href, new_text)
    return new_text, counts["downloaded"], counts["failed"], counts["skipped"]


def _download_one(
    url: str,
    page_id: str,
    files_dir: Path,
    counts: Dict[str, int],
) -> Optional[str]:
    """Скачивает одно вложение; возвращает относительную ссылку files/<имя>
    либо None (ссылку в тексте не менять)."""
    # Чужой хост (другой Confluence/сервер) — не наша зона авторизации, не трогаем.
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        base_host = urlparse(_config.CONFLUENCE_BASE_URL).netloc
        if parsed.netloc != base_host:
            logger.info(
                "[attachment_migrator] Чужой хост '%s' (база %s) — ссылка оставлена: %s",
                parsed.netloc, base_host, url,
            )
            return None

    filename = unquote(os.path.basename(urlsplit(url).path)) or "attachment"
    target_name = _target_name(page_id, filename)
    target_path = files_dir / target_name
    rel = f"files/{target_name}"  # относительно .md, POSIX-слеши

    # Идемпотентность: файл уже скачан прошлым прогоном — повторно не качаем.
    if target_path.exists():
        return rel

    data = _browser_get_bytes(_absolute_url(url))
    if data is None:
        counts["failed"] += 1
        logger.warning(
            "[attachment_migrator] Не скачалось вложение '%s' (page_id=%s) — "
            "серверная ссылка оставлена", filename, page_id,
        )
        return None

    limit_bytes = _config.ATTACHMENT_MAX_MB * 1024 * 1024
    if len(data) > limit_bytes:
        counts["skipped"] += 1
        logger.warning(
            "[attachment_migrator] Вложение '%s' (%.1f МБ) больше лимита %d МБ — "
            "пропущено, серверная ссылка оставлена (поднять лимит: ATTACHMENT_MAX_MB)",
            filename, len(data) / (1024 * 1024), _config.ATTACHMENT_MAX_MB,
        )
        return None

    files_dir.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    counts["downloaded"] += 1
    logger.info("[attachment_migrator] Сохранено вложение '%s' -> %s", filename, rel)
    return rel


def _target_name(page_id: str, filename: str) -> str:
    """Детерминированное имя: <санированный-stem>-<uid8><ext>."""
    stem, ext = os.path.splitext(filename)
    uid = hashlib.sha1(f"{page_id}/{filename}".encode("utf-8")).hexdigest()[:8]
    stem = _FORBIDDEN_CHARS_RE.sub("_", stem).strip(". ")
    if len(stem) > _MAX_STEM_LEN:
        logger.info(
            "[attachment_migrator] Имя '%s' длиннее %d символов — усечено "
            "(uid сохраняет уникальность)", filename, _MAX_STEM_LEN,
        )
        stem = stem[:_MAX_STEM_LEN].rstrip("._- ")
    return f"{stem}-{uid}{ext}" if stem else f"{uid}{ext}"
