@echo off
REM ============================================================================
REM  Этапы 3-4 роадмапа: ввод задач в ПРОМ и хронология в src-репозитории.
REM  Ветка = цепочка событий "задача" и "выгрузка". Рабочее дерево
REM  "архив + введённые ранее" живёт вне репозитория; на каждую задачу
REM  файла - critic apply в него (один раз), копия в целевой каталог
REM  (без migration-*), reject-all, git commit + git tag <префикс><JIRA-ID>.
REM  Введённые ранее берутся из тегов текущей ветки: в файле - только
REM  вводимые. Без файла - только событие "выгрузка" (пересчёт состояния;
REM  первый раз - коммит "ПРОМ-срез"). Push НЕ выполняется без --push.
REM
REM  Использование (из-под PowerShell):
REM    .\run-history.bat <каталог-архива> <корень-git-репозитория> [<файл-списка-задач>] [опции]
REM
REM  Параметры:
REM    каталог-архива    - нетронутая выгрузка (sources/raw), только чтение
REM    корень-репозитория- git-репозиторий src-<сервис>; дерево должно быть
REM                        ЧИСТЫМ, HEAD на ветке, git настроен (user.name/email)
REM    файл-списка-задач - вводимые JIRA-ID построчно; для хронологии подходит
REM                        как есть migration-apply-order.md из архива
REM  Опции:
REM    --target-subdir <путь>  куда вливать внутри репо (по умолчанию sources/confluence)
REM    --tag-prefix <префикс>  префикс тегов (по умолчанию src/; хронология - hist/)
REM    --commit-prefix <текст> префикс коммита (по умолчанию "Ввод в эксплуатацию";
REM                            хронология - "Срез хронологии")
REM    --allow-unlisted        разрешить задачи, которых нет в манифесте архива
REM    --base-dir <каталог>    где держать рабочее дерево (по умолчанию %TEMP%);
REM                            в контуре - каталог вне репозитория, вне архива
REM                            и вне проверки антивируса
REM    --refill-each           прежний способ: пересборка из архива на каждую
REM                            задачу (в разы медленнее, результат тот же)
REM    --dry-run               показать введённые ранее, план и ошибки проверок
REM    --push                  в конце git push + push тегов этого прогона
REM
REM  Примеры:
REM    .\run-history.bat C:\src-cc\sources\raw C:\src-cc --base-dir C:\src-work
REM    .\run-history.bat C:\src-cc\sources\raw C:\src-cc intro.txt --base-dir C:\src-work --dry-run
REM    .\run-history.bat C:\src-cc\sources\raw C:\src-cc C:\src-cc\sources\raw\migration-apply-order.md --tag-prefix hist/ --commit-prefix "Срез хронологии" --base-dir C:\src-work
REM
REM  Остановки-предохранители: грязное дерево, HEAD не на ветке, тег вне
REM  ветки, недопустимое имя тега, задача вне манифеста, целевой каталог
REM  вне репо или внутри .git, новые файлы под .gitignore, ошибка critic/git
REM  (целевой каталог возвращается к HEAD) - стоп с кодом != 0.
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
