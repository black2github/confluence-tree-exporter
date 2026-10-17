# app/scripts/CI/kitcheck/fence.py
"""Границы блоков кода (fenced code blocks) — одно правило для прибора
самопроверки и для разметки CriticMarkup (`critic.py`, бандл
confluence-tree-exporter). Только стандартная библиотека, Python 3.8+,
без зависимостей — модуль едет в обе поставки: канон (`_meta/tools/kitcheck/`)
и бандл (`app/scripts/CI/kitcheck/`).

Правило (решение владельца 2026-10-17, исследование «Прибор — распознавание
блоков кода и ограждений», независимая оценка сессии «КК и хвосты»):

- Ограждение — строка целиком: ``` или ~~~ (3+) с любым отступом (блоки в
  пунктах списков законны), дальше инфо-строка. У ``` инфо-строка без «`»
  (CommonMark): склейка инлайн-кода выгрузки ```"filter"``: { и строка
  ```x``` — не ограждения. BOM и хвостовой \\r строки не мешают.
- Вложенность — по CommonMark: блок закрывает только ограждение того же вида
  и не короче открывающего; ``` внутри ````, ~~~ внутри ``` — содержимое
  блока (образцы карточек в шаблонах канона: ````markdown с ```mermaid
  внутри). Содержимое блока непрозрачно — стек уровней не нужен.
- Закрывающее ограждение с инфо-строкой (опечатка автора: ```text ... ```text)
  ЗАКРЫВАЕТ блок — иначе хвост файла молча ушёл бы в код, из проверок и из-под
  маркеров CriticMarkup; GitLab такую строку закрытием не считает (дальше в
  рендере всё код), поэтому она — находка `close-with-info`.
- Незакрытый блок — код до конца текста (как в рендере); если после
  открывающего ограждения есть непустой текст — находка `unclosed`. Голое
  ограждение последней строкой файла ничего не прячет — находки нет.
- Ограждение внутри цитаты («> ```») ограждением не считается (как и
  прежде); в карточке любая цитата — отдельный брак сторожа цитат.
"""
import re
from functools import lru_cache
from typing import List, NamedTuple, Optional, Sequence, Tuple

_FENCE_LINE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*?)\r?\n?$")


def fence_of(line: str) -> Optional[Tuple[str, int, str]]:
    """(вид «`» или «~», длина, инфо-строка без пробелов по краям) — если
    строка является ограждением; иначе None."""
    m = _FENCE_LINE_RE.match(line.lstrip("﻿"))
    if not m:
        return None
    run, info = m.group(1), m.group(2).strip()
    if run[0] == "`" and "`" in info:
        return None
    return run[0], len(run), info


class Block(NamedTuple):
    """Блок кода: индексы строк открывающего и закрывающего ограждения
    (у незакрытого `end` — последняя строка текста)."""
    start: int
    end: int
    closed: bool
    close_with_info: bool


@lru_cache(maxsize=512)
def _scan(lines: Tuple[str, ...]) -> Tuple[Block, ...]:
    out: List[Block] = []
    n = len(lines)
    k = 0
    while k < n:
        f = fence_of(lines[k])
        if f is None:
            k += 1
            continue
        kind, length, _info = f
        j = k + 1
        closed = with_info = False
        while j < n:
            g = fence_of(lines[j])
            if g is not None and g[0] == kind and g[1] >= length:
                closed, with_info = True, bool(g[2])
                break
            j += 1
        out.append(Block(k, j if closed else n - 1, closed, with_info))
        k = j + 1
    return tuple(out)


def blocks(lines: Sequence[str]) -> Tuple[Block, ...]:
    """Блоки кода строк текста (результат кэшируется по содержимому: прибор
    размечает один файл из многих мест)."""
    return _scan(tuple(lines))


def code_flags(lines: Sequence[str]) -> List[bool]:
    """По строке: True — ограждение или строка внутри блока кода."""
    flags = [False] * len(lines)
    for b in blocks(lines):
        for k in range(b.start, b.end + 1):
            flags[k] = True
    return flags


class FenceIssue(NamedTuple):
    """Находка: `line` — номер строки (с 1) открывающего ограждения;
    `kind` — 'close-with-info' (в GitLab блок не закрыт, дальше всё код) или
    'unclosed' (блок не закрыт до конца файла); `close_line` — номер строки
    закрывающего ограждения с инфо-строкой (у 'unclosed' — 0)."""
    line: int
    kind: str
    close_line: int


def issues(lines: Sequence[str]) -> List[FenceIssue]:
    """Находки разметки блоков: закрытие с инфо-строкой; незакрытый блок,
    после открывающего ограждения которого есть непустой текст."""
    out: List[FenceIssue] = []
    for b in blocks(lines):
        if b.closed and b.close_with_info:
            out.append(FenceIssue(b.start + 1, "close-with-info", b.end + 1))
        elif not b.closed and any(ln.strip() for ln in lines[b.start + 1:]):
            out.append(FenceIssue(b.start + 1, "unclosed", 0))
    return out
