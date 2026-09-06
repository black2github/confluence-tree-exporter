@echo off
REM ============================================================================
REM  Починка УЖЕ ВЫГРУЖЕННЫХ деревьев (утилита repair_export).
REM  Работает с ЛОКАЛЬНЫМИ .md - доступ к Confluence НЕ требуется, поэтому
REM  лечить можно и во внутреннем контуре, и во внешнем после переноса.
REM
REM  Зачем: правки экспортёра действуют с ОЧЕРЕДНОЙ выгрузки, а уже сделанные
REM  деревья остаются как есть. Все починки идемпотентны - повторный запуск
REM  безопасен.
REM
REM  Использование:
REM    run-repair.bat <каталог|файл> <ключи починки> [--dry-run]
REM  Ключи починки (нужен хотя бы один):
REM    --unfold                       склеить свёрнутые значения frontmatter
REM                                   в одну строку (длинный title выглядел
REM                                   обрезанным с незакрытой кавычкой)
REM    --flatten-nested               уплощить вложенность маркеров: apply и
REM                                   reject на таких файлах падают с ошибкой
REM                                   "литеральная вложенность запрещена"
REM    --unapproved-jira <file.json>  проставить страничный флаг unapproved_jira
REM                                   там, где состав страницы принадлежит
REM                                   неутверждённой задаче (после этого
REM                                   reject очищает страницу целиком)
REM    --dry-run                      показать, что изменилось бы, ничего не
REM                                   записывая
REM
REM  Примеры:
REM    run-repair.bat conf-requirements\ --unfold --dry-run
REM    run-repair.bat conf-requirements\ --unfold --flatten-nested
REM    run-repair.bat conf-requirements\ --unapproved-jira unapproved.json
REM
REM  Порядок работы: сначала --dry-run и просмотр списка, затем тот же запуск
REM  без него. После --flatten-nested проверьте разметку: run-critic.bat lint
REM ============================================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: окружение не установлено. Сначала запустите install.bat
    exit /b 1
)
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m app.scripts.repair_export %*
endlocal
