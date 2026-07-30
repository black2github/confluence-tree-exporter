# app/filter_approved_fragments.py

import logging
from app.content_extractor import create_approved_fragments_extractor

logger = logging.getLogger(__name__)


def filter_approved_fragments(html: str) -> str:
    """
    Извлекает подтвержденные фрагменты с гибридной разметкой (Markdown + HTML)
    """
    logger.debug("[filter_approved_fragments] <- {%s}", html[:200] + "...")

    extractor = create_approved_fragments_extractor()
    result = extractor.extract(html)

    logger.debug("[filter_approved_fragments] -> {%s}", result)
    return result

def test_filter_approved_fragments():
    """Тестовый метод для проверки работы filter_all_fragments()"""

    # МЕСТО ДЛЯ ВСТАВКИ HTML ФРАГМЕНТА
    # полный фрагмент
    html_fragment = '''
    <p class="auto-cursor-target"><br /></p><table class="wrapped"><tbody><tr><td class="highlight-grey" title="Цвет фона: " data-highlight-colour="grey"><strong title="">hdr1</strong></td><td><br /></td></tr><tr><td class="highlight-grey" title="Цвет фона: " data-highlight-colour="grey"><strong title="">hdr2&nbsp;</strong></td><td><div class="content-wrapper"><p class="auto-cursor-target"><br /></p><p class="auto-cursor-target"><br /></p><table class="wrapped" data-mce-resize="false"><tbody><tr><th colspan="2" scope="colgroup">hdr21</th><th scope="col">hdr22</th></tr><tr><td colspan="2"><strong>Шаг 111</strong><br /><strong><br /></strong></td><td><p><br /></p></td></tr><tr><td colspan="2"><p><strong>Шаг 4</strong></p></td><td><p class="auto-cursor-target"><br /></p><table class="wrapped" data-mce-resize="false"><tbody><tr><th>hdr221</th><th>hdr222</th></tr><tr><td>текст1</td><td><p class="auto-cursor-target">текст2</p><table class="wrapped" data-mce-resize="false"><colgroup><col /><col /></colgroup><tbody><tr><th scope="col">Шаги</th><th scope="col"><span style="color: rgb(255,102,0);">Описание</span></th></tr><tr><td>hdr222.1</td><td><span style="color: rgb(255,102,0);">hdr222.2</span></td></tr><tr><td>hdr222.3</td><td><span style="color: rgb(255,102,0);">hdr222.4</span></td></tr></tbody></table><p class="auto-cursor-target"><span style="color: rgb(255,102,0);">текст3</span></p></td></tr></tbody></table><p class="auto-cursor-target"><br /></p></td></tr></tbody></table><p class="auto-cursor-target"><br /></p></div></td></tr></tbody></table><p class="auto-cursor-target"><br /></p><p class="auto-cursor-target"><br /></p>
            '''
    print("=== ВХОДНОЙ HTML ===")
    print(html_fragment)
    print("\n=== РЕЗУЛЬТАТ ОБРАБОТКИ ===")

    result = filter_approved_fragments(html_fragment)

    print(f"'{result}'")
    print("\n=== КОНЕЦ ===")


if __name__ == "__main__":
    test_filter_approved_fragments()