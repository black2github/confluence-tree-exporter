# app/scripts/repair_export.py
#
# Разовая починка УЖЕ ВЫГРУЖЕННЫХ деревьев: правки экспортёра действуют начиная
# со следующей миграции, а сделанные ранее выгрузки остаются как есть.
#
# Две починки, обе включаются флагами и обе идемпотентны:
#
#   --unfold  Свёрнутые значения frontmatter → одной строкой. PyYAML сворачивал
#             длинное значение по ширине 80 символов (для кириллицы ~150 байт),
#             и построчные читатели видели обрезанный заголовок с незакрытой
#             кавычкой: разные страницы выглядели дублями (инцидент 2026-08-23).
#             YAML при этом валиден, поэтому чиним НЕ переразбором файла, а
#             склейкой строк — прочие байты не трогаем.
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

from app.scripts.CI.critic import TASK_ID_PATTERN, UNAPPROVED_PAGE_KEY

# Маска Jira ID для проверки списка неутверждённых (как в migrate_confluence_tree).
_TASK_ID_RE = re.compile(TASK_ID_PATTERN)

# Вставка CriticMarkup с идентификатором задачи.
_INS_RE = re.compile(r"\{\+\+\s*(" + TASK_ID_PATTERN + r")\s*:")

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


def marker_tasks(body: str) -> set:
    """Идентификаторы задач, чьи вставки есть в теле страницы."""
    return {m.group(1) for m in _INS_RE.finditer(body)}


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


def repair_file(path: Path, unfold: bool, unapproved: Optional[set]) -> Dict:
    """Починить один файл. Возвращает отчёт; ключ 'changed' — писать ли файл."""
    report: Dict = {"path": path, "unfolded": 0, "flagged": None,
                    "changed": False, "skipped": None, "new_text": None}
    with open(path, "r", encoding="utf-8", newline="") as f:
        original = f.read()

    parts = split_frontmatter(original)
    if parts is None:
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

    if new_fm == fm_body:
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
    report["new_text"] = head + new_fm + rest
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Починка уже выгруженных деревьев Confluence")
    parser.add_argument("root", help="корень выгрузки (каталог с .md) или один файл")
    parser.add_argument("--unfold", action="store_true",
                        help="склеить свёрнутые значения frontmatter в одну строку")
    parser.add_argument("--unapproved-jira", metavar="FILE",
                        help="JSON со списком неутверждённых задач: проставить "
                             "страничный флаг unapproved_jira")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что изменилось бы, ничего не записывая")
    args = parser.parse_args(argv)

    if not args.unfold and not args.unapproved_jira:
        parser.error("укажите хотя бы одну починку: --unfold и/или --unapproved-jira")

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
    changed = unfolded_total = flagged_total = 0
    skipped: List[Tuple[Path, str]] = []

    for path in files:
        rep = repair_file(path, args.unfold, unapproved)
        if rep["skipped"] and rep["skipped"] != "нет frontmatter":
            skipped.append((path, rep["skipped"]))
        if not rep["changed"]:
            continue
        changed += 1
        unfolded_total += rep["unfolded"]
        if rep["flagged"]:
            flagged_total += 1
        what = []
        if rep["unfolded"]:
            what.append("склеено строк: " + str(rep["unfolded"]))
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
          ", флагов проставлено " + str(flagged_total) +
          ", пропущено с предупреждением " + str(len(skipped)) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
