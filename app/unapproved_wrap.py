# app/unapproved_wrap.py
#
# Форс-обёртка неутверждённого состава страницы (эмуляция похода в RAG, 2026-08-07).
#
# Контекст: критик-конвейер помечает маркерами только ЦВЕТНЫЕ фрагменты; чёрное
# считается «уже на ПРОМ». Для новой страницы это неверно — весь состав чёрный
# с рождения. Если джира из ЧЁРНОЙ строки истории входит в JSON-список
# неутверждённых (app/color_map.find_forced_unapproved), содержимое страницы
# оборачивается вставками этой задачи: reject-all даёт пустую страницу
# (состава в ПРОМ нет), apply по задаче принимает состав целиком.
#
# Правила (решения пользователя, риски приняты):
#   • оборачиваются только ЧИСТЫЕ куски — существующие маркеры других задач
#     остаются соседями, вложенность маркеров не создаётся (нотация запрещает);
#   • fenced-код НЕ оборачивается (ограничение нотации: critic разбирает маркеры
#     по зонам и маркер «через забор» не увидит) — предупреждение в отчёте;
#   • markdown-таблица без служебного столбца status оборачивается ЦЕЛИКОМ одним
#     блочным маркером; таблица с существующими статусами — построчно (+ID в
#     пустые ячейки status), чтобы reject нашей задачи не удалял чужие вставки;
#   • HTML-остров без critic-разметки оборачивается целиком; остров с чужими
#     critic-строками — построчно (class="critic-row-ins" data-task=ID) на <tr>
#     без critic-класса и без <th> (шапка нужна чужим задачам при apply);
#   • байты вне вставляемых маркеров не меняются (никакого переформатирования).

import re
from typing import Dict, List, Tuple

from app.scripts.CI.critic import (
    STATUS_COLUMN, TASK_ID_PATTERN, _split_fenced_regions,
)

# Любой существующий маркер CriticMarkup (нежадно, многострочно) — зона «не трогать».
_ANY_MARKER_RE = re.compile(
    r"\{\+\+\s*" + TASK_ID_PATTERN + r"\s*:.*?\+\+\}"
    r"|\{--\s*" + TASK_ID_PATTERN + r"\s*:.*?--\}"
    r"|\{~~\s*" + TASK_ID_PATTERN + r"\s*:.*?~~\}",
    re.DOTALL,
)

_CRITIC_ROW_RE = re.compile(r'class="critic-row-(?:ins|del)"')
_CRITIC_SPAN_RE = re.compile(r'class="critic-(?:ins|del)"')


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_separator_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s.replace("|", "").replace(" ", "")) <= {"-", ":"}


def _status_part_index(header_line: str) -> int:
    """Индекс ячейки `status` в шапке markdown-таблицы; -1 — столбца нет."""
    parts = [p.strip().lower() for p in header_line.strip().strip("|").split("|")]
    try:
        return parts.index(STATUS_COLUMN)
    except ValueError:
        return -1


def _wrap_block(lines: List[str], task_id: str) -> str:
    """Блок целиком в один многострочный маркер вставки."""
    body = "\n".join(lines)
    return "{++" + task_id + ": " + body + "++}"


def _force_md_table(lines: List[str], task_id: str, report: Dict) -> str:
    """Markdown-таблица: обёртка целиком ЛИБО построчные +ID при чужих статусах."""
    sidx = _status_part_index(lines[0])
    has_foreign = sidx >= 0 and any(
        re.fullmatch(r"[+-]" + TASK_ID_PATTERN,
                     (l.strip().strip("|").split("|") + [""] * (sidx + 1))[sidx].strip())
        for l in lines[2:] if _is_table_line(l) and not _is_separator_line(l)
    )
    if not has_foreign:
        report["tables_wrapped"] += 1
        return _wrap_block(lines, task_id)

    out: List[str] = list(lines[:2])          # шапка и разделитель — без изменений
    for line in lines[2:]:
        if not _is_table_line(line) or _is_separator_line(line):
            out.append(line)
            continue
        stripped = line.strip().strip("|")
        parts = stripped.split("|")
        while len(parts) <= sidx:
            parts.append(" ")
        if parts[sidx].strip():               # чужой статус — строка не наша
            out.append(line)
            continue
        parts[sidx] = " +" + task_id + " "
        out.append("| " + " | ".join(p.strip() for p in parts) + " |")
        report["table_rows"] += 1
    return "\n".join(out)


def _force_html_island(island: str, task_id: str, report: Dict) -> str:
    """HTML-остров: обёртка целиком либо пометка чистых <tr> при чужой разметке."""
    if not (_CRITIC_ROW_RE.search(island) or _CRITIC_SPAN_RE.search(island)):
        report["tables_wrapped"] += 1
        return _wrap_block([island], task_id)

    # Пометка построчно: <tr>…</tr> без critic-класса и без <th> (шапку не метим —
    # она нужна чужим задачам при их apply).
    out_parts: List[str] = []
    pos = 0
    for m in re.finditer(r"<tr(\s[^>]*)?>(.*?)</tr>", island, re.DOTALL | re.IGNORECASE):
        out_parts.append(island[pos:m.start()])
        attrs, body = m.group(1) or "", m.group(2)
        if "critic-row-" in attrs or "<th" in body.lower():
            out_parts.append(m.group(0))
        else:
            out_parts.append(
                f'<tr class="critic-row-ins" data-task="{task_id}"{attrs}>{body}</tr>')
            report["island_rows"] += 1
        pos = m.end()
    out_parts.append(island[pos:])
    return "".join(out_parts)


def _force_segment(seg: str, task_id: str, report: Dict) -> str:
    """Чистый (без маркеров) сегмент text-зоны: блочная обёртка построчно.

    Блоки: HTML-остров (<table…</table>), markdown-таблица, обычный блок
    (абзац/заголовок/список до пустой строки). Пустые строки — вне маркеров.
    """
    out: List[str] = []
    lines = seg.split("\n")
    i = 0
    block: List[str] = []

    def flush_block():
        if block:
            report["blocks"] += 1
            out.append(_wrap_block(block, task_id))
            block.clear()

    while i < len(lines):
        line = lines[i]
        low = line.lstrip().lower()
        if low.startswith("<table"):
            flush_block()
            j = i
            while j < len(lines) and "</table>" not in lines[j].lower():
                j += 1
            island = "\n".join(lines[i:j + 1])
            out.append(_force_html_island(island, task_id, report))
            i = j + 1
            continue
        if _is_table_line(line):
            flush_block()
            j = i
            while j < len(lines) and _is_table_line(lines[j]):
                j += 1
            out.append(_force_md_table(lines[i:j], task_id, report))
            i = j
            continue
        if not line.strip():
            flush_block()
            out.append(line)
            i += 1
            continue
        block.append(line)
        i += 1
    flush_block()
    return "\n".join(out)


def wrap_unapproved(md_text: str, task_id: str) -> Tuple[str, Dict]:
    """Оборачивает неутверждённый состав страницы вставками задачи task_id.

    Возвращает (текст, отчёт). Отчёт: blocks / tables_wrapped / table_rows /
    island_rows / code_blocks_skipped + warnings (код-блоки не помечены —
    известное ограничение нотации, решение пользователя 2026-08-07)."""
    report: Dict = {"blocks": 0, "tables_wrapped": 0, "table_rows": 0,
                    "island_rows": 0, "code_blocks_skipped": 0, "warnings": []}
    out_regions: List[str] = []
    for kind, region, _start in _split_fenced_regions(md_text):
        if kind == "code":
            report["code_blocks_skipped"] += 1
            out_regions.append(region)
            continue
        # Сегментация по существующим маркерам: чужие вставки/удаления — соседями.
        parts: List[str] = []
        pos = 0
        for m in _ANY_MARKER_RE.finditer(region):
            parts.append(_force_segment(region[pos:m.start()], task_id, report))
            parts.append(m.group(0))
            pos = m.end()
        parts.append(_force_segment(region[pos:], task_id, report))
        out_regions.append("".join(parts))
    if report["code_blocks_skipped"]:
        report["warnings"].append(
            f"код-блоков не помечено: {report['code_blocks_skipped']} — ограничение "
            f"нотации (fenced-код вне маркеров); при reject-all они останутся")
    return "".join(out_regions), report
