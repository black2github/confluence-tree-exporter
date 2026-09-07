# migrate_confluence_tree.py

# Алгоритм
# Входные параметры (аналогично migrate_confluence_page.py):
# python migrate_confluence_tree.py <page_id> <service_code> <subdir> [source]
# Обход дерева (migrate_subtree):
# 1. Загружает данные страницы через get_page_data_cached (с fallback на get_page_title_only для пустых контейнеров).
# 2. Проверяет правила исключения из PAGE_EXCLUSION_RULES_FILE.
# 3. Запрашивает прямых потомков через confluence.get_child_pages(page_id) — с retry на таймаут.
# 4. Если у страницы есть дочерние → сохраняет её собственный контент как <title>.md в текущей директории
#    (если контент есть) и РЯДОМ создаёт каталог <title>/ для дочерних, затем рекурсивно обходит потомков
#    внутри этого каталога. Если у родителя нет собственного контента — создаётся только каталог.
# 5. Если страница листовая → сохраняет как <title>.md в текущей директории.
#
# Структура на диске: conf-requirements/ corp-cards/ лимиты/ Родительская-страница.md          ← контент самой
# родительской (если есть) Родительская-страница/            ← папка с дочерними страницами Дочерняя-листовая.md
# Вложенный-раздел.md             ← контент вложенного раздела (если есть) Вложенный-раздел/               ← папка
# для его детей Ещё-одна-страница.md
#
# doc_id в frontmatter вычисляется как путь относительно OUTPUT_ROOT (без расширения).
# Для родительской страницы doc_id и путь к её каталогу детей совпадают по имени,
# что даёт естественную иерархию: parent — это файл, children — файлы в одноимённой папке.
#
# Повторное использование из migrate_confluence_page.py:
# • safe_filename — очистка заголовка для имени файла/каталога
# • page_to_frontmatter — генерация frontmatter
# • write_md_file — запись .md с frontmatter
# • OUTPUT_ROOT — корневой путь вывода

import os
import re
import sys
import html
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from app.confluence_loader import confluence, get_page_title_only
from app.page_cache import (
    get_page_data,
    fetch_page_title_via_http,
    fetch_child_pages_via_http,
)
from app.page_exclusion_filter import load_exclusion_rules, is_page_excluded
from app.config import (
    PAGE_EXCLUSION_RULES_FILE,
    CONFLUENCE_BASE_URL,
    CONFLUENCE_USE_HTTP,
    MIGRATE_INCLUDE_UNAPPROVED,
)
from app.scripts.migrate_confluence_page import (
    safe_filename,
    page_to_frontmatter,
    write_md_file,
    OUTPUT_ROOT,
    build_doc_id,
)

from requests import ReadTimeout

# Текст ссылки может содержать экранированные скобки (\[ \]), которые добавляет
# _escape_link_text в content_extractor. Поэтому в качестве текста ссылки матчим
# либо экранированный символ (\\.), либо любой символ кроме '\' и неэкранированного ']'.
# Простой [^\]]* остановился бы на первом же ']' внутри экранированного '\]'.
_LINK_TEXT = r'((?:\\.|[^\]\\])*)'
# ID-плейсхолдер может нести необязательный суффикс ?title=SPACE/Title+Words —
# его дописывает content_extractor, когда у ссылки есть и ID, и заголовок. Суффикс
# используется как fallback-ключ резолва по заголовку, если ID не найден в реестре.
_LINK_BY_ID_RE = re.compile(r'\[' + _LINK_TEXT + r'\]\(confluence://(\d+)(?:\?title=([^)]*))?\)')
_LINK_BY_TITLE_RE = re.compile(r'\[' + _LINK_TEXT + r'\]\(confluence://title/([^)]*)\)')

# Внутри HTML-таблиц ссылки генерируются как HTML-тег <a href="confluence://...">.
# Здесь резолвим только атрибут href, не трогая текст и закрывающий </a>.
_HTML_LINK_BY_ID_RE = re.compile(r'<a href="confluence://(\d+)(?:\?title=([^"]*))?">')
_HTML_LINK_BY_TITLE_RE = re.compile(r'<a href="confluence://title/([^"]*)">')


def _title_key(title: str) -> str:
    """Нормализует заголовок страницы в ключ реестра title_registry.

    Заголовок может прийти двумя путями, и оба нужно привести к одному виду:
    • напрямую из метаданных Confluence (save_page_file, seed_registries_from_disk);
    • восстановленным из плейсхолдера confluence://title/... на этапе Pass 2.

    Плейсхолдер для ссылок внутри HTML-таблиц проходит через _escape_html_attr,
    который кодирует кавычки как &quot; (а & как &amp;). Без html.unescape такой
    заголовок ('… "Список карт"') никогда не совпал бы с реестром, где лежит
    настоящая строка с кавычками. Дополнительно схлопываем пробелы и приводим
    к нижнему регистру, чтобы мелкие расхождения в вёрстке не ломали матчинг.
    """
    title = html.unescape(title)
    title = re.sub(r"\s+", " ", title).strip()
    return title.lower()


def _decode_title_path(title_encoded: str) -> str:
    """Восстанавливает заголовок из хвоста плейсхолдера для поиска в title_registry.

    Кодирование (content_extractor/page_cache) симметрично: литеральный '+' в
    заголовке экранируется как %2B, затем пробелы кодируются как '+'. Раскодируем в
    обратном порядке — сначала '+'→пробел, потом %2B→'+'. Иначе литеральный плюс
    (напр. в продукте "O2+") был бы неотличим от закодированного пробела, и заголовок
    вида '[O2+NEW] …' не совпал бы с ключом реестра ('[O2 NEW] …' вместо '[O2+NEW] …').

    Берём только хвост после '/' — это сам заголовок; space-key (если был) отбрасываем.
    """
    return title_encoded.split("/")[-1].replace("+", " ").replace("%2B", "+")


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

MAX_RETRIES = 3


def get_direct_children(page_id: str, use_http: bool = False, retry_count: int = 0) -> List[Dict]:
    """Возвращает прямых потомков страницы Confluence (без рекурсии).

    use_http=True — потомки запрашиваются через прямой HTTP-доступ
    (page-tree endpoint), в обход REST API.
    """
    if use_http:
        # HTTP-ветка имеет собственную обработку ошибок и логирование
        # внутри fetch_child_pages_via_http; retry здесь не применяем.
        return fetch_child_pages_via_http(page_id)
    try:
        return confluence.get_child_pages(page_id) or []
    except ReadTimeout:
        if retry_count < MAX_RETRIES:
            wait = 2 ** retry_count
            logger.warning(
                "Timeout getting children of %s, retry %d/%d in %ds",
                page_id, retry_count + 1, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            return get_direct_children(page_id, retry_count=retry_count + 1)
        logger.error("Failed to get children of page %s after %d retries", page_id, MAX_RETRIES)
        return []
    except Exception as e:
        logger.error("Failed to get children of page %s: %s", page_id, e)
        return []


# Каркас без содержания: теги, разделители markdown-таблиц, пробелы. После отката
# правки от HTML-острова остаётся пустая обвязка <table><tbody></tbody></table> —
# для читателя это пустая страница, поэтому в проверке «опустела ли целиком» она
# за содержимое не считается.
_STRUCTURE_ONLY_RE = re.compile(r"<[^>]+>|&[a-zA-Z]+;|[|\-:`~*#>\s]+")


def decide_page_flag(content_md: str, forced: str = "") -> Tuple[str, Optional[dict]]:
    """
    Нужен ли странице флаг заморозки `unapproved_jira` и что сказать в отчёте.

    Правило: если ВЕСЬ состав страницы снимается откатом и принадлежит ОДНОЙ
    задаче — страница в ПРОМ не входит, флаг ставится по этой задаче. Иначе
    решение «нет в ПРОМ» живёт только в списке --unapproved-jira, а забытая там
    задача даёт преждевременный apply неутверждённого в ПРОМ — тихая порча.

    Ограничители (важнее самого срабатывания): несколько задач — угадывать ID
    нельзя, случай уходит в отчёт; страница пуста или опустела лишь частично —
    флага нет (чёрный текст = утверждено, ложная заморозка недопустима);
    уже стоящий флаг не перезаписывается, но расхождение с вычисленным попадает
    в отчёт.

    Возвращает (флаг, запись отчёта или None).
    """
    from app.scripts.CI.critic import collect_task_occurrences, process_text

    auto, note = "", None
    if content_md.strip():
        rest, _ = process_text(content_md, "reject", None)
        if not _STRUCTURE_ONLY_RE.sub("", rest).strip():     # осталась одна обвязка
            tasks = sorted(collect_task_occurrences(content_md))
            if len(tasks) == 1:
                auto = tasks[0]
            elif len(tasks) > 1 and not forced:
                # Вопрос имеет смысл, только пока решения нет: если задача названа
                # списком --unapproved-jira, владелец уже выбрал — молчим.
                note = {"kind": "несколько задач", "tasks": tasks}
    if forced and auto and forced != auto:
        note = {"kind": "расхождение", "tasks": [forced, auto]}
    return (forced or auto), note


def _resolve_page_content(page_data: Dict, include_unapproved: bool, critic: bool,
                          critic_acc: Optional[dict], name: str) -> str:
    """Возвращает markdown-содержимое страницы по выбранному режиму.

    approved (по умолчанию) / all (--all) / critic (--tasks). Критик-режим строит карту
    «цвет→задача» из raw_html (до удаления истории), гоняет create_critic_extractor и
    попутно наполняет аккумулятор отчёта (карта уже построена — без повторного разбора).

    Эмуляция похода в RAG (2026-08-07): при непустом config.UNAPPROVED_JIRA_IDS
    джира из ЧЁРНОЙ строки истории, входящая в список, означает «состав страницы
    не утверждён». Реакция по режиму: critic — весь чёрный контент оборачивается
    вставками этой задачи; approved — страница пропускается с предупреждением
    (неутверждённому в подтверждённой выгрузке не место); all — контент полный,
    но frontmatter получает status: draft (признак кладётся в page_data).
    """
    import app.config as _config
    forced = None
    if _config.UNAPPROVED_JIRA_IDS:
        from app.color_map import find_forced_unapproved
        forced = find_forced_unapproved(
            page_data.get("raw_html", "") or "", _config.UNAPPROVED_JIRA_IDS)
        if forced:
            page_data["_forced_unapproved"] = forced.task
            for w in forced.warnings:
                logger.warning("  ⚠ '%s': %s", name, w)
            if not critic and not include_unapproved:
                logger.warning(
                    "  ⚠ '%s': состав не утверждён (%s) — страница пропущена в "
                    "approved-режиме (используйте --tasks или --all)", name, forced.task)
                return ""

    if critic:
        from app.color_map import build_color_task_map, survey_body_colors
        from app.content_extractor import create_critic_extractor
        raw = page_data.get("raw_html", "") or ""
        from app.scripts.CI.critic import postpass_drop_contained_deletions
        result = build_color_task_map(raw)
        extractor = create_critic_extractor(result.color_to_task)
        content = extractor.extract(raw)
        # Пост-проход 4.5.4: отбросить {--C2: X--}, где X входит в состав чужой вставки.
        content, dropped = postpass_drop_contained_deletions(content)
        if forced:
            # Форс-обёртка неутверждённого состава: чёрные куски → вставки задачи.
            from app.unapproved_wrap import wrap_unapproved
            content, wrap_rep = wrap_unapproved(content, forced.task)
            marks = (wrap_rep["blocks"] + wrap_rep["tables_wrapped"]
                     + wrap_rep["table_rows"] + wrap_rep["island_rows"])
            logger.info("  ⚑ '%s': состав не утверждён (%s) — обёрнуто %d фрагментов",
                        name, forced.task, marks)
            for w in wrap_rep["warnings"]:
                logger.warning("  ⚠ '%s': %s", name, w)
            if critic_acc is not None:
                # Форс-задача — в реестр мигрировавших задач (по ней будут делать apply).
                entry = critic_acc["tasks"].setdefault(
                    forced.task, {"color": "black (forced-unapproved)",
                                  "confidence": "forced", "pages": set(),
                                  "markers": 0, "date": None})
                entry["pages"].add(name)
                entry["markers"] += marks
                # дата первой записи — в предложение порядка вливания (2026-08-10)
                if forced.first_seen and (entry["date"] is None
                                          or forced.first_seen < entry["date"]):
                    entry["date"] = forced.first_seen
        # ГЕЙТ НА ВЫХОДЕ: конвейер не имеет права выпустить разметку, которую
        # его же critic не читает. Литеральная вложенность маркеров валит
        # apply/reject жёстко и обрывает прогон на первом файле — узнавали об
        # этом только на боевом дереве у команды (инцидент 2026-09-05).
        # Не падаем: страница уже собрана, терять её из-за разметки нельзя —
        # предупреждаем и кладём в отчёт как позицию ручного разбора.
        from app.scripts.CI.critic import find_literal_nesting, find_markers_in_code
        nesting = find_literal_nesting(content)
        for nest in nesting:
            logger.warning(
                "  ⚠ '%s': строка %d — маркер %s содержит внутри %s "
                "(литеральная вложенность; чинится run-repair --flatten-nested)",
                name, nest["line"], nest["outer"], ", ".join(nest["inner"]) or "другой маркер")
        # Второй случай той же природы: блок кода накрыл маркеры. Переносится он
        # байт-в-байт, поэтому apply/reject внутрь не заглядывают — неутверждённое
        # остаётся в «чистом ПРОМ» молча (инцидент 2026-09-06: блок в 20 689
        # символов прятал 65 маркеров).
        in_code = find_markers_in_code(content)
        for blk in in_code:
            logger.warning(
                "  ⚠ '%s': строка %d — блок кода (%d символов) накрыл %d маркеров "
                "задач %s; apply/reject их не увидят",
                name, blk["line"], blk["chars"], blk["markers"], ", ".join(blk["tasks"]))

        if critic_acc is not None:
            from app.scripts.migrate_colors import accumulate_page
            accumulate_page(critic_acc, name, result, survey_body_colors(raw, result))
            for nest in nesting:
                critic_acc["literal_nesting"].append(
                    {"page": name, "line": nest["line"],
                     "outer": nest["outer"], "inner": nest["inner"]})
            for blk in in_code:
                critic_acc["markers_in_code"].append(
                    {"page": name, "line": blk["line"], "chars": blk["chars"],
                     "markers": blk["markers"], "tasks": blk["tasks"]})
            for rec in extractor._critic_report:  # признаки 1 (вложенность) и 2 (примыкание)
                critic_acc["nested"].append(
                    {"page": name, "tasks": rec["tasks"], "html": rec["html"][:500],
                     "confidence": rec.get("confidence", "high")})
            for d in dropped:  # пост-проход: переклассифицировано и отброшено
                critic_acc["nested"].append(
                    {"page": name, "tasks": [d["task"], d["insert_task"]],
                     "html": d["text"][:500], "confidence": "post-pass"})
        return content
    content_field = "full_content" if include_unapproved else "approved_content"
    return page_data.get(content_field, "")


def save_page_file(
    page_data: Dict,
    page_id: str,
    title: str,
    service_code: str,
    source: str,
    filepath: Path,
    stats: Dict,
    page_registry: Dict[str, Path],
    title_registry: Dict[str, Path],
    include_unapproved: bool = False,
    critic: bool = False,
    critic_acc: Optional[dict] = None,
) -> bool:
    """Сохраняет страницу Confluence как .md файл с frontmatter.

    Регистрирует сохранённый файл в реестрах page_registry и title_registry
    для последующего разрешения ссылок на этапе post-processing.
    Возвращает True при успешном сохранении.

    Режим содержимого: approved (по умолчанию), all (include_unapproved), critic (critic —
    полное содержимое с маркерами CriticMarkup). critic — вариант «полного» содержимого.
    """
    content_md = _resolve_page_content(page_data, include_unapproved, critic, critic_acc, title)
    if not content_md or not content_md.strip():
        mode = "critic" if critic else ("full_content" if include_unapproved else "approved_content")
        logger.warning("  Страница '%s' (id=%s) не содержит %s, пропущена", title, page_id, mode)
        stats["skipped"] += 1
        return False

    # Коллизия имён: файл с таким же заголовком уже есть. Перезаписываем с
    # предупреждением (миграция разовая, далее работа по ФС под git — перезапись
    # безопасна и откатываема). См. app/scripts/CI/analysis-naming-strategies.md.
    if filepath.exists():
        logger.warning("  ⚠ Файл уже существует — ПЕРЕЗАПИСЫВАЕМ: %s", filepath)
        stats["overwritten"] = stats.get("overwritten", 0) + 1

    # doc_id — location-независимая смарт-ссылка {{SERVICE: title}}, не зависит от
    # пути файла: страницу можно двигать по дереву без смены doc_id.
    # См. app/scripts/CI/design-smart-link-doc-id.md.
    doc_id = build_doc_id(service_code, title)

    # Миграция картинок: скачиваем вложения в img/ рядом с .md и заменяем плейсхолдеры
    # confluence-attachment:// на относительные ссылки (если конвертация шла с MIGRATE_IMAGES).
    import app.config as _config
    if _config.MIGRATE_IMAGES:
        from app.image_migrator import migrate_images_in_content
        content_md, downloaded, failed = migrate_images_in_content(
            content_md, str(page_id), filepath
        )
        if downloaded or failed:
            logger.info("  🖼 Картинки '%s': скачано %d, ошибок %d", title, downloaded, failed)

    # Миграция файлов-вложений (--with-attachments): ссылки /download/attachments/…
    # скачиваются в files/ рядом с .md и заменяются относительными.
    if _config.MIGRATE_ATTACHMENTS:
        from app.attachment_migrator import migrate_file_attachments_in_content
        content_md, a_dl, a_fail, a_skip = migrate_file_attachments_in_content(
            content_md, str(page_id), filepath
        )
        if a_dl or a_fail or a_skip:
            logger.info("  📎 Вложения '%s': скачано %d, ошибок %d, пропущено по размеру %d",
                        title, a_dl, a_fail, a_skip)

    page = {
        "id": page_id,
        "title": title,
        "approved_content": content_md,
        "requirement_type": page_data.get("requirement_type", "unknown"),
    }

    # Точный признак неподтверждённого контента: full_content и approved_content
    # различаются только при наличии цветных (неподтверждённых) фрагментов.
    # Форс-режим (состав не утверждён по списку задач) — тоже неподтверждённое:
    # страница обязана получить status: draft и в режиме --all, где маркеров нет.
    has_unapproved = (page_data.get("full_content") != page_data.get("approved_content")
                      or bool(page_data.get("_forced_unapproved")))

    # Сторож заморозки: если ВЕСЬ состав страницы принадлежит одной задаче, флаг
    # `unapproved_jira` проставляется сам. Иначе решение «этой страницы нет в ПРОМ»
    # живёт только в списке --unapproved-jira, и забытая там задача даёт
    # преждевременный apply неутверждённого в ПРОМ — худший вид тихой порчи.
    # Записывает ЭКСПОРТЁР, а не reject: флаг обязан лежать в архиве, из которого
    # опись Doc-as-Code читает `frozen:<ID>`, а reject правит производную копию,
    # которая пересоздаётся из архива на каждой задаче этапа 4.
    forced = page_data.get("_forced_unapproved", "") or ""
    auto_flag, flag_note = ("", None) if not critic else decide_page_flag(content_md, forced)
    if critic_acc is not None:
        if flag_note:
            critic_acc["auto_page_flag"].append({"page": title, **flag_note})
        elif auto_flag and not forced:
            critic_acc["auto_page_flag"].append(
                {"page": title, "kind": "поставлен", "tasks": [auto_flag]})

    frontmatter = page_to_frontmatter(
        page, service_code, source, doc_id,
        include_unapproved=include_unapproved or critic,  # критик — полное содержимое
        has_unapproved=has_unapproved,
        unapproved_jira=forced or auto_flag,
    )
    write_md_file(filepath, frontmatter, content_md)

    page_registry[page_id] = filepath
    title_registry[_title_key(title)] = filepath

    stats["migrated"] += 1
    return True


def migrate_subtree(
    page_id: str,
    service_code: str,
    source: str,
    output_dir: Path,
    exclusion_rules,
    stats: Dict,
    visited: set,
    page_registry: Dict[str, Path],
    title_registry: Dict[str, Path],
    depth: int = 0,
    use_http: bool = False,
    include_unapproved: bool = False,
    critic: bool = False,
    critic_acc: Optional[dict] = None,
) -> None:
    """Рекурсивно мигрирует страницу Confluence и всё её поддерево.

    Принцип "файл рядом с папкой":
    • Страница с детьми → файл <title>.md в текущей директории (если есть контент)
      + папка <title>/ рядом для дочерних страниц.
    • Страница без детей → файл <title>.md в текущей директории.
    • Страница без контента, но с детьми → только папка <title>/ без файла рядом
      (виртуальный контейнер).

    use_http=True — и контент, и потомки, и заголовки запрашиваются через
    прямой HTTP-доступ (как браузер), в обход Confluence REST API.
    include_unapproved=True — в .md пишется всё содержимое (full_content),
    иначе только подтверждённые фрагменты (approved_content).
    """
    if page_id in visited:
        logger.warning("Обнаружена циклическая ссылка для page_id=%s, пропускаем", page_id)
        return
    visited.add(page_id)

    indent = "  " * depth

    # Загружаем данные страницы (контент + метаданные)
    page_data = get_page_data(page_id, use_http=use_http)

    # Определяем заголовок
    if page_data:
        title = page_data.get("title", "")
    elif use_http:
        title = fetch_page_title_via_http(page_id) or ""
    else:
        title = get_page_title_only(page_id) or ""

    if not title:
        logger.warning("%sНе удалось определить заголовок для page_id=%s, пропускаем", indent, page_id)
        stats["skipped"] += 1
        return

    # Проверяем правила исключения
    if is_page_excluded(title, exclusion_rules):
        logger.info("%s[исключена] '%s' (id=%s)", indent, title, page_id)
        return

    # Получаем прямых потомков и фильтруем исключённые
    children = [
        c for c in get_direct_children(page_id, use_http=use_http)
        if not is_page_excluded(c.get("title", ""), exclusion_rules)
    ]

    dir_name = safe_filename(title)

    if children:
        # Родительская страница с дочерними:
        # 1) Сохраняем собственный контент как <dir_name>.md в текущей output_dir
        # 2) Создаём папку <dir_name>/ рядом для детей
        # 3) Рекурсивно обходим детей внутри этой папки

        # Критик — вариант «полного» содержимого, поэтому наличие проверяем по full_content
        # (иначе целиком цветная страница без чёрного текста считалась бы пустой).
        content_field = "full_content" if (include_unapproved or critic) else "approved_content"
        has_own_content = bool(
            page_data and page_data.get(content_field, "").strip()
        )

        if has_own_content:
            filepath = output_dir / f"{dir_name}.md"
            if save_page_file(page_data, page_id, title, service_code, source, filepath, stats,
                              page_registry, title_registry, include_unapproved=include_unapproved,
                              critic=critic, critic_acc=critic_acc):
                logger.info(
                    "%s[file+dir] %s.md + %s/  (id=%s, дочерних=%d)",
                    indent, dir_name, dir_name, page_id, len(children),
                )
        else:
            logger.info(
                "%s[virtual-dir] %s/  (id=%s, дочерних=%d, без собственного контента)",
                indent, dir_name, page_id, len(children),
            )

        page_dir = output_dir / dir_name
        page_dir.mkdir(parents=True, exist_ok=True)

        for child in children:
            migrate_subtree(
                child["id"],
                service_code,
                source,
                page_dir,
                exclusion_rules,
                stats,
                visited,
                page_registry,
                title_registry,
                depth + 1,
                use_http=use_http,
                include_unapproved=include_unapproved,
                critic=critic,
                critic_acc=critic_acc,
            )

    else:
        # Листовая страница → сохраняем как <dir_name>.md
        if not page_data:
            logger.warning("%sНет данных для '%s' (id=%s), пропущена", indent, title, page_id)
            stats["skipped"] += 1
            return

        filepath = output_dir / f"{dir_name}.md"
        if save_page_file(page_data, page_id, title, service_code, source, filepath, stats,
                          page_registry, title_registry, include_unapproved=include_unapproved,
                          critic=critic, critic_acc=critic_acc):
            logger.info("%s[ok] %s.md  (id=%s)", indent, dir_name, page_id)


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def seed_registries_from_disk(
    page_registry: Dict[str, Path],
    title_registry: Dict[str, Path],
) -> int:
    """Подмешивает в реестры страницы, уже лежащие на диске в OUTPUT_ROOT.

    Реестры из Pass 1 содержат только страницы текущего запуска. Но ссылки
    часто ведут на страницы из других поддеревьев/сервисов, мигрированных
    ранее. У каждого .md-файла во frontmatter есть confluence_page_id и title —
    этого достаточно, чтобы разрешить и ID-ссылки (confluence://ID), и
    title-ссылки (confluence://title/...) на ранее сохранённые файлы.

    Записи текущего запуска имеют приоритет: уже существующие ключи не
    перезаписываются. Возвращает число подмешанных файлов.
    """
    if not OUTPUT_ROOT.exists():
        return 0

    known = set(page_registry.values()) | set(title_registry.values())
    added = 0
    for filepath in OUTPUT_ROOT.rglob("*.md"):
        if filepath in known:
            continue
        try:
            text = filepath.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict):
            continue

        page_id = fm.get("confluence_page_id")
        if page_id:
            page_registry.setdefault(str(page_id), filepath)
        title = fm.get("title")
        if title:
            title_registry.setdefault(_title_key(str(title)), filepath)
        if page_id or title:
            added += 1
    return added


def resolve_confluence_links(
    page_registry: Dict[str, Path],
    title_registry: Dict[str, Path],
    files: Optional[set] = None,
) -> Tuple[int, int]:
    """Pass 2: заменяет плейсхолдеры confluence:// на относительные пути в сохранённых .md файлах.

    files — множество файлов, которые нужно обработать (переписать). Если не
    задано, берутся все файлы из реестров. При подмешивании ранее сохранённых
    страниц (seed_registries_from_disk) сюда передаётся только набор текущего
    запуска, чтобы не трогать чужие файлы — подмешанные служат лишь целями ссылок.

    Возвращает (resolved, unresolved) — количество разрешённых и неразрешённых ссылок.
    Неразрешённые ID-ссылки заменяются абсолютным URL Confluence.
    Неразрешённые title-ссылки оставляются как текст в скобках без URL.
    """
    counts = {"resolved": 0, "unresolved": 0}

    if files is None:
        files = set(page_registry.values()) | set(title_registry.values())

    for filepath in files:
        if not filepath.exists():
            continue

        text = filepath.read_text(encoding="utf-8")

        def replace_id(m: re.Match) -> str:
            link_text, page_id, title_encoded = m.group(1), m.group(2), m.group(3)
            target = page_registry.get(page_id)
            # Fallback: ID нет в реестре, но плейсхолдер несёт заголовок — пробуем по нему.
            if not target and title_encoded:
                raw_title = _decode_title_path(title_encoded)
                target = title_registry.get(_title_key(raw_title))
            if target:
                rel = Path(os.path.relpath(target, filepath.parent))
                counts["resolved"] += 1
                return f"[{link_text}]({str(rel).replace(chr(92), '/')})"
            counts["unresolved"] += 1
            return f"[{link_text}]({CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={page_id})"

        def replace_title(m: re.Match) -> str:
            link_text, title_encoded = m.group(1), m.group(2)
            # title_encoded может быть "SPACE/Title+Words" или просто "Title+Words"
            raw_title = _decode_title_path(title_encoded)
            target = title_registry.get(_title_key(raw_title))
            if target:
                rel = Path(os.path.relpath(target, filepath.parent))
                counts["resolved"] += 1
                return f"[{link_text}]({str(rel).replace(chr(92), '/')})"
            counts["unresolved"] += 1
            return f"[{link_text}]"

        def replace_html_id(m: re.Match) -> str:
            # HTML-форма внутри таблиц: заменяем только href, текст и </a> остаются.
            page_id, title_encoded = m.group(1), m.group(2)
            target = page_registry.get(page_id)
            # Fallback по заголовку (title_encoded приходит HTML-экранированным,
            # &quot; и пр. снимает _title_key через html.unescape).
            if not target and title_encoded:
                raw_title = _decode_title_path(title_encoded)
                target = title_registry.get(_title_key(raw_title))
            if target:
                rel = Path(os.path.relpath(target, filepath.parent))
                counts["resolved"] += 1
                return f'<a href="{str(rel).replace(chr(92), "/")}">'
            counts["unresolved"] += 1
            return f'<a href="{CONFLUENCE_BASE_URL}/pages/viewpage.action?pageId={page_id}">'

        def replace_html_title(m: re.Match) -> str:
            title_encoded = m.group(1)
            raw_title = _decode_title_path(title_encoded)
            target = title_registry.get(_title_key(raw_title))
            if target:
                rel = Path(os.path.relpath(target, filepath.parent))
                counts["resolved"] += 1
                return f'<a href="{str(rel).replace(chr(92), "/")}">'
            counts["unresolved"] += 1
            # Неразрешённый title в HTML-теге: <a> нельзя «снять» одной заменой href,
            # поэтому ведём на канонический Confluence-URL вида /display/SPACE/Title.
            parts = title_encoded.split("/")
            if len(parts) > 1 and parts[0]:
                return f'<a href="{CONFLUENCE_BASE_URL}/display/{parts[0]}/{parts[-1]}">'
            return f'<a href="{CONFLUENCE_BASE_URL}/dosearchsite.action?queryString={parts[-1]}">'

        new_text = _LINK_BY_ID_RE.sub(replace_id, text)
        new_text = _LINK_BY_TITLE_RE.sub(replace_title, new_text)
        new_text = _HTML_LINK_BY_ID_RE.sub(replace_html_id, new_text)
        new_text = _HTML_LINK_BY_TITLE_RE.sub(replace_html_title, new_text)

        if new_text != text:
            filepath.write_text(new_text, encoding="utf-8")

    return counts["resolved"], counts["unresolved"]


# Имя генерируемого навигационного файла секции и служебная папка вложений.
INDEX_FILENAME = "index.md"
_IMG_DIRNAME = "img"


def _read_md_title(md_path: Path) -> str:
    """Возвращает title из frontmatter .md-файла, иначе имя файла без расширения."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return md_path.stem
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        if isinstance(fm, dict) and fm.get("title"):
            return str(fm["title"])
    return md_path.stem


def _dir_has_listing(d: Path) -> bool:
    """True, если в папке есть что перечислять: .md (кроме index.md) или не-img подпапка."""
    for p in d.iterdir():
        if p.is_file() and p.suffix == ".md" and p.name != INDEX_FILENAME:
            return True
        if p.is_dir() and p.name != _IMG_DIRNAME:
            return True
    return False


def generate_section_indexes(base_dir: Path) -> int:
    """Pass 3 (опционально, флаг --with-index): кладёт навигационный index.md в каждую
    папку дерева под base_dir.

    index.md перечисляет относительными ссылками вложенные .md (с заголовками из их
    frontmatter) и подпапки (ссылка на их index.md). Сам файл намеренно БЕЗ frontmatter:
    линтер его пропускает по имени, а индексатор — по отсутствию service_code, поэтому в
    требования/ChromaDB он не попадает. Папки img/ и сам index.md в перечень не входят.

    Цели ссылок оборачиваются в угловые скобки <...>, чтобы скобки/иные спецсимволы в
    кириллических именах (напр. «(115)») не ломали markdown-ссылку.

    Идемпотентно: при повторном запуске index.md перезаписывается заново. Возвращает
    число созданных index-файлов.
    """
    if not base_dir.exists():
        return 0

    created = 0
    dirs = [base_dir] + [d for d in base_dir.rglob("*") if d.is_dir()]
    for d in dirs:
        if d.name == _IMG_DIRNAME:
            continue

        md_children = sorted(
            (p for p in d.iterdir()
             if p.is_file() and p.suffix == ".md" and p.name != INDEX_FILENAME),
            key=lambda p: p.name,
        )
        sub_dirs = sorted(
            (p for p in d.iterdir() if p.is_dir() and p.name != _IMG_DIRNAME),
            key=lambda p: p.name,
        )
        if not md_children and not sub_dirs:
            continue

        lines = [f"# {d.name}", ""]
        for p in md_children:
            lines.append(f"- [{_read_md_title(p)}](<{p.name}>)")
        for sd in sub_dirs:
            # Ссылку на index.md подпапки даём только если он там будет (есть что листать),
            # иначе показываем имя папки без ссылки — чтобы не плодить битые ссылки.
            if _dir_has_listing(sd):
                lines.append(f"- [{sd.name}/](<{sd.name}/{INDEX_FILENAME}>)")
            else:
                lines.append(f"- {sd.name}/")
        lines.append("")

        (d / INDEX_FILENAME).write_text("\n".join(lines), encoding="utf-8")
        created += 1

    return created


def main():
    # Версия при старте: сверка бандлов на внутреннем/внешнем контуре глазами
    # (совпадать должны и номер, и отпечаток исходников).
    from app.version import banner
    logger.info(banner("confluence-tree-exporter"))
    logger.info("")

    # Флаги --http, --all, --keep-history, --with-images, --with-index и
    # --drop-strikethrough можно указать в любом месте аргументов; они переопределяют
    # соответствующие значения из конфигурации.
    flags = {"--http", "--all", "--tasks", "--keep-history", "--with-images",
             "--with-attachments", "--with-index", "--drop-strikethrough"}

    # Ключ со значением: --unapproved-jira <file.json> — список Jira ID неутверждённых
    # задач (эмуляция похода в RAG). Извлекается ДО построения позиционных args.
    argv = list(sys.argv[1:])
    unapproved_path = None
    if "--unapproved-jira" in argv:
        k = argv.index("--unapproved-jira")
        if k + 1 >= len(argv) or argv[k + 1].startswith("--"):
            print("ОШИБКА: --unapproved-jira требует путь к JSON-файлу со списком Jira ID.")
            sys.exit(2)
        unapproved_path = argv[k + 1]
        del argv[k:k + 2]

    args = [a for a in argv if a not in flags]
    use_http = ("--http" in sys.argv) or CONFLUENCE_USE_HTTP
    include_unapproved = ("--all" in sys.argv) or MIGRATE_INCLUDE_UNAPPROVED
    critic = "--tasks" in sys.argv
    keep_history = "--keep-history" in sys.argv
    with_images = "--with-images" in sys.argv
    with_attachments = "--with-attachments" in sys.argv
    with_index = "--with-index" in sys.argv
    drop_strikethrough = "--drop-strikethrough" in sys.argv

    # --tasks и --all взаимоисключающие: оба берут ПОЛНОЕ содержимое, но --tasks добавляет
    # маркеры CriticMarkup, а --all — плоский текст. Одно представление на файл.
    if critic and ("--all" in sys.argv):
        print("ОШИБКА: --tasks и --all взаимоисключающие (оба — полное содержимое). "
              "Выберите одно.")
        sys.exit(2)

    if len(args) < 3:
        print("Usage: python migrate_confluence_tree.py "
              "<page_id> <service_code> <subdir> [source] [--http] [--all] [--keep-history] "
              "[--with-images] [--with-index]")
        print("Example: python migrate_confluence_tree.py "
              "12345 CORP_CARDS лимиты DBOCORPESPLN")
        print("Example (прямой HTTP, в обход API): python migrate_confluence_tree.py "
              "12345 CORP_CARDS лимиты DBOCORPESPLN --http")
        print("Example (всё содержимое, включая неподтверждённое): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --all")
        print("Example (разметка задач CriticMarkup + манифест/отчёт): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --tasks")
        print("Example (список неутверждённых задач — состав таких страниц метится "
              "вставками): python migrate_confluence_tree.py 12345 CORP_CARDS лимиты "
              "--tasks --unapproved-jira unapproved.json")
        print("Example (сохранить раздел 'История изменений'): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --keep-history")
        print("Example (мигрировать картинки в img/ рядом с .md): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --with-images")
        print("Example (сгенерировать навигационные index.md по папкам, для SSG): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --with-index")
        print("Example (исключить зачёркнутый текст при любом раскладе, в т.ч. с --all): "
              "python migrate_confluence_tree.py 12345 CORP_CARDS лимиты --all --drop-strikethrough")
        sys.exit(1)

    # Список неутверждённых задач (эмуляция похода в RAG): JSON — либо список строк,
    # либо {"unapproved_jira": [...]}. Ключа нет — конвейер работает как раньше.
    if unapproved_path:
        import json as _json
        import app.config as _cfg
        from app.color_map import TASK_ID_RE as _task_re
        try:
            data = _json.loads(Path(unapproved_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"ОШИБКА: не удалось прочитать {unapproved_path}: {e}")
            sys.exit(2)
        ids = data.get("unapproved_jira") if isinstance(data, dict) else data
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            print(f"ОШИБКА: {unapproved_path} должен содержать список строк Jira ID "
                  '(["GBO-1", ...]) или {"unapproved_jira": [...]}.')
            sys.exit(2)
        bad = [x for x in ids if x.strip() and not _task_re.fullmatch(x.strip())]
        if bad:
            print(f"ОШИБКА: не похожи на Jira ID: {bad} (маска PROJECT-123).")
            sys.exit(2)
        _cfg.UNAPPROVED_JIRA_IDS = {x.strip() for x in ids if x.strip()}
        logger.info("Неутверждённые задачи (эмуляция RAG): %d id из %s",
                    len(_cfg.UNAPPROVED_JIRA_IDS), unapproved_path)

    # Переопределяем политику удаления истории на время процесса (вариант A).
    # remove_history_sections() читает app.config.REMOVE_HISTORY_SECTIONS динамически.
    if keep_history:
        import app.config as _config
        _config.REMOVE_HISTORY_SECTIONS = False

    # Включаем миграцию картинок на время процесса. Фабрики экстракторов читают
    # app.config.MIGRATE_IMAGES динамически — флаг должен быть выставлен ДО конвертации.
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

    root_page_id = args[0].strip()
    service_code = args[1]
    subdir = args[2]
    source = args[3] if len(args) > 3 else "DBOCORPESPLN"

    exclusion_rules = load_exclusion_rules(PAGE_EXCLUSION_RULES_FILE)

    service_part = service_code.lower().replace("_", "-")
    base_output_dir = OUTPUT_ROOT / service_part / subdir

    logger.info("Migrating Confluence tree from page_id=%s ...", root_page_id)
    logger.info("Output: %s", base_output_dir)
    logger.info("Access mode: %s", "direct HTTP (в обход API)" if use_http else "REST API")
    logger.info("Content mode: %s",
                "CriticMarkup (маркеры задач) [--tasks]" if critic
                else ("ВСЁ содержимое (включая неподтверждённое)" if include_unapproved
                      else "только подтверждённые фрагменты"))
    logger.info("History mode: %s",
                "СОХРАНЯТЬ раздел истории" if keep_history else "удалять раздел истории")
    logger.info("Images mode: %s",
                "СКАЧИВАТЬ картинки в img/" if with_images else "картинки игнорируются")
    if drop_strikethrough and critic:
        strike_mode = ("ИСКЛЮЧАТЬ только ЧЁРНОЕ зачёркнутое; цветное сохраняется "
                       "маркерами {--…--} (им управляет critic)")
    elif drop_strikethrough:
        strike_mode = "ИСКЛЮЧАТЬ зачёркнутый текст (при любом раскладе)"
    else:
        strike_mode = "по умолчанию (зачёркнутое — как обычно)"
    logger.info("Strikethrough mode: %s", strike_mode)
    logger.info("Index mode: %s",
                "генерировать index.md по папкам" if with_index else "без index.md")
    logger.info("")

    stats: Dict = {"migrated": 0, "skipped": 0, "overwritten": 0}
    visited: set = set()
    page_registry: Dict[str, Path] = {}
    title_registry: Dict[str, Path] = {}

    # Аккумулятор манифеста/отчёта миграции цвета — наполняется только в режиме --tasks.
    critic_acc = None
    if critic:
        from app.scripts.migrate_colors import new_accumulator
        critic_acc = new_accumulator()

    logger.info("Pass 1: traversing Confluence tree ...")
    migrate_subtree(
        root_page_id,
        service_code,
        source,
        base_output_dir,
        exclusion_rules,
        stats,
        visited,
        page_registry,
        title_registry,
        use_http=use_http,
        include_unapproved=include_unapproved,
        critic=critic,
        critic_acc=critic_acc,
    )

    logger.info("")
    logger.info("Pass 2: resolving internal links ...")
    # Файлы текущего запуска фиксируем ДО подмешивания — только их переписываем.
    current_files = set(page_registry.values()) | set(title_registry.values())
    seeded = seed_registries_from_disk(page_registry, title_registry)
    if seeded:
        logger.info("  Подмешано из ранее сохранённых .md (frontmatter): %d", seeded)
    resolved, unresolved = resolve_confluence_links(
        page_registry, title_registry, files=current_files
    )

    indexes = 0
    if with_index:
        logger.info("")
        logger.info("Pass 3: generating section index.md ...")
        indexes = generate_section_indexes(base_output_dir)

    # Режим --tasks: пишем реестр мигрировавших задач (manifest) и отчёт цвета (ТЗ 4.9/4.10).
    if critic and critic_acc is not None:
        from datetime import date as _date
        from app.scripts.migrate_colors import finalize, write_reports
        logger.info("")
        logger.info("Pass 4: writing migration manifest and color report ...")
        manifest, report = finalize(critic_acc, service_code, _date.today().isoformat())
        write_reports(base_output_dir, manifest, report)
        rst = report["stats"]
        logger.info("  Отчёт цвета: %s", base_output_dir / "migration-colors-report.md")
        logger.info("  Задач в манифесте: %d; ручной разбор: %d позиций (уплощений: %d)",
                    len(manifest["tasks"]), rst["positions_manual_review"],
                    rst["nested_flattened"])

    logger.info("")
    logger.info("Migration complete:")
    logger.info("  Migrated:           %d", stats["migrated"])
    logger.info("  Overwritten:        %d  (коллизии имён — перезаписаны)", stats["overwritten"])
    logger.info("  Skipped:            %d", stats["skipped"])
    logger.info("  Links resolved:     %d", resolved)
    logger.info("  Links unresolved:   %d  (replaced with absolute Confluence URLs)", unresolved)
    if with_index:
        logger.info("  Index files:        %d", indexes)
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Заполните пустые поля frontmatter вручную (owner, jira_id)")
    logger.info("  2. Запустите: python scripts/lint_frontmatter.py")
    logger.info("  3. git add, commit, push, open PR")


if __name__ == "__main__":
    main()