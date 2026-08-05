# scripts/migrate_confluence_page.py

import sys
import re
import logging
import yaml
from pathlib import Path
from typing import Dict, Optional

from app.confluence_loader import load_pages_by_ids
from app.page_cache import fetch_page_data_via_http
from app.service_registry import get_platform_status

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


OUTPUT_ROOT = Path("conf-requirements")

INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]+')
WHITESPACE = re.compile(r"\s+")


def safe_filename(title: str, max_length: int = 100) -> str:
    """Превращает заголовок Confluence-страницы в имя файла, сохраняя кириллицу.

    Удаляет только символы, запрещённые в именах файлов на Windows/Linux/macOS.
    Пробелы заменяет на дефисы для читаемости в URL и Git.
    """
    name = title.strip()
    name = INVALID_FILENAME_CHARS.sub("", name)
    name = WHITESPACE.sub("-", name)
    name = name.strip("-.")
    if not name:
        name = "untitled"
    return name[:max_length]


def build_doc_id(service_code: str, title: str) -> str:
    """Строит doc_id как location-независимую смарт-ссылку {{SERVICE: label}}.

    SERVICE — код сервиса как есть (тот же вид, что в frontmatter/манифесте).
    label — полный заголовок Confluence, ВКЛЮЧАЯ префикс ([КК_ВК] ...), без
    вырезания/нормализации: label == title == manifest name. Парсинг ведётся по
    первому ':' (код сервиса двоеточий не содержит), поэтому двоеточия и кавычки
    внутри label допустимы.

    Формат не зависит от пути файла: файл можно двигать по дереву каталогов без
    смены doc_id (связь doc_id → путь/url держит манифест карточек).
    См. app/scripts/CI/design-smart-link-doc-id.md.
    """
    return "{{" + f"{service_code}: {title}" + "}}"


def page_to_frontmatter(
    page: Dict,
    service_code: str,
    source: str,
    doc_id: str,
    include_unapproved: bool = False,
    has_unapproved: bool = False,
) -> Dict:
    """Строит frontmatter из метаданных Confluence-страницы.

    Поля, которых нет в Confluence как структурированные данные (owner,
    jira_id, related, author, tags, reviewers, date, parent), оставляются
    пустыми. Линтер при первой попытке коммита укажет на пропуски —
    аналитик заполнит их вручную при ревью миграции.

    status: "draft" только если миграция шла с --all (include_unapproved)
    И на странице реально есть неподтверждённые фрагменты (has_unapproved).
    Без --all пишется только подтверждённый контент → "active"; с --all,
    но без неподтверждённого на странице, контент фактически подтверждён → "active".
    ("active" — подтверждённый/живой документ в терминах целевой схемы;
    допустимые статусы: draft | review | active | deprecated.)

    Поля reviewers и tags — YAML-списки (не строки): целевая схема ожидает массив.
    """
    req_type = (page.get("requirement_type") or "unknown").strip()
    is_platform = get_platform_status(service_code)
    status = "draft" if (include_unapproved and has_unapproved) else "active"

    fm: Dict = {
        # Идентификация
        "doc_id": doc_id,
        "title": page.get("title", ""),
        "description": "",

        # Классификация
        "doc_type": "requirement",
        "requirement_type": req_type,
        "service_code": service_code,
        "microservice": "",
        "feature": "",
        "is_platform": is_platform,

        # Источник и трассировка
        "source": source,
        "jira_id": "",
        "jira_ids": "",
        "confluence_page_id": str(page["id"]),

        # Статус и владение
        "status": status,
        "owner": "",
        "author": "",
        "reviewers": [],
        "version": "1.0.0",
        "date": "",
        "created_date": "",
        "updated_date": "",

        # Связи
        "related": "",

        # Теги
        "tags": [],
    }

    # target_system — для интеграционных требований
    # В load_pages_by_ids это поле сейчас не возвращается. Оставляем пустым;
    # при ревью миграции аналитик заполнит вручную. Если в будущем добавишь
    # извлечение target_system в page_cache — подхвати page.get("target_system")
    # здесь автоматически.
    if req_type == "integration":
        fm["target_system"] = page.get("target_system", "")

    return fm


def write_md_file(
    filepath: Path,
    frontmatter: Dict,
    content_md: str,
) -> None:
    """Сохраняет .md файл с frontmatter и Markdown-содержимым."""
    filepath.parent.mkdir(parents=True, exist_ok=True)

    frontmatter_str = yaml.dump(
        frontmatter,
        allow_unicode=True,        # критично для кириллицы в значениях
        default_flow_style=False,
        sort_keys=False,
    )

    full_text = f"---\n{frontmatter_str}---\n\n{content_md}\n"
    filepath.write_text(full_text, encoding="utf-8")


def migrate_page(
    page: Dict,
    service_code: str,
    source: str,
    subdir: str,
    include_unapproved: bool = False,
) -> Optional[Path]:
    """Конвертирует одну страницу Confluence в .md файл.

    Использует approved_content из load_pages_by_ids — это уже готовый
    Markdown (гибридный Markdown + HTML), сформированный твоим форматером
    через filter_approved_fragments.
    """
    content_md = page.get("approved_content", "")
    if not content_md or not content_md.strip():
        logger.warning("  ⚠ Page %s has no approved content, skipped", page["id"])
        return None

    # Физический путь файла развязан с doc_id: расположение задаёт безопасное имя
    # файла в дереве сервиса (cc/<subdir>/<filename>.md), а doc_id — это
    # location-независимая смарт-ссылка {{SERVICE: title}}.
    filename = safe_filename(page["title"])
    service_part = service_code.lower().replace("_", "-")
    filepath = OUTPUT_ROOT / service_part / subdir / f"{filename}.md"
    doc_id = build_doc_id(service_code, page["title"])

    # Коллизия имён: файл с таким же заголовком уже есть. Перезаписываем с
    # предупреждением (миграция разовая, далее работа по ФС под git).
    # См. app/scripts/CI/analysis-naming-strategies.md.
    if filepath.exists():
        logger.warning("  ⚠ Файл уже существует — ПЕРЕЗАПИСЫВАЕМ: %s", filepath)

    # Миграция картинок: скачиваем вложения в img/ рядом с .md и заменяем плейсхолдеры
    # confluence-attachment:// на относительные ссылки. Плейсхолдеры есть в content_md
    # только если конвертация шла с включённым MIGRATE_IMAGES.
    import app.config as _config
    if _config.MIGRATE_IMAGES:
        from app.image_migrator import migrate_images_in_content
        content_md, downloaded, failed = migrate_images_in_content(
            content_md, str(page["id"]), filepath
        )
        if downloaded or failed:
            logger.info("  🖼 Картинки: скачано %d, ошибок %d", downloaded, failed)

    # Миграция файлов-вложений (--with-attachments): ссылки /download/attachments/…
    # скачиваются в files/ рядом с .md и заменяются относительными.
    if _config.MIGRATE_ATTACHMENTS:
        from app.attachment_migrator import migrate_file_attachments_in_content
        content_md, a_dl, a_fail, a_skip = migrate_file_attachments_in_content(
            content_md, str(page["id"]), filepath
        )
        if a_dl or a_fail or a_skip:
            logger.info("  📎 Вложения: скачано %d, ошибок %d, пропущено по размеру %d",
                        a_dl, a_fail, a_skip)

    frontmatter = page_to_frontmatter(
        page, service_code, source, doc_id,
        include_unapproved=include_unapproved,
        has_unapproved=page.get("has_unapproved", False),
    )
    write_md_file(filepath, frontmatter, content_md)

    return filepath


def main():
    from app.config import CONFLUENCE_USE_HTTP, MIGRATE_INCLUDE_UNAPPROVED

    # Флаги --http, --all, --keep-history, --with-images и --drop-strikethrough можно
    # указать в любом месте аргументов; они переопределяют значения из конфигурации.
    flags = {"--http", "--all", "--keep-history", "--with-images",
             "--with-attachments", "--drop-strikethrough"}
    raw_args = [a for a in sys.argv[1:] if a not in flags]
    use_http = ("--http" in sys.argv) or CONFLUENCE_USE_HTTP
    include_unapproved = ("--all" in sys.argv) or MIGRATE_INCLUDE_UNAPPROVED
    keep_history = "--keep-history" in sys.argv
    with_images = "--with-images" in sys.argv
    with_attachments = "--with-attachments" in sys.argv
    drop_strikethrough = "--drop-strikethrough" in sys.argv

    if len(raw_args) < 3:
        print("Usage: python migrate_confluence_page.py "
              "<page_id,...> <service_code> <subdir> [source] [--http] [--all] [--keep-history]")
        print("Example: python migrate_confluence_page.py "
              "12345,67890 CORP_CARDS лимиты DBOCORPESPLN")
        print("Example (прямой HTTP, в обход API): python migrate_confluence_page.py "
              "12345,67890 CORP_CARDS лимиты DBOCORPESPLN --http")
        print("Example (всё содержимое, включая неподтверждённое): "
              "python migrate_confluence_page.py 12345 CORP_CARDS лимиты --all")
        print("Example (сохранить раздел 'История изменений'): "
              "python migrate_confluence_page.py 12345 CORP_CARDS лимиты --keep-history")
        print("Example (мигрировать картинки в img/ рядом с .md): "
              "python migrate_confluence_page.py 12345 CORP_CARDS лимиты --with-images")
        print("Example (исключить зачёркнутый текст при любом раскладе, в т.ч. с --all): "
              "python migrate_confluence_page.py 12345 CORP_CARDS лимиты --all --drop-strikethrough")
        sys.exit(1)

    # Переопределяем политику удаления истории на время процесса (вариант A).
    # remove_history_sections() читает app.config.REMOVE_HISTORY_SECTIONS динамически.
    if keep_history:
        import app.config as _config
        _config.REMOVE_HISTORY_SECTIONS = False

    # Включаем миграцию картинок на время процесса. Фабрики экстракторов читают
    # app.config.MIGRATE_IMAGES динамически — флаг должен быть выставлен ДО конвертации
    # (load_pages_by_ids ниже), чтобы конвертер выдал плейсхолдеры картинок.
    if with_images:
        import app.config as _config
        _config.MIGRATE_IMAGES = True

    # Включаем миграцию файлов-вложений (files/ рядом с .md); слой миграции читает
    # app.config.MIGRATE_ATTACHMENTS динамически.
    if with_attachments:
        import app.config as _config
        _config.MIGRATE_ATTACHMENTS = True

    # Исключение зачёркнутого текста при любом раскладе. Фабрики экстракторов читают
    # app.config.EXCLUDE_STRIKETHROUGH динамически — флаг выставляем ДО конвертации.
    if drop_strikethrough:
        import app.config as _config
        _config.EXCLUDE_STRIKETHROUGH = True

    page_ids = raw_args[0].split(",")
    service_code = raw_args[1]
    subdir = raw_args[2]
    source = raw_args[3] if len(raw_args) > 3 else "DBOCORPESPLN"

    if use_http:
        # Прямой HTTP-доступ: предзаполняем общий page_cache через браузерный
        # запрос. Дальше load_pages_by_ids (-> get_page_data_cached) берёт данные
        # из кеша (cache HIT) и не обращается к закрытому REST API.
        logger.info("Loading %d page(s) from Confluence via direct HTTP...", len(page_ids))
        for page_id in page_ids:
            result = fetch_page_data_via_http(page_id.strip())
            if result:
                logger.info("  ✓ Fetched page_id=%s title='%s'", page_id, result["title"])
            else:
                logger.warning("  ⚠ Failed to fetch page_id=%s", page_id)
    else:
        logger.info("Loading %d page(s) from Confluence via REST API...", len(page_ids))

    logger.info("Content mode: %s",
                "ВСЁ содержимое (включая неподтверждённое)" if include_unapproved
                else "только подтверждённые фрагменты")
    logger.info("History mode: %s",
                "СОХРАНЯТЬ раздел истории" if keep_history else "удалять раздел истории")
    logger.info("Images mode: %s",
                "СКАЧИВАТЬ картинки в img/" if with_images else "картинки игнорируются")

    pages = load_pages_by_ids(page_ids, include_unapproved=include_unapproved)

    if not pages:
        logger.error(
            "No pages loaded. Possible reasons: missing title, full_markdown, "
            "or approved_content in source pages (see load_pages_by_ids logs)."
        )
        sys.exit(1)

    logger.info("Migrating to %s/%s/ ...", service_code, subdir)

    migrated = []
    skipped = []

    for page in pages:
        result = migrate_page(page, service_code, source, subdir, include_unapproved=include_unapproved)
        if result:
            migrated.append(result)
            logger.info("  ✓ %s → %s", page["title"], result)
        else:
            skipped.append(page["id"])

    logger.info("")
    logger.info("Migration complete:")
    logger.info("  Loaded from Confluence: %d", len(pages))
    logger.info("  Migrated:               %d", len(migrated))
    logger.info("  Skipped:                %d", len(skipped))
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Fill in empty frontmatter fields (owner, jira_id) manually")
    logger.info("  2. Run: python scripts/lint_frontmatter.py")
    logger.info("  3. git add, commit, push, open PR")


if __name__ == "__main__":
    main()