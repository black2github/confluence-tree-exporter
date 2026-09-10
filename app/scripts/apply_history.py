# app/scripts/apply_history.py
#
# Автоматизация этапа 4 роадмапа (хронология): позадачное вливание истории
# в src-репозиторий (2026-08-10, по запросу пользователя).
#
# На каждую задачу из списка выполняется цикл роадмапа (с 2026-09-08 — на
# накопительном дереве «архив + принятые задачи», которое живёт вне репозитория):
#   1) critic apply текущей задачи в накопительное дерево (один раз на задачу);
#   2) целевой каталог очищается и наполняется копией накопительного дерева;
#   3) critic reject-all — «хвост» непринятых задач исключается;
#   4) git commit среза + git tag <префикс><JIRA-ID>.
# Прежний способ (--refill-each: пересборка из архива и повторный apply ВСЕХ
# принятых на каждой итерации) даёт побайтно те же срезы — проверено на всех
# 148 срезах дерева [КК], — но работы у него квадратично: 11 026 проходов apply
# против 148. Оставлен ключом для сверки.
#
# Автоматизируется «режим без ревью» (коммит прямо в текущую ветку);
# режим с MR-ревью по природе ручной. Push НЕ выполняется без явного --push.
#
# Модель веток (решение владельца 2026-09-10): основная ветка репозитория
# сервиса = ПРОМ; коммит скрипта = ВВОД задачи в эксплуатацию, тег
# <префикс><JIRA-ID> — момент ввода. Введённые РАНЕЕ задачи скрипт берёт сам
# из тегов, достижимых с HEAD, в порядке коммитов ветки (реестр введённых =
# теги); файл задач содержит только ВВОДИМЫЕ. Порядок ввода произвольный:
# состояние ветки — множество введённых задач. Хронология «все задачи по
# порядку списка» — архивный режим по запросу (--commit-prefix "Срез
# хронологии") на репозитории без тегов задач; ветка future — снимок apply-all.
# Служебные файлы экспортёра/доводки (migration-*) — часть архива raw/, в
# целевой каталог требований НЕ копируются (инцидент: попадали в опись как
# «страницы без page_id» и «приложения»).
#
# Цепочка событий (решение владельца 2026-09-10): ветка — последовательность
# событий двух видов, «выгрузка» и «задача». Перед нарезкой новых задач скрипт
# считает состояние «архив + все введённые ранее» и, если оно отличается от
# вершины ветки (архив обновлён новой выгрузкой), фиксирует его отдельным
# коммитом «Выгрузка: …» без тега. Иначе первая новая задача молча получила
# бы дифф с механической разницей выгрузок. Старые теги не перестраиваются:
# тег задачи показывает её вклад на момент своей выгрузки, дополнения
# разметки видны в коммите выгрузки. Хронология на ветке future — тот же
# скрипт с --tag-prefix hist/ и --commit-prefix "Срез хронологии".
#
# Хвост после запуска — задачи манифеста архива (migration-manifest.yaml),
# ещё не введённые; critic list здесь бесполезен: после reject-all маркеров в
# целевом каталоге нет по определению.
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
#   • тег вводимой задачи уже есть на ветке — задача пропускается с
#     предупреждением (уже введена); тег есть, но вне ветки — стоп;
#   • ошибка critic/git на любом шаге — стоп с ненулевым кодом (уже созданные
#     срезы остаются в истории; повторный запуск с тем же списком продолжит
#     с места остановки — введённые задачи он увидит по тегам и пропустит).
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
# репозитория хронологии, и пакет app должен находиться независимо от cwd.
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


def introduced_tasks(repo: Path, tag_prefix: str) -> List[str]:
    """Задачи, уже введённые на текущей ветке: теги <префикс><JIRA-ID>,
    достижимые с HEAD, в порядке коммитов (первый ввод — первым). Теги без
    JIRA-ID в имени (например src/PROM) и теги вне ветки не считаются."""
    r = _git(repo, "for-each-ref", "--format=%(refname:short) %(*objectname)%(objectname)",
             f"refs/tags/{tag_prefix}")
    if r.returncode != 0:
        return []
    by_commit = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, obj = parts
        suffix = name[len(tag_prefix):]
        if not TASK_ID_RE.fullmatch(suffix):
            continue
        # %(*objectname) — коммит аннотированного тега, иначе пусто и остаётся
        # objectname лёгкого тега; для лёгкого тега obj = коммит.
        commit = obj[-40:]
        by_commit.setdefault(commit, []).append(suffix)
    if not by_commit:
        return []
    order = _git(repo, "rev-list", "--reverse", "--first-parent", "HEAD").stdout.split()
    out: List[str] = []
    for c in order:
        out.extend(sorted(by_commit.get(c, [])))
    return out


def preflight(repo: Path, raw: Path, target: Path,
              tag_prefix: str, ids: List[str]) -> List[str]:
    """Проверки до первого изменения. Возвращает список ошибок (пусто = можно).
    `ids` — только вводимые задачи (введённые ранее уже отфильтрованы)."""
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
            errors.append(f"тег уже существует вне текущей ветки: {tag_prefix}{tid} — "
                          f"теги не перезаписываются (задача введена в другой ветке?)")
    return errors


# Служебные файлы экспортёра/доводки в архиве: не требования, в целевой
# каталог не переносятся (в накопительное дерево — переносятся: критику там
# доступен манифест).
SERVICE_FILE_PATTERNS = ("migration-*",)


def refill_target(raw: Path, target: Path) -> None:
    """Очистить целевой каталог и заново наполнить копией архива без
    служебных файлов (SERVICE_FILE_PATTERNS)."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(raw, target,
                    ignore=shutil.ignore_patterns(*SERVICE_FILE_PATTERNS))


DEFAULT_COMMIT_PREFIX = "Ввод в эксплуатацию"
REFRESH_PREFIX = "Выгрузка"

_UNAPPROVED_RE = re.compile(r"^unapproved_jira:\s*['\"]?([\w-]+)['\"]?", re.M)
_MANIFEST_TASK_RE = re.compile(r"^  ([A-Z][A-Z0-9]{1,19}-\d+):\s*$", re.M)


def empty_reason(raw: Path, tid: str, introduced: Optional[List[str]] = None) -> str:
    """Почему ввод задачи не изменил файлы: её страницы (по архиву raw — там
    маркеры задачи ещё на месте) под флагом ДРУГОЙ, ещё не введённой задачи:
    страница целиком не в ПРОМ, reject-all опустошает её, вклад задачи проявится
    с вводом владельца страницы. Пусто = причина иная."""
    owners: dict = {}
    skip = set(introduced or ())
    for f in raw.rglob("*.md"):
        if f.name.startswith("migration-"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if tid not in text:
            continue
        m = _UNAPPROVED_RE.search(text)
        if m and m.group(1) != tid and m.group(1) not in skip:
            owners[m.group(1)] = owners.get(m.group(1), 0) + 1
    if not owners:
        return ""
    parts = ", ".join(f"{k} ({v} стр.)" for k, v in sorted(owners.items()))
    return f"страницы задачи под флагом ещё не введённой задачи: {parts}"


def manifest_tasks(raw: Path) -> List[str]:
    """Задачи из migration-manifest.yaml архива (порядок файла); [] если нет."""
    mf = raw / "migration-manifest.yaml"
    if not mf.is_file():
        return []
    text = mf.read_text(encoding="utf-8", errors="replace")
    head = text.split("\ntasks:", 1)
    if len(head) < 2:
        return []
    return _MANIFEST_TASK_RE.findall(head[1])


def _commit_target(repo: Path, rel_target: str, msg: str) -> Tuple[bool, str, bool]:
    """git add целевого каталога и коммит, если есть изменения.
    Возвращает (ok, сообщение, был_ли_коммит)."""
    _git(repo, "add", "--", rel_target)
    if _git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return True, "", False
    r = _git(repo, "commit", "-m", msg)
    if r.returncode != 0:
        return False, f"git commit: {r.stderr[-500:] or r.stdout[-500:]}", False
    return True, msg, True


def refresh_commit(repo: Path, source: Path, prior: List[str], target: Path,
                   rel_target: str, source_is_base: bool) -> Tuple[bool, str, bool]:
    """Событие «выгрузка»: состояние «архив + введённые ранее» с reject-all.
    source_is_base — source уже содержит применённые prior (накопительное
    дерево); иначе source = архив и prior применяются здесь.
    (ok, сообщение, был_ли_коммит)."""
    refill_target(source, target)
    if not source_is_base:
        for tid in prior:
            r = _critic(repo, "apply", tid, "--path", rel_target)
            if r.returncode != 0:
                return False, f"critic apply {tid} (введена ранее): код {r.returncode}\n{r.stderr[-500:]}", False
    r = _critic(repo, "reject-all", "--path", rel_target)
    if r.returncode != 0:
        return False, f"critic reject-all: код {r.returncode}\n{r.stderr[-500:]}", False
    msg = (f"{REFRESH_PREFIX}: архив обновлён, состояние пересчитано "
           f"(ранее принятых: {len(prior)})")
    return _commit_target(repo, rel_target, msg)


def commit_message(prefix: str, current: str, n_prev: int) -> str:
    """Сообщение коммита среза: префикс задаёт смысл (ввод / архивная хронология),
    хвост одинаков — задача и число ранее применённых поверх ПРОМ."""
    return f"{prefix}: {current} (apply поверх ПРОМ + {n_prev} ранее принятых)"


def apply_one(repo: Path, raw: Path, target: Path, applied: List[str],
              tag_prefix: str, rel_target: str,
              commit_prefix: str = DEFAULT_COMMIT_PREFIX) -> Tuple[bool, str]:
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
    msg = commit_message(commit_prefix, current, len(applied) - 1)
    commit_args = ["commit", "-m", msg]
    note = ""
    if empty:
        commit_args.append("--allow-empty")
        why = empty_reason(raw, current, applied)
        note = " [пустой срез: задача не изменила файлы" + (f"; {why}" if why else "") + "]"
    r = _git(repo, *commit_args)
    if r.returncode != 0:
        return False, f"git commit: {r.stderr[-500:] or r.stdout[-500:]}"
    r = _git(repo, "tag", f"{tag_prefix}{current}")
    if r.returncode != 0:
        return False, f"git tag: {r.stderr[-500:]}"
    return True, f"срез {current}: коммит + тег {tag_prefix}{current}{note}"


def apply_one_accumulated(repo: Path, base: Path, target: Path, applied: List[str],
                          tag_prefix: str, rel_target: str,
                          commit_prefix: str = DEFAULT_COMMIT_PREFIX,
                          raw: Optional[Path] = None) -> Tuple[bool, str]:
    """Цикл для ОДНОЙ задачи на накопительном дереве (2026-09-07).

    Отличие от apply_one: `base` — дерево «архив + все принятые задачи», которое
    живёт между итерациями ВНЕ репозитория. Новая задача применяется в него один
    раз; срез — копия base с reject-all. Результат тот же, что у пересборки из
    архива с повторным apply всех принятых (apply правит только участки своей
    задачи, порядок применения на итог не влияет), а работы линейно, а не
    квадратично: на 148 задачах — 148 проходов apply вместо 11 026.
    """
    current = applied[-1]
    r = _critic(repo, "apply", current, "--path", str(base))
    if r.returncode != 0:
        return False, f"critic apply {current}: код {r.returncode}\n{r.stderr[-500:]}"
    refill_target(base, target)
    r = _critic(repo, "reject-all", "--path", rel_target)
    if r.returncode != 0:
        return False, f"critic reject-all: код {r.returncode}\n{r.stderr[-500:]}"

    _git(repo, "add", "--", rel_target)
    empty = _git(repo, "diff", "--cached", "--quiet").returncode == 0
    msg = commit_message(commit_prefix, current, len(applied) - 1)
    commit_args = ["commit", "-m", msg]
    note = ""
    if empty:
        commit_args.append("--allow-empty")
        why = empty_reason(raw if raw is not None else base, current, applied)
        note = " [пустой срез: задача не изменила файлы" + (f"; {why}" if why else "") + "]"
    r = _git(repo, *commit_args)
    if r.returncode != 0:
        return False, f"git commit: {r.stderr[-500:] or r.stdout[-500:]}"
    r = _git(repo, "tag", f"{tag_prefix}{current}")
    if r.returncode != 0:
        return False, f"git tag: {r.stderr[-500:]}"
    return True, f"срез {current}: коммит + тег {tag_prefix}{current}{note}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Этап 4 роадмапа: ввод задач в эксплуатацию в src-репозиторий "
                    "(режим без ревью: коммит в текущую ветку + тег на ввод; "
                    "введённые ранее задачи берутся из тегов ветки).")
    ap.add_argument("raw", type=Path,
                    help="каталог архива (нетронутая выгрузка, sources/raw)")
    ap.add_argument("repo", type=Path,
                    help="корень git-репозитория хронологии (src-<сервис>)")
    ap.add_argument("tasks", type=Path, nargs="?", default=None,
                    help="файл со списком ВВОДИМЫХ задач по порядку: голые JIRA-ID "
                         "построчно или блок команд из migration-apply-order.md "
                         "(введённые ранее перечислять не нужно — они видны по тегам). "
                         "Без файла — только событие «выгрузка»: состояние «архив + "
                         "введённые ранее» пересчитывается и фиксируется коммитом")
    ap.add_argument("--target-subdir", default="sources/confluence",
                    help="целевой каталог вливания внутри репозитория "
                         "(по умолчанию sources/confluence)")
    ap.add_argument("--tag-prefix", default="src/",
                    help="префикс тегов срезов (по умолчанию src/)")
    ap.add_argument("--refill-each", action="store_true",
                    help="прежний способ: на каждую задачу пересобирать дерево из архива "
                         "и заново применять все принятые (квадратично; результат тот же — "
                         "доказано побайтной сверкой 148 срезов; оставлен для сверки)")
    ap.add_argument("--base-dir", type=Path, default=None,
                    help="каталог для накопительного дерева (по умолчанию %%TEMP%%; в "
                         "контуре укажите каталог вне проверки антивируса и ВНЕ репозитория)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать план (задачи по порядку) и выйти без изменений")
    ap.add_argument("--push", action="store_true",
                    help="в конце: git push + push тегов (по умолчанию НЕ пушится)")
    ap.add_argument("--commit-prefix", default=DEFAULT_COMMIT_PREFIX,
                    help="префикс сообщения коммита: по умолчанию «Ввод в "
                         "эксплуатацию» (master = ПРОМ, коммит = ввод задачи); "
                         "для хронологии на ветке future — «Срез хронологии» "
                         "(вместе с --tag-prefix hist/)")
    args = ap.parse_args(argv)

    try:
        from app.version import banner
        print(f"# {banner('apply-history')}", file=sys.stderr)
    except ImportError:
        pass

    if args.tasks is None:
        ids, warnings = [], []
        print("# файл задач не задан: только событие «выгрузка» (без вводов)", file=sys.stderr)
    else:
        ids, warnings = read_task_list(args.tasks)
        for w in warnings:
            print(f"# ⚠ {w}", file=sys.stderr)
        if not ids:
            print("# ОШИБКА: в файле задач не найдено ни одного JIRA-ID.", file=sys.stderr)
            return 2

    repo = args.repo
    target = repo / args.target_subdir

    # Введённые ранее — по тегам ветки; задачи из файла, уже введённые, — пропуск.
    prior = introduced_tasks(repo, args.tag_prefix)
    print(f"# введено ранее (теги {args.tag_prefix}* на ветке): {len(prior)}"
          f"{': ' + ', '.join(prior[-8:]) if prior else ''}"
          f"{' (последние 8)' if len(prior) > 8 else ''}", file=sys.stderr)
    already = [t for t in ids if t in prior]
    for t in already:
        print(f"# ⚠ {t} уже введена (тег {args.tag_prefix}{t}) — пропущена",
              file=sys.stderr)
    ids = [t for t in ids if t not in prior]
    refresh_only = not ids
    if refresh_only:
        print("# задач к вводу нет" + (": все перечисленные уже введены" if already else "")
              + " — только событие «выгрузка».", file=sys.stderr)
    else:
        print(f"# к вводу: {len(ids)}: {', '.join(ids[:8])}"
              f"{' …' if len(ids) > 8 else ''}", file=sys.stderr)
    errors = preflight(repo, args.raw, target, args.tag_prefix, ids)
    if args.dry_run:
        for i, tid in enumerate(ids, 1):
            print(f"#   {i}. {tid} -> commit + tag {args.tag_prefix}{tid} "
                  f"(поверх ПРОМ + {len(prior) + i - 1} ранее принятых)",
                  file=sys.stderr)
        for e in errors:
            print(f"# ОШИБКА (preflight): {e}", file=sys.stderr)
        print("# dry-run: изменений не внесено.", file=sys.stderr)
        return 2 if errors else 0
    if errors:
        for e in errors:
            print(f"# ОШИБКА: {e}", file=sys.stderr)
        return 2

    # Накопительное дерево (по умолчанию с 2026-09-08): «архив + принятые задачи»
    # живёт ВНЕ репозитория — рабочее дерево хронологии обязано оставаться чистым
    # между срезами. Каждая задача применяется в него один раз; срез — копия с
    # reject-all. Эквивалентность прежнему способу доказана побайтной сверкой всех
    # 148 срезов дерева [КК]; работы при этом линейно, а не квадратично.
    base: Optional[Path] = None
    base_root: Optional[Path] = None
    if not args.refill_each:
        if args.base_dir is not None:
            base_root = args.base_dir.resolve() / "onix-history-base"
            try:
                base_root.relative_to(repo.resolve())
                print("# ОШИБКА: --base-dir внутри репозитория — дерево грязнило бы хронология",
                      file=sys.stderr)
                return 2
            except ValueError:
                pass
            if base_root.exists():
                shutil.rmtree(base_root)
            base_root.mkdir(parents=True)
        else:
            import tempfile
            base_root = Path(tempfile.mkdtemp(prefix="onix-history-"))
        base = base_root / "base"
        shutil.copytree(args.raw, base)
        print(f"# накопительное дерево: {base}", file=sys.stderr)
        for tid in prior:                    # введённые ранее — в дерево, без коммитов
            r = _critic(repo, "apply", tid, "--path", str(base))
            if r.returncode != 0:
                print(f"# ОШИБКА critic apply {tid} (введена ранее): код {r.returncode}\n"
                      f"{r.stderr[-500:]}", file=sys.stderr)
                shutil.rmtree(base_root, ignore_errors=True)
                return 1

    # Событие «выгрузка»: если «архив + введённые ранее» отличается от вершины
    # ветки — отдельный коммит без тега, чтобы дифф первой новой задачи был чистым.
    ok, message, committed = refresh_commit(
        repo, base if base is not None else args.raw, prior, target,
        args.target_subdir, source_is_base=base is not None)
    if not ok:
        print(f"# ОШИБКА: {message}", file=sys.stderr)
        if base_root is not None:
            shutil.rmtree(base_root, ignore_errors=True)
        return 1
    if committed:
        print(f"# выгрузка: {message}", file=sys.stderr)
    if refresh_only:
        if base_root is not None:
            shutil.rmtree(base_root, ignore_errors=True)
        if not committed:
            print("# состояние не изменилось: архив тот же, вводов нет — делать нечего.",
                  file=sys.stderr)
            return 2
        print("# готово: выгрузка зафиксирована. Отправка (вручную): git push", file=sys.stderr)
        return 0

    applied: List[str] = list(prior)
    for i, tid in enumerate(ids, 1):
        applied.append(tid)
        if base is not None:
            ok, message = apply_one_accumulated(repo, base, target, applied,
                                                args.tag_prefix, args.target_subdir,
                                                args.commit_prefix, raw=args.raw)
        else:
            ok, message = apply_one(repo, args.raw, target, applied,
                                    args.tag_prefix, args.target_subdir,
                                    args.commit_prefix)
        status = "✓" if ok else "✗"
        print(f"# [{i}/{len(ids)}] {status} {message}", file=sys.stderr)
        if not ok:
            print("# ОСТАНОВ: вводы до этой задачи уже в истории; после исправления "
                  "запустите снова с тем же списком — введённые задачи скрипт увидит "
                  "по тегам и пропустит (удаляйте теги ТОЛЬКО если вводы нужно "
                  "переделать).", file=sys.stderr)
            if base_root is not None:
                shutil.rmtree(base_root, ignore_errors=True)
            return 1

    if base_root is not None:
        shutil.rmtree(base_root, ignore_errors=True)     # накопительное дерево больше не нужно

    # финальный хвост: задачи манифеста архива, ещё не введённые (critic list
    # после reject-all всегда пуст — маркеров в целевом каталоге нет)
    manifest = manifest_tasks(args.raw)
    if manifest:
        rest = [t for t in manifest if t not in applied]
        print(f"# --- не введено (по манифесту архива): {len(rest)} из {len(manifest)} ---",
              file=sys.stderr)
        print(("#   " + ", ".join(rest[:20]) + (" …" if len(rest) > 20 else ""))
              if rest else "# (пусто: все задачи манифеста введены)", file=sys.stderr)
    else:
        r = _critic(repo, "list", "--path", args.target_subdir)
        tail = (r.stdout or "").strip()
        print("# --- манифеста в архиве нет; critic list по целевому каталогу ---",
              file=sys.stderr)
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
