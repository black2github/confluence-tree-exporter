# app/filter_all_fragments.py

import logging
from app.content_extractor import create_all_fragments_extractor

logger = logging.getLogger(__name__)


def filter_all_fragments(html: str) -> str:
    """
    Извлекает все фрагменты из HTML возвращая их с гибридной разметкой (Markdown + HTML)
    без учета цвета элементов
    """
    logger.debug("[filter_all_fragments] <- {%s}", html[:200] + "...")
    logger.debug("[filter_all_fragments] <- {%s}", html)

    extractor = create_all_fragments_extractor()
    result = extractor.extract(html)

    logger.debug("[filter_all_fragments] -> {%s}", result)
    return result


def test_filter_all_fragments():
    """Тестовый метод для проверки работы filter_all_fragments()"""

    # МЕСТО ДЛЯ ВСТАВКИ HTML ФРАГМЕНТА
    html_fragment = '''
<h1 style="text-decoration: none;">Общая информация о методе</h1><table class="fixed-table wrapped"><colgroup><col style="width: 144.0px;" /><col style="width: 1011.0px;" /></colgroup><thead><tr><td style="text-align: left;"><strong>Название метода</strong></td><td style="text-align: left;"><p>Повторная отправка кода подтверждения</p></td></tr><tr><td style="text-align: left;"><p align="left"><strong>Alias</strong></p></td><td style="text-align: left;"><p style="text-align: left;"><span class="nolink">uaa/clientuser/renewConfirm</span></p></td></tr></thead><tbody><tr><td style="text-align: left;"><p><strong>Тип сервиса</strong></p></td><td style="text-align: left;"><p>REST</p></td></tr></tbody></table><p class="auto-cursor-target"><br /></p>
'''

    print("=== ВХОДНОЙ HTML ===")
    print(html_fragment)
    print("\n=== РЕЗУЛЬТАТ ОБРАБОТКИ ===")

    result = filter_all_fragments(html_fragment)

    print(f"'{result}'")
    print("\n=== КОНЕЦ ===")


if __name__ == "__main__":
    test_filter_all_fragments()