# app/scripts/repair_export.py
#
# Разовая починка УЖЕ ВЫГРУЖЕННЫХ деревьев: правки экспортёра действуют начиная
# со следующей миграции, а сделанные ранее выгрузки остаются как есть.
#
# Починки включаются флагами, все идемпотентны:
#
#   --unfold  Свёрнутые значения frontmatter → одной строкой. PyYAML сворачивал
#             длинное значение по ширине 80 символов (для кириллицы ~150 байт),
#             и построчные читатели видели обрезанный заголовок с незакрытой
#             кавычкой: разные страницы выглядели дублями (инцидент 2026-08-23).
#             YAML при этом валиден, поэтому чиним НЕ переразбором файла, а
#             склейкой строк — прочие байты не трогаем.
#
#   --flatten-nested
#             Литеральная вложенность маркеров → рядом стоящие маркеры. Нотация
#             вложенность запрещает, apply/reject на таком файле падают жёстко и
#             обрывают весь прогон (инцидент 2026-09-05).
#
#   --unfence-html
#             Ограждения кода внутри HTML-таблицы → <pre>…</pre>. Экспортёр
#             заворачивал JSON-подобный абзац в ```…``` даже внутри ячейки,
#             отданной сырым HTML; содержимое между ограждениями считается кодом
#             и переносится байт-в-байт, поэтому apply/reject не видят маркеры
#             внутри — неутверждённое молча остаётся в «чистом ПРОМ». На дереве
#             [КК] так пряталось 204 фрагмента разметки в 8 файлах.
#
#   --flag-page <JIRA-ID>
#             Страничный флаг `unapproved_jira: <ID>` на ВСЕ страницы пути —
#             для замороженного поддерева: требования не исключены, а отложены,
#             их держат в git и возвращают через `critic apply <ID>`. Ключом
#             --unapproved-jira такое не закрыть: он берёт задачу из маркеров в
#             теле, а у таких страниц маркеров нет вовсе (нет таблицы «История
#             изменений» → нет карты «цвет → задача»). Сторож: задача обязана
#             встречаться маркером в обрабатываемом пути либо быть в манифесте
#             миграции — иначе флаг невидим для `critic list`, страница осталась
#             бы пустой навсегда и никто бы этого не заметил.
#
#   --unapproved-jira <file.json>
#             Проставить страничный флаг `unapproved_jira: <ID>` там, где состав
#             страницы целиком принадлежит неутверждённой задаче. По флагу
#             critic reject опустошает страницу целиком — это лечит остаток
#             fenced-кода макросов, который нотация пометить не может.
#             Задача определяется по маркерам в теле: годится ровно один ID из
#             списка; несколько — конфликт, страница пропускается с сообщением.
#
# Гарантия безопасности: после каждой правки сверяется СМЫСЛ frontmatter
# (yaml.safe_load до и после). Расхождение — файл не пишется, строка в отчёт.
#
# Запуск:
#     python app/scripts/repair_export.py <корень выгрузки> --unfold --dry-run
#     python app/scripts/repair_export.py <корень выгрузки> --unfold
#     python app/scripts/repair_export.py <корень выгрузки> --unapproved-jira unapproved.json

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from app.scripts.CI.critic import (
    TASK_ID_PATTERN, UNAPPROVED_PAGE_KEY, collect_task_occurrences,
    _INS_RE, _DEL_RE, _OPENERS, _find_table_islands, _split_fenced_regions,
)

# Маска Jira ID для проверки списка неутверждённых (как в migrate_confluence_tree).
_TASK_ID_RE = re.compile(TASK_ID_PATTERN)

# Опенер вставки с идентификатором задачи. Имя НЕ _INS_RE: под этим именем из
# critic импортируется регулярка целого маркера с двумя группами — совпадение
# имён перекрывало импорт и роняло уплощение.
_INS_OPENER_RE = re.compile(r"\{\+\+\s*(" + TASK_ID_PATTERN + r")\s*:")

# Строка-ключ frontmatter с непустым значением: `key: значение`.
_KEY_WITH_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*): +(?!$)(.*)$")

# Продолжение свёрнутого значения — отступ, но НЕ список и НЕ вложенный ключ.
_NESTED_KEY_RE = re.compile(r"^\s+[A-Za-z_][A-Za-z0-9_-]*:( |$)")
_LIST_ITEM_RE = re.compile(r"^\s+- ")

# Блочный скаляр (| или >): многострочность там осмысленная, не трогаем.
_BLOCK_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*: *[|>][0-9+-]*\s*$")


def split_frontmatter(text: str) -> Optional[Tuple[str, str, str]]:
    """(открывающая строка, тело frontmatter, остальной файл) либо None."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[0], "".join(lines[1:i]), "".join(lines[i:])
    return None


def unfold_frontmatter(fm_body: str) -> Tuple[str, int]:
    """
    Склеить свёрнутые значения. Возвращает (новое тело, число склеенных строк).

    Склеиваются только продолжения простых значений: список, вложенный ключ и
    блочный скаляр остаются как есть — там перенос строк осмысленный.
    """
    lines = fm_body.splitlines(keepends=True)
    out: List[str] = []
    joined = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\r\n")
        eol = line[len(stripped):]

        if _BLOCK_SCALAR_RE.match(stripped) or not _KEY_WITH_VALUE_RE.match(stripped):
            out.append(line)
            i += 1
            continue

        value = stripped
        j = i + 1
        while j < len(lines):
            nxt = lines[j].rstrip("\r\n")
            if not nxt.startswith(" ") or not nxt.strip():
                break
            if _LIST_ITEM_RE.match(nxt) or _NESTED_KEY_RE.match(nxt):
                break
            value += " " + nxt.strip()   # перенос в свёрнутом скаляре = пробел
            j += 1
            joined += 1
        out.append(value + eol)
        i = j if j > i + 1 else i + 1

    return "".join(out), joined


# Полный вложенный маркер любого типа — по нему режется тело внешнего маркера.
_ANY_FULL_MARKER_RE = re.compile(
    r"\{\+\+\s*" + TASK_ID_PATTERN + r"\s*:.*?\+\+\}"
    r"|\{--\s*" + TASK_ID_PATTERN + r"\s*:.*?--\}"
    r"|\{~~\s*" + TASK_ID_PATTERN + r"\s*:.*?~~\}",
    re.DOTALL,
)

# Токены разметки — снимаются при сверке, что уплощение не тронуло текст.
_MARKER_TOKENS_RE = re.compile(
    r"\{[+~-]{2}\s*" + TASK_ID_PATTERN + r"\s*:\s?|\+\+\}|--\}|~~\}|~>"
)


# Токены разметки для разбора со стеком: опенеры с id и закрыватели.
_TOKEN_RE = re.compile(
    r"\{\+\+\s*(?P<ins_id>" + TASK_ID_PATTERN + r")\s*:\s?"
    r"|\{--\s*(?P<del_id>" + TASK_ID_PATTERN + r")\s*:\s?"
    r"|\{~~\s*(?P<sub_id>" + TASK_ID_PATTERN + r")\s*:\s?"
    r"|\+\+\}|--\}|~~\}"
)


def strip_markers(text: str) -> str:
    """Текст без разметки — инвариант, который уплощение обязано сохранить."""
    return _MARKER_TOKENS_RE.sub("", text)


def _flatten_region(region: str) -> Tuple[str, int]:
    """
    Уплощить вложенность в одной текстовой зоне. Возвращает (текст, число правок).

    Разбор — со СТЕКОМ по токенам, а не регуляркой целого маркера: нежадный
    матчинг закрывает внешний маркер на первом же `++}`, то есть на закрывателе
    ВЛОЖЕННОГО, и настоящая структура остаётся невидимой (на этом первая версия
    уплощения крутилась вхолостую).

    Несопоставимые токены (незакрытый опенер, лишний закрыватель, подстановка
    ~~ во внешней позиции) — зона возвращается без изменений: лучше оставить
    файл линтеру, чем испортить разметку догадкой.
    """
    tokens = list(_TOKEN_RE.finditer(region))
    if not tokens:
        return region, 0

    root: List[dict] = []
    stack: List[dict] = []
    pos = 0

    def add_text(chunk: str):
        target = stack[-1]["children"] if stack else root
        if chunk:
            target.append({"kind": "text", "text": chunk})

    for tok in tokens:
        add_text(region[pos:tok.start()])
        pos = tok.end()
        opener_id = tok.group("ins_id") or tok.group("del_id") or tok.group("sub_id")
        if opener_id:
            kind = "ins" if tok.group("ins_id") else ("del" if tok.group("del_id") else "sub")
            if kind == "sub":
                return region, 0            # подстановку не дробим
            stack.append({"kind": kind, "task": opener_id, "children": [],
                          "raw_open": tok.group(0)})
        else:
            closer = tok.group(0)
            want = {"++}": "ins", "--}": "del", "~~}": "sub"}[closer]
            if not stack or stack[-1]["kind"] != want:
                return region, 0            # разметка не сходится — не трогаем
            node = stack.pop()
            (stack[-1]["children"] if stack else root).append(node)
    add_text(region[pos:])

    if stack:
        return region, 0                    # остались незакрытые опенеры

    nested = _count_nested(root)
    if not nested:
        return region, 0

    return "".join(_render_flat(root)), nested


def _count_nested(nodes: List[dict]) -> int:
    """Сколько маркеров содержат внутри себя другие маркеры."""
    total = 0
    for node in nodes:
        if node["kind"] == "text":
            continue
        if any(ch["kind"] != "text" for ch in node["children"]):
            total += 1
        total += _count_nested(node["children"])
    return total


def _render_flat(nodes: List[dict], parent: Optional[dict] = None) -> List[str]:
    """
    Развернуть дерево в плоскую последовательность маркеров.

    Текст родителя остаётся за его задачей, вложенные маркеры выносятся рядом.
    Пробельные куски маркером не накрываются — это структура (переводы строк,
    маркеры списка), а не требование.
    """
    opener = {"ins": "{++", "del": "{--"}
    closer = {"ins": "++}", "del": "--}"}
    out: List[str] = []
    for node in nodes:
        if node["kind"] == "text":
            if parent is not None and node["text"].strip():
                out.append(f"{opener[parent['kind']]}{parent['task']}: "
                           f"{node['text']}{closer[parent['kind']]}")
            else:
                out.append(node["text"])
            continue
        if any(ch["kind"] != "text" for ch in node["children"]):
            out.extend(_render_flat(node["children"], node))
        else:
            body = "".join(ch["text"] for ch in node["children"])
            out.append(f"{opener[node['kind']]}{node['task']}: {body}{closer[node['kind']]}")
    return out


def flatten_nested(text: str) -> Tuple[str, int]:
    """
    Уплощить литеральную вложенность маркеров во всём файле.

    {++A: X {++B: Y++} Z++}  →  {++A: X++} {++B: Y++} {++A: Z++}

    Нотация вложенность запрещает (разбор регулярками, не рекурсивным парсером),
    и apply/reject на таком файле падают жёстко, обрывая весь прогон. Экспортёр
    её всё же порождает, когда блочный маркер накрывает участок с врезками
    соседних задач (инцидент 2026-09-05).

    Внутренние маркеры переносятся как есть; текст внешнего маркера остаётся за
    его задачей. Пробельные куски маркером не накрываются — это структура
    (переводы строк, маркеры списка), а не требование. Замены {~~…~>…~~}
    не трогаем: у подстановки два тела, безопасного дробления нет.
    """
    out, total = [], 0
    for kind, region, _base in _split_fenced_regions(text):
        if kind == "code":
            out.append(region)              # fenced-код — байт-в-байт
            continue
        new_region, count = _flatten_region(region)
        out.append(new_region)
        total += count
    return "".join(out), total


_FENCE_TOKEN_RE = re.compile(r"`{3,}")


def unfence_html(text: str) -> Tuple[str, int]:
    """
    Ограждения кода внутри HTML-острова заменить на <pre>…</pre>.

    Экспортёр заворачивал JSON-подобный абзац в ```…``` даже внутри ячейки
    таблицы, отданной сырым HTML. В markdown такое не рендерится как код нигде,
    а для конвейера последствия тяжелее: содержимое между ограждениями считается
    кодом и переносится байт-в-байт, поэтому apply/reject не видят маркеры
    внутри — неутверждённые требования молча остаются в «чистом ПРОМ».
    На дереве [КК] так пряталось 204 фрагмента разметки в 8 файлах.

    Заменяются ТОЛЬКО ограничители, содержимое не трогается. Остров с нечётным
    числом ограждений пропускается: пары не сходятся — значит, случай не наш,
    и гадать нельзя (его покажет линтер).
    """
    islands = _find_table_islands(text)
    if not islands:
        return text, 0
    out: List[str] = []
    last = 0
    total = 0
    for start, end in islands:
        segment = text[start:end]
        spans = [m.span() for m in _FENCE_TOKEN_RE.finditer(segment)]
        if len(spans) < 2 or len(spans) % 2:
            continue
        parts: List[str] = []
        prev = 0
        for i, (a, b) in enumerate(spans):
            parts.append(segment[prev:a])
            parts.append("<pre>" if i % 2 == 0 else "</pre>")
            prev = b
        parts.append(segment[prev:])
        out.append(text[last:start])
        out.append("".join(parts))
        last = end
        total += len(spans) // 2
    out.append(text[last:])
    return "".join(out), total


def marker_tasks(body: str) -> set:
    """Идентификаторы задач, чьи вставки есть в теле страницы."""
    return {m.group(1) for m in _INS_OPENER_RE.finditer(body)}


def set_page_flag(fm_body: str, task: str) -> Tuple[str, bool]:
    """Дописать `unapproved_jira: <task>` после строки status, иначе в конец."""
    if re.search(r"^" + UNAPPROVED_PAGE_KEY + r":", fm_body, re.MULTILINE):
        return fm_body, False
    eol = "\r\n" if "\r\n" in fm_body else "\n"
    new_line = UNAPPROVED_PAGE_KEY + ": " + task + eol
    lines = fm_body.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.startswith("status:"):
            lines.insert(idx + 1, new_line)
            return "".join(lines), True
    return fm_body + new_line, True


def page_flag_of(fm_body: str) -> Optional[str]:
    """Задача из страничного флага frontmatter, если он уже стоит."""
    m = re.search(r"^" + UNAPPROVED_PAGE_KEY + r":\s*(\S+)", fm_body, re.MULTILINE)
    return m.group(1) if m else None


def task_known_in_tree(task: str, files: List[Path]) -> bool:
    """Встречается ли задача маркером хоть на одной странице обрабатываемого пути.

    Сторож для --flag-page. Флаг задачи, которой в дереве нет, невидим: `list`
    показывает только маркеры, поэтому такая задача не попадёт ни в «хвост»
    незавершённых, ни в порядок вливания — страница останется пустой навсегда,
    и никто этого не заметит. Асимметрия ошибок: лишний отказ безобиден,
    потерянные требования — нет.
    """
    for path in files:
        with open(path, "r", encoding="utf-8", newline="") as f:
            if task in collect_task_occurrences(f.read()):
                return True
    return False


def task_in_manifest(task: str, root: Path) -> bool:
    """Задача перечислена в манифесте миграции рядом с выгрузкой (или выше по пути)."""
    base = root if root.is_dir() else root.parent
    for folder in [base, *base.parents][:5]:
        manifest = folder / "migration-manifest.yaml"
        if manifest.is_file():
            try:
                with open(manifest, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                return False
            return task in (data.get("tasks") or {})
    return False


def load_unapproved_ids(path: Path) -> set:
    """Список неутверждённых задач: ["GBO-1", ...] или {"unapproved_jira": [...]}."""
    data = json.loads(path.read_text(encoding="utf-8"))
    ids = data.get("unapproved_jira") if isinstance(data, dict) else data
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ValueError('нужен список строк Jira ID или {"unapproved_jira": [...]}')
    result = {x.strip() for x in ids if x.strip()}
    bad = [x for x in result if not _TASK_ID_RE.fullmatch(x)]
    if bad:
        raise ValueError("не похожи на Jira ID: " + str(sorted(bad)) + " (маска PROJECT-123)")
    return result


def repair_file(path: Path, unfold: bool, unapproved: Optional[set],
                flatten: bool = False, unfence: bool = False,
                flag_page: Optional[str] = None) -> Dict:
    """Починить один файл. Возвращает отчёт; ключ 'changed' — писать ли файл."""
    report: Dict = {"path": path, "unfolded": 0, "flagged": None, "flattened": 0,
                    "unfenced": 0, "changed": False, "skipped": None, "new_text": None}
    with open(path, "r", encoding="utf-8", newline="") as f:
        original = f.read()

    parts = split_frontmatter(original)
    if parts is None:
        # Уплощение и распакование ограждений работают и без frontmatter —
        # они про тело файла.
        if flatten or unfence:
            new_text = original
            if flatten:
                new_text, count = flatten_nested(new_text)
                if count:
                    if strip_markers(new_text) != strip_markers(original):
                        report["skipped"] = "уплощение изменило бы текст — файл не тронут"
                        return report
                    report["flattened"] = count
            if unfence:
                new_text, fenced = unfence_html(new_text)
                report["unfenced"] = fenced
            if new_text != original:
                report["changed"] = True
                report["new_text"] = new_text
                return report
        report["skipped"] = "нет frontmatter"
        return report
    head, fm_body, rest = parts

    try:
        before = yaml.safe_load(fm_body) or {}
    except yaml.YAMLError as e:
        report["skipped"] = "frontmatter не разбирается: " + str(e)
        return report

    new_fm = fm_body
    if unfold:
        new_fm, joined = unfold_frontmatter(new_fm)
        report["unfolded"] = joined

    if flag_page:
        # Прямая простановка флага по пути: страница заморожена целиком, её состав
        # в ПРОМ не входит. В отличие от --unapproved-jira задача берётся не из
        # маркеров в теле — у таких страниц маркеров может не быть вовсе
        # (нет таблицы «История изменений» → нет карты «цвет → задача»).
        current = page_flag_of(new_fm)
        if current and current != flag_page:
            report["skipped"] = "на странице уже стоит флаг другой задачи: " + current
            return report
        new_fm, added = set_page_flag(new_fm, flag_page)
        if added:
            report["flagged"] = flag_page

    if unapproved:
        tasks = marker_tasks(rest) & unapproved
        if len(tasks) > 1:
            report["skipped"] = "несколько неутверждённых задач: " + str(sorted(tasks))
            return report
        if len(tasks) == 1:
            task = tasks.pop()
            new_fm, added = set_page_flag(new_fm, task)
            if added:
                report["flagged"] = task

    new_rest = rest
    if flatten:
        new_rest, count = flatten_nested(rest)
        if count:
            # Инвариант: уплощение переставляет ТОЛЬКО разметку, текст неприкосновенен
            if strip_markers(new_rest) != strip_markers(rest):
                report["skipped"] = "уплощение изменило бы текст — файл не тронут"
                return report
            report["flattened"] = count

    if unfence:
        # Инвариант держится конструкцией: unfence_html подменяет ровно найденные
        # ограничители и не трогает ни байта между ними (см. тесты режима).
        new_rest, fenced = unfence_html(new_rest)
        report["unfenced"] = fenced

    if new_fm == fm_body and new_rest == rest:
        return report

    # Гейт смысла: правка обязана быть чисто оформительской (плюс новый флаг).
    try:
        after = yaml.safe_load(new_fm) or {}
    except yaml.YAMLError as e:
        report["skipped"] = "после правки frontmatter не разбирается: " + str(e)
        return report
    expected = dict(before)
    if report["flagged"]:
        expected[UNAPPROVED_PAGE_KEY] = report["flagged"]
    if after != expected:
        report["skipped"] = "смысл frontmatter изменился бы — файл не тронут"
        return report

    report["changed"] = True
    report["new_text"] = head + new_fm + new_rest
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Починка уже выгруженных деревьев Confluence")
    parser.add_argument("root", help="корень выгрузки (каталог с .md) или один файл")
    parser.add_argument("--unfold", action="store_true",
                        help="склеить свёрнутые значения frontmatter в одну строку")
    parser.add_argument("--flatten-nested", action="store_true",
                        help="уплощить литеральную вложенность маркеров "
                             "(apply/reject на таких файлах падают)")
    parser.add_argument("--unfence-html", action="store_true",
                        help="ограждения кода внутри HTML-таблиц заменить на "
                             "<pre> (иначе apply/reject не видят маркеры внутри)")
    parser.add_argument("--flag-page", metavar="JIRA-ID",
                        help="проставить страничный флаг unapproved_jira на ВСЕХ "
                             "страницах пути (замороженное поддерево: маркеров в "
                             "теле может не быть вовсе)")
    parser.add_argument("--unapproved-jira", metavar="FILE",
                        help="JSON со списком неутверждённых задач: проставить "
                             "страничный флаг unapproved_jira")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что изменилось бы, ничего не записывая")
    args = parser.parse_args(argv)

    if not (args.unfold or args.unapproved_jira or args.flatten_nested
            or args.unfence_html or args.flag_page):
        parser.error("укажите хотя бы одну починку: --unfold, --flatten-nested, "
                     "--unfence-html, --flag-page и/или --unapproved-jira")
    if args.flag_page and not _TASK_ID_RE.fullmatch(args.flag_page):
        parser.error("--flag-page: %r не похож на Jira ID" % args.flag_page)

    root = Path(args.root)
    if not root.exists():
        print("ОШИБКА: путь не существует: " + str(root))
        return 2

    unapproved = None
    if args.unapproved_jira:
        try:
            unapproved = load_unapproved_ids(Path(args.unapproved_jira))
        except (OSError, ValueError) as e:
            print("ОШИБКА: " + args.unapproved_jira + ": " + str(e))
            return 2
        print("Неутверждённых задач в списке: " + str(len(unapproved)))

    files = sorted(root.rglob("*.md")) if root.is_dir() else [root]

    if args.flag_page:
        # Сторож: флаг задачи, которой в дереве нет маркерами и нет в манифесте,
        # невидим для `critic list` — страница осталась бы пустой навсегда.
        if not (task_known_in_tree(args.flag_page, files)
                or task_in_manifest(args.flag_page, root)):
            print("ОШИБКА: задача " + args.flag_page + " не встречается в "
                  "обрабатываемом пути маркерами и не найдена в манифесте "
                  "миграции. Флаг такой задачи не увидит ни `critic list`, ни "
                  "порядок вливания — страницы остались бы пустыми навсегда. "
                  "Проверьте идентификатор.")
            return 2

    changed = unfolded_total = flagged_total = flattened_total = unfenced_total = 0
    skipped: List[Tuple[Path, str]] = []

    for path in files:
        rep = repair_file(path, args.unfold, unapproved, args.flatten_nested,
                          args.unfence_html, args.flag_page)
        if rep["skipped"] and rep["skipped"] != "нет frontmatter":
            skipped.append((path, rep["skipped"]))
        if not rep["changed"]:
            continue
        changed += 1
        unfolded_total += rep["unfolded"]
        flattened_total += rep["flattened"]
        unfenced_total += rep["unfenced"]
        if rep["flagged"]:
            flagged_total += 1
        what = []
        if rep["unfolded"]:
            what.append("склеено строк: " + str(rep["unfolded"]))
        if rep["flattened"]:
            what.append("уплощено маркеров: " + str(rep["flattened"]))
        if rep["unfenced"]:
            what.append("ограждений распаковано: " + str(rep["unfenced"]))
        if rep["flagged"]:
            what.append("флаг " + rep["flagged"])
        prefix = "[dry-run] " if args.dry_run else ""
        print(prefix + str(path) + ": " + ", ".join(what))
        if not args.dry_run:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(rep["new_text"])

    for path, why in skipped:
        print("ПРОПУЩЕНО " + str(path) + ": " + why)

    prefix = "[dry-run] " if args.dry_run else ""
    print(prefix + "файлов просмотрено " + str(len(files)) +
          ", изменено " + str(changed) +
          " (склеено строк " + str(unfolded_total) +
          ", уплощено маркеров " + str(flattened_total) +
          ", ограждений распаковано " + str(unfenced_total) +
          ", флагов проставлено " + str(flagged_total) +
          ", пропущено с предупреждением " + str(len(skipped)) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
