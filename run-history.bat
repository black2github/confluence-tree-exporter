@echo off
REM ============================================================================
REM  Этап 4 роадмапа: позадачное вливание истории в src-репозиторий (летопись).
REM  Автоматизирует "режим без ревью": на каждую задачу списка -
REM  raw -> целевой каталог, critic apply всех принятых, reject-all,
REM  git commit + git tag src/<JIRA-ID>. Push НЕ выполняется без --push.
REM
REM  Использование (из-под PowerShell):
REM    .\run-history.bat <каталог-архива> <корень-git-репозитория> <файл-списка-задач> [опции]
REM
REM  Параметры:
REM    каталог-архива    - нетронутая выгрузка (sources/raw), только чтение
REM    корень-репозитория- git-репозиторий летописи (src-<сервис>); дерево
REM                        должно быть ЧИСТЫМ, git настроен (user.name/email)
REM    файл-списка-задач - JIRA-ID построчно ПО ПОРЯДКУ вливания; подходит
REM                        как есть блок команд из migration-apply-order.md
REM  Опции:
REM    --target-subdir <путь>  куда вливать внутри репо (по умолчанию sources/confluence)
REM    --tag-prefix <префикс>  префикс тегов (по умолчанию src/)
REM    --dry-run               показать план, ничего не менять
REM    --push                  в конце git push + push тегов
REM
REM  Пример:
REM    .\run-history.bat C:\src-cc\sources\raw C:\src-cc apply-order.txt --dry-run
REM
REM  Остановки-предохранители: грязное дерево, существующий тег среза,
REM  архив внутри целевого каталога, ошибка critic/git - стоп с кодом != 0.
REM ============================================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ОШИБКА: окружение не установлено. Сначала запустите install.bat
    exit /b 1
)
set "PYTHONPATH=%~dp0"
".venv\Scripts\python.exe" -m app.scripts.apply_history %*
endlocal
