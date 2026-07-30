# app/color_map.py
#
# Модуль 1 (срез 1a), ТЗ п. 4.2: построение карты «цвет требования → задача Jira» из
# таблицы секции «История изменений» страницы Confluence.
#
# Принцип оформления в Confluence: текст в столбце «Описание» строки истории написан тем же
# цветом, что и требования, внесённые этой задачей на странице. Отсюда:
#   цвет фрагмента → строка истории с тем же цветом описания → столбец «Задача в Jira» → id.
#
# Ключевые правила ТЗ:
#   • столбцы ищем ТОЛЬКО по нормализованному тексту заголовка, НИКОГДА по позиции (4.2.б);
#   • карта строится ЗАНОВО для каждой страницы, между страницами не переиспользуется (4.2.д);
#   • в карту идут только НЕ-чёрные цвета строк: чёрная строка = задача уже на ПРОМ (согласовано);
#   • коллизия «один цвет → разные задачи» → берём строку с самой поздней датой, confidence=low (4.2.е);
#   • канонический вход — rendered view, где jira-макрос часто сломан и ключа не содержит:
#     резолвер работает по цепочке макрос-с-ключом / ссылка browse/ / plain-text / regex (4.2.г).
#
# Модуль автономный: только stdlib + bs4; переиспользует определение таблицы истории из
# history_cleaner и классификацию чёрного из style_utils.

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from app.history_cleaner import _is_history_table
from app.utils.style_utils import (
    normalize_color, is_black_color, is_ignored_color, is_near_black, delta_e_to_black,
)

# Синонимы заголовков столбцов (нормализованные). Вынесено в «конфиг» модуля (ТЗ п. 4.2.б).
COLUMN_SYNONYMS = {
    "date": {"дата", "date"},
    "description": {"описание", "description", "desc"},
    "author": {"автор", "author"},
    "jira": {"задача в jira", "задача в джира", "задача", "jira", "ticket", "issue",
             "задача jira", "номер задачи"},
}

# Маска идентификатора задачи Jira (ТЗ п. 4.2.г, финальный fallback).
TASK_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


def _normalize_header(text: str) -> str:
    """Нормальная форма заголовка: нижний регистр, схлопнутые пробелы, ё→е (ТЗ п. 4.2.б)."""
    return re.sub(r"\s+", " ", text.strip()).lower().replace("ё", "е")


@dataclass
class RowInfo:
    """Разобранная строка истории с ненулевой (не-чёрной) разметкой описания."""
    colors: List[str]                 # нормализованные #rrggbb не-чёрные цвета описания
    task_ids: List[str]               # все извлечённые id (первый — основной)
    date: Optional[Tuple[int, int, int]]  # (год, месяц, день) для разрешения коллизий
    raw_html: str                     # исходный HTML строки (для отчёта)


@dataclass
class HistoryMapResult:
    """Результат разбора истории одной страницы (ТЗ п. 4.2)."""
    color_to_task: Dict[str, str] = field(default_factory=dict)   # #rrggbb -> TASK-ID
    confidence: Dict[str, str] = field(default_factory=dict)      # #rrggbb -> 'high'|'low'
    collisions: List[dict] = field(default_factory=list)          # цвет → несколько задач
    unresolved_jira: List[dict] = field(default_factory=list)     # цвет есть, id не извлечён
    multi_id_rows: List[dict] = field(default_factory=list)       # серия задач одного цвета
    no_history: bool = False                                      # таблица истории не найдена
    warnings: List[str] = field(default_factory=list)


def find_history_table(soup: BeautifulSoup) -> Optional[Tag]:
    """Находит таблицу «Истории изменений» (переиспользует детектор history_cleaner)."""
    for table in soup.find_all("table"):
        if _is_history_table(table):
            return table
    return None


def _identify_columns(table: Tag) -> Tuple[Dict[str, int], Optional[Tag]]:
    """Определяет индексы столбцов по тексту заголовка. Возвращает (роль→индекс, строка-шапка).

    Ищет строку, в которой ≥2 ячейки совпадают с синонимами ролей, — это шапка. Порядок
    столбцов на разных страницах различается, поэтому только по тексту (ТЗ п. 4.2.б).
    """
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        role_by_index: Dict[str, int] = {}
        for idx, cell in enumerate(cells):
            norm = _normalize_header(cell.get_text(" ", strip=True))
            for role, names in COLUMN_SYNONYMS.items():
                if norm in names and role not in role_by_index:
                    role_by_index[role] = idx
        if len(role_by_index) >= 2 and "description" in role_by_index:
            return role_by_index, row
    return {}, None


def _nearest_color(text_node) -> Optional[str]:
    """Эффективный (ближайший) цвет текстового узла: ``color`` ближайшего предка,
    задающего цвет. CSS: внутренний цвет перекрывает внешний, поэтому внешний цветной
    спан с чёрным текстом внутри даёт ЧЁРНЫЙ. None — цвет не задан ни одним предком.
    """
    cur = text_node.parent
    while cur is not None and isinstance(cur, Tag):
        style = (cur.get("style") or "").lower()
        m = re.search(r"color\s*:\s*([^;]+)", style)
        if m:
            return m.group(1).strip()
        if cur.name == "font" and cur.get("color"):
            return cur.get("color").strip()
        cur = cur.parent
    return None


def _extract_row_colors(cell: Tag) -> List[str]:
    """НЕ-чёрные ЭФФЕКТИВНЫЕ цвета текста ячейки «Описание» (ТЗ п. 4.2.в).

    Цвет берётся по БЛИЖАЙШЕМУ предку каждого текстового прогона (внутренний цвет
    перекрывает внешний): внешний цветной спан с чёрным текстом внутри = чёрный и в карту
    не идёт. Одна строка может нести несколько разных цветов — все попадают в результат
    (п. 4.2.г «серия задач одного цвета»).
    """
    colors: List[str] = []
    seen = set()
    for text_node in cell.find_all(string=True):
        if not str(text_node).strip():
            continue
        raw = _nearest_color(text_node)
        # Чёрный / UI-цвет / перцептивно-чёрный (near-black, ТЗ 4.3.1) — не цвет правки.
        if not raw or is_black_color(raw) or is_ignored_color(raw) or is_near_black(raw):
            continue
        norm = normalize_color(raw)
        if norm and norm not in seen:
            seen.add(norm)
            colors.append(norm)
    return colors


def _resolve_jira_ids(cell: Tag) -> List[str]:
    """Цепочка резолверов id задачи из ячейки «Задача в Jira» (ТЗ п. 4.2.г).

    Порядок: макрос-с-ключом (storage) → data-macro-parameters → ссылка browse/ →
    plain-text по маске. Первый сработавший способ и даёт результат (может вернуть
    несколько id — серия задач одного цвета). Пустой список = id извлечь не удалось.
    """
    # 1. Storage-формат: <ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">KEY
    for macro in cell.find_all("ac:structured-macro"):
        if (macro.get("ac:name") or "").lower() == "jira":
            for param in macro.find_all("ac:parameter"):
                if (param.get("ac:name") or "").lower() == "key":
                    key = param.get_text(strip=True)
                    if key:
                        return [key]

    # 2. Rendered-форма с сохранёнными параметрами макроса: data-macro-parameters="key=KEY|..."
    for tag in cell.find_all(attrs={"data-macro-parameters": True}):
        params = tag.get("data-macro-parameters") or ""
        m = re.search(r"key=([A-Z][A-Z0-9]{1,9}-\d+)", params)
        if m:
            return [m.group(1)]

    # 3. Гиперссылка вида .../browse/KEY (разрешённый макрос в rendered view).
    hrefs: List[str] = []
    for a in cell.find_all("a", href=True):
        m = re.search(r"/browse/([A-Z][A-Z0-9]{1,9}-\d+)", a["href"])
        if m and m.group(1) not in hrefs:
            hrefs.append(m.group(1))
    if hrefs:
        return hrefs

    # 4. Финальный fallback — маска по тексту ячейки.
    ids: List[str] = []
    for m in TASK_ID_RE.finditer(cell.get_text(" ", strip=True)):
        if m.group(0) not in ids:
            ids.append(m.group(0))
    return ids


def _extract_date(cell: Optional[Tag]) -> Optional[Tuple[int, int, int]]:
    """Дата строки для разрешения коллизий: сперва <time datetime=YYYY-MM-DD>, затем DD.MM.YYYY."""
    if cell is None:
        return None
    t = cell.find("time")
    if t and t.get("datetime"):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t["datetime"])
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{2})\.(\d{2})\.(\d{4})\b", cell.get_text(" ", strip=True))
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def survey_body_colors(raw_html: str, result: "HistoryMapResult") -> Dict[str, dict]:
    """Перепись всех цветов ТЕЛА страницы с частотами и классификацией (ТЗ п. 4.3, 4.10).

    Секция истории удаляется перед обходом (её цвета — служебные, не требования).
    Классификация каждого цвета: 'black' | 'task' | 'unknown'. Для 'unknown' различается
    причина: 'jira-unresolved' (цвет есть в истории, но id не извлечён) или 'not-in-history'
    (цвета нет в таблице истории вовсе, ТЗ п. 4.2.ж — плейсхолдер UNKNOWN-*).
    """
    from app.history_cleaner import remove_history_sections
    body = remove_history_sections(raw_html, enabled=True)
    soup = BeautifulSoup(body, "html.parser")

    unresolved_colors = {u["color"] for u in result.unresolved_jira}

    # Считаем по ТЕКСТОВЫМ прогонам и их ЭФФЕКТИВНОМУ (ближайшему) цвету, а не по тегам:
    # внешний цветной спан с чёрным текстом внутри не должен считаться цветным (ТЗ 4.3).
    freq: Dict[str, int] = {}
    for text_node in soup.find_all(string=True):
        if not str(text_node).strip():
            continue
        raw = _nearest_color(text_node)
        if not raw:
            continue
        norm = normalize_color(raw)
        if norm:
            freq[norm] = freq.get(norm, 0) + 1

    # Порядок проверок обязателен (ТЗ п. 4.3.2): точный чёрный → UI → задача (история) →
    # near-black (ΔE) → UNKNOWN. Шаг near-black ПОСЛЕ поиска в истории, иначе тёмный
    # цвет-маркер был бы проглочен как чёрный.
    summary: Dict[str, dict] = {}
    for color, count in freq.items():
        if is_black_color(color):
            summary[color] = {"count": count, "classification": "black", "task": None}
        elif is_ignored_color(color):
            summary[color] = {"count": count, "classification": "ignored", "task": None}
        elif color in result.color_to_task:
            summary[color] = {"count": count, "classification": "task",
                              "task": result.color_to_task[color]}
        elif is_near_black(color):
            # Вне палитры, но перцептивно чёрный (ТЗ 4.3.1) → чёрный. ΔE — для калибровки порога.
            summary[color] = {"count": count, "classification": "near-black", "task": None,
                              "delta_e": round(delta_e_to_black(color) or 0.0, 2)}
        else:
            reason = "jira-unresolved" if color in unresolved_colors else "not-in-history"
            summary[color] = {
                "count": count,
                "classification": "unknown",
                "task": "UNKNOWN-" + color.lstrip("#"),
                "reason": reason,
                "delta_e": round(delta_e_to_black(color) or 0.0, 2),  # калибровка порога
            }
    return summary


def build_color_task_map(raw_html: str) -> HistoryMapResult:
    """Строит карту «цвет → задача» из истории изменений страницы (ТЗ п. 4.2).

    raw_html — сырой HTML страницы ДО удаления секции истории (порядок критичен, п. 4.2.а).
    """
    result = HistoryMapResult()
    soup = BeautifulSoup(raw_html, "html.parser")

    table = find_history_table(soup)
    if table is None:
        result.no_history = True
        return result

    roles, header_row = _identify_columns(table)
    if "description" not in roles or "jira" not in roles:
        result.no_history = True
        result.warnings.append("не удалось опознать столбцы «Описание»/«Задача в Jira»")
        return result

    di, ji = roles["description"], roles["jira"]
    dti = roles.get("date")

    # Собираем строки с не-чёрной разметкой описания.
    rows_info: List[RowInfo] = []
    for row in table.find_all("tr"):
        if row is header_row:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        if max(di, ji, dti if dti is not None else 0) >= len(cells):
            continue
        colors = _extract_row_colors(cells[di])
        if not colors:
            continue  # чёрная/бесцветная строка — задача уже на ПРОМ, в карту не идёт
        ids = _resolve_jira_ids(cells[ji])
        date = _extract_date(cells[dti]) if dti is not None else None
        rows_info.append(RowInfo(colors=colors, task_ids=ids, date=date, raw_html=str(row)))
        if len(ids) > 1:
            result.multi_id_rows.append({"colors": colors, "task_ids": ids})
            result.warnings.append(
                f"несколько задач в одной ячейке {ids} — для маркеров берётся первый")

    # Группируем кандидатов по цвету.
    by_color: Dict[str, List[RowInfo]] = {}
    for ri in rows_info:
        for color in ri.colors:
            by_color.setdefault(color, []).append(ri)

    for color, candidates in by_color.items():
        resolved = [ri for ri in candidates if ri.task_ids]
        if not resolved:
            # Цвет есть в истории, но id не извлекается (напр. сломанный jira-макрос).
            result.unresolved_jira.append({
                "color": color,
                "rows": [ri.raw_html for ri in candidates],
            })
            continue

        distinct_tasks = {ri.task_ids[0] for ri in resolved}
        if len(distinct_tasks) > 1:
            # Коллизия: один цвет → разные задачи. Берём самую позднюю дату (ТЗ п. 4.2.е).
            latest = max(resolved, key=lambda ri: ri.date or (0, 0, 0))
            result.color_to_task[color] = latest.task_ids[0]
            result.confidence[color] = "low"
            result.collisions.append({
                "color": color,
                "chosen": latest.task_ids[0],
                "candidates": sorted(distinct_tasks),
            })
        else:
            result.color_to_task[color] = resolved[0].task_ids[0]
            result.confidence[color] = "high"

    return result
