# app/scripts/CI/raw_oracle.py
"""Raw-оракул снятий (FB-32 КК, решение владельца канона 23.10, ход 24.10).

ЗАЧЕМ. Архив выгрузки `sources/raw` несёт истинные пометки правок задач в CriticMarkup;
применённая копия `sources/confluence` их теряет (зачёркнутое снято или оставлено — по копии
не видно). Фрагмент, который в raw стоит под маркером УДАЛЕНИЯ введённой задачи, не должен
дословно присутствовать в применённой копии той же страницы: если присутствует — снятие не
доведено (команда КК находила такие недоснятия вслепую). Модуль делает это проверяемым.

ЧТО СЧИТАЕТСЯ del-фрагментом — все три нотации критика (critic.py), разбор его же функциями:
  - wiki: {--ID: …--} и старая часть {~~ID: старое~>новое~~};
  - HTML-острова (верхнеуровневые <table>…</table>): span.critic-del и tr.critic-row-del с
    data-task="ID";
  - markdown-таблица со столбцом status: строка со значением «-ID».
Маркеры внутри fenced-блоков кода не действуют и не проверяются.

ПРИСУТСТВУЕТ ДОСЛОВНО — после одинаковой нормализации обеих сторон (normalize): HTML-теги сняты
(блочные границы и ячейки — пробелом, строчные — без пробела), маркеры CriticMarkup и значения
столбца status сняты, markdown-ссылка — её текст, экранирование «\\[» раскрыто, сущности
&lt;/&gt;/&amp; раскрыты, пробелы схлопнуты, регистр не учитывается, пунктуация по краям
фрагмента снята. Короче порога (4 слова и 20 символов) — не проверяется.

ОГРАНИЧИТЕЛИ на несрабатывание: задача не введена (её фрагменты в копии и должны быть,
reject-all); тот же текст вставлен введённой задачей на той же странице (законно вернулся);
фрагмент в ячейке вложенной таблицы HTML-острова (по умолчанию пропуск — там маркеры были
ненадёжны до экспортёра 1.9.8); короткий; у страницы нет применённой копии; текст фрагмента
законно остаётся на странице в другом месте (повторов в копии не больше, чем в raw вне
удалений введённых задач). Повторы одного фрагмента — одна находка с ×N.

Интерфейс рассчитан и на режим карточек (второй срез, развилка владельца): check_page принимает
любой текст-цель, не только копию.
"""
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

try:
    from . import critic as _critic
except ImportError:  # запуск файлом / импорт из каталога прибора
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import critic as _critic  # type: ignore

# порог длины: короче — шум на «Да», «Строка (100)», номерах шагов; 4 слова И 20 символов
MIN_WORDS = 4
MIN_CHARS = 20

_TASK = _critic.TASK_ID_PATTERN
_PID_RE = re.compile(r"^confluence_page_id:\s*['\"]?(\d+)", re.M)
# блочные теги — граница слова (ячейки, абзацы, строки); прочие (strong, span, a …) — без пробела:
# «<strong>Е</strong><strong>сли</strong>» — одно слово «Если»
_BLOCK_TAG_RE = re.compile(r"</?(?:td|th|tr|p|div|li|ul|ol|br|table|thead|tbody|tfoot|h[1-6]|"
                           r"blockquote|pre|caption)\b[^>]*>", re.I)
# строчные HTML-теги — закрытым перечнем имён: плейсхолдеры атрибутов в угловых скобках
# («<Признак открытия нового счета>», «<cvv2>») — текст, не теги; в raw они стоят как есть, в
# копии — через &lt;…&gt;, и снятие «всего в угловых скобках» выбрасывало их с одной стороны
# (три ложных сигнала на КК, 2166860176 и 2166860206)
_INLINE_TAGS = ("a|span|strong|em|b|i|u|s|strike|del|ins|code|sub|sup|font|img|col|colgroup|"
                "small|big|mark|abbr|cite|q|kbd|var|samp|label|hr|time|section|article|header|"
                "footer|figure|figcaption|dl|dt|dd|details|summary|input|button|nobr|wbr")
_ANY_TAG_RE = re.compile(r"</?(?:" + _INLINE_TAGS + r")\b[^>]*>", re.I)
_MARKER_OPEN_RE = re.compile(r"\{(?:\+\+|--|~~)\s*" + _TASK + r"\s*:\s*")
_MARKER_CLOSE_RE = re.compile(r"\+\+\}|--\}|~~\}|~>")
_STATUS_TOKEN_RE = re.compile(r"(?<![\w-])[+-]" + _TASK + r"(?![\w-])")
_MD_LINK_RE = re.compile(r"!?\[((?:\\.|[^\]\\])*)\]\([^)]*\)")
_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
_EDGE_PUNCT = " .,;:!?-–—«»\"'()[]|*_"


@dataclass
class Fragment:
    task: str
    kind: str          # wiki | sub | html-span | html-row | md-row
    text: str          # исходный текст фрагмента (как в raw)
    line: int          # строка raw
    nested: bool = False
    norm: str = ""


@dataclass
class Finding:
    task: str
    kind: str
    norm: str
    count: int         # сколько раз «лишний» фрагмент найден в цели
    line: int          # первая строка цели


@dataclass
class PageReport:
    findings: List[Finding] = field(default_factory=list)
    short: int = 0
    nested: int = 0
    reinserted: int = 0
    kept_elsewhere: int = 0
    checked: int = 0


def normalize(text: str) -> str:
    """Одна нормализация для raw-фрагмента и для цели (см. шапку модуля)."""
    t = _BLOCK_TAG_RE.sub(" ", text)
    t = _ANY_TAG_RE.sub("", t)
    t = _MARKER_OPEN_RE.sub(" ", t)
    t = _MARKER_CLOSE_RE.sub(" ", t)
    t = _STATUS_TOKEN_RE.sub(" ", t)
    t = _MD_LINK_RE.sub(lambda m: m.group(1), t)
    t = _ESCAPE_RE.sub(r"\1", t)
    t = html.unescape(t)
    # разметка markdown карточки против HTML архива: выделение «**»/«__», код «`», кавычки
    # «ёлочки»/лапки против прямых, «ё» против «е» — одинаково с обеих сторон (без этого режим
    # карточек на КК давал фиктивный ноль: SCR-CL-20 — 56 фрагментов, ни одно совпадение)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    t = re.sub(r"[«»“”„\"]", '"', t).replace("ё", "е").replace("Ё", "Е")
    t = t.replace("|", " ")
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t.strip(_EDGE_PUNCT).strip()


def _long_enough(norm: str) -> bool:
    return len(norm) >= MIN_CHARS and len(norm.split()) >= MIN_WORDS


def _text_regions(text: str) -> List[Tuple[str, int, int]]:
    """(текст зоны, её смещение в файле, первая строка) — только зоны вне блоков кода."""
    out: List[Tuple[str, int, int]] = []
    pos = 0
    for kind, region, line in _critic._split_fenced_regions(text):
        if kind == "text":
            out.append((region, pos, line))
        pos += len(region)
    return out


def _island_fragments(island: str, base_line: int, want: str) -> List[Fragment]:
    """span/tr с классом critic-del (want='del') или critic-ins (want='ins') внутри острова;
    вложенность — по числу предков table внутри острова (сам остров — первый)."""
    soup = BeautifulSoup(island, "html.parser")
    out: List[Fragment] = []
    span_cls, row_cls = f"critic-{want}", f"critic-row-{want}"
    for tag in soup.find_all(["span", "tr"]):
        classes = tag.get("class") or []
        if (tag.name == "span" and span_cls not in classes) or (tag.name == "tr" and row_cls not in classes):
            continue
        task = tag.get("data-task", "")
        depth = sum(1 for p in tag.parents if getattr(p, "name", None) == "table")
        line = base_line + (getattr(tag, "sourceline", 1) or 1) - 1
        out.append(Fragment(task=task, kind="html-span" if tag.name == "span" else "html-row",
                            text=tag.decode_contents(), line=line, nested=depth > 1))
    return out


def _md_status_rows(region: str, base_line: int, sign: str) -> List[Fragment]:
    """Строки markdown-таблиц со столбцом status = «-ID» (sign='-') или «+ID» (sign='+')."""
    out: List[Fragment] = []
    lines = region.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if (_critic._is_table_row(lines[i]) and i + 1 < len(lines)
                and _critic._is_separator_row(lines[i + 1])):
            pidx = _critic._find_status_part_index(lines[i], _critic.STATUS_COLUMN)
            j = i + 2
            while j < len(lines) and _critic._is_table_row(lines[j]):
                if pidx is not None:
                    parts, _end = _critic._split_row_parts(lines[j])
                    if pidx < len(parts):
                        m = _critic._STATUS_VAL_RE.match(parts[pidx].strip())
                        if m and m.group(1) == sign:
                            cells = [c for k, c in enumerate(parts) if k != pidx]
                            out.append(Fragment(task=m.group(2), kind="md-row",
                                                text=" | ".join(cells), line=base_line + j))
                j += 1
            i = j
        else:
            i += 1
    return out


def _collect(text: str, want: str) -> List[Fragment]:
    frags: List[Fragment] = []
    for region, _off, base in _text_regions(text):
        if want == "del":
            for m in _critic._DEL_RE.finditer(region):
                frags.append(Fragment(m.group(1), "wiki", m.group(2),
                                      base + region.count("\n", 0, m.start())))
            for m in _critic._SUB_RE.finditer(region):
                frags.append(Fragment(m.group(1), "sub", m.group(2),
                                      base + region.count("\n", 0, m.start())))
        else:
            for m in _critic._INS_RE.finditer(region):
                frags.append(Fragment(m.group(1), "wiki", m.group(2),
                                      base + region.count("\n", 0, m.start())))
            for m in _critic._SUB_RE.finditer(region):
                frags.append(Fragment(m.group(1), "sub", m.group(3),
                                      base + region.count("\n", 0, m.start())))
        for s, e in _critic._find_table_islands(region):
            frags.extend(_island_fragments(region[s:e], base + region.count("\n", 0, s), want))
        frags.extend(_md_status_rows(region, base, "-" if want == "del" else "+"))
    for f in frags:
        f.norm = normalize(f.text)
    return frags


def collect_del_fragments(raw_text: str) -> List[Fragment]:
    """Все del-фрагменты страницы (все задачи)."""
    return _collect(raw_text, "del")


def collect_ins_fragments(raw_text: str) -> List[Fragment]:
    """Все вставки страницы (все задачи): {++…++}, правая часть ~~, critic-ins, строки «+ID»."""
    return _collect(raw_text, "ins")


def _norm_lines(text: str) -> Tuple[str, List[int]]:
    """Нормализованный текст цели построчно (через пробел) и смещения начала строк."""
    parts: List[str] = []
    starts: List[int] = []
    pos = 0
    for ln in text.splitlines():
        n = normalize(ln)
        starts.append(pos)
        parts.append(n)
        pos += len(n) + 1
    return " ".join(parts), starts


def _line_of(starts: List[int], offset: int) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def check_page(raw_text: str, target_text: str, introduced: Set[str],
               include_nested: bool = False) -> PageReport:
    """Сверка одной страницы: del-фрагменты введённых задач против текста цели (применённой
    копии или — во втором срезе — карточки)."""
    rep = PageReport()
    dels = [f for f in collect_del_fragments(raw_text) if f.task in introduced]
    if not dels:
        return rep
    ins_all = collect_ins_fragments(raw_text)
    inserted = [f.norm for f in ins_all if f.task in introduced and f.norm]
    raw_norm, _ = _norm_lines(raw_text)
    # raw «как должно остаться» в применённой копии: без удалений введённых задач и без вставок
    # невведённых (reject-all снимает их) — каждое вхождение по одному разу
    raw_kept = raw_norm
    gone = dels + [f for f in ins_all if f.task not in introduced]
    for f in sorted(gone, key=lambda x: -len(x.norm)):
        if f.norm:
            raw_kept = raw_kept.replace(f.norm, " ", 1)
    target_norm, starts = _norm_lines(target_text)
    seen: Dict[Tuple[str, str], Finding] = {}
    for f in dels:
        if f.nested and not include_nested:
            rep.nested += 1
            continue
        if not _long_enough(f.norm):
            rep.short += 1
            continue
        if any(f.norm in ins for ins in inserted):
            rep.reinserted += 1
            continue
        rep.checked += 1
        key = (f.task, f.norm)
        if key in seen:
            continue
        in_target = target_norm.count(f.norm)
        allowed = raw_kept.count(f.norm)
        if in_target > allowed:
            rep.findings.append(Finding(f.task, f.kind, f.norm, in_target - allowed,
                                        _line_of(starts, target_norm.find(f.norm))))
            seen[key] = rep.findings[-1]
        elif in_target:
            rep.kept_elsewhere += 1
    return rep


def page_id_of(text: str) -> Optional[str]:
    m = _PID_RE.search(text[:4000])
    return m.group(1) if m else None


def index_pages(root: Path, skip_dirs: Iterable[str] = ()) -> Dict[str, Path]:
    """page_id → файл (первый по сортировке)."""
    out: Dict[str, Path] = {}
    skip = set(skip_dirs)
    for p in sorted(root.rglob("*.md")):
        if skip & set(p.relative_to(root).parts):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        pid = page_id_of(head)
        if pid:
            out.setdefault(pid, p)
    return out


def check_tree(raw_root: Path, applied_root: Path, introduced: Set[str],
               include_nested: bool = False) -> Tuple[List[str], Dict[str, int]]:
    """Строки отчёта (⚠/i) и счётчики по дереву raw против дерева применённых копий. bs4 и
    нормализация — только для raw-файлов, где встречается ID введённой задачи."""
    stats = {"pages": 0, "no_copy": 0, "short": 0, "nested": 0, "reinserted": 0,
             "kept": 0, "checked": 0, "findings": 0}
    lines: List[str] = []
    if not introduced:
        return lines, stats
    id_re = re.compile("|".join(re.escape(t) for t in sorted(introduced)))
    copies: Optional[Dict[str, Path]] = None
    for raw in sorted(raw_root.rglob("*.md")):
        try:
            text = raw.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not id_re.search(text):
            continue
        pid = page_id_of(text)
        if pid is None:
            continue
        if copies is None:
            copies = index_pages(applied_root, skip_dirs=("raw",))
        rel = raw.relative_to(raw_root)
        copy = copies.get(pid) or (applied_root / rel if (applied_root / rel).is_file() else None)
        dels_here = [f for f in collect_del_fragments(text) if f.task in introduced]
        if not dels_here:
            continue
        stats["pages"] += 1
        if copy is None:
            stats["no_copy"] += 1
            continue
        target = copy.read_text(encoding="utf-8", errors="replace")
        rep = check_page(text, target, introduced, include_nested)
        stats["short"] += rep.short
        stats["nested"] += rep.nested
        stats["reinserted"] += rep.reinserted
        stats["kept"] += rep.kept_elsewhere
        stats["checked"] += rep.checked
        for f in rep.findings:
            stats["findings"] += 1
            try:
                where = copy.relative_to(applied_root.parent).as_posix()
            except ValueError:
                where = copy.as_posix()
            lines.append(f"⚠ сверка по архиву raw: удалённое введённой задачей {f.task} осталось в применённой копии "
                         f"страницы {pid} ({where}:{f.line})"
                         + (f" ×{f.count}" if f.count > 1 else "") + f" — «{f.norm[:80]}»")
    return lines, stats


# ======================================================================================
# Режим карточек (второй срез, решение владельца пилота 25.10): тот же оракул против docs/ —
# del-фрагмент введённой задачи дословно присутствует в карточке-потребителе страницы
# (confluence_page_ids карточки содержит page_id) — незакрытый дозаход после ввода задачи.
# Отдельный отчёт, не selfcheck: на мигрированном до ввода комплекте сигналов десятки.
# ======================================================================================

# разделы карточки, где текст источника законно цитируется или фиксируется как пробел, —
# закрытый перечень по шаблонам канона (_meta/templates): совпадение в них не сигнал
CARD_EXCLUDED_SECTIONS = (
    "происхождение",
    "долги и открытые вопросы",
    "открытые вопросы",
    "что не переносится из источника",
    "изображения источника",
    "источники миграции",
    "долги по ссылкам",
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_DEBT_LINE_RE = re.compile(r"\bOQ-\d+\b|(?<![\w-])долг", re.I)
_CARD_PIDS_RE = re.compile(r"^confluence_page_ids?:\s*\[?([^\]\n]*)", re.M)


def _heading_key(title: str) -> str:
    t = re.sub(r"^\d+(?:\.\d+)*\.?\s*", "", title.strip())          # «10. » — номер раздела
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)                              # «(обязательный…)»
    return t.strip().lower()


def card_mask(text: str) -> Tuple[str, int]:
    """Текст карточки, где строки исключённых разделов и строки долга/OQ заменены пустыми
    (нумерация строк сохраняется), и число исключённых строк."""
    out: List[str] = []
    skip_level = 0
    n = 0
    in_fm = False
    for i, ln in enumerate(text.splitlines()):
        if i == 0 and ln.strip() == "---":
            in_fm = True
            out.append("")
            continue
        if in_fm:
            out.append("")
            if ln.strip() == "---":
                in_fm = False
            continue
        m = _HEADING_RE.match(ln)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and _heading_key(m.group(2)) in CARD_EXCLUDED_SECTIONS:
                skip_level = level
        if skip_level or _DEBT_LINE_RE.search(ln):
            out.append("")
            n += 1
        else:
            out.append(ln)
    return "\n".join(out), n


def card_page_ids(text: str) -> List[str]:
    m = _CARD_PIDS_RE.search(text[:4000])
    return re.findall(r"\d{4,}", m.group(1)) if m else []


def index_cards(docs: Path) -> Dict[str, List[Path]]:
    """page_id → карточки docs/, в confluence_page_ids которых он стоит."""
    out: Dict[str, List[Path]] = {}
    for p in sorted(docs.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(docs).parts):
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        for pid in card_page_ids(head):
            out.setdefault(pid, []).append(p)
    return out


# находка короче этого — «проверить» в отчёте (решение калибровки 25.10: порог 4/20 не
# поднимаем — пропуск хуже шума, — а короткие общие фразы помечаем для человека)
CHECK_WORDS = 4
CHECK_CHARS = 30


@dataclass
class CardRow:
    card: str
    page: str
    task: str
    fragment: str
    line: int
    count: int
    section: str = ""
    check: bool = False


def _section_at(text: str, line: int) -> str:
    """Ближайший заголовок карточки над строкой (без «#»)."""
    title = ""
    for i, ln in enumerate(text.splitlines(), 1):
        if i > line:
            break
        m = _HEADING_RE.match(ln)
        if m:
            title = m.group(2).strip()
    return title


def check_cards(raw_root: Path, docs: Path, introduced: Set[str],
                include_nested: bool = True) -> Tuple[List[CardRow], Dict[str, int]]:
    """Del-фрагменты введённых задач против карточек-потребителей страниц. Ячейки вложенных
    таблиц по умолчанию проверяются (решение 25.10: ЭФ состоят из вложенных таблиц, снятия
    живут там; ненадёжная разметка — до экспортёра 1.9.8, архив КК — 1.9.13)."""
    stats = {"pages": 0, "pages_no_card": 0, "cards": 0, "checked": 0, "short": 0, "nested": 0,
             "reinserted": 0, "kept": 0, "excluded": 0, "findings": 0}
    rows: List[CardRow] = []
    if not introduced:
        return rows, stats
    id_re = re.compile("|".join(re.escape(t) for t in sorted(introduced)))
    cards = index_cards(docs)
    seen_cards: Set[Path] = set()
    for raw in sorted(raw_root.rglob("*.md")):
        try:
            text = raw.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not id_re.search(text):
            continue
        pid = page_id_of(text)
        if pid is None or not any(f.task in introduced for f in collect_del_fragments(text)):
            continue
        stats["pages"] += 1
        targets = cards.get(pid, [])
        if not targets:
            stats["pages_no_card"] += 1
            continue
        for card in targets:
            seen_cards.add(card)
            full = card.read_text(encoding="utf-8", errors="replace")
            masked, _n = card_mask(full)
            rep = check_page(text, masked, introduced, include_nested)
            rep_full = check_page(text, full, introduced, include_nested)
            stats["checked"] += rep.checked
            stats["short"] += rep.short
            stats["nested"] += rep.nested
            stats["reinserted"] += rep.reinserted
            stats["kept"] += rep.kept_elsewhere
            stats["excluded"] += max(0, len(rep_full.findings) - len(rep.findings))
            for f in rep.findings:
                stats["findings"] += 1
                try:
                    rel = card.relative_to(docs).as_posix()
                except ValueError:
                    rel = card.as_posix()
                rows.append(CardRow(rel, pid, f.task, f.norm, f.line, f.count,
                                    _section_at(full, f.line),
                                    len(f.norm.split()) <= CHECK_WORDS or len(f.norm) < CHECK_CHARS))
    stats["cards"] = len(seen_cards)
    return rows, stats


def cards_report(rows: List[CardRow], stats: Dict[str, int], introduced: Set[str],
                 docs: Path, raw_root: Path) -> List[str]:
    esc = lambda x: str(x).replace("|", "\\|")  # noqa: E731
    out = ["# Сверка по архиву raw: удалённое введёнными задачами осталось в карточках комплекта", "",
           f"Комплект: `{docs}`; архив: `{raw_root}`; введённые задачи: {', '.join(sorted(introduced)) or '—'}.",
           "Сигнал — фрагмент, который в архиве raw стоит под маркером удаления введённой задачи, дословно "
           "(после нормализации) присутствует в карточке-потребителе страницы чаще, чем в архиве вне "
           "удалений введённых задач: снятие не доведено до карточки (дозаход после ввода задачи).", "",
           "## Сводка", "",
           f"- страниц архива raw с маркерами удаления введённых задач: {stats['pages']}, из них без карточки-"
           f"потребителя: {stats['pages_no_card']}; карточек проверено: {stats['cards']};",
           f"- удалённых фрагментов проверено: {stats['checked']}; находок: {stats['findings']};",
           f"- пропущено: короткие (меньше {MIN_WORDS} слов или {MIN_CHARS} символов) {stats['short']}, "
           f"в ячейках вложенных таблиц {stats['nested']}, вставлены введённой задачей "
           f"{stats['reinserted']}, законно встречаются вне удалений {stats['kept']}, только в разделах "
           f"«Происхождение», «Долги и открытые вопросы» и т. п. или в строках долга / OQ "
           f"{stats['excluded']}.", ""]
    if not rows:
        out.append("**Находок нет** — удалённое введёнными задачами в карточках не осталось.")
        return out
    by_task: Dict[str, int] = {}
    by_card: Dict[str, int] = {}
    for r in rows:
        by_task[r.task] = by_task.get(r.task, 0) + 1
        by_card[r.card] = by_card.get(r.card, 0) + 1
    out += ["### По задачам", "", "| Задача | Находок |", "|---|---|"]
    out += [f"| {t} | {n} |" for t, n in sorted(by_task.items())]
    out += ["", "### По карточкам", "", "| Карточка | Находок |", "|---|---|"]
    out += [f"| {esc(c)} | {n} |" for c, n in sorted(by_card.items(), key=lambda x: (-x[1], x[0]))]
    n_check = sum(1 for r in rows if r.check)
    out += ["", "## Находки", "",
            f"«проверить» — фрагмент из {CHECK_WORDS} слов или короче {CHECK_CHARS} символов: общая фраза "
            f"может совпасть случайно, решает человек ({n_check} из {len(rows)}).", "",
            "| Карточка | Раздел карточки | Страница | Задача | Фрагмент (первые 120 символов) | Строка карточки | Проверить |",
            "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda x: (x.card, x.line)):
        frag = r.fragment[:120] + ("…" if len(r.fragment) > 120 else "")
        out.append(f"| {esc(r.card)} | {esc(r.section) or '—'} | {r.page} | {r.task} | {esc(frag)}"
                   + (f" ×{r.count}" if r.count > 1 else "") + f" | {r.line} | "
                   + ("проверить" if r.check else "") + " |")
    return out


def _introduced_from_git(raw_root: Path) -> Set[str]:
    """Введённые задачи — теги src/<ID> с HEAD; правило бандла — apply_history.introduced_tasks."""
    root = next((d for d in [raw_root.resolve(), *raw_root.resolve().parents]
                 if (d / ".git").exists()), None)
    if root is None:
        return set()
    try:
        from app.scripts.apply_history import introduced_tasks
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from app.scripts.apply_history import introduced_tasks  # type: ignore
    return set(introduced_tasks(root, "src/"))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import time
    ap = argparse.ArgumentParser(description="Сверка по архиву raw: удалённое введёнными задачами "
                                             "введённых задач в карточках docs/ (отчёт markdown)")
    ap.add_argument("--cards", type=Path, required=True, help="каталог docs/ комплекта")
    ap.add_argument("--raw", type=Path, required=True, help="архив выгрузки sources/raw")
    ap.add_argument("--introduced", type=Path, default=None,
                    help="файл с ID введённых задач (по строке); без ключа — теги src/* с HEAD")
    ap.add_argument("--skip-nested-cells", action="store_true",
                    help="не проверять ячейки вложенных таблиц HTML-островов (по умолчанию в режиме "
                         "карточек проверяются)")
    ap.add_argument("--out", type=Path, required=True, help="файл отчёта (markdown)")
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t0 = time.time()
    if a.introduced is not None:
        ids = {ln.strip() for ln in a.introduced.read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.strip().startswith("#")}
    else:
        ids = _introduced_from_git(a.raw)
    rows, stats = check_cards(a.raw, a.cards, ids, not a.skip_nested_cells)
    lines = cards_report(rows, stats, ids, a.cards, a.raw)
    lines += ["", f"Время прогона: {time.time() - t0:.1f} с."]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"находок {stats['findings']} в {stats['cards']} карточках; отчёт: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
