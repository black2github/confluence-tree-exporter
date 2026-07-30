# Путь: scripts/dump_confluence_page.py
"""
Служебный скрипт для сохранения raw HTML страницы Confluence в файл.

Использование:
    python scripts/dump_confluence_page.py <page_id> [output_dir]

Примеры:
    python scripts/dump_confluence_page.py 291472147
    python scripts/dump_confluence_page.py 291472147 debug/html

Сохраняет файл:
    <output_dir>/<page_id>_<safe_title>.html

По умолчанию output_dir = "debug/html"
"""

import sys
import re
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

OUTPUT_ROOT = Path("debug/html")

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')
WHITESPACE = re.compile(r"\s+")


def safe_filename(title: str, max_length: int = 100) -> str:
    """Превращает заголовок страницы в безопасное имя файла."""
    name = title.strip()
    name = INVALID_FILENAME_CHARS.sub("", name)
    name = WHITESPACE.sub("-", name)
    name = name.strip("-.")
    if not name:
        name = "untitled"
    return name[:max_length]


def dump_page(page_id: str, output_dir: Path, use_http: bool = False) -> Path:
    """
    Загружает страницу из Confluence и сохраняет raw HTML в файл.

    Использует page_cache — тот же путь, что и основной пайплайн,
    поэтому результат гарантированно идентичен тому, что обрабатывает
    content_extractor.

    Args:
        page_id: Идентификатор страницы Confluence.
        output_dir: Каталог для сохранения HTML.
        use_http: Если True — получать страницу через прямой HTTP-доступ
                  (как браузер), в обход Confluence REST API.

    Returns:
        Путь к сохранённому файлу.
    """
    from app.page_cache import get_page_data

    logger.info(
        "Loading page %s from Confluence (%s)...",
        page_id, "direct HTTP" if use_http else "REST API",
    )
    page_data = get_page_data(page_id, use_http=use_http)

    if not page_data:
        logger.error("Failed to load page %s — check page_id and Confluence connectivity.", page_id)
        sys.exit(1)

    title = page_data.get("title", "untitled")
    raw_html = page_data.get("raw_html", "")

    if not raw_html:
        logger.error("Page %s loaded but raw_html is empty.", page_id)
        sys.exit(1)

    filename = f"{page_id}_{safe_filename(title)}.html"
    filepath = output_dir / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(raw_html, encoding="utf-8")

    logger.info("Title:  %s", title)
    logger.info("Saved:  %s", filepath)
    logger.info("Size:   %d bytes", len(raw_html.encode("utf-8")))

    return filepath


def main():
    from app.config import CONFLUENCE_USE_HTTP

    # Флаг --http можно указать в любом месте аргументов; он переопределяет
    # значение CONFLUENCE_USE_HTTP из конфигурации.
    args = [a for a in sys.argv[1:] if a != "--http"]
    use_http = ("--http" in sys.argv) or CONFLUENCE_USE_HTTP

    if len(args) < 1:
        print("Usage: python scripts/dump_confluence_page.py <page_id> [output_dir] [--http]")
        print("Example: python scripts/dump_confluence_page.py 291472147")
        print("Example: python scripts/dump_confluence_page.py 291472147 debug/html --http")
        sys.exit(1)

    page_id = args[0].strip()
    output_dir = Path(args[1]) if len(args) > 1 else OUTPUT_ROOT

    dump_page(page_id, output_dir, use_http=use_http)


if __name__ == "__main__":
    main()