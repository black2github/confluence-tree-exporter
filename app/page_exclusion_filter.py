# app/page_exclusion_filter.py

import json
import logging
import re
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).parent          # app/
_PROJECT_ROOT = _MODULE_DIR.parent           # project root

_cached_rules: Dict | None = None
_cached_rules_file: str | None = None


def _resolve_rules_path(rules_file: str) -> Path:
    """Если путь относительный — резолвит его от корня проекта."""
    p = Path(rules_file)
    if p.is_absolute():
        return p
    candidate = _PROJECT_ROOT / rules_file
    if candidate.exists():
        return candidate
    return p  # вернём как есть; exists() вернёт False и будет залогировано


def load_exclusion_rules(rules_file: str) -> Dict:
    """
    Загружает правила исключения страниц из JSON-файла и кешируeт их в памяти.

    Формат файла:
    {
      "prefixes": ["удалено", "draft", ...],   // строки, переводятся в lower() при загрузке
      "patterns": ["^\\[.*?\\]\\s*архив$", ...]  // regex-шаблоны, применяются без учёта регистра
    }

    Returns:
        {"prefixes": List[str], "compiled_patterns": List[re.Pattern]}
    """
    global _cached_rules, _cached_rules_file

    if _cached_rules is not None and _cached_rules_file == rules_file:
        return _cached_rules

    path = _resolve_rules_path(rules_file)
    if not path.exists():
        logger.warning("[load_exclusion_rules] Rules file not found: %s. No pages will be excluded.", rules_file)
        _cached_rules = {"prefixes": [], "compiled_patterns": []}
        _cached_rules_file = rules_file
        return _cached_rules

    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error("[load_exclusion_rules] Failed to load rules file %s: %s", rules_file, e)
        _cached_rules = {"prefixes": [], "compiled_patterns": []}
        _cached_rules_file = rules_file
        return _cached_rules

    prefixes = [p.strip().lower() for p in raw.get("prefixes", []) if p.strip()]

    compiled_patterns: List[re.Pattern] = []
    for pat in raw.get("patterns", []):
        try:
            compiled_patterns.append(re.compile(pat, re.IGNORECASE))
        except re.error as e:
            logger.warning("[load_exclusion_rules] Invalid pattern %r: %s", pat, e)

    _cached_rules = {"prefixes": prefixes, "compiled_patterns": compiled_patterns}
    _cached_rules_file = rules_file

    logger.debug(
        "[load_exclusion_rules] Loaded %d prefixes and %d patterns from %s",
        len(prefixes), len(compiled_patterns), rules_file
    )
    return _cached_rules


def is_page_excluded(title: str, rules: Dict) -> bool:
    """
    Проверяет, должна ли страница с указанным заголовком быть исключена из обхода.

    Args:
        title: Заголовок страницы Confluence.
        rules: Словарь правил, полученный из load_exclusion_rules().

    Returns:
        True если страницу нужно проигнорировать, False иначе.
    """
    normalized = title.strip().lower()

    for prefix in rules["prefixes"]:
        if normalized.startswith(prefix):
            return True

    for pattern in rules["compiled_patterns"]:
        if pattern.search(normalized):
            return True

    return False
