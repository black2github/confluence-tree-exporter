# app/scripts/CI/critic.py
#
# CLI-утилита применения и отката правок CriticMarkup ПО КОНКРЕТНОЙ ЗАДАЧЕ Jira.
# Ядро процесса Doc as Code: используется и аналитиками вручную, и в CI.
#
# Зачем свой код, а не pymdownx.critic: штатное расширение умеет только «принять всё
# разом» и работает на этапе рендеринга HTML, тогда как задачи выходят на ПРОМ независимо
# друг от друга и операция нужна над ИСХОДНЫМ markdown по конкретному TASK-ID (ТЗ п. 5.2,
# п. 11.2). Поэтому apply/reject реализованы здесь напрямую.
#
# Размещение: ТЗ называет файл tools/critic.py, но каталога tools/ в репозитории нет —
# CI-утилиты живут в app/scripts/CI/ (build_cards.py, manifest_builder.py, ...), поэтому
# файл кладётся сюда, чтобы не плодить параллельную структуру (ТЗ п. 3).
#
# Три нотации правок (ТЗ п. 4.4, 4.6, 4.7):
#   1. inline CriticMarkup:   {++ID: текст++}  {--ID: текст--}  {~~ID: старое~>новое~~}
#   2. markdown-таблица:      служебный столбец `status` со значением +ID / -ID (целая строка)
#   3. сырой HTML-остров:     <span class="critic-ins|critic-del" data-task="ID">…</span>
#                             <tr   class="critic-row-ins|critic-row-del" data-task="ID">…</tr>
#
# Жёсткие правила (ТЗ п. 2, 5.3, 11.7):
#   • переводы строк и кодировка сохраняются байт-в-байт (CRLF не нормализуется);
#   • меняется ТОЛЬКО то, что относится к маркерам целевой задачи — никакого переформатирования;
#   • файл без изменений не перезаписывается (идемпотентность);
#   • любая неоднозначность (вложенный/незакрытый маркер) — прерывание с файлом и строкой
#     и ненулевым кодом возврата: лучше остановиться, чем испортить текст.
#
# Команды: apply / reject / apply-all / reject-all (Этап 1 ТЗ п. 10) во всех трёх нотациях,
# list [--manifest] (Этап 5) и lint (Модуль 3, ТЗ п. 6) — правила E1-E8 и предупреждение W1
# (маркеры разных задач в одном предложении). Не реализовано предупреждение о «висящем»
# маркере по дате коммита: требует обращения к истории git, вынесено отдельно.

import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

# Маска идентификатора задачи (ТЗ п. 4.2) + плейсхолдер неразрешённого цвета (ТЗ п. 4.2.ж).
# UNKNOWN-* обязан проходить как валидный ID, чтобы apply/reject его обрабатывали, но
# линтер (Модуль 3) обязан на него ругаться — это уже вне области Этапа 1.
TASK_ID_PATTERN = r"(?:[A-Z][A-Z0-9]{1,9}-\d+|UNKNOWN-[0-9a-fA-F]+)"
_TASK_ID_FULL_RE = re.compile(r"^" + TASK_ID_PATTERN + r"$")

# Имя служебного столбца markdown-таблиц (ТЗ п. 4.6 — «вынести в конфиг»). Модуль автономный,
# поэтому конфиг — константа модуля с переопределением флагом CLI --status-column.
STATUS_COLUMN = "status"

# Опенеры маркеров. Специально отличаются от смарт-ссылки {{сервис:элемент}} (две скобки) —
# конфликта нет (ТЗ п. 4.8).
_OPENERS = ("{++", "{--", "{~~")

# Маркеры целиком. Нежадный `.*?` берёт ближайший закрыватель; re.DOTALL — для многострочных
# маркеров, открытых на одной строке и закрытых через несколько абзацев (ТЗ п. 5.3).
_INS_RE = re.compile(r"\{\+\+\s*(" + TASK_ID_PATTERN + r")\s*:\s*(.*?)\+\+\}", re.DOTALL)
_DEL_RE = re.compile(r"\{--\s*(" + TASK_ID_PATTERN + r")\s*:\s*(.*?)--\}", re.DOTALL)
_SUB_RE = re.compile(r"\{~~\s*(" + TASK_ID_PATTERN + r")\s*:\s*(.*?)~>(.*?)~~\}", re.DOTALL)

# Открывающая/закрывающая fenced-fence в начале строки (``` или ~~~, 3+ символа).
_FENCE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})")


class CriticError(Exception):
    """Неоднозначность или битый маркер: прерывание с указанием файла и строки (ТЗ п. 5.3)."""

    def __init__(self, message: str, path: Optional[Path] = None, line: Optional[int] = None):
        self.message = message
        self.path = path
        self.line = line
        loc = ""
        if path is not None:
            loc = str(path)
            if line is not None:
                loc += f":{line}"
            loc += ": "
        super().__init__(loc + message)


# --------------------------------------------------------------------------------------
# Разбиение на зоны: fenced-код (не трогаем) и обычный текст (обрабатываем).
# --------------------------------------------------------------------------------------

def _split_fenced_regions(text: str) -> List[Tuple[str, str, int]]:
    """Делит текст на зоны с сохранением содержимого байт-в-байт.

    Возвращает список (kind, region_text, start_line), где kind ∈ {"code", "text"}.
    Внутри fenced code blocks (``` … ``` / ~~~ … ~~~) разметка не разбирается и не
    применяется (ТЗ п. 5.3). start_line — 1-based номер первой строки зоны в исходном
    тексте (для сообщений об ошибках).
    """
    lines = text.splitlines(keepends=True)
    regions: List[Tuple[str, str, int]] = []
    buf: List[str] = []
    buf_start = 1
    in_fence = False
    line_no = 0

    def flush(kind: str):
        if buf:
            regions.append((kind, "".join(buf), buf_start))
        buf.clear()

    for line in lines:
        line_no += 1
        is_fence = bool(_FENCE_RE.match(line))
        if not in_fence and is_fence:
            # начинается fenced-блок: закрываем текстовую зону, открываем кодовую
            flush("text")
            buf_start = line_no
            buf.append(line)
            in_fence = True
        elif in_fence and is_fence:
            # закрывается fenced-блок
            buf.append(line)
            flush("code")
            buf_start = line_no + 1
            in_fence = False
        else:
            if not buf:
                buf_start = line_no
            buf.append(line)

    # Хвост. Незакрытый fenced-блок отдаём как код — его содержимое и так не обрабатывается.
    flush("code" if in_fence else "text")
    return regions


# --------------------------------------------------------------------------------------
# Валидация inline-маркеров: вложенность и незакрытость → hard-fail.
# --------------------------------------------------------------------------------------

def _line_of(region_text: str, offset: int, base_line: int) -> int:
    """Номер строки в исходном тексте для смещения offset внутри зоны."""
    return base_line + region_text.count("\n", 0, offset)


def _validate_inline(region_text: str, base_line: int, path: Optional[Path]) -> None:
    """Проверяет корректность inline-маркеров в текстовой зоне (ТЗ п. 5.3, п. 6).

    Не рекурсивный парсер (ТЗ п. 11.1): маркеры разбираются регулярками, а любая
    литеральная вложенность и любой незакрытый опенер приводят к hard-fail.
    """
    # 1. Вложенность: содержимое любого маркера не должно содержать ещё один опенер.
    for rx in (_INS_RE, _DEL_RE, _SUB_RE):
        for m in rx.finditer(region_text):
            for grp in m.groups()[1:]:  # groups()[0] — это id, пропускаем
                if grp and any(op in grp for op in _OPENERS):
                    raise CriticError(
                        "литеральная вложенность маркеров запрещена",
                        path=path, line=_line_of(region_text, m.start(), base_line),
                    )

    # 2. Незакрытость: убираем все валидные маркеры и ищем осевшие опенеры.
    stripped = region_text
    for rx in (_INS_RE, _DEL_RE, _SUB_RE):
        stripped = rx.sub("", stripped)
    for op in _OPENERS:
        if op in stripped:
            # Смещение считаем по первому вхождению опенера в ИСХОДНОМ тексте зоны.
            raise CriticError(
                f"незакрытый или некорректный маркер {op}",
                path=path, line=_line_of(region_text, region_text.find(op), base_line),
            )


# --------------------------------------------------------------------------------------
# Пас 1: inline CriticMarkup.
# --------------------------------------------------------------------------------------

def _matches(task_id: Optional[str], found_id: str) -> bool:
    """Совпадает ли найденный id с целевым. task_id=None → режим *-all (любой валидный id)."""
    return task_id is None or found_id == task_id


def _apply_inline(region_text: str, op: str, task_id: Optional[str]) -> Tuple[str, int]:
    """Применяет/откатывает inline-маркеры целевой задачи (ТЗ п. 5.2).

    Маркеры прочих задач остаются нетронутыми — это принципиально (ТЗ п. 5.2).
    """
    count = 0

    def repl_ins(m):
        nonlocal count
        if not _matches(task_id, m.group(1)):
            return m.group(0)
        count += 1
        return m.group(2) if op == "apply" else ""

    def repl_del(m):
        nonlocal count
        if not _matches(task_id, m.group(1)):
            return m.group(0)
        count += 1
        return "" if op == "apply" else m.group(2)

    def repl_sub(m):
        nonlocal count
        if not _matches(task_id, m.group(1)):
            return m.group(0)
        count += 1
        return m.group(3) if op == "apply" else m.group(2)

    region_text = _INS_RE.sub(repl_ins, region_text)
    region_text = _DEL_RE.sub(repl_del, region_text)
    region_text = _SUB_RE.sub(repl_sub, region_text)
    return region_text, count


# --------------------------------------------------------------------------------------
# Пас 2: markdown-таблицы, служебный столбец `status`.
# --------------------------------------------------------------------------------------

def _norm(cell: str) -> str:
    """Нормализует ячейку заголовка для сопоставления: strip + схлопывание пробелов + lower."""
    return re.sub(r"\s+", " ", cell.strip()).lower()


def _line_ending(line: str) -> str:
    """Возвращает завершитель строки ('\\r\\n', '\\n', '\\r' или '')."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


# Разделитель ячеек — '|', НЕ экранированный обратным слэшем. В ячейках реально встречается
# `\|` (экранированный пайп), и делить по нему нельзя, иначе разъедутся ячейки и индекс столбца.
_SPLIT_PIPE_RE = re.compile(r"(?<!\\)\|")


def _is_table_row(line: str) -> bool:
    """Строка pipe-таблицы: непустое ядро, начинающееся с '|' (наш конвертер всегда так пишет)."""
    core = line[: len(line) - len(_line_ending(line))].strip()
    return core.startswith("|") and len(_SPLIT_PIPE_RE.findall(core)) >= 2


def _is_separator_row(line: str) -> bool:
    """Разделитель заголовка таблицы: ячейки вида ---, :--, --:, :-:."""
    core = line[: len(line) - len(_line_ending(line))].strip()
    if not core.startswith("|"):
        return False
    cells = _SPLIT_PIPE_RE.split(core)[1:-1]
    if not cells:
        return False
    return all(re.match(r"^\s*:?-{1,}:?\s*$", c) for c in cells)


def _split_row_parts(line: str) -> Tuple[List[str], str]:
    """Разбивает строку таблицы по неэкранированным '|' с сохранением завершителя строки.

    Части включают ведущий и хвостовой пустые сегменты (до первого и после последнего '|'),
    так что реконструкция '|'.join(parts) + ending даёт исходную строку байт-в-байт (каждый
    разделитель — ровно один символ '|', экранированные `\\|` остаются внутри ячеек).
    """
    ending = _line_ending(line)
    core = line[: len(line) - len(ending)]
    return _SPLIT_PIPE_RE.split(core), ending


def _find_status_part_index(header_line: str, status_column: str) -> Optional[int]:
    """Индекс сегмента столбца `status` в разбиении header по '|' (или None, если столбца нет)."""
    parts, _ = _split_row_parts(header_line)
    target = status_column.lower()
    for idx, part in enumerate(parts):
        if _norm(part) == target:
            return idx
    return None


# Значение служебного столбца: знак (+/-) и id задачи.
_STATUS_VAL_RE = re.compile(r"^([+-])(" + TASK_ID_PATTERN + r")$")


def _transform_table(block: List[str], op: str, task_id: Optional[str],
                     status_column: str) -> Tuple[List[str], int]:
    """Обрабатывает один блок markdown-таблицы (ТЗ п. 4.6, 5.2).

    Правки внутри ячеек (обычный CriticMarkup) обрабатываются отдельным inline-пасом,
    здесь — только целостные строки через столбец `status`. После обработки, если столбец
    `status` не содержит ни одного непустого значения, он удаляется целиком (ТЗ п. 5.2).
    """
    header, separator, body = block[0], block[1], block[2:]
    status_pidx = _find_status_part_index(header, status_column)
    if status_pidx is None:
        return block, 0

    count = 0
    new_body: List[str] = []
    for row in body:
        parts, ending = _split_row_parts(row)
        if status_pidx >= len(parts):
            new_body.append(row)
            continue
        m = _STATUS_VAL_RE.match(parts[status_pidx].strip())
        if not m or not _matches(task_id, m.group(2)):
            new_body.append(row)
            continue

        sign = m.group(1)
        count += 1
        # ТЗ п. 5.2:
        #   status=+ID: apply → очистить ячейку;   reject → удалить строку.
        #   status=-ID: apply → удалить строку;     reject → очистить ячейку.
        drop = (sign == "+" and op == "reject") or (sign == "-" and op == "apply")
        if drop:
            continue  # строка удаляется целиком
        parts[status_pidx] = " "  # ячейка очищается
        new_body.append("|".join(parts) + ending)

    if count == 0:
        return block, 0

    # Удаление столбца `status`, если во всех оставшихся строках он пуст (ТЗ п. 5.2).
    def _status_empty(line: str) -> bool:
        parts, _ = _split_row_parts(line)
        return status_pidx >= len(parts) or parts[status_pidx].strip() == ""

    if all(_status_empty(r) for r in new_body):
        n_hdr = len(_split_row_parts(header)[0])

        def _drop_col(line: str) -> str:
            parts, ending = _split_row_parts(line)
            # Удаляем ячейку столбца только у строк той же ширины, что заголовок: рваную
            # (некорректную) строку не трогаем, чтобы не превратить одну поломку в другую.
            if len(parts) == n_hdr and status_pidx < len(parts):
                del parts[status_pidx]
            return "|".join(parts) + ending
        header = _drop_col(header)
        separator = _drop_col(separator)
        new_body = [_drop_col(r) for r in new_body]

    return [header, separator] + new_body, count


def _apply_tables(text: str, op: str, task_id: Optional[str],
                  status_column: str) -> Tuple[str, int]:
    """Находит блоки markdown-таблиц и обрабатывает их служебный столбец `status`."""
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    total = 0
    i = 0
    n = len(lines)
    while i < n:
        if (_is_table_row(lines[i]) and i + 1 < n and _is_separator_row(lines[i + 1])):
            j = i + 2
            while j < n and _is_table_row(lines[j]):
                j += 1
            new_block, cnt = _transform_table(lines[i:j], op, task_id, status_column)
            out.extend(new_block)
            total += cnt
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "".join(out), total


# --------------------------------------------------------------------------------------
# Пас 3: сырые HTML-острова, HTML-нотация (ТЗ п. 4.7).
# --------------------------------------------------------------------------------------

_TABLE_TAG_RE = re.compile(r"</?table\b[^>]*>", re.IGNORECASE)
_HAS_CRITIC_RE = re.compile(r'class\s*=\s*["\'][^"\']*critic-', re.IGNORECASE)


def _find_table_islands(text: str) -> List[Tuple[int, int]]:
    """Возвращает диапазоны верхнеуровневых <table>…</table> с учётом вложенности.

    HTML-нотация правок живёт внутри сохранённых сырым HTML таблиц (ТЗ п. 4.7);
    вложенные таблицы обрабатываются счётчиком глубины, чтобы не оборвать остров рано.
    """
    islands: List[Tuple[int, int]] = []
    depth = 0
    start = -1
    for m in _TABLE_TAG_RE.finditer(text):
        if m.group(0).lower().startswith("</"):
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    islands.append((start, m.end()))
                    start = -1
        else:
            if depth == 0:
                start = m.start()
            depth += 1
    return islands


def _has_class(tag, name: str) -> bool:
    cls = tag.get("class")
    return bool(cls) and name in cls


def _apply_html(text: str, op: str, task_id: Optional[str]) -> Tuple[str, int]:
    """Применяет/откатывает HTML-нотацию правок внутри сырых HTML-таблиц (ТЗ п. 4.7).

    Разбор — парсером (BeautifulSoup), не регулярками: атрибуты могут идти в произвольном
    порядке, содержимое — быть вложенным. Переписывается только изменённый остров, чтобы
    не порождать лишний diff в неизменных таблицах.
    """
    if not _HAS_CRITIC_RE.search(text):
        return text, 0

    total = 0
    result_parts: List[str] = []
    last = 0
    for start, end in _find_table_islands(text):
        island = text[start:end]
        if not _HAS_CRITIC_RE.search(island):
            continue
        new_island, cnt = _transform_html_island(island, op, task_id)
        if cnt:
            result_parts.append(text[last:start])
            result_parts.append(new_island)
            last = end
            total += cnt
    result_parts.append(text[last:])
    return "".join(result_parts), total


def _transform_html_island(island: str, op: str, task_id: Optional[str]) -> Tuple[str, int]:
    """Мутирует critic-элементы внутри одного HTML-острова по таблице ТЗ п. 4.7."""
    soup = BeautifulSoup(island, "html.parser")
    count = 0

    # span.critic-ins / span.critic-del
    for span in soup.find_all("span", class_=lambda c: c and (
            "critic-ins" in c or "critic-del" in c)):
        if not _matches(task_id, span.get("data-task", "")):
            continue
        is_ins = _has_class(span, "critic-ins")
        count += 1
        # apply: ins → снять обёртку (оставить), del → удалить с содержимым.
        # reject: ins → удалить с содержимым, del → снять обёртку (оставить).
        keep = (op == "apply" and is_ins) or (op == "reject" and not is_ins)
        if keep:
            span.unwrap()
        else:
            span.decompose()

    # tr.critic-row-ins / tr.critic-row-del
    for tr in soup.find_all("tr", class_=lambda c: c and (
            "critic-row-ins" in c or "critic-row-del" in c)):
        if not _matches(task_id, tr.get("data-task", "")):
            continue
        is_ins = _has_class(tr, "critic-row-ins")
        count += 1
        # apply: row-ins → снять class+data-task, row-del → удалить строку.
        # reject: row-ins → удалить строку, row-del → снять class+data-task.
        keep = (op == "apply" and is_ins) or (op == "reject" and not is_ins)
        if keep:
            if tr.has_attr("class"):
                del tr["class"]
            if tr.has_attr("data-task"):
                del tr["data-task"]
        else:
            tr.decompose()

    return (str(soup), count) if count else (island, 0)


# --------------------------------------------------------------------------------------
# Обработка одного файла.
# --------------------------------------------------------------------------------------

def process_text(text: str, op: str, task_id: Optional[str],
                 status_column: str = STATUS_COLUMN,
                 path: Optional[Path] = None) -> Tuple[str, int]:
    """Обрабатывает весь текст файла и возвращает (новый_текст, число_правок).

    Frontmatter и прочий текст, не относящийся к маркерам, не трогаются: inline-регулярки
    требуют опенер `{++`/`{--`/`{~~` (смарт-ссылка `{{` им не подходит), а табличный и
    HTML-пасы работают только над своими конструкциями. Переводы строк сохраняются.
    """
    regions = _split_fenced_regions(text)
    out: List[str] = []
    total = 0
    for kind, region_text, base_line in regions:
        if kind == "code":
            out.append(region_text)  # fenced-код — байт-в-байт (ТЗ п. 5.3)
            continue
        _validate_inline(region_text, base_line, path)
        region_text, c_html = _apply_html(region_text, op, task_id)
        region_text, c_inline = _apply_inline(region_text, op, task_id)
        region_text, c_table = _apply_tables(region_text, op, task_id, status_column)
        total += c_html + c_inline + c_table
        out.append(region_text)
    return "".join(out), total


def _read_text_preserving(path: Path) -> str:
    """Читает файл в utf-8 БЕЗ трансляции переводов строк (newline='') — CRLF сохраняется."""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write_text_preserving(path: Path, text: str) -> None:
    """Пишет файл в utf-8 БЕЗ трансляции переводов строк — то, что в строке, то и на диске."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def process_file(path: Path, op: str, task_id: Optional[str],
                 status_column: str = STATUS_COLUMN,
                 dry_run: bool = False) -> int:
    """Обрабатывает один .md файл. Возвращает число правок. Не пишет неизменённый файл."""
    original = _read_text_preserving(path)
    new_text, count = process_text(original, op, task_id, status_column, path)
    if count == 0 or new_text == original:
        return 0  # идемпотентность: без изменений файл не трогаем (ТЗ п. 5.3)
    if not dry_run:
        _write_text_preserving(path, new_text)
    return count


# --------------------------------------------------------------------------------------
# Модуль 3: линтер (ТЗ п. 6). Запускается в CI на каждый MR и командой `critic.py lint`.
# --------------------------------------------------------------------------------------

# «Свободные» маркеры — ловят конструкцию по опенеру/закрывателю без требования валидного ID
# (для правила 2 «маркер без идентификатора / с идентификатором не по маске»).
_LOOSE_RES = {
    "ins": re.compile(r"\{\+\+(.*?)\+\+\}", re.DOTALL),
    "del": re.compile(r"\{--(.*?)--\}", re.DOTALL),
    "sub": re.compile(r"\{~~(.*?)~~\}", re.DOTALL),
}
# Префикс «ID:» в начале содержимого маркера.
_ID_PREFIX_RE = re.compile(r"^\s*(" + TASK_ID_PATTERN + r")\s*:")
# Любой опенер маркера (для поиска незакрытых).
_ANY_OPENER_RE = re.compile(r"\{\+\+|\{--|\{~~")
# Смарт-ссылка {{сервис:элемент}} (ТЗ п. 4.8) — внутрь неё маркер попадать не должен.
_SMARTLINK_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)


class Finding:
    """Находка линтера в формате, пригодном для аннотаций CI (файл, строка, уровень)."""

    __slots__ = ("path", "line", "level", "rule", "message")

    def __init__(self, path: Optional[Path], line: int, level: str, rule: str, message: str):
        self.path = path
        self.line = line
        self.level = level      # "error" | "warning"
        self.rule = rule        # короткий код правила (E1..E7, W1..W2)
        self.message = message

    def _loc(self) -> str:
        return f"{self.path}:{self.line}" if self.path is not None else f"line {self.line}"

    def format_text(self) -> str:
        return f"{self._loc()}: [{self.level}] {self.rule}: {self.message}"

    def as_dict(self) -> dict:
        return {
            "path": str(self.path) if self.path is not None else None,
            "line": self.line,
            "level": self.level,
            "rule": self.rule,
            "message": self.message,
        }

    def _key(self):
        return (str(self.path), self.line, self.level, self.rule, self.message)


def _region_spans(text: str) -> List[Tuple[int, int, str]]:
    """Абсолютные диапазоны зон (start, end, kind), kind ∈ {"code", "text"}."""
    spans = []
    off = 0
    for kind, region_text, _ in _split_fenced_regions(text):
        spans.append((off, off + len(region_text), kind))
        off += len(region_text)
    return spans


def _line_at(text: str, pos: int) -> int:
    """1-based номер строки для абсолютного смещения (CRLF считается по '\\n')."""
    return text.count("\n", 0, pos) + 1


def _in_ranges(pos: int, ranges) -> bool:
    return any(s <= pos < e for s, e in ranges)


def lint_text(text: str, path: Optional[Path] = None) -> List[Finding]:
    """Проверяет разметку CriticMarkup по правилам ТЗ п. 6. Возвращает список находок."""
    findings: List[Finding] = []

    spans = _region_spans(text)
    smartlinks = [(m.start(), m.end()) for m in _SMARTLINK_RE.finditer(text)]
    islands = _find_table_islands(text)

    def kind_at(pos: int) -> str:
        for s, e, k in spans:
            if s <= pos < e:
                return k
        return "text"

    def add(line: int, level: str, rule: str, message: str):
        findings.append(Finding(path, line, level, rule, message))

    def line_of_start(pos: int) -> str:
        ls = text.rfind("\n", 0, pos) + 1
        le = text.find("\n", pos)
        return text[ls: le if le != -1 else len(text)]

    valid_markers = []        # list[(start, end, id)] — для W1
    covered_spans = []        # диапазоны опознанных маркеров — для поиска незакрытых (E4)

    for opener_kind, rx in _LOOSE_RES.items():
        for m in rx.finditer(text):
            covered_spans.append((m.start(), m.end()))
            start = m.start()
            line = _line_at(text, start)
            content = m.group(1)

            # E2: маркер без ID / с ID не по маске.
            idm = _ID_PREFIX_RE.match(content)
            if not idm:
                add(line, "error", "E2",
                    "маркер без идентификатора задачи или с идентификатором не по маске")
            else:
                task_id = idm.group(1)
                after = content[idm.end():]

                # E3: плейсхолдер UNKNOWN-* (неразобранный остаток миграции).
                if task_id.startswith("UNKNOWN-"):
                    add(line, "error", "E3",
                        f"плейсхолдер {task_id} не разобран аналитиком (остаток миграции)")

                # E1: литеральная вложенность маркеров.
                if any(op in after for op in _OPENERS):
                    add(line, "error", "E1", "литеральная вложенность маркеров запрещена")

                valid_markers.append((start, m.end(), task_id))

            # Структурные правила — по положению маркера, независимо от валидности ID.
            if _in_ranges(start, smartlinks):
                add(line, "error", "E6", "маркер внутри смарт-ссылки {{сервис:элемент}}")
            if _in_ranges(start, islands):
                add(line, "error", "E7",
                    "CriticMarkup внутри сырого HTML — здесь применяется HTML-нотация (п. 4.7)")
            if kind_at(start) == "code":
                add(line, "error", "E5", "маркер внутри fenced code block")
            elif "\n" in m.group(0):
                # E5: многострочный маркер, разрывающий синтаксическую конструкцию.
                if _is_table_row(line_of_start(start) + "\n"):
                    add(line, "error", "E5",
                        "маркер открыт внутри строки таблицы и закрыт за её пределами")
                if re.search(r"\n[ \t]*\n", m.group(0)):
                    add(line, "error", "E5",
                        "маркер пересекает границу абзаца/элемента списка")

    # E3 в HTML-нотации: data-task="UNKNOWN-…" — тот же неразобранный остаток миграции,
    # что и текстовый {++UNKNOWN…}, но внутри сырых HTML-таблиц (ТЗ п. 4.7).
    for m in re.finditer(r'data-task="(UNKNOWN-[0-9a-fA-F]+)"', text):
        add(_line_at(text, m.start(1)), "error", "E3",
            f"плейсхолдер {m.group(1)} не разобран аналитиком (HTML-нотация)")

    # E4: незакрытый маркер — опенер, не входящий ни в один опознанный маркер и не в коде.
    for m in _ANY_OPENER_RE.finditer(text):
        pos = m.start()
        if not _in_ranges(pos, covered_spans) and kind_at(pos) != "code":
            add(_line_at(text, pos), "error", "E4",
                f"незакрытый или некорректно вложенный маркер {m.group(0)}")

    # W1: маркеры разных задач в одном предложении (сигнал проверить зависимость в Jira).
    valid_markers.sort(key=lambda t: t[0])
    for (a_start, a_end, a_id), (b_start, b_end, b_id) in zip(valid_markers, valid_markers[1:]):
        between = text[a_end:b_start]
        if a_id != b_id and not re.search(r"[.!?]\s|\n[ \t]*\n", between):
            add(_line_at(text, b_start), "warning", "W1",
                f"маркеры разных задач в одном предложении ({a_id}, {b_id}) — "
                f"проверьте зависимость задач в Jira")

    # E8: удаляемый текст ВХОДИТ в состав вставки другой задачи в том же файле (ТЗ п. 6,
    # определение п. 4.5.1). Ошибка: {--…--} объявляет текст существующим на ПРОМ, хотя он —
    # часть чужого черновика; ведёт к неверному reject (недостоверный «текущий ПРОМ»).
    for f in find_contained_deletions(text):
        add(_line_at(text, f["pos"]), "error", "E8",
            f"удаляемый текст входит в состав вставки задачи {f['insert_task']} — "
            f"на ПРОМ не был (нарушение п. 4.5.1)")

    # Стабильный порядок + дедупликация одинаковых находок.
    seen = set()
    unique = []
    for f in sorted(findings, key=lambda f: (f.line, f.rule, f.message)):
        if f._key() not in seen:
            seen.add(f._key())
            unique.append(f)
    return unique


def lint_file(path: Path) -> List[Finding]:
    """Прогоняет линтер по одному .md файлу."""
    return lint_text(_read_text_preserving(path), path)


# --------------------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------------------

def _iter_md_files(path: Path) -> List[Path]:
    """Список .md по пути: сам файл, либо рекурсивный обход каталога (отсортировано)."""
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.md"))


def _run_edit(op: str, task_id: Optional[str], root: Path, status_column: str,
              dry_run: bool) -> int:
    """Общая логика apply/reject/apply-all/reject-all. Возвращает код возврата процесса."""
    files = _iter_md_files(root)
    changed_files = 0
    total = 0
    for fp in files:
        try:
            count = process_file(fp, op, task_id, status_column, dry_run)
        except CriticError as e:
            print(f"ОШИБКА: {e}", file=sys.stderr)
            return 2  # ненулевой код при любой неоднозначности (ТЗ п. 5.3)
        if count:
            changed_files += 1
            total += count
            verb = "будет изменён" if dry_run else "изменён"
            print(f"{fp}: {verb} ({count} правок)")

    label = "задача " + task_id if task_id else "все задачи"
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}{op} ({label}): файлов изменено {changed_files}, правок {total}")
    return 0


def _run_lint(root: Path, fmt: str) -> int:
    """Прогоняет линтер по .md и печатает находки. Возвращает 1, если есть ошибки (ТЗ п. 6)."""
    findings: List[Finding] = []
    for fp in _iter_md_files(root):
        findings.extend(lint_file(fp))

    if fmt == "json":
        print(json.dumps([f.as_dict() for f in findings], ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f.format_text())

    errors = sum(1 for f in findings if f.level == "error")
    warnings = len(findings) - errors
    if fmt == "text":
        print(f"[critic lint] ошибок: {errors}, предупреждений: {warnings}", file=sys.stderr)
    # Ошибки блокируют слияние; одни предупреждения — нет (ТЗ п. 6).
    return 1 if errors else 0


_STATUS_CELL_RE = re.compile(r"\|\s*[+-](" + TASK_ID_PATTERN + r")\s*\|")
_DATA_TASK_RE = re.compile(r'data-task="(' + TASK_ID_PATTERN + r')"')


def collect_task_occurrences(text: str) -> dict:
    """Собирает идентификаторы задач и номера строк из всех нотаций одного файла (ТЗ п. 5.4).

    Источники: inline-маркеры {++/--/~~ ID:}, служебный столбец status (+ID/-ID),
    HTML-нотация data-task="ID". Содержимое fenced code blocks игнорируется.
    Возвращает {task_id: [отсортированные уникальные номера строк]}.
    """
    spans = _region_spans(text)
    code = [(s, e) for s, e, kind in spans if kind == "code"]

    def in_code(pos: int) -> bool:
        return any(s <= pos < e for s, e in code)

    occ: dict = {}

    def add(task: str, pos: int):
        if in_code(pos):
            return
        occ.setdefault(task, set()).add(_line_at(text, pos))

    for rx in (_INS_RE, _DEL_RE, _SUB_RE):
        for m in rx.finditer(text):
            add(m.group(1), m.start())
    for m in _STATUS_CELL_RE.finditer(text):
        add(m.group(1), m.start(1))
    for m in _DATA_TASK_RE.finditer(text):
        add(m.group(1), m.start(1))

    return {task: sorted(lines) for task, lines in occ.items()}


def _norm_ws(s: str) -> str:
    """Схлопывание пробелов + обрезка (nbsp/zero-width уже считаются \\s в Unicode-режиме)."""
    return re.sub(r"\s+", " ", s).strip()


# Порог длины: защита от тривиальных совпадений коротких строк («то», «=») при проверке
# вхождения — чтобы не отбросить реальное удаление из-за случайной подстроки (ТЗ 4.5.4).
_MIN_CONTAINED_LEN = 4


def _insertions_by_task(text: str) -> dict:
    """Нормализованные тексты вставок по задачам: {++ID: X++} и правая часть {~~ID: ..~>X~~}."""
    ins: dict = {}
    for m in _INS_RE.finditer(text):
        ins.setdefault(m.group(1), []).append(_norm_ws(m.group(2)))
    for m in _SUB_RE.finditer(text):
        ins.setdefault(m.group(1), []).append(_norm_ws(m.group(3)))
    return ins


def find_contained_deletions(text: str) -> List[dict]:
    """Удаляемые фрагменты, чей текст ВХОДИТ в состав вставки ДРУГОЙ задачи в том же файле.

    Нарушение определения п. 4.5.1: удаляемый текст объявлен существующим на ПРОМ, хотя
    находится внутри чужого черновика (никогда на ПРОМ не был). Используется и пост-проходом
    миграции (4.5.4), и линтером (п. 6). Проверяет {--ID: X--} и левую часть {~~ID: X~>..~~}.
    """
    ins = _insertions_by_task(text)
    findings: List[dict] = []

    def check(task: str, xtext: str, pos: int):
        xn = _norm_ws(xtext)
        if len(xn) < _MIN_CONTAINED_LEN:
            return
        for c1, contents in ins.items():
            if c1 != task and any(xn in c for c in contents):
                findings.append({"task": task, "text": xn, "insert_task": c1, "pos": pos})
                return

    for m in _DEL_RE.finditer(text):
        check(m.group(1), m.group(2), m.start())
    for m in _SUB_RE.finditer(text):
        check(m.group(1), m.group(2), m.start())  # левая часть (старое)
    return findings


def postpass_drop_contained_deletions(text: str):
    """Пост-проход миграции (ТЗ 4.5.4): отбрасывает {--C2: X--}, где X входит в состав чужой
    вставки — фрагмент никогда не был на ПРОМ (черновик). Возвращает (новый_текст, dropped)."""
    ins = _insertions_by_task(text)
    dropped: List[dict] = []

    def repl(m):
        task, xtext = m.group(1), m.group(2)
        xn = _norm_ws(xtext)
        if len(xn) >= _MIN_CONTAINED_LEN:
            for c1, contents in ins.items():
                if c1 != task and any(xn in c for c in contents):
                    dropped.append({"task": task, "text": xn, "insert_task": c1})
                    return ""  # отбросить черновик целиком (на ПРОМ не был)
        return m.group(0)

    return _DEL_RE.sub(repl, text), dropped


def _run_list(root: Path, fmt: str, manifest_path: Optional[str]) -> int:
    """Отчёт «что ещё не реализовано» (ТЗ п. 5.4): задачи с маркерами в репозитории.

    С --manifest разделяет мигрировавшие (есть в манифесте) и новые задачи и показывает
    остаток переходного периода — сколько мигрировавших задач ещё имеют маркеры.
    """
    tasks: dict = {}  # task_id -> list[(file, line)]
    for fp in _iter_md_files(root):
        text = _read_text_preserving(fp)
        for task, lines in collect_task_occurrences(text).items():
            tasks.setdefault(task, []).extend((str(fp), ln) for ln in lines)

    found = set(tasks)
    migrated_ids: set = set()
    if manifest_path:
        import yaml  # ленивый импорт: базовый инструмент зависит только от stdlib + bs4
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
        migrated_ids = set((manifest.get("tasks") or {}).keys())

    migrated_present = sorted(found & migrated_ids)
    new_present = sorted(found - migrated_ids)
    cleared = sorted(migrated_ids - found)  # мигрировавшие задачи БЕЗ маркеров (вышли на ПРОМ)

    if fmt == "json":
        payload = {
            "tasks": {t: [{"file": f, "line": ln} for f, ln in sorted(tasks[t])]
                      for t in sorted(tasks)},
            "total_tasks": len(tasks),
        }
        if manifest_path:
            payload["migrated_with_markers"] = migrated_present
            payload["new_tasks"] = new_present
            payload["transition_remaining"] = len(migrated_present)
            payload["migrated_cleared"] = cleared
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    heading = "#" if fmt == "md" else ""
    print(f"{heading} Незавершённые задачи (маркеры в репозитории): {len(tasks)}")
    for task in sorted(tasks):
        positions = sorted(tasks[task])
        print(f"- {task}: {len(positions)} позиций")
        for f, ln in positions:
            print(f"    {f}:{ln}")
    if manifest_path:
        print("")
        print(f"Переходный период: мигрировавших задач с маркерами — {len(migrated_present)} "
              f"из {len(migrated_ids)} (снято на ПРОМ: {len(cleared)}).")
        if migrated_present:
            print("  Остались мигрировавшие: " + ", ".join(migrated_present))
        if new_present:
            print("  Новые задачи (не из манифеста): " + ", ".join(new_present))
    return 0


def _valid_task_id(value: str) -> str:
    if not _TASK_ID_FULL_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"'{value}' не соответствует маске идентификатора задачи ({TASK_ID_PATTERN})"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="critic.py",
        description="Применение и откат правок CriticMarkup по конкретной задаче Jira.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p):
        p.add_argument("--path", default=".", help="файл .md или каталог (обход *.md)")
        p.add_argument("--status-column", default=STATUS_COLUMN,
                       help=f"имя служебного столбца таблиц (по умолчанию '{STATUS_COLUMN}')")

    p_apply = sub.add_parser("apply", help="принять правки задачи (задача вышла на ПРОМ)")
    p_apply.add_argument("task_id", type=_valid_task_id)
    p_apply.add_argument("--dry-run", action="store_true")
    _add_common(p_apply)

    p_reject = sub.add_parser("reject", help="откатить правки задачи (задача отменена)")
    p_reject.add_argument("task_id", type=_valid_task_id)
    p_reject.add_argument("--dry-run", action="store_true")
    _add_common(p_reject)

    p_apply_all = sub.add_parser("apply-all", help="принять правки ВСЕХ задач (целевое состояние)")
    _add_common(p_apply_all)

    p_reject_all = sub.add_parser("reject-all", help="откатить правки ВСЕХ задач (текущий ПРОМ)")
    _add_common(p_reject_all)

    p_lint = sub.add_parser("lint", help="проверить разметку CriticMarkup (Модуль 3, п. 6)")
    p_lint.add_argument("--path", default=".", help="файл .md или каталог (обход *.md)")
    p_lint.add_argument("--format", choices=("text", "json"), default="text",
                        dest="fmt", help="формат вывода находок")

    p_list = sub.add_parser("list", help="список незавершённых задач в репозитории (п. 5.4)")
    p_list.add_argument("--path", default=".", help="файл .md или каталог (обход *.md)")
    p_list.add_argument("--format", choices=("text", "md", "json"), default="text",
                        dest="fmt", help="формат вывода")
    p_list.add_argument("--manifest", default=None,
                        help="migration-manifest.yaml — разделить мигрировавшие/новые задачи")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.path)
    if not root.exists():
        print(f"ОШИБКА: путь не найден: {root}", file=sys.stderr)
        return 2

    if args.command == "lint":
        return _run_lint(root, args.fmt)

    if args.command == "list":
        return _run_list(root, args.fmt, args.manifest)

    status_column = getattr(args, "status_column", STATUS_COLUMN)
    op = "apply" if args.command in ("apply", "apply-all") else "reject"
    task_id = getattr(args, "task_id", None)  # None для *-all
    dry_run = getattr(args, "dry_run", False)
    return _run_edit(op, task_id, root, status_column, dry_run)


if __name__ == "__main__":
    sys.exit(main())
