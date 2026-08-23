# app/scripts/apply_history.py
#
# Автоматизация этапа 4 роадмапа (летопись): позадачное вливание истории
# в src-репозиторий (2026-08-10, по запросу пользователя).
#
# На каждую задачу из списка выполняется цикл роадмапа:
#   1) целевой каталог очищается и заново наполняется копией архива (raw);
#   2) critic apply — для ВСЕХ задач накопительного списка (принятые ранее + текущая);
#   3) critic reject-all — «хвост» непринятых задач исключается;
#   4) git commit среза + git tag <префикс><JIRA-ID>.
#
# Автоматизируется «режим без ревью» (коммит прямо в текущую ветку летописи);
# режим с MR-ревью по природе ручной. Push НЕ выполняется без явного --push.
#
# Список задач — текстовый файл: понимает и голые JIRA-ID построчно, и блок
# команд из отчёта migration-apply-order.md («run-critic.bat apply ID --path .»);
# строки REM/# и пустые пропускаются. Дубли ID — предупреждение, берётся
# первое вхождение (порядок значим).
#
# Предохранители (асимметрия ошибок — лучше остановиться, чем испортить):
#   • рабочее дерево репозитория обязано быть чистым до старта;
#   • целевой каталог обязан лежать ВНУТРИ репозитория и не совпадать с корнем;
#   • архив (raw) обязан лежать ВНЕ целевого каталога;
#   • существующий тег — стоп (теги летописи не перезаписываются);
#   • ошибка critic/git на любом шаге — стоп с ненулевым кодом (уже созданные
#     срезы остаются в истории, продолжить можно с места остановки, убрав
#     принятые задачи из списка... точнее — оставив их в НАЧАЛЕ списка: список
#     накопительный, скрипт сам принимает все предыдущие задачи заново).
#
# Пустой срез (задача не изменила ни одного файла) фиксируется коммитом
# --allow-empty с пометкой: тег обязан существовать для трассировки.

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

TASK_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,19}-\d+\b")

# Корень пакета (каталог, содержащий app/) — сабпроцесс critic запускается из
# репозитория летописи, и пакет app должен находиться независимо от cwd.
_PKG_ROOT = Path(__file__).resolve().parents[2]


def read_task_list(path: Path) -> Tuple[List[str], List[str]]:
    """Список JIRA-ID из файла в порядке следования. Возвращает (ids, warnings)."""
    ids: List[str] = []
    warnings: List[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#") or s.upper().startswith("REM "):
            continue
        m = TASK_ID_RE.search(s)
        if not m:
            warnings.append(f"строка {line_no}: JIRA-ID не найден, пропущена: {s[:60]!r}")
            continue
        tid = m.group(0)
        if tid in ids:
            warnings.append(f"строка {line_no}: дубль {tid} — берётся первое вхождение")
            continue
        ids.append(tid)
    return ids, warnings


def _run(cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=repo)


def _critic(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """critic тем же интерпретатором, тем же пакетом app (бандл или канон)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_PKG_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "app.scripts.CI.critic", *args],
                          cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def preflight(repo: Path, raw: Path, target: Path,
              tag_prefix: str, ids: List[str]) -> List[str]:
    """Проверки до первого изменения. Возвращает список ошибок (пусто = можно)."""
    errors: List[str] = []
    if not raw.is_dir():
        errors.append(f"архив не найден: {raw}")
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        errors.append(f"не git-репозиторий: {repo}")
        return errors
    # target строго внутри репозитория и не корень (rmtree!)
    try:
        rel = target.resolve().relative_to(repo.resolve())
    except ValueError:
        errors.append(f"целевой каталог вне репозитория: {target}")
        return errors
    if str(rel) in (".", ""):
        errors.append("целевой каталог не может совпадать с корнем репозитория")
    try:
        raw.resolve().relative_to(target.resolve())
        errors.append("архив лежит внутри целевого каталога — он был бы удалён")
    except ValueError:
        pass
    st = _git(repo, "status", "--porcelain")
    if st.stdout.strip():
        errors.append("рабочее дерево репозитория не чисто — закоммитьте или уберите "
                      "изменения до старта:\n" + st.stdout.strip()[:500])
    for tid in ids:
        if _git(repo, "rev-parse", "--verify", "--quiet",
                f"refs/tags/{tag_prefix}{tid}").returncode == 0:
            errors.append(f"тег уже существует: {tag_prefix}{tid} — теги летописи "
                          f"не перезаписываются (задача уже влита?)")
    return errors


def refill_target(raw: Path, target: Path) -> None:
    """Очистить целевой каталог и заново наполнить копией архива."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(raw, target)


def apply_one(repo: Path, raw: Path, target: Path, applied: List[str],
              tag_prefix: str, rel_target: str) -> Tuple[bool, str]:
    """Цикл роадмапа для ОДНОЙ задачи (последней в applied). (ok, сообщение)."""
    current = applied[-1]
    refill_target(raw, target)
    for tid in applied:                       # накопительный список: все принятые
        r = _critic(repo, "apply", tid, "--path", rel_target)
        if r.returncode != 0:
            return False, f"critic apply {tid}: код {r.returncode}\n{r.stderr[-500:]}"
    r = _critic(repo, "reject-all", "--path", rel_target)
    if r.returncode != 0:
        return False, f"critic reject-all: код {r.returncode}\n{r.stderr[-500:]}"

    _git(repo, "add", "--", rel_target)
    empty = _git(repo, "diff", "--cached", "--quiet").returncode == 0
    msg = (f"Срез летописи: {current} (apply поверх ПРОМ + {len(applied) - 1} "
           f"ранее принятых)")
    commit_args = ["commit", "-m", msg]
    note = ""
    if empty:
        commit_args.append("--allow-empty")
        note = " [пустой срез: задача не изменила файлы]"
    r = _git(repo, *commit_args)
    if r.returncode != 0:
        return False, f"git commit: {r.stderr[-500:] or r.stdout[-500:]}"
    r = _git(repo, "tag", f"{tag_prefix}{current}")
    if r.returncode != 0:
        return False, f"git tag: {r.stderr[-500:]}"
    return True, f"срез {current}: коммит + тег {tag_prefix}{current}{note}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Этап 4 роадмапа: позадачное вливание истории в src-репозиторий "
                    "(режим без ревью: коммит в текущую ветку + тег на срез).")
    ap.add_argument("raw", type=Path,
                    help="каталог архива (нетронутая выгрузка, sources/raw)")
    ap.add_argument("repo", type=Path,
                    help="корень git-репозитория летописи (src-<сервис>)")
    ap.add_argument("tasks", type=Path,
                    help="файл со списком задач ПО ПОРЯДКУ: голые JIRA-ID построчно "
                         "или блок команд из migration-apply-order.md")
    ap.add_argument("--target-subdir", default="sources/confluence",
                    help="целевой каталог вливания внутри репозитория "
                         "(по умолчанию sources/confluence)")
    ap.add_argument("--tag-prefix", default="src/",
                    help="префикс тегов срезов (по умолчанию src/)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план (задачи по порядку) и выйти без изменений")
    ap.add_argument("--push", action="store_true",
                    help="в конце: git push + push тегов (по умолчанию НЕ пушится)")
    args = ap.parse_args(argv)

    try:
        from app.version import banner
        print(f"# {banner('apply-history')}", file=sys.stderr)
    except ImportError:
        pass

    ids, warnings = read_task_list(args.tasks)
    for w in warnings:
        print(f"# ⚠ {w}", file=sys.stderr)
    if not ids:
        print("# ОШИБКА: в файле задач не найдено ни одного JIRA-ID.", file=sys.stderr)
        return 2

    repo = args.repo
    target = repo / args.target_subdir
    print(f"# задач к вливанию: {len(ids)}: {', '.join(ids[:8])}"
          f"{' …' if len(ids) > 8 else ''}", file=sys.stderr)
    if args.dry_run:
        for i, tid in enumerate(ids, 1):
            print(f"#   {i}. {tid} -> commit + tag {args.tag_prefix}{tid}",
                  file=sys.stderr)
        print("# dry-run: изменений не внесено.", file=sys.stderr)
        return 0

    errors = preflight(repo, args.raw, target, args.tag_prefix, ids)
    if errors:
        for e in errors:
            print(f"# ОШИБКА: {e}", file=sys.stderr)
        return 2

    applied: List[str] = []
    for i, tid in enumerate(ids, 1):
        applied.append(tid)
        ok, message = apply_one(repo, args.raw, target, applied,
                                args.tag_prefix, args.target_subdir)
        status = "✓" if ok else "✗"
        print(f"# [{i}/{len(ids)}] {status} {message}", file=sys.stderr)
        if not ok:
            print("# ОСТАНОВ: срезы до этой задачи уже в истории; после исправления "
                  "запустите снова с тем же списком — существующие теги перечислит "
                  "preflight (уберите влитые задачи из НАЧАЛА списка нельзя — список "
                  "накопительный; удалите их теги ТОЛЬКО если срезы нужно переделать).",
                  file=sys.stderr)
            return 1

    # финальный хвост: что осталось непринятым в последнем срезе
    r = _critic(repo, "list", "--path", args.target_subdir)
    tail = (r.stdout or "").strip()
    print("# --- хвост (непринятые задачи в последнем срезе) ---", file=sys.stderr)
    print(tail if tail else "# (пусто)", file=sys.stderr)

    if args.push:
        for cmd in (["push"], ["push", "--tags"]):
            r = _git(repo, *cmd)
            if r.returncode != 0:
                print(f"# ОШИБКА git {' '.join(cmd)}: {r.stderr[-300:]}", file=sys.stderr)
                return 1
        print("# push выполнен (ветка + теги).", file=sys.stderr)
    else:
        print(f"# готово. Отправка на сервер (вручную): git push && git push --tags",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
