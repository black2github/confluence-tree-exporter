# app/prom_invariant.py
"""Сторож «ПРОМ не потерян» для критик-выгрузки (флаг --check-prom, 2026-10-23).

Зачем. Четыре дефекта разметки октября (1.9.7–1.9.9: зачёркивание снаружи цвета,
пункты списков в островах, частичное перекрытие чёрным, ведущий пробел) три месяца
жили незамеченными по одной причине: потеря молчаливая. Ни один путь не падает —
текст просто исчезает из среза ПРОМ (reject-all) или склеивается при apply. Тесты
проверяли расстановку маркеров, а не сохранность текста. Этот модуль проверяет
именно сохранность, независимо от того, каким путём ставился маркер.

Инвариант. Пусть S_prom — слова источника, которые НЕ являются цветной
незачёркнутой правкой (чёрный текст и чёрное/цветное зачёркнутое), S_apply — слова
источника без зачёркнутого. Тогда:
  слова(reject-all(md)) == S_prom      — ПРОМ не потерян и вставки сняты;
  слова(apply-all(md))  ⊇ S_apply      — вставки не потеряны.
Сравнение по мультимножествам слов (буквы/цифры), без учёта порядка и разметки:
markdown и HTML-острова нормализуются к словам одинаково.

Границы слов. Источник режется на прогоны по состоянию (чёрное / вставка /
удаление), граница блока и пробельный узел дают пробел. Markdown склеивает
отдельные текстовые узлы («сч»+«ё»+«т» → «счёт»), поэтому расхождение, у которого
потерянное слово входит подстрокой в «лишнее» (или наоборот), считается границей
слов, а не потерей, и в нарушение не попадает — это информационный счётчик.

Известные исключения модели (НЕ нарушения, учитываются вызывающим):
  • пост-проход ТЗ п. 4.5.4 отбрасывает {--X--}, когда X входит в чужую вставку, —
    слова таких X передаются в dropped_texts и вычитаются из ожидания;
  • уплощение вложенности (ТЗ п. 4.5) и форс-обёртка неутверждённого состава
    оборачивают чёрный текст намеренно — страницы с форсом сторож не проверяет,
    уплощения остаются в отчёте позициями ручного разбора и могут дать сигнал.

Ограничение. Сторож словарный: он видит пропажу и лишнее слово, но не порядок и
не пунктуацию. Этого достаточно против класса «фрагмент ушёл не в тот маркер».
"""

from __future__ import annotations

import html as _html
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.history_cleaner import remove_history_sections

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")

# Граница блока в источнике = пробел между прогонами (иначе «Автоматически</td><td>Если»
# склеивается в одно слово, которого в markdown нет).
_BLOCK_TAGS = frozenset({
    "p", "div", "li", "td", "th", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4",
    "h5", "h6", "br", "pre", "blockquote", "section", "hr", "thead", "tbody",
})

# Нормализация markdown к словам: маркеры критика, HTML-теги островов (только
# настоящие: латинское имя тега; <Номер заявки> — текст), картинки, адреса ссылок,
# служебный столбец status и его ячейки ±ID.
_MD_MARK_OPEN_RE = re.compile(r"\{(?:\+\+|--)[^:{}]*:\s*")
_MD_MARK_CLOSE_RE = re.compile(r"(?:\+\+|--)\}")
_MD_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_MD_STATUS_CELL_RE = re.compile(r"\|\s*(?:status|[+\-±][A-Za-z0-9_-]+)\s*(?=\|)")


def words(text: str) -> Counter:
    """Мультимножество слов (без регистра)."""
    return Counter(w.lower() for w in _WORD_RE.findall(text))


def md_plain(md: str) -> str:
    """Markdown критик-выгрузки → текст без разметки (для подсчёта слов)."""
    s = _MD_MARK_OPEN_RE.sub("", md)
    s = _MD_MARK_CLOSE_RE.sub("", s)
    s = _MD_IMAGE_RE.sub(" ", s)
    s = _MD_LINK_TARGET_RE.sub("]", s)
    s = _MD_STATUS_CELL_RE.sub("|", s)
    s = _MD_TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    return s.replace(" ", " ")


def _effective_state(node: NavigableString, extractor) -> str:
    """Состояние текстового узла источника: 'black' | 'ins' | 'del'.

    Цвет — ближайший собственный цвет предка (CSS), зачёркнутость — любой предок
    <s>/<del>/<strike>/line-through. Правка — по _is_edit_color экстрактора (та же
    палитра чёрных и та же карта задач, что у разметки)."""
    color: Optional[str] = None
    struck = False
    cur = node.parent
    while isinstance(cur, Tag):
        if color is None:
            own = extractor._element_own_color(cur)
            if own:
                color = own
        if (cur.name in ("s", "del", "strike")
                or "line-through" in (cur.get("style") or "").lower()):
            struck = True
        cur = cur.parent
    if not extractor._is_edit_color(color):
        return "black"
    return "del" if struck else "ins"


def source_word_sets(raw_html: str, extractor) -> Tuple[Counter, Counter]:
    """(S_prom, S_apply) по исходному HTML страницы.

    Источник проходит ту же подготовку, что перед экстракцией: удаление истории,
    макросы Confluence, пустые абзацы — чтобы сравнивать с тем, что экстрактор
    действительно видел."""
    soup = BeautifulSoup(remove_history_sections(raw_html or ""), "html.parser")
    extractor._process_confluence_macros(soup)
    extractor._remove_empty_paragraphs(soup)

    runs: List[Tuple[str, str]] = []   # (state, text); соседние прогоны одного состояния склеиваются

    def separator() -> None:
        if runs:
            runs[-1] = (runs[-1][0], runs[-1][1] + " ")

    for node in soup.descendants:
        if isinstance(node, Tag):
            if node.name in _BLOCK_TAGS:
                separator()
            continue
        if isinstance(node, Comment) or not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is not None and parent.name in ("script", "style"):
            continue
        text = str(node)
        if not text.strip():
            separator()          # пробельный узел между инлайнами — разделитель
            continue
        state = _effective_state(node, extractor)
        if runs and runs[-1][0] == state:
            runs[-1] = (state, runs[-1][1] + text)
        else:
            runs.append((state, text))

    prom: Counter = Counter()
    applied: Counter = Counter()
    for state, text in runs:
        w = words(text)
        if state == "black":
            prom += w
            applied += w
        elif state == "del":
            prom += w
        else:
            applied += w
    return prom, applied


def _split_boundary(diff: Counter, other: Iterable[str]) -> Tuple[Counter, Counter]:
    """Делит расхождение на настоящее и «граница слов»: слово, входящее подстрокой
    в слово противоположной стороны (или содержащее его), — склейка/разрыв."""
    other_words = [o for o in other]
    real: Counter = Counter()
    boundary: Counter = Counter()
    for w, n in diff.items():
        if any(w != o and (w in o or o in w) for o in other_words):
            boundary[w] = n
        else:
            real[w] = n
    return real, boundary


@dataclass
class PromCheck:
    """Результат проверки одной страницы."""
    lost_reject: Counter = field(default_factory=Counter)    # ПРОМ источника, нет после reject
    leaked_reject: Counter = field(default_factory=Counter)  # после reject есть, в ПРОМ источника нет
    lost_apply: Counter = field(default_factory=Counter)     # вставка источника, нет после apply
    boundary_reject: int = 0                                 # только границы слов (информационно)
    boundary_apply: int = 0
    words_prom: int = 0
    words_reject: int = 0

    @property
    def ok(self) -> bool:
        return not (self.lost_reject or self.leaked_reject or self.lost_apply)

    @staticmethod
    def _top(cnt: Counter, limit: int = 20) -> str:
        return ", ".join(f"{w}×{n}" if n > 1 else w for w, n in cnt.most_common(limit))

    def summary(self) -> str:
        parts = []
        if self.lost_reject:
            parts.append(f"потеряно из ПРОМ {sum(self.lost_reject.values())} слов "
                         f"({self._top(self.lost_reject)})")
        if self.leaked_reject:
            parts.append(f"лишнее в ПРОМ {sum(self.leaked_reject.values())} слов "
                         f"({self._top(self.leaked_reject)})")
        if self.lost_apply:
            parts.append(f"потеряно при apply {sum(self.lost_apply.values())} слов "
                         f"({self._top(self.lost_apply)})")
        return "; ".join(parts) if parts else "расхождений нет"

    def as_report(self) -> Dict:
        return {
            "lost_reject": self.lost_reject.most_common(),
            "leaked_reject": self.leaked_reject.most_common(),
            "lost_apply": self.lost_apply.most_common(),
            "boundary_reject": self.boundary_reject,
            "boundary_apply": self.boundary_apply,
            "words_prom": self.words_prom,
            "words_reject": self.words_reject,
        }


def check_prom_invariant(raw_html: str, content_md: str, extractor,
                         dropped_texts: Iterable[str] = ()) -> PromCheck:
    """Проверяет инвариант для одной страницы.

    raw_html — исходный HTML (с историей: удаление — внутри), content_md — итоговый
    markdown критик-выгрузки (после пост-прохода), extractor — тот же критик-экстрактор
    (нужны его палитра чёрных и карта задач), dropped_texts — тексты удалений,
    отброшенных пост-проходом ТЗ п. 4.5.4 (ожидание уменьшается на их слова)."""
    from app.scripts.CI.critic import process_text_until_stable

    prom, applied = source_word_sets(raw_html, extractor)
    for text in dropped_texts:
        dropped = words(text)
        prom -= dropped                      # Counter: отрицательных не оставляет

    rejected = process_text_until_stable(content_md, "reject", None)[0]
    appliedmd = process_text_until_stable(content_md, "apply", None)[0]
    rw = words(md_plain(rejected))
    aw = words(md_plain(appliedmd))

    lost = prom - rw
    leaked = rw - prom
    lost_real, lost_b = _split_boundary(lost, leaked)
    leaked_real, leaked_b = _split_boundary(leaked, lost)
    lost_apply = applied - aw
    lost_apply_real, apply_b = _split_boundary(lost_apply, aw - applied)

    return PromCheck(
        lost_reject=lost_real,
        leaked_reject=leaked_real,
        lost_apply=lost_apply_real,
        boundary_reject=sum(lost_b.values()) + sum(leaked_b.values()),
        boundary_apply=sum(apply_b.values()),
        words_prom=sum(prom.values()),
        words_reject=sum(rw.values()),
    )
