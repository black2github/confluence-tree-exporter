# app/style_utils.py

import re
from typing import Optional
from bs4 import Tag

def has_colored_style(element: Tag) -> bool:
    """
    Проверяет, имеет ли элемент цветной стиль.
    Возвращает True, если имеет цвет, отличный от черного.
    """
    if not isinstance(element, Tag):
        return False

    style = element.get("style", "").lower()
    if not style or "color" not in style:
        return False

    color_match = re.search(r'color\s*:\s*([^;]+)', style)
    if not color_match:
        return False

    color_value = color_match.group(1).strip()

    is_black = is_black_color(color_value)

    return not is_black  # True если НЕ черный (т.е. цветной)


# Набор стандартных комбинаций цветов редактора Confluence, воспринимаемых глазом
# как чёрный. НЕ переписывать (собран на реальных данных); расширять — согласовывать
# отдельно (ТЗ п. 4.3 / п. 11.5). Сравнение см. в is_black_color: буквальное ИЛИ
# нормализованное, поэтому набор покрывает и другие написания тех же цветов.
black_colors = {
    'black', '#000', '#000000',
    'rgb(0,0,0)', 'rgb(0, 0, 0)',
    'rgb(8,8,8)', 'rgb(8, 8, 8)',
    'rgb(68,68,68)', 'rgb(68, 68, 68)',
    'rgb(32,33,34)', 'rgb(32, 33, 34)',
    'rgb(153,51,102)', 'rgb(153, 51, 102)',
    'rgba(0,0,0,1)', 'rgba(0, 0, 0, 1)',
    'rgb(51,51,0)', 'rgb(51, 51, 0)',
    'rgb(0,51,0)', 'rgb(0, 51, 0)',
    'rgb(0,51,102)', 'rgb(0, 51, 102)',
    'rgb(51,51,51)', 'rgb(51, 51, 51)',
    'rgb(23,43,77)', 'rgb(23, 43, 77)'
}

# Минимальный словарь именованных цветов CSS, встречающихся в разметке Confluence.
# Нужен нормализации (ТЗ п. 4.3: «поддержать black и другие именованные»).
_NAMED_COLORS = {
    'black': '#000000',
    'white': '#ffffff',
    'red': '#ff0000',
    'green': '#008000',
    'blue': '#0000ff',
    'gray': '#808080',
    'grey': '#808080',
    'silver': '#c0c0c0',
    'maroon': '#800000',
    'olive': '#808000',
    'lime': '#00ff00',
    'aqua': '#00ffff',
    'teal': '#008080',
    'navy': '#000080',
    'fuchsia': '#ff00ff',
    'magenta': '#ff00ff',
    'purple': '#800080',
    'orange': '#ffa500',
    'yellow': '#ffff00',
}


def normalize_color(color_value: str) -> Optional[str]:
    """Приводит любое представление цвета к каноническому виду ``#rrggbb`` (ТЗ п. 4.3).

    Поддерживает: именованные цвета, ``#rgb``, ``#rrggbb``, ``rgb(...)``, ``rgba(...)``
    (альфа игнорируется), произвольные пробелы и любой регистр. Возвращает нормализованную
    строку в нижнем регистре или ``None``, если значение распознать не удалось.
    """
    if not color_value:
        return None

    value = color_value.strip().lower()

    # Именованный цвет.
    if value in _NAMED_COLORS:
        return _NAMED_COLORS[value]

    # Hex-форма: #rgb или #rrggbb.
    m = re.fullmatch(r'#([0-9a-f]{3}|[0-9a-f]{6})', value)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = ''.join(ch * 2 for ch in h)  # #abc -> #aabbcc
        return '#' + h

    # rgb()/rgba() — извлекаем первые три числовых компонента, альфу отбрасываем.
    m = re.fullmatch(r'rgba?\(\s*([0-9]{1,3})\s*,\s*([0-9]{1,3})\s*,\s*([0-9]{1,3})\s*'
                     r'(?:,\s*[0-9.]+\s*)?\)', value)
    if m:
        try:
            r, g, b = (int(m.group(i)) for i in (1, 2, 3))
        except ValueError:
            return None
        if any(c > 255 for c in (r, g, b)):
            return None
        return '#{:02x}{:02x}{:02x}'.format(r, g, b)

    return None


def to_rgb_notation(color_value: str) -> Optional[str]:
    """Обратная конверсия цвета к форме ``rgb(r,g,b)`` — как он записан в HTML Confluence.

    Нужна только для отчётов: канонический `#rrggbb` в исходнике страницы не встречается,
    поэтому по нему аналитик не найдёт цвет поиском по HTML. Пробелы не ставятся —
    Confluence пишет компоненты слитно (проверено на реальных выгрузках).
    Возвращает ``None``, если значение распознать не удалось.
    """
    norm = normalize_color(color_value)
    if not norm:
        return None
    r, g, b = (int(norm[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgb({r},{g},{b})"


# Нормализованные формы чёрного — считаются один раз (ТЗ п. 4.3: сравнение после
# нормализации ОБЕИХ сторон).
_BLACK_NORMALIZED = {n for n in (normalize_color(c) for c in black_colors) if n}


# --- Перцептивная близость к чёрному (ТЗ п. 4.3.1): ΔE CIE76 в CIE Lab ---------------
# Цвет вне палитры редактора (систематический артефакт: вставка из буфера, REST/HTML-макрос,
# тема), визуально неотличимый от чёрного, классифицируется как чёрный без ручного разбора.
# ВАЖНО (п. 4.3.2): эту проверку применять ТОЛЬКО ПОСЛЕ поиска цвета в таблице истории —
# иначе тёмный цвет-маркер (тёмно-синий/зелёный из палитры) будет проглочен как чёрный.
# Поэтому near-black НЕ входит в is_black_color и не добавляется в black_colors (набор
# остаётся ручным — иначе порог «уползает» от прогона к прогону).

# Порог ΔE. По умолчанию 3 (граница воспринимаемого различия). Калибруется по отчёту
# первого прогона — в тёмной области Lab перцептивно неравномерен (ТЗ п. 4.3.1, треб. 5).
NEAR_BLACK_DELTA_E = 3.0


def _srgb_hex_to_lab(hex6: str):
    """sRGB '#rrggbb' → (L*, a*, b*) в CIE Lab. Линеаризация гаммы + XYZ с белой точкой D65."""
    r = int(hex6[1:3], 16) / 255.0
    g = int(hex6[3:5], 16) / 255.0
    b = int(hex6[5:7], 16) / 255.0

    def _lin(c):  # линеаризация sRGB-гаммы (пропуск = типовая ошибка в тёмной области)
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _lin(r), _lin(g), _lin(b)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    # Нормировка на белую точку D65.
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def _f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


# Lab-координаты элементов black_colors — считаются один раз.
_BLACK_LAB = [_srgb_hex_to_lab(n) for n in _BLACK_NORMALIZED]


def delta_e_to_black(color_value: str) -> Optional[float]:
    """ΔE CIE76 от цвета до БЛИЖАЙШЕГО элемента black_colors (или None, если цвет не распознан)."""
    norm = normalize_color(color_value)
    if not norm:
        return None
    l0, a0, b0 = _srgb_hex_to_lab(norm)
    return min(((l0 - l) ** 2 + (a0 - a) ** 2 + (b0 - b) ** 2) ** 0.5
               for (l, a, b) in _BLACK_LAB)


def is_near_black(color_value: str, threshold: Optional[float] = None) -> bool:
    """True, если цвет перцептивно неотличим от чёрного (ΔE < порога, ТЗ п. 4.3.1).

    Применять ТОЛЬКО после поиска в истории (п. 4.3.2). Точный чёрный уже покрыт
    is_black_color — здесь именно «вне палитры, но визуально чёрный».
    """
    d = delta_e_to_black(color_value)
    if d is None:
        return False
    return d < (NEAR_BLACK_DELTA_E if threshold is None else threshold)


# Нефункциональные цвета интерфейса Confluence/Atlassian — НЕ разметка требований, а стили
# ссылок/фона. При миграции цвета их не считаем правкой (не порождают маркер/UNKNOWN),
# трактуем как «не требование». Расширять по мере обнаружения на реальных данных.
IGNORED_COLORS = {
    'rgb(0,82,204)', 'rgb(0, 82, 204)',   # #0052cc — синий цвет гиперссылок Atlassian
    'rgb(244,245,247)', 'rgb(244, 245, 247)',  # #f4f5f7 — светло-серый фон/подложка (N10)
}
_IGNORED_NORMALIZED = {n for n in (normalize_color(c) for c in IGNORED_COLORS) if n}


def is_ignored_color(color_value: str) -> bool:
    """True для нефункциональных UI-цветов (ссылки/фон): их не трактуем как правку требования.

    Сравнение по нормализованной форме (как для чёрного). Пустое/нераспознанное значение — False.
    """
    if not color_value:
        return False
    value = color_value.strip().lower()
    if value in IGNORED_COLORS:
        return True
    norm = normalize_color(value)
    return norm is not None and norm in _IGNORED_NORMALIZED


def is_black_color(color_value: str) -> bool:
    """
    Проверяет, является ли цвет черным.

    Сравнение — буквальное (историческое поведение) ИЛИ по нормализованной форме
    (ТЗ п. 4.3): это только РАСШИРЯЕТ множество «чёрного» (разные написания одного и
    того же цвета, напр. rgb(51,51,51) и #333333), но никогда его не сужает.
    """
    value = color_value.strip().lower()

    # 1. Буквальное совпадение — прежнее поведение, никогда не теряется.
    if value in black_colors:
        return True

    # 2. Нормализованное совпадение — новые написания уже-чёрных цветов.
    norm = normalize_color(value)
    return norm is not None and norm in _BLACK_NORMALIZED
